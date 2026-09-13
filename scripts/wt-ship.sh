#!/usr/bin/env bash
# 把工作树里的改动收口到 main 并推送远端。
#
# 顺序：提交 → 对齐 origin/main → 全量测试 → 快进合并 → 推送 → 清理工作树。
# 任何一步失败都会立刻中止，工作树原样保留，方便原地排查。
#
# 用法：scripts/wt-ship.sh <分支名> "<提交说明>"
set -euo pipefail

if [ $# -lt 2 ]; then
  echo "用法: scripts/wt-ship.sh <分支名> \"<提交说明>\"" >&2
  exit 2
fi

BRANCH="$1"
MESSAGE="$2"

# 目录名与 wt-new.sh 保持一致：分支名里的斜杠换成短横线。
SAFE_NAME="${BRANCH//\//-}"

COMMON_DIR="$(git rev-parse --path-format=absolute --git-common-dir)"
MAIN_REPO="$(dirname "$COMMON_DIR")"
WT_PATH="$MAIN_REPO/.worktrees/$SAFE_NAME"

[ -d "$WT_PATH" ] || { echo "错误：工作树不存在：$WT_PATH" >&2; exit 1; }

# 1) 提交工作树里的改动
if [ -n "$(git -C "$WT_PATH" status --porcelain)" ]; then
  git -C "$WT_PATH" add -A
  git -C "$WT_PATH" commit -m "$MESSAGE"
else
  echo "提示：工作树没有未提交改动，沿用已有提交。"
fi

# 2) 先对齐远端，免得推送到最后一步才发现落后
git -C "$WT_PATH" fetch origin --prune --quiet
if ! git -C "$WT_PATH" merge-base --is-ancestor origin/main HEAD; then
  echo "origin/main 有新提交，先 rebase 到最新..."
  git -C "$WT_PATH" rebase origin/main
fi

# 3) 在「即将合并进去的那份代码」上跑全量测试，通过才允许合并
#    优先用工作树内的副本：改动可能就在改脚本本身，测的必须是将要合并的那份。
CHECK="$WT_PATH/scripts/wt-check.sh"
[ -f "$CHECK" ] || CHECK="$MAIN_REPO/scripts/wt-check.sh"
bash "$CHECK" "$WT_PATH"

# 4) 主工作树必须干净，且能快进合并
#    排除 .worktrees/：那是本流程自己产生的工具目录，不该阻塞合并。
DIRTY="$(git -C "$MAIN_REPO" status --porcelain -- . ':(exclude).worktrees')"
if [ -n "$DIRTY" ]; then
  echo "错误：主工作树有未提交改动，请先处理：" >&2
  echo "$DIRTY" >&2
  exit 1
fi

git -C "$MAIN_REPO" checkout --quiet main
git -C "$MAIN_REPO" merge --ff-only "$BRANCH"

# 5) 推送
git -C "$MAIN_REPO" push origin main

# 6) 清理工作树与临时分支
git -C "$MAIN_REPO" worktree remove "$WT_PATH"
git -C "$MAIN_REPO" branch -d "$BRANCH"

echo "完成：已合并并推送 main，工作树 $WT_PATH 已清理。"
