#!/usr/bin/env bash
# 在指定工作树里跑全部测试。任何一项失败立即返回非零，绝不吞掉错误。
#
# 用法：scripts/wt-check.sh [工作树路径]      # 省略则用当前所在的工作树
set -euo pipefail

if [ $# -ge 1 ]; then
  WT="$1"
else
  WT="$(git rev-parse --show-toplevel)"
fi

[ -d "$WT" ] || { echo "错误：目录不存在：$WT" >&2; exit 1; }

echo "== 在 $WT 运行全部测试 =="
cd "$WT"

# 三个测试都是独立脚本，失败时以非零码退出。
python3 tests/test_session.py
python3 tests/test_commands.py
python3 tests/test_commands_ui.py
python3 tests/test_smoke.py
python3 tests/test_tui_boot.py

echo "== 全部测试通过 =="
