#!/bin/bash
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

# =============================================================================
# start-all.sh — 一键启动 旅行规划 Multi-Agent + AgentGateway + Dashboard
#
# 依赖：
#   Rust (cargo)    — 编译网关二进制
#   Python 3.13+    — 所有 Agent + Dashboard
#   Node.js         — 构建 UI
#   uv              — Python 包管理（Agent 依赖安装）
#
# 用法：
#   cp agents.env.example agents.env   # 填入 API Key
#   bash start-all.sh
#
# 覆盖变量：
#   SKIP_BUILD=1       跳过 cargo build
#   UI_PY=/path/to/python3   Dashboard 使用的 Python
#   TA_PY=/path/to/python3   Agent 使用的 Python
#   ADMIN_PORT=15000
#   PROXY_PORT=3000
#   UI_PORT=7860
#   REGISTRY_PORT=8090
#   LITELLM_MODEL=openai/qwen-plus
#   LITELLM_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1
# =============================================================================
set -euo pipefail

# ── 确保 cargo 在 PATH 中（Rust 工具链）────────────────────────────────────
export PATH="$HOME/.cargo/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"

# ── 自动加载 agents.env（统一 API Key 配置文件）─────────────────────────────
# 优先读取 agents.env，再读取 .env（兼容旧用法），不覆盖已有的环境变量
for _envfile in "$REPO_ROOT/agents.env" "$REPO_ROOT/.env"; do
    if [[ -f "$_envfile" ]]; then
        # set -a 使 source 的变量自动 export；${VAR:-} 使已设置的变量不被覆盖
        set -a
        # shellcheck source=/dev/null
        source "$_envfile"
        set +a
        echo -e "\033[90m○ 已加载 $_envfile\033[0m"
        break
    fi
done
BINARY="$REPO_ROOT/target/debug/agentgateway"
CONFIG="$REPO_ROOT/examples/travel-agent-demo/config.yaml"
DASHBOARD="$REPO_ROOT/dashboard/app.py"
AGENTS_DIR="$REPO_ROOT/agents/airbnb_planner_multiagent-main"
REGISTRY_SCRIPT="$AGENTS_DIR/mock_registry.py"

# ── Python 路径 ──────────────────────────────────────────────────────────────
# 可通过环境变量覆盖，否则自动检测：
#   1. agents 目录下的 .venv（uv sync 创建）
#   2. PATH 中的 python3
_VENV_PY="$AGENTS_DIR/.venv/bin/python3"

if [[ -z "${TA_PY:-}" ]]; then
    if [[ -x "$_VENV_PY" ]]; then
        TA_PY="$_VENV_PY"
    else
        TA_PY="$(command -v python3 2>/dev/null || true)"
    fi
fi
UI_PY="${UI_PY:-$(command -v python3 2>/dev/null || true)}"           # Dashboard

# ── 端口 / 参数
# ──────────────────────────────────────────────────────────────
ADMIN_PORT="${ADMIN_PORT:-15000}"
PROXY_PORT="${PROXY_PORT:-3000}"
UI_PORT="${UI_PORT:-7860}"
REGISTRY_PORT="${REGISTRY_PORT:-8090}"
SKIP_BUILD="${SKIP_BUILD:-0}"
LITELLM_MODEL="${LITELLM_MODEL:-openai/qwen-plus}"
LITELLM_API_BASE="${LITELLM_API_BASE:-https://dashscope.aliyuncs.com/compatible-mode/v1}"

ADMIN="http://localhost:${ADMIN_PORT}"

# ── 日志目录 ──────────────────────────────────────────────────────────────────
GW_LOG="/tmp/agentgateway-travel.log"
UI_LOG="/tmp/agentgateway-ui.log"
REGISTRY_LOG="/tmp/mock-registry.log"
LISTENER_LOG="/tmp/agent-listener.log"
AGENT_LOG_DIR="/tmp/travel-agents"
mkdir -p "$AGENT_LOG_DIR"

GW_PID=""
UI_PID=""
REGISTRY_PID=""
declare -A AGENT_PIDS=()

# ── 颜色 ──────────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'
RED='\033[0;31m'; BOLD='\033[1m'; RESET='\033[0m'
info()  { echo -e "${CYAN}▶ $*${RESET}"; }
ok()    { echo -e "${GREEN}✓ $*${RESET}"; }
step()  { echo -e "\n${YELLOW}${BOLD}━━━ $* ━━━${RESET}"; }
warn()  { echo -e "${RED}⚠ $*${RESET}"; }
die()   { echo -e "${RED}✗ $*${RESET}" >&2; exit 1; }
skip()  { echo -e "\033[90m○ $* (跳过)\033[0m"; }

# ── 清理 ──────────────────────────────────────────────────────────────────────
cleanup() {
    echo ""
    info "正在关闭所有进程..."
    [[ -n "$GW_PID" ]]       && kill "$GW_PID"       2>/dev/null && ok "网关已停止 (pid $GW_PID)"       || true
    [[ -n "$UI_PID" ]]       && kill "$UI_PID"       2>/dev/null && ok "Dashboard 已停止 (pid $UI_PID)" || true
    [[ -n "$REGISTRY_PID" ]] && kill "$REGISTRY_PID" 2>/dev/null && ok "Registry 已停止 (pid $REGISTRY_PID)" || true
    for name in "${!AGENT_PIDS[@]}"; do
        pid="${AGENT_PIDS[$name]}"
        kill "$pid" 2>/dev/null && ok "$name 已停止 (pid $pid)" || true
    done
    info "日志目录: $AGENT_LOG_DIR"
}
trap cleanup EXIT INT TERM

# ── 等待 HTTP 就绪 ─────────────────────────────────────────────────────────────
wait_http() {
    local url="$1" name="$2" tries="${3:-30}"
    for i in $(seq 1 "$tries"); do
        if curl -sf "$url" >/dev/null 2>&1; then
            ok "$name 就绪"
            return 0
        fi
        sleep 0.5
    done
    warn "$name 未在预期时间内响应，继续启动..."
    return 0
}

# ── 启动 Agent 进程 ─────────────────────────────────────────────────────────
# 同时记录日志路径，避免名称转换错误
declare -A AGENT_LOGS=()

start_agent() {
    local name="$1" log="$2"; shift 2
    "$@" >"$log" 2>&1 &
    local pid=$!
    AGENT_PIDS["$name"]=$pid
    AGENT_LOGS["$name"]=$log
    info "$name 启动中 (pid=$pid  log=$log)"
}

# ── 0. 前置检查 ───────────────────────────────────────────────────────────────
step "0. 检查环境 & 清理旧进程"

command -v cargo &>/dev/null || die "未找到 cargo，请安装 Rust: https://rustup.rs"
command -v curl  &>/dev/null || die "未找到 curl"
[[ -n "$UI_PY" && -x "$UI_PY" ]] || die "未找到 python3，请确保 Python 3.13+ 在 PATH 中（或设置 UI_PY 环境变量）"
[[ -n "$TA_PY" && -x "$TA_PY" ]] || die "未找到 python3，请确保 Python 3.13+ 在 PATH 中（或设置 TA_PY 环境变量）"

[[ -z "${DASHSCOPE_API_KEY:-}" ]] && die "请设置 DASHSCOPE_API_KEY（可在 agents.env 中配置）"
ok "DASHSCOPE_API_KEY 已设置 (${DASHSCOPE_API_KEY:0:10}...)"

if [[ -n "${GOOGLE_API_KEY:-}" ]]; then
    ok "GOOGLE_API_KEY 已设置 → Airbnb Agent 将启动"
    HAS_GOOGLE_KEY=1
else
    warn "GOOGLE_API_KEY 未设置 → 跳过 Airbnb Agent"
    HAS_GOOGLE_KEY=0
fi

if [[ -n "${SERPAPI_KEY:-}" ]]; then
    ok "SERPAPI_KEY 已设置 → Flight / Hotel / Event / Finance Agent 将启动"
    HAS_SERPAPI=1
else
    warn "SERPAPI_KEY 未设置 → 跳过以下 4 个 Agent："
    warn "  FlightAgent / HotelAgent / EventAgent / FinanceAgent"
    warn "  获取免费额度：https://serpapi.com/dashboard"
    HAS_SERPAPI=0
fi

if [[ -n "${TRIPADVISOR_API_KEY:-}" ]]; then
    ok "TRIPADVISOR_API_KEY 已设置 → TripAdvisor Agent 将启动"
    HAS_TRIPADVISOR=1
else
    warn "TRIPADVISOR_API_KEY 未设置 → 跳过 TripAdvisor Agent"
    warn "  获取方式：https://www.tripadvisor.com/developers"
    HAS_TRIPADVISOR=0
fi

# Anthropic-compatible endpoint — Weather Agent, Hotel Agent, Document Agents 共用
if [[ -n "${DOCUMENT_API_KEY:-}" ]]; then
    ok "DOCUMENT_API_KEY 已设置 → WeatherAgent/HotelAgent (Claude) + 3 个文档 Agent 将启动"
else
    warn "DOCUMENT_API_KEY 未设置 → WeatherAgent/HotelAgent (Claude) + FinanceDoc/InfosecDoc/DeptDoc 调用 LLM 将因鉴权失败"
fi

ok "Python (UI):    $UI_PY ($($UI_PY --version 2>&1))"
ok "Python (Agent): $TA_PY ($($TA_PY --version 2>&1))"

# 清理占用 Agent 端口的旧进程
info "清理旧进程..."
for port in 8083 8084 8090 10001 10002 10003 10004 10005 10006 10007 10009 10010 10011; do
    pid=$(lsof -ti :"$port" 2>/dev/null) || true
    if [[ -n "$pid" ]]; then
        kill "$pid" 2>/dev/null && info "  已清理 port $port (pid $pid)" || true
    fi
done
sleep 1

# ── 1. 编译网关 ───────────────────────────────────────────────────────────────
if [[ "$SKIP_BUILD" == "1" && -x "$BINARY" ]]; then
    step "1. 跳过编译（SKIP_BUILD=1）"
    ok "$BINARY"
else
    # 1a. 构建 Next.js UI（产物写入 ui/out/，将被嵌入二进制）
    step "1a. 构建 Next.js UI"
    command -v node &>/dev/null || die "未找到 node，请安装 Node.js"
    cd "$REPO_ROOT/ui"
    [[ -d node_modules ]] || npm install --silent
    # 构建是纯本地操作，强制屏蔽代理变量（避免开梯子时 Node.js 进程卡在网络请求）
    env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        NEXT_TELEMETRY_DISABLED=1 NO_PROXY="*" no_proxy="*" \
        npm run build 2>&1 | tail -5
    ok "Next.js UI 构建完成 → ui/out/"
    # 强制 cargo 重新编译 ui.rs（include_dir! 不会自动感知 ui/out 内容变化）
    touch "$REPO_ROOT/crates/agentgateway/src/ui.rs"

    # 1b. 编译网关（嵌入 UI 静态文件）
    step "1b. 编译网关（cargo build --features ui）"
    cd "$REPO_ROOT"
    # 确保 report_build_info.sh 可执行（新机器首次 clone 后 Git 不保留 chmod +x）
    chmod +x common/scripts/report_build_info.sh 2>/dev/null || true
    # cargo 构建为本地编译，屏蔽代理避免卡住
    env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        VERSION="${VERSION:-dev}" cargo build -p agentgateway -p agentgateway-app --features agentgateway/ui 2>&1 | tail -3
    ok "编译完成: $BINARY"
fi

# ── 1c. 安装 Python 依赖（如果 .venv 不存在）──────────────────────────────────
if [[ ! -d "$AGENTS_DIR/.venv" ]]; then
    step "1c. 安装 Agent Python 依赖 (uv sync)"
    command -v uv &>/dev/null || die "未找到 uv，请安装: curl -LsSf https://astral.sh/uv/install.sh | sh"
    cd "$AGENTS_DIR"
    uv sync 2>&1 | tail -5
    ok "Python 依赖安装完成"
    # 更新 TA_PY 指向新创建的 .venv
    TA_PY="$AGENTS_DIR/.venv/bin/python3"
    cd "$REPO_ROOT"
fi

# ── 2. 启动 Mock Registry ─────────────────────────────────────────────────────
step "2. 启动 Mock Agent Registry（语义路由）"
info "REGISTRY_PORT=$REGISTRY_PORT"
env REGISTRY_PORT="$REGISTRY_PORT" "$TA_PY" "$REGISTRY_SCRIPT" >"$REGISTRY_LOG" 2>&1 &
REGISTRY_PID=$!
wait_http "http://localhost:${REGISTRY_PORT}/health" "Mock Registry" 20

# ── 3. 启动子 Agent（travel-agents 环境）───────────────────────────────────
step "3. 启动旅行规划子 Agent（travel-agents conda 环境）"

# LLM：通过 LiteLLM 接 DashScope（Qwen agents），替代 vLLM
export LITELLM_MODEL="$LITELLM_MODEL"
export LITELLM_API_BASE="$LITELLM_API_BASE"
export OPENAI_API_KEY="$DASHSCOPE_API_KEY"
export GOOGLE_GENAI_USE_VERTEXAI="TRUE"   # 绕过 Google API key 检查
[[ -n "${GOOGLE_API_KEY:-}" ]] && export GOOGLE_API_KEY

# Claude 端点 — Weather Agent、Hotel Agent、Doc Agents 共用
# DOCUMENT_API_KEY 同时作为 ANTHROPIC_API_KEY
export ANTHROPIC_API_KEY="${DOCUMENT_API_KEY:-}"
export ANTHROPIC_API_BASE="${DOCUMENT_API_BASE:-https://api.anthropic.com}"

info "LLM (Qwen)   → model=$LITELLM_MODEL  api_base=$LITELLM_API_BASE"
info "LLM (Claude) → model=anthropic/claude-sonnet-4-6  api_base=${ANTHROPIC_API_BASE}"

cd "$AGENTS_DIR"

# Weather Agent (port 10001) — Claude Sonnet 4.6
# env 覆盖 LITELLM_MODEL，防止继承全局 openai/qwen-plus
start_agent "WeatherAgent"     "$AGENT_LOG_DIR/weather.log" \
    env LITELLM_MODEL="anthropic/claude-sonnet-4-6" \
        "$TA_PY" agents_in_use/weather_agent_claude/__main__.py

# TripAdvisor Agent (port 10003) —— 需要 TRIPADVISOR_API_KEY
if [[ "$HAS_TRIPADVISOR" == "1" ]]; then
    start_agent "TripAdvisorAgent" "$AGENT_LOG_DIR/tripadvisor.log" \
        "$TA_PY" tripadvisor_agent/__main__.py
else
    skip "TripAdvisorAgent（需要 TRIPADVISOR_API_KEY）"
fi

# Event Agent (port 10004) —— 需要 SERPAPI_KEY
if [[ "$HAS_SERPAPI" == "1" ]]; then
    start_agent "EventAgent" "$AGENT_LOG_DIR/event.log" \
        "$TA_PY" event_agent/__main__.py
else
    skip "EventAgent（需要 SERPAPI_KEY）"
fi

# Finance Agent (port 10005) —— 需要 SERPAPI_KEY
if [[ "$HAS_SERPAPI" == "1" ]]; then
    start_agent "FinanceAgent" "$AGENT_LOG_DIR/finance.log" \
        "$TA_PY" finance_agent/__main__.py
else
    skip "FinanceAgent（需要 SERPAPI_KEY）"
fi

# Flight Agent (port 10006) —— 需要 SERPAPI_KEY
if [[ "$HAS_SERPAPI" == "1" ]]; then
    start_agent "FlightAgent" "$AGENT_LOG_DIR/flight.log" \
        "$TA_PY" agents_in_use/flight_agent_qwen/__main__.py
else
    skip "FlightAgent（需要 SERPAPI_KEY）"
fi

# Hotel Agent (port 10007) — Claude Sonnet 4.6，需要 SERPAPI_KEY
if [[ "$HAS_SERPAPI" == "1" ]]; then
    start_agent "HotelAgent" "$AGENT_LOG_DIR/hotel.log" \
        env LITELLM_MODEL="anthropic/claude-sonnet-4-6" \
            "$TA_PY" agents_in_use/hotel_agent_claude/__main__.py
else
    skip "HotelAgent（需要 SERPAPI_KEY）"
fi

# Airbnb Agent (port 10002) —— 需要 GOOGLE_API_KEY
if [[ "$HAS_GOOGLE_KEY" == "1" ]]; then
    start_agent "AirbnbAgent" "$AGENT_LOG_DIR/airbnb.log" \
        "$TA_PY" airbnb_agent/__main__.py
else
    skip "AirbnbAgent（需要 GOOGLE_API_KEY）"
fi

# Document agents（ANTHROPIC_API_KEY 和 ANTHROPIC_API_BASE 已在步骤3前 export）
# DOCUMENT_API_KEY / DOCUMENT_API_BASE / DOCUMENT_API_MODEL 三项供 doc agent 内部 _resolve_llm_config() 读取
export DOCUMENT_API_KEY="${DOCUMENT_API_KEY:-}"
# 强制 doc agents 使用 Claude Sonnet 4.6，避免继承全局 LITELLM_MODEL
export DOCUMENT_API_BASE="${DOCUMENT_API_BASE:-https://api.anthropic.com}"
export DOCUMENT_API_MODEL="${DOCUMENT_API_MODEL:-anthropic/claude-sonnet-4-6}"

# Finance Document Agent (port 10009)
start_agent "FinanceDocAgent"  "$AGENT_LOG_DIR/finance_doc.log" \
    "$TA_PY" agents_in_use/finance_document_agent/__main__.py

# Infosec Document Agent (port 10010)
start_agent "InfosecDocAgent"  "$AGENT_LOG_DIR/infosec_doc.log" \
    "$TA_PY" agents_in_use/infosec_document_agent/__main__.py

# Dept Doc Reader Agent (port 10011)
start_agent "DeptDocAgent"     "$AGENT_LOG_DIR/dept_doc.log" \
    "$TA_PY" agents_in_use/dept_doc_reader_agent/__main__.py

info "等待子 Agent 初始化 (8s)..."
sleep 8

for name in "${!AGENT_PIDS[@]}"; do
    pid="${AGENT_PIDS[$name]}"
    log="${AGENT_LOGS[$name]}"
    if kill -0 "$pid" 2>/dev/null; then
        ok "$name (pid=$pid) 运行中"
    else
        warn "$name (pid=$pid) 已退出，查看: $log"
        tail -5 "$log" 2>/dev/null | sed 's/^/    /' || true
    fi
done

# ── 4. 启动 Host Agent（travel-agents 环境）────────────────────────────────
step "4. 启动 Host Agent (协调器, port 8083)"

export VLLM_BASE_URL="$LITELLM_API_BASE"
export VLLM_API_KEY="$DASHSCOPE_API_KEY"
export VLLM_MODEL_ID="qwen-plus"
export REGISTRY_BASE_URL="http://localhost:${REGISTRY_PORT}"
export APP_URL="http://localhost:3001"

start_agent "HostAgent" "$AGENT_LOG_DIR/host.log" \
    "$TA_PY" agents_in_use/host_agent/__main__.py

info "等待 Host Agent 初始化 (10s)..."
sleep 10

if kill -0 "${AGENT_PIDS[HostAgent]}" 2>/dev/null; then
    ok "HostAgent (pid=${AGENT_PIDS[HostAgent]}) 运行中"
else
    warn "HostAgent 启动失败，查看: ${AGENT_LOGS[HostAgent]}"
    tail -10 "${AGENT_LOGS[HostAgent]}" 2>/dev/null | sed 's/^/    /' || true
fi

# ── 4b. 启动 Agent Listener（实时调用树，port 8084）──────────────────────────
step "4b. 启动 Agent Listener (port 8084)"

command -v node &>/dev/null || warn "未找到 node，跳过 Agent Listener（实时调用树不可用）"
if command -v node &>/dev/null; then
    start_agent "AgentListener" "$LISTENER_LOG" \
        node "$REPO_ROOT/ui/agent-listener/server.js"
    wait_http "http://localhost:8084/tree" "Agent Listener" 10
fi

# ── 5. 启动 AgentGateway（Agentgateway conda 环境）─────────────────────────
step "5. 启动 AgentGateway (cargo binary)"

cd "$REPO_ROOT"
"$BINARY" -f "$CONFIG" >"$GW_LOG" 2>&1 &
GW_PID=$!
info "网关 pid=$GW_PID  log=$GW_LOG"

for i in $(seq 1 40); do
    if curl -sf "http://localhost:15021/healthz/ready" >/dev/null 2>&1; then
        ok "AgentGateway 就绪"
        break
    fi
    if ! kill -0 "$GW_PID" 2>/dev/null; then
        echo "网关启动日志:"
        tail -20 "$GW_LOG" | sed 's/^/  /'
        die "网关进程意外退出，查看: $GW_LOG"
    fi
    sleep 0.5
    [[ $i -eq 40 ]] && die "网关 20s 内未就绪，查看: $GW_LOG"
done

# ── 6. 预热流量 ───────────────────────────────────────────────────────────────
step "6. 发送预热流量（填充工作记忆）"

for round in 1 2 3; do
    code=$(curl -s -o /dev/null -w "%{http_code}" \
        -X POST "http://localhost:${PROXY_PORT}/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -d '{"model":"qwen-plus","messages":[{"role":"user","content":"帮我规划一个旅游行程"}],"max_tokens":50}' 2>/dev/null)
    echo "  预热 $round → HTTP $code"
done

WM=$(curl -sf "$ADMIN/knowledge/working_memory" 2>/dev/null | \
    "$UI_PY" -c "import sys,json; d=json.load(sys.stdin); print(len(d))" 2>/dev/null || echo "?")
ok "工作记忆条目: $WM"

# ── 7. 启动 Dashboard（需要 gradio/plotly/pandas）─────────────────────────────
step "7. 启动 Knowledge Dashboard (port $UI_PORT)"

# 自动安装 dashboard 依赖（如果缺失）
if ! "$UI_PY" -c "import gradio" 2>/dev/null; then
    info "安装 Dashboard 依赖..."
    "$UI_PY" -m pip install -q -r "$REPO_ROOT/dashboard/requirements.txt" 2>&1 | tail -3
fi

"$UI_PY" "$DASHBOARD" \
    --admin "$ADMIN" \
    --proxy-port "$PROXY_PORT" \
    --host 0.0.0.0 \
    --port "$UI_PORT" \
    >"$UI_LOG" 2>&1 &
UI_PID=$!
wait_http "http://localhost:${UI_PORT}/" "Dashboard" 30

# ── 8. 启动摘要 ───────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}╔════════════════════════════════════════════════════════╗${RESET}"
echo -e "${GREEN}${BOLD}║      旅行规划 Multi-Agent + AgentGateway 已启动        ║${RESET}"
echo -e "${GREEN}${BOLD}╠════════════════════════════════════════════════════════╣${RESET}"
echo -e "${GREEN}${BOLD}║${RESET}  Travel Agent UI → ${CYAN}http://localhost:${ADMIN_PORT}/ui/travel${RESET}  ← 对话入口"
echo -e "${GREEN}${BOLD}║${RESET}  Admin UI        → ${CYAN}http://localhost:${ADMIN_PORT}/ui${RESET}"
echo -e "${GREEN}${BOLD}║${RESET}  Dashboard       → ${CYAN}http://localhost:${UI_PORT}${RESET}  (知识图谱)"
echo -e "${GREEN}${BOLD}║${RESET}  Admin API       → ${CYAN}http://localhost:${ADMIN_PORT}${RESET}"
echo -e "${GREEN}${BOLD}║${RESET}  Mock Registry   → ${CYAN}http://localhost:${REGISTRY_PORT}${RESET}"
echo -e "${GREEN}${BOLD}║${RESET}  Agent Listener  → ${CYAN}http://localhost:8084${RESET}  (调用树 SSE)"
echo -e "${GREEN}${BOLD}║${RESET}"
echo -e "${GREEN}${BOLD}║${RESET}  Python 环境："
echo -e "${GREEN}${BOLD}║${RESET}    UI/Dashboard → $UI_PY"
echo -e "${GREEN}${BOLD}║${RESET}    Agents       → $TA_PY"
echo -e "${GREEN}${BOLD}║${RESET}"
echo -e "${GREEN}${BOLD}║${RESET}  Agent 代理端口（通过网关访问）："
echo -e "${GREEN}${BOLD}║${RESET}    :3001 → Host Agent       (8083)  协调器          [DASHSCOPE/Qwen]"
echo -e "${GREEN}${BOLD}║${RESET}    :3002 → Weather Agent    (10001) 天气             [Claude]"
echo -e "${GREEN}${BOLD}║${RESET}    :3003 → Airbnb Agent     (10002) 住宿             [GOOGLE_API_KEY]"
echo -e "${GREEN}${BOLD}║${RESET}    :3004 → TripAdvisor      (10003) 景点             [TRIPADVISOR_API_KEY]"
echo -e "${GREEN}${BOLD}║${RESET}    :3005 → Event Agent      (10004) 活动             [SERPAPI_KEY/Qwen]"
echo -e "${GREEN}${BOLD}║${RESET}    :3006 → Finance Agent    (10005) 金融             [SERPAPI_KEY/Qwen]"
echo -e "${GREEN}${BOLD}║${RESET}    :3007 → Flight Agent     (10006) 航班             [SERPAPI_KEY/Qwen]"
echo -e "${GREEN}${BOLD}║${RESET}    :3008 → Hotel Agent      (10007) 酒店             [Claude]"
echo -e "${GREEN}${BOLD}║${RESET}    :3009 → Finance Doc      (10009) 财务文档         [Claude]"
echo -e "${GREEN}${BOLD}║${RESET}    :3010 → Infosec Doc      (10010) 信息安全文档     [Claude]"
echo -e "${GREEN}${BOLD}║${RESET}    :3011 → Dept Doc Reader  (10011) 部门文档阅读     [Claude]"
echo -e "${GREEN}${BOLD}║${RESET}"
echo -e "${GREEN}${BOLD}║${RESET}  API Key 状态："
echo -e "${GREEN}${BOLD}║${RESET}    DASHSCOPE_API_KEY    → $(  [[ -n "${DASHSCOPE_API_KEY:-}"    ]] && echo "✓ 已设置  (Host/Flight/Event/Finance Agent)" || echo "✗ 未设置")"
echo -e "${GREEN}${BOLD}║${RESET}    DOCUMENT_API_KEY     → $(  [[ -n "${DOCUMENT_API_KEY:-}"     ]] && echo "✓ 已设置  (Weather/Hotel/Doc Agents via Anthropic API)" || echo "✗ 未设置  (Weather/Hotel/Doc Agents 调用 Claude 将失败)")"
echo -e "${GREEN}${BOLD}║${RESET}    SERPAPI_KEY          → $(  [[ -n "${SERPAPI_KEY:-}"          ]] && echo "✓ 已设置  (Flight/Hotel/Event/Finance 可用)" || echo "✗ 未设置  (4 个 Agent 已跳过)")"
echo -e "${GREEN}${BOLD}║${RESET}    TRIPADVISOR_API_KEY  → $(  [[ -n "${TRIPADVISOR_API_KEY:-}"  ]] && echo "✓ 已设置  (TripAdvisor 可用)"               || echo "✗ 未设置  (TripAdvisor 已跳过)")"
echo -e "${GREEN}${BOLD}║${RESET}    GOOGLE_API_KEY       → $(  [[ -n "${GOOGLE_API_KEY:-}"       ]] && echo "✓ 已设置  (Airbnb 可用)"                    || echo "✗ 未设置  (Airbnb 已跳过)")"
echo -e "${GREEN}${BOLD}║${RESET}"
echo -e "${GREEN}${BOLD}║${RESET}  日志："
echo -e "${GREEN}${BOLD}║${RESET}    网关:      $GW_LOG"
echo -e "${GREEN}${BOLD}║${RESET}    Dashboard: $UI_LOG"
echo -e "${GREEN}${BOLD}║${RESET}    Agents:    $AGENT_LOG_DIR/"
echo -e "${GREEN}${BOLD}║${RESET}"
echo -e "${GREEN}${BOLD}║${RESET}  Ctrl+C 退出并关闭所有进程"
echo -e "${GREEN}${BOLD}╚════════════════════════════════════════════════════════╝${RESET}"
echo ""

# ── 9. 心跳流量（维持工作记忆）────────────────────────────────────────────
info "持续发送 LLM 心跳（每 10s）... Ctrl+C 停止"
LOOP=0
while true; do
    sleep 10
    LOOP=$((LOOP + 1))
    SID="sess-$(( (LOOP % 4) + 1 ))"
    curl -s -o /dev/null \
        -X POST "http://localhost:${PROXY_PORT}/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -H "X-Session-ID: $SID" \
        -d "{\"model\":\"qwen-plus\",\"messages\":[{\"role\":\"user\",\"content\":\"旅游问题 $LOOP\"}],\"max_tokens\":30}" \
        2>/dev/null || true

    if (( LOOP % 6 == 0 )); then
        WM=$(curl -sf "$ADMIN/knowledge/working_memory" 2>/dev/null | \
            "$UI_PY" -c "import sys,json; d=json.load(sys.stdin); print(len(d))" 2>/dev/null || echo "?")
        echo "  [轮次 $LOOP] 工作记忆: $WM 条"
    fi
done
