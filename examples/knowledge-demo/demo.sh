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

# ─────────────────────────────────────────────────────────────────────────────
# AgentGateway — Evolutionary Memory (Feature 6.5) Demo
#
# 展示内容：
#   1. 启动网关（带 knowledge 配置）
#   2. 发送若干 HTTP 请求（模拟成功 / 失败流量）
#   3. 查询 /knowledge/working_memory  — 工作记忆快照
#   4. 查询 /knowledge/stats           — 路由级统计（EWMA 延迟、成功率）
#   5. 提交用户纠正 POST /knowledge/corrections
#   6. 查询 /knowledge/corrections     — 确认纠正已记录
#   7. （可选）模拟 KDN 命中：启动一个 mock KDN，发送带指纹的请求
#
# 依赖：curl, jq, cargo (已构建)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BINARY="$REPO_ROOT/target/debug/agentgateway"
CONFIG="$SCRIPT_DIR/config.yaml"
ADMIN="http://localhost:15000"
PROXY="http://localhost:3000"
GW_PID=""

# ── colours ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; RESET='\033[0m'
info()  { echo -e "${CYAN}▶ $*${RESET}"; }
ok()    { echo -e "${GREEN}✓ $*${RESET}"; }
step()  { echo -e "\n${YELLOW}━━━ $* ━━━${RESET}"; }

# Pretty-print JSON — use jq if available, else python3, else cat
pjson() {
    if command -v jq &>/dev/null; then jq '.'
    elif command -v python3 &>/dev/null; then python3 -m json.tool
    else cat
    fi
}

cleanup() {
    if [[ -n "$GW_PID" ]]; then
        info "Stopping gateway (pid $GW_PID)..."
        kill "$GW_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# ── 0. Build ──────────────────────────────────────────────────────────────────
step "0. Build (debug)"
cd "$REPO_ROOT"
cargo build -p agentgateway-app 2>&1 | tail -3
ok "Build complete"

# ── 1. Start gateway ──────────────────────────────────────────────────────────
step "1. Start AgentGateway"
GW_LOG="/tmp/agentgateway-demo.log"
"$BINARY" -f "$CONFIG" >"$GW_LOG" 2>&1 &
GW_PID=$!
info "Gateway pid=$GW_PID  (logs → $GW_LOG)"
for i in $(seq 1 20); do
    if curl -sf http://localhost:15021/healthz/ready >/dev/null 2>&1; then
        ok "Gateway ready"
        break
    fi
    sleep 0.5
done

# ── 2. Send traffic ───────────────────────────────────────────────────────────
step "2. Send mixed traffic (10 requests)"
for i in $(seq 1 7); do
    curl -sf "$PROXY/" -o /dev/null && echo "  req $i → 200 OK" || true
done
# Trigger some 404s (treated as non-5xx → success in knowledge store)
for i in $(seq 8 10); do
    curl -sf "$PROXY/not-found-$i" -o /dev/null || echo "  req $i → non-2xx (expected)"
done
ok "Traffic sent"

# ── 3. Working memory snapshot ────────────────────────────────────────────────
step "3. Working Memory snapshot  GET /knowledge/working_memory"
curl -sf "$ADMIN/knowledge/working_memory" | pjson
ok "Working memory entries shown above"

# ── 4. Route stats ────────────────────────────────────────────────────────────
step "4. Route stats  GET /knowledge/stats"
curl -sf "$ADMIN/knowledge/stats" | pjson
ok "Stats shown above (ewma_latency_ms, success_rate)"

# ── 5. Submit a user correction ───────────────────────────────────────────────
step "5. Submit user correction  POST /knowledge/corrections"
curl -sf -X POST "$ADMIN/knowledge/corrections" \
    -H "Content-Type: application/json" \
    -d '{"route_key":"default","note":"prefer backend B for low-latency workloads"}' \
    | cat
echo
ok "Correction submitted"

# ── 6. Read corrections back ──────────────────────────────────────────────────
step "6. Read corrections  GET /knowledge/corrections"
curl -sf "$ADMIN/knowledge/corrections" | pjson
ok "Correction persisted in KnowledgeStore"

# ── 7. KDN overlap detection (unit-level demo) ────────────────────────────────
step "7. KDN fingerprint overlap detection (in-process)"
cat <<'RUST_DEMO'
  The working_memory module computes FNV-1a fingerprints of LLM prompt
  prefixes (first 512 bytes).  When two requests share the same fingerprint,
  find_by_fingerprint() returns both entries — the gateway can then query
  the KDN for a cached KV state.

  Configure KDN endpoint in config.yaml:
    knowledge:
      kdnEndpoint: "http://kdn-service:9000"

  The KdnClient will POST:
    { "fingerprint": <u64>, "model": "gpt-4", "route_key": "..." }
  and use the returned cache_id to skip redundant LLM prefill.
RUST_DEMO
ok "See knowledge/kdn_client.rs for full implementation"

echo -e "\n${GREEN}Demo complete.${RESET}"
