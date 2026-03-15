#!/usr/bin/env bash
# Copyright 2026 Tsinghua University
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# This file was created by Tsinghua University and is not part of
# the original agentgateway project by Solo.io.

# clear-wm.sh — 清空 AgentGateway 工作记忆（Working Memory）
#
# 用法：
#   bash clear-wm.sh                     # 默认连接 localhost:15000
#   ADMIN_PORT=15001 bash clear-wm.sh    # 自定义 Admin 端口
#   bash clear-wm.sh --sessions          # 同时提示 Session 清空方法
#
# 依赖：curl, python3

set -euo pipefail

ADMIN_PORT="${ADMIN_PORT:-15000}"
ADMIN="http://localhost:${ADMIN_PORT}"
CLEAR_SESSIONS=0

for arg in "$@"; do
    case "$arg" in
        --sessions) CLEAR_SESSIONS=1 ;;
        *) echo "未知参数: $arg" >&2; exit 1 ;;
    esac
done

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; RESET='\033[0m'
ok()   { echo -e "${GREEN}✓ $*${RESET}"; }
warn() { echo -e "${YELLOW}⚠ $*${RESET}"; }
die()  { echo -e "${RED}✗ $*${RESET}" >&2; exit 1; }

# ── 检查网关是否可达 ────────────────────────────────────────────────────────
if ! curl -sf "${ADMIN}/knowledge/working_memory" -o /dev/null 2>/dev/null; then
    die "无法连接 AgentGateway Admin API（${ADMIN}）"
fi

# ── 清空前打印当前条目数 ────────────────────────────────────────────────────
WM_BEFORE=$(curl -sf "${ADMIN}/knowledge/working_memory" 2>/dev/null | \
    python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "?")
SESS_BEFORE=$(curl -sf "${ADMIN}/knowledge/sessions" 2>/dev/null | \
    python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "?")

echo "清空前：工作记忆 ${WM_BEFORE} 条 | Session ${SESS_BEFORE} 个"

# ── 清空工作记忆（校验响应体必须是 "ok"，不是 JSON 数组）──────────────────
RESP=$(curl -s -w "\n%{http_code}" -X DELETE "${ADMIN}/knowledge/working_memory" 2>/dev/null)
BODY=$(echo "$RESP" | head -n -1)
CODE=$(echo "$RESP" | tail -n 1)

if [[ "$CODE" != "200" ]]; then
    die "清空失败（HTTP $CODE）"
fi

# 旧版网关对 DELETE 也返回 200 但 body 是 JSON 数组，检测此情况
if echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if isinstance(d,list) else 1)" 2>/dev/null; then
    warn "网关返回了 JSON 数组而非 'ok'——运行中的是旧版二进制，不支持 DELETE 清空"
    warn "请重启网关以加载新版本："
    warn "  bash start-all.sh         （完整重启）"
    warn "  SKIP_BUILD=1 bash start-all.sh  （跳过编译，直接用已编译二进制重启）"
    exit 1
fi

ok "工作记忆已清空（DELETE /knowledge/working_memory → $CODE, body='${BODY}'）"

# ── 可选：提示 Session 清空方法 ────────────────────────────────────────────
if [[ "$CLEAR_SESSIONS" == "1" ]]; then
    warn "Session 状态存储在网关内存中，无单独清空端点"
    warn "如需清空 Session，请重启网关：bash start-all.sh"
fi

# ── 清空后验证 ──────────────────────────────────────────────────────────────
WM_AFTER=$(curl -sf "${ADMIN}/knowledge/working_memory" 2>/dev/null | \
    python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "?")
echo "清空后：工作记忆 ${WM_AFTER} 条"
