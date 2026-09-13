#!/usr/bin/env bash
# 开一个隔离工作树：所有改动都发生在这里，main 分支始终保持可运行状态。
#
# 用法：scripts/wt-new.sh <分支名>
# 成功时把工作树路径打印到标准输出（最后一行），便于脚本串联。
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "用法: scripts/wt-new.sh <分支名>" >&2
  exit 2
fi

BRANCH="$1"
# 目录名扁平化：分支名里的斜杠换成短横线（feature/xxx → feature-xxx），
# 避免多一层目录，也让 .worktrees/ 下保持一层平铺。
SAFE_NAME="${BRANCH//\//-}"

# 从 git 的公共目录反推主仓库根，这样无论在哪个工作树里调用都能定位正确。
COMMON_DIR="$(git rev-parse --path-format=absolute --git-common-dir)"
MAIN_REPO="$(dirname "$COMMON_DIR")"
WT_ROOT="$MAIN_REPO/.worktrees"
WT_PATH="$WT_ROOT/$SAFE_NAME"

git -C "$MAIN_REPO" fetch origin --prune --quiet

if [ -e "$WT_PATH" ]; then
  echo "错误：路径已存在：$WT_PATH" >&2
  exit 1
fi

if git -C "$MAIN_REPO" show-ref --verify --quiet "refs/heads/$BRANCH"; then
  echo "错误：本地分支 $BRANCH 已存在，请换个名字或先删除它" >&2
  exit 1
fi

mkdir -p "$WT_ROOT"
git -C "$MAIN_REPO" worktree add -b "$BRANCH" "$WT_PATH" origin/main

echo "$WT_PATH"
