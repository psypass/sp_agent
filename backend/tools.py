"""Agent 可用的工具集合：实现、OpenAI schema 与注册表。

所有工具都被限制在工作目录 ROOT 之内，路径越界会被拒绝。
"""

import os
import subprocess
from pathlib import Path

# 工作目录，由主程序在导入后通过 set_root() 覆盖为实际项目根目录
ROOT = Path.cwd().resolve()


def set_root(root) -> None:
    """由主程序调用，设置工具操作的工作目录。"""
    global ROOT
    ROOT = Path(root).resolve()


def _safe_path(path: str) -> Path:
    """把相对路径解析到 ROOT 内，越界则报错。"""
    target = (ROOT / path).resolve() if not os.path.isabs(path) else Path(path).resolve()
    if target != ROOT and ROOT not in target.parents:
        raise ValueError(f"拒绝访问工作目录之外的路径：{path}")
    return target


def bash(command: str) -> str:
    """在项目根目录执行 shell 命令，返回合并后的输出。"""
    result = subprocess.run(
        command, shell=True, cwd=ROOT, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120,
    )
    output = result.stdout[-12000:] or "（无输出）"
    return f"退出码：{result.returncode}\n{output}"


def read_file(path: str, offset: int = 1, limit: int = 400) -> str:
    """读取文本文件，可按行号范围读取。offset 起始行，limit 最多读取的行数。"""
    target = _safe_path(path)
    if not target.is_file():
        return f"错误：不是文件或不存在：{path}"
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(offset - 1, 0)
    chunk = lines[start:start + limit]
    numbered = "\n".join(f"{start + i + 1}\t{line}" for i, line in enumerate(chunk))
    tail = "" if start + limit >= len(lines) else f"\n...（共 {len(lines)} 行，已截断）"
    return numbered + tail or "（空文件）"


def write_file(path: str, content: str) -> str:
    """创建或覆盖一个文本文件，内容原样写入。"""
    target = _safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"已写入 {path}（{len(content)} 字符）"


def edit_file(path: str, old: str, new: str) -> str:
    """把文件中的 old 字符串精确替换为 new。要求 old 唯一出现。"""
    target = _safe_path(path)
    if not target.is_file():
        return f"错误：文件不存在：{path}"
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        return "错误：未找到要替换的内容 old。"
    if count > 1:
        return f"错误：old 出现了 {count} 次，不唯一，请提供更长的上下文。"
    target.write_text(text.replace(old, new, 1), encoding="utf-8")
    return f"已更新 {path}"


def list_dir(path: str = ".") -> str:
    """列出目录内容，包含类型、大小和名称。"""
    target = _safe_path(path)
    if not target.is_dir():
        return f"错误：不是目录：{path}"
    entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
    if not entries:
        return "（空目录）"
    rows = []
    for entry in entries:
        kind = "DIR " if entry.is_dir() else "FILE"
        size = "" if entry.is_dir() else f"{entry.stat().st_size}"
        rows.append(f"{kind}\t{size}\t{entry.name}")
    return "\n".join(rows)


def grep(pattern: str, path: str = ".", include: str = "") -> str:
    """在目录内按正则搜索文本。include 为可选的文件名 glob，如 '*.py'。"""
    target = _safe_path(path)
    if not target.exists():
        return f"错误：路径不存在：{path}"
    cmd = ["grep", "-rn", "-E", pattern]
    if include:
        cmd += ["--include", include]
    cmd.append(str(target))
    result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = result.stdout.strip()
    if not output:
        return "（无匹配）"
    lines = output.splitlines()[:200]
    return "\n".join(lines)


def run_python(code: str) -> str:
    """执行一段 Python 代码，返回标准输出/错误。用于计算或快速验证。"""
    result = subprocess.run(
        ["python3", "-c", code], cwd=ROOT, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60,
    )
    output = result.stdout[-8000:] or "（无输出）"
    return f"退出码：{result.returncode}\n{output}"


TOOLS = [
    {"type": "function", "function": {
        "name": "bash", "description": "在当前项目根目录执行 shell 命令，例如 ls、pytest、git status",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "read_file", "description": "读取文本文件，可指定起始行 offset 和最多行数 limit",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "相对工作目录的文件路径"},
            "offset": {"type": "integer", "description": "起始行号，从 1 开始，默认 1"},
            "limit": {"type": "integer", "description": "最多读取的行数，默认 400"},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "write_file", "description": "创建或覆盖文件，内容原样写入",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"},
        }, "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "edit_file", "description": "把文件中的 old 字符串精确替换为 new，要求 old 唯一出现",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
        }, "required": ["path", "old", "new"]},
    }},
    {"type": "function", "function": {
        "name": "list_dir", "description": "列出目录内容，含类型与大小",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "相对路径，默认当前目录"},
        }},
    }},
    {"type": "function", "function": {
        "name": "grep", "description": "在目录内按正则搜索文本，可限定文件类型",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "正则表达式"},
            "path": {"type": "string", "description": "搜索目录，默认当前目录"},
            "include": {"type": "string", "description": "文件名 glob，如 *.py"},
        }, "required": ["pattern"]},
    }},
    {"type": "function", "function": {
        "name": "run_python", "description": "执行一段 Python 代码并返回输出，用于计算或验证",
        "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
    }},
]

FUNCTIONS = {
    "bash": bash,
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "list_dir": list_dir,
    "grep": grep,
    "run_python": run_python,
}


def build_system_prompt(root) -> str:
    """根据工作目录生成 System Prompt。"""
    return f"""你是中文编程 Agent，工作目录是 {root}。
你可以使用以下工具（都在工作目录 {root} 内操作）：
- bash：执行 shell 命令。
- read_file：读取文件内容（支持 offset/limit 行号范围），优于 cat。
- write_file：创建或覆盖文件，内容原样写入，避免 heredoc 转义问题。
- edit_file：把文件中的 old 精确替换为 new，要求 old 唯一出现。
- list_dir：列出目录内容，含类型与大小。
- grep：在目录内按正则搜索文本，可限定文件类型。
- run_python：执行 Python 代码片段，用于计算或验证。

用法约定：
- 读文件优先用 read_file；改文件优先用 edit_file（精确替换）或 write_file（重写）。
- 查看目录用 list_dir；查找内容用 grep；需要复杂 shell 处理再用 bash。
- 需要计算或验证小逻辑时用 run_python。
- 每次只做一步，先看输出再决定下一步；改完用 read_file 或相关命令验证结果。

安全约定：
- 只在当前工作目录内操作，不要访问或修改目录外的文件。
- 不要执行破坏性命令（如 rm -rf、git reset --hard、格式化等），除非用户明确要求。
- 执行前先用一句话说明这条命令/工具调用的用途。

每次修改后，用中文简要说明你做了什么。"""
