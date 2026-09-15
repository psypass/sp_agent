"""上下文管理：估算体积、按对话轮次裁剪、对丢弃的历史做摘要压缩。

核心约束：OpenAI 协议要求每个 assistant(tool_calls) 后面必须紧跟其 tool 结果。
因此裁剪必须以「轮次」为最小单位整块丢弃，不能按消息条数随意删。

摘要压缩：被丢弃的旧轮次不会直接消失，而是压缩成一段简报，以 system 消息的
形式插在原始 system prompt 之后，让长任务不丢失早期线索。
"""

# 默认阈值：messages 序列化后的字符数上限，超过就从最老的轮次开始丢
DEFAULT_MAX_CHARS = 256000
# 无论如何都保留的最近轮次数
KEEP_RECENT_ROUNDS = 6


def _msg_chars(msg) -> int:
    """估算单条消息的字符数（含工具调用参数）。"""
    total = 0
    content = getattr(msg, "content", None)
    if content is None and isinstance(msg, dict):
        content = msg.get("content")
    if content:
        total += len(content)
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls is None and isinstance(msg, dict):
        tool_calls = msg.get("tool_calls")
    if tool_calls:
        for call in tool_calls:
            fn = getattr(call, "function", None) or call.get("function", {})
            total += len(getattr(fn, "name", None) or fn.get("name", ""))
            total += len(getattr(fn, "arguments", None) or fn.get("arguments", ""))
    return total


def total_chars(messages) -> int:
    """整个 messages 列表的粗略字符数。"""
    return sum(_msg_chars(m) for m in messages)


def _role_of(msg):
    return msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)


def _text_of(msg) -> str:
    content = getattr(msg, "content", None)
    if content is None and isinstance(msg, dict):
        content = msg.get("content")
    return content or ""


def _split_rounds(messages):
    """把 messages 按「user 消息」切成轮次块。

    返回 (system_msgs, rounds)，rounds 是 list[list[msg]]，每轮从一条 user 消息开始，
    直到下一条 user 消息之前（包含中间的 assistant / tool 消息）。
    """
    system_msgs = []
    rounds = []
    current = None
    for msg in messages:
        role = _role_of(msg)
        if role == "system":
            system_msgs.append(msg)
            continue
        if role == "user":
            if current:
                rounds.append(current)
            current = [msg]
        else:
            if current is None:
                current = []
            current.append(msg)
    if current:
        rounds.append(current)
    return system_msgs, rounds


def trim(messages, max_chars: int = DEFAULT_MAX_CHARS, keep_recent: int = KEEP_RECENT_ROUNDS):
    """在超限时从最老的轮次整块丢弃。

    返回 (新 messages, 被丢弃的轮次列表)。被丢弃的轮次交给调用方做摘要。
    - system 消息永远保留。
    - 至少保留最近 keep_recent 轮（即使仍超限也不再丢，交由模型处理）。
    """
    if total_chars(messages) <= max_chars:
        return messages, []

    system_msgs, rounds = _split_rounds(messages)
    dropped = []
    while len(rounds) > keep_recent and (
        total_chars(system_msgs) + sum(total_chars(r) for r in rounds) > max_chars
    ):
        dropped.append(rounds.pop(0))

    new_messages = list(system_msgs)
    for r in rounds:
        new_messages.extend(r)
    return new_messages, dropped


def render_rounds(rounds) -> str:
    """把若干轮次渲染成纯文本，供摘要模型阅读。"""
    lines = []
    for r in rounds:
        for msg in r:
            role = _role_of(msg)
            text = _text_of(msg)
            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls is None and isinstance(msg, dict):
                tool_calls = msg.get("tool_calls")
            if tool_calls:
                names = []
                for call in tool_calls:
                    fn = getattr(call, "function", None) or call.get("function", {})
                    names.append(getattr(fn, "name", None) or fn.get("name", ""))
                text = (text + " " if text else "") + f"[调用工具: {', '.join(names)}]"
            if text:
                lines.append(f"{role}: {text}")
    return "\n".join(lines)


SUMMARY_SYSTEM = (
    "你是对话摘要助手。请把下面这段较早的对话压缩成简洁的中文简报，"
    "保留关键事实：用户的目标、已完成的改动、涉及的文件与命令、重要结论。"
    "只输出简报正文，不要客套。若已有旧简报，请把新内容合并进去。"
)


def summarize(dropped_rounds, client, model, existing_summary: str = "") -> str:
    """调用模型把丢弃的轮次压成简报文本。失败时回退为截断拼接。"""
    fresh = render_rounds(dropped_rounds)
    if not fresh.strip():
        return existing_summary

    user_content = fresh
    if existing_summary:
        user_content = f"【已有简报】\n{existing_summary}\n\n【新增对话】\n{fresh}"

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SUMMARY_SYSTEM},
                {"role": "user", "content": user_content},
            ],
        )
        summary = (response.choices[0].message.content or "").strip()
        return summary or existing_summary
    except Exception:
        # 摘要失败不致命：退化为粗暴截断，保证不中断主流程
        merged = (existing_summary + "\n" + fresh).strip()
        return merged[-2000:]


def summary_message(summary: str, note: str = "") -> dict:
    """把摘要包装成一条 system 消息（插在原始 system prompt 之后）。"""
    text = "[历史摘要] 以下是更早对话的压缩内容：\n" + summary.strip()
    if note:
        text += f"\n{note}"
    return {"role": "system", "content": text}
