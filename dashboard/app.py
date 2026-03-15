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

"""
AgentGateway Knowledge Management Dashboard
============================================
A three-tab Gradio UI that visualises:
  1. 工作记忆 — ring-buffer of recent request traces
  2. 语义路由 — per-route EWMA latency / success-rate statistics
  3. KDN      — session fingerprint overlap & KV-cache reuse potential

Run:
    pip install -r requirements.txt
    python app.py --admin http://localhost:15000

When the gateway is not running, mock data is served automatically.
"""
from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import gradio as gr
import pandas as pd
import plotly.graph_objects as go
import requests as _requests

from api_client import get_client, MockApiClient, get_kdn_client, MockKdnApiClient
from charts import (
    kdn_fingerprint_heatmap,
    kdn_overlap_bar,
    kdn_session_overview,
    sr_latency_bar,
    sr_requests_stacked,
    sr_success_rate_gauge,
    wm_knowledge_timeline,
    wm_domain_donut,
    wm_knowledge_bar,
    wm_reuse_events,
    tr_dag_chart,
    tr_complexity_gauge,
    tr_history_timeline,
    tr_strategy_pie,
)

# ── helpers ───────────────────────────────────────────────────────────────────

_ADMIN_URL = "http://localhost:15000"
_PROXY_PORT = 3000  # 默认 LLM 代理端口（由 CLI 覆盖）
_KDN_URL = "http://localhost:9000"  # KDN 服务端口（由 CLI 覆盖）

# ── Agent 注册表（旅行规划 Multi-Agent 系统）─────────────────────────────────
# 每个 Agent 通过 AgentGateway 代理：直接端口 → 网关代理端口
TRAVEL_AGENTS = [
    {"key": "weather",     "name": "Weather Agent",           "emoji": "🌤️", "port": 10001, "gw_port": 3002, "desc": "天气预报 · 气候信息"},
    {"key": "flight",      "name": "Flight Agent",            "emoji": "✈️", "port": 10006, "gw_port": 3007, "desc": "航班搜索 · 机票比价"},
    {"key": "hotel",       "name": "Hotel Agent",             "emoji": "🏨", "port": 10007, "gw_port": 3008, "desc": "酒店 · 度假村 · 住宿"},
    {"key": "finance_doc", "name": "Finance Document Agent",  "emoji": "💰", "port": 10009, "gw_port": 3009, "desc": "报销制度 · 费用口径 · 差旅标准"},
    {"key": "infosec_doc", "name": "InfoSec Document Agent",  "emoji": "🔒", "port": 10010, "gw_port": 3010, "desc": "信息安全 · 出境设备 · 保密要求"},
    {"key": "dept_doc",    "name": "Dept Doc Reader Agent",   "emoji": "📋", "port": 10011, "gw_port": 3011, "desc": "采购 · 外事出入境 · 安全备案"},
]

_AGENT_BY_KEY = {a["key"]: a for a in TRAVEL_AGENTS}
_AGENT_CHOICES = [(f"{a['emoji']} {a['name']}", a["key"]) for a in TRAVEL_AGENTS]

# ── 路由 → 知识领域 友好名称映射 ──────────────────────────────────────────────
_ROUTE_DOMAIN_LABELS: dict[str, str] = {
    "default/route0": "🌤️ 天气与气候",
    "default/route1": "✈️ 航班与住宿",
    "default/route2": "📋 差旅规章",
    "default/api/chat": "💬 通用对话",
    "prod/api/chat": "💬 通用对话",
}


def _route_label(route_key: str) -> str:
    return _ROUTE_DOMAIN_LABELS.get(route_key, route_key.split("/")[-1])


def _ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _client():
    return get_client(_ADMIN_URL)


def _kdn_client():
    return get_kdn_client(_KDN_URL)


def _gateway_status() -> tuple[str, str]:
    c = _client()
    kdn = _kdn_client()
    gw_part = "⚠️  Mock 模式（网关未连接）" if isinstance(c, MockApiClient) else "✅  已连接 AgentGateway Admin API"
    kdn_part = "⚠️  KDN Mock 模式" if isinstance(kdn, MockKdnApiClient) else "✅  已连接 KDN 服务"
    combined = f"{gw_part}　|　{kdn_part}"
    status = "warning" if isinstance(c, MockApiClient) or isinstance(kdn, MockKdnApiClient) else "success"
    return combined, status


# ── Task Router Helpers ───────────────────────────────────────────────────

def _parse_agents_text(text: str) -> list[dict]:
    """Parse agent list from pipe-delimited text: name|description|url|skills."""
    agents = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3:
            name, desc, url = parts[0], parts[1], parts[2]
            skills = [s.strip() for s in parts[3].split(",")] if len(parts) > 3 else []
            agents.append({
                "name": name,
                "description": desc,
                "url": url,
                "skills": skills,
            })
    return agents


def _dag_to_dataframe(dag_data: dict) -> pd.DataFrame:
    """Convert DAG data to DataFrame for display."""
    nodes = dag_data.get("nodes", [])
    rows = []
    for node in nodes:
        assignment = node.get("assignedAgent") or {}
        rows.append({
            "子任务ID": node.get("id", "—"),
            "描述": node.get("description", "—")[:50],
            "所需能力": ", ".join(node.get("requiredCapabilities", [])),
            "分配Agent": assignment.get("agentName", "—"),
            "置信度": f"{assignment.get('confidence', 0) * 100:.0f}%",
            "复杂度": f"{node.get('estimatedComplexity', 0):.2f}",
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _history_to_df(history: list[dict]) -> pd.DataFrame:
    """Convert routing history to DataFrame."""
    rows = []
    for i, h in enumerate(reversed(history)):
        rows.append({
            "#": len(history) - i,
            "任务": h.get("task", "")[:40],
            "复杂度": f"{h.get('complexityScore', 0):.2f}",
            "决策": "✅ 直接" if h.get("decisionType") == "direct" else "🔀 分解",
            "策略": h.get("strategy", "—"),
            "目标": h.get("target", "—")[:20],
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ── Agent Panel helpers ───────────────────────────────────────────────────────

def _check_agent_health(agent: dict) -> dict:
    """检查单个 Agent 健康状态（直连 + 网关代理双路）。"""
    result = {"direct": False, "gateway": False, "name": None, "desc": None}
    for label, port in [("direct", agent["port"]), ("gateway", agent["gw_port"])]:
        for path in ["/.well-known/agent.json", "/.well-known/agent-card.json"]:
            try:
                r = _requests.get(
                    f"http://localhost:{port}{path}", timeout=2.0
                )
                if r.status_code == 200:
                    data = r.json()
                    result[label] = True
                    if result["name"] is None:
                        result["name"] = data.get("name") or agent["name"]
                        result["desc"] = data.get("description") or agent["desc"]
                    break
            except Exception:
                pass
    return result


def refresh_agents() -> pd.DataFrame:
    """刷新所有 Agent 状态，返回 DataFrame。"""
    rows = []
    for a in TRAVEL_AGENTS:
        h = _check_agent_health(a)
        direct_ok = h["direct"]
        gw_ok = h["gateway"]
        status = (
            "🟢 在线" if direct_ok else "🔴 离线"
        )
        gw_status = "🟢 代理就绪" if gw_ok else ("⚠️ Agent 离线" if not direct_ok else "🟡 代理异常")
        rows.append({
            "Agent": f"{a['emoji']} {a['name']}",
            "状态": status,
            "直连端口": a["port"],
            "网关代理": f":{a['gw_port']}",
            "网关状态": gw_status,
            "功能": h["desc"] or a["desc"],
        })
    return pd.DataFrame(rows)


def send_agent_message(message: str, agent_key: str, use_gateway: bool) -> str:
    """通过网关（或直连）向指定 Agent 发送 A2A 消息。"""
    if not message.strip():
        return "_请输入消息内容_"

    agent = _AGENT_BY_KEY.get(agent_key)
    if not agent:
        return f"❌ 未知 Agent: {agent_key}"

    port = agent["gw_port"] if use_gateway else agent["port"]
    url = f"http://localhost:{port}/"
    via = f"网关 :{port}" if use_gateway else f"直连 :{port}"

    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {
            "message": {
                "messageId": uuid.uuid4().hex,
                "role": "user",
                "parts": [{"kind": "text", "text": message}],
            }
        },
    }

    try:
        t0 = time.time()
        resp = _requests.post(url, json=payload, timeout=120)
        elapsed = int((time.time() - t0) * 1000)
        resp.raise_for_status()
        data = resp.json()

        # 解析 A2A 响应（不同版本格式有差异）
        text = _extract_a2a_text(data)
        header = (
            f"**{agent['emoji']} {agent['name']}** via `{via}` · {elapsed} ms\n\n---\n\n"
        )
        return header + text

    except _requests.exceptions.ConnectionError:
        return f"❌ 连接失败：Agent 未运行（{via}）\n\n请确认 `start-all.sh` 已启动。"
    except _requests.exceptions.Timeout:
        return f"⏱️ 请求超时（120s）：Agent 可能正在处理复杂任务，请稍后重试。"
    except Exception as e:
        return f"❌ 请求失败：{e}"


def _extract_a2a_text(data: dict) -> str:
    """从 A2A JSON-RPC 响应中提取文本内容。"""
    # 尝试多种响应格式
    result = data.get("result") or {}

    # 格式1: result.message.parts[].text
    msg = result.get("message") or {}
    parts = msg.get("parts") or []
    texts = [p.get("text", "") for p in parts if p.get("text")]
    if texts:
        return "\n".join(texts)

    # 格式2: result.parts[].text
    parts = result.get("parts") or []
    texts = [p.get("text", "") for p in parts if p.get("text")]
    if texts:
        return "\n".join(texts)

    # 格式3: result.status.message.parts
    status = result.get("status") or {}
    msg = status.get("message") or {}
    parts = msg.get("parts") or []
    texts = [p.get("text", "") for p in parts if p.get("text")]
    if texts:
        return "\n".join(texts)

    # 格式4: result.artifacts[].parts[].text
    for art in result.get("artifacts") or []:
        parts = art.get("parts") or []
        texts = [p.get("text", "") for p in parts if p.get("text")]
        if texts:
            return "\n".join(texts)

    # 最后：原始 JSON
    return f"```json\n{json.dumps(data, ensure_ascii=False, indent=2)[:2000]}\n```"


# ── Tab 1: Working Memory ─────────────────────────────────────────────────────

def refresh_wm():
    from collections import Counter

    c = _client()
    entries = c.working_memory() or []
    status_text, _ = _gateway_status()

    # 为每条记录打上领域标签
    for e in entries:
        e["domain_label"] = _route_label(e["route_key"])

    total = len(entries)
    domain_counts = Counter(e["domain_label"] for e in entries)
    num_domains = len(domain_counts)

    # 含指纹的条目 = 可复用上下文
    fp_entries = [e for e in entries if e.get("context_fingerprint") is not None]
    reusable_count = len(fp_entries)

    # 指纹重复出现次数 = 知识被召回次数
    fp_counter = Counter(e["context_fingerprint"] for e in fp_entries)
    recall_count = sum(cnt - 1 for cnt in fp_counter.values() if cnt > 1)

    top_domain = domain_counts.most_common(1)[0][0] if domain_counts else "—"
    success = sum(1 for e in entries if e["outcome"] == "success")
    quality_pct = success / max(total, 1) * 100

    kpi_md = f"""
| 指标 | 值 |
|------|-----|
| 知识条目总数 | **{total}** 条 |
| 涉及知识领域 | **{num_domains}** 个 |
| 可复用上下文 | **{reusable_count}** 条（{reusable_count / max(total, 1) * 100:.0f}%）|
| 知识被召回次数 | **{recall_count}** 次 |
| 知识有效率 | **{quality_pct:.0f}%** |
| 最活跃领域 | {top_domain} |
"""

    # 知识库内容快照：按领域汇总
    rows = []
    for domain, cnt in sorted(domain_counts.items(), key=lambda x: -x[1]):
        d_entries = [e for e in entries if e["domain_label"] == domain]
        d_fp = [e for e in d_entries if e.get("context_fingerprint")]
        d_fp_counts = Counter(e["context_fingerprint"] for e in d_fp)
        d_recall = sum(c - 1 for c in d_fp_counts.values() if c > 1)
        d_success = sum(1 for e in d_entries if e["outcome"] == "success")
        d_quality = d_success / len(d_entries) * 100
        quality_label = "⭐⭐⭐ 优" if d_quality >= 95 else ("⭐⭐ 良" if d_quality >= 80 else "⭐ 需改善")
        last_ts = max(e["timestamp_secs"] for e in d_entries)
        first_ts = min(e["timestamp_secs"] for e in d_entries)
        rows.append(
            {
                "知识领域": domain,
                "记录时段": f"{_ts(first_ts)[11:19]} ~ {_ts(last_ts)[11:19]}",
                "知识条目数": cnt,
                "可复用上下文": len(d_fp_counts),
                "被召回次数": d_recall,
                "质量评级": quality_label,
            }
        )
    df = pd.DataFrame(rows) if rows else pd.DataFrame()

    return (
        status_text,
        kpi_md,
        wm_knowledge_timeline(entries),
        wm_domain_donut(entries),
        wm_knowledge_bar(entries),
        wm_reuse_events(entries),
        df,
    )


# ── Tab 2: Semantic Routing ───────────────────────────────────────────────────

def refresh_sr():
    c = _client()
    stats = c.stats() or []
    corrections = c.corrections() or []
    status_text, _ = _gateway_status()

    # Stats table
    rows = []
    for s in stats:
        total = s["total_requests"]
        rate = s["success_count"] / max(total, 1) * 100
        rows.append(
            {
                "路由": s["route_key"],
                "总请求数": total,
                "成功": s["success_count"],
                "失败": s["failure_count"],
                "成功率": f"{rate:.1f}%",
                "EWMA延迟(ms)": f"{s['ewma_latency_ms']:.1f}",
            }
        )
    stats_df = pd.DataFrame(rows) if rows else pd.DataFrame()

    # Corrections table
    corr_rows = []
    for c_ in corrections:
        corr_rows.append(
            {
                "时间": _ts(c_["timestamp_secs"]),
                "路由": c_["route_key"],
                "纠正说明": c_["note"],
            }
        )
    corr_df = pd.DataFrame(corr_rows) if corr_rows else pd.DataFrame()

    return (
        status_text,
        stats_df,
        sr_latency_bar(stats),
        sr_success_rate_gauge(stats),
        sr_requests_stacked(stats),
        corr_df,
    )


def submit_correction(route_key: str, note: str):
    if not route_key.strip():
        return gr.update(value="⚠️ 路由键不能为空"), gr.update()
    if not note.strip():
        return gr.update(value="⚠️ 纠正说明不能为空"), gr.update()
    c = _client()
    ok = c.post_correction(route_key.strip(), note.strip())
    if ok:
        msg = f"✅ 已记录：[{route_key}] {note[:60]}…"
    else:
        msg = "❌ 提交失败"
    # refresh corrections table
    corr = c.corrections() or []
    rows = [{"时间": _ts(x["timestamp_secs"]), "路由": x["route_key"], "纠正说明": x["note"]} for x in corr]
    return gr.update(value=msg), pd.DataFrame(rows) if rows else pd.DataFrame()


# ── Tab 4: Task Router ────────────────────────────────────────────────────

async def submit_route(task_input, agents_input, history_state):
    """Route a task and update history (async to prevent UI freeze)."""
    strategy = "vectorPrefilterLlm"
    try:
        if not task_input.strip():
            return (
                gr.update(value="⚠️ 任务描述不能为空"),
                gr.update(value="", visible=False),  # loading_md
                gr.update(visible=False), gr.update(), gr.update(),
                gr.update(visible=False), gr.update(),
                gr.update(visible=False), gr.update(),
                history_state, gr.update(), gr.update(), gr.update(),
                gr.update(),
            )

        # Parse agents
        agents = _parse_agents_text(agents_input)
        if not agents:
            return (
                gr.update(value="⚠️ Agent 配置格式错误（需要：名称|描述|URL|技能）"),
                gr.update(value="", visible=False),  # loading_md
                gr.update(visible=False), gr.update(), gr.update(),
                gr.update(visible=False), gr.update(),
                gr.update(visible=False), gr.update(),
                history_state, gr.update(), gr.update(), gr.update(),
                gr.update(),
            )

        # Show loading state
        import asyncio

        # Show loading indicator
        loading_text = "⏳ 正在处理路由请求中... (这可能需要 10-20 秒)"

        # Call API (run in thread pool to avoid blocking)
        c = _client()
        strategy_param = strategy
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: c.route_task(
                task=task_input,
                agents=agents,
                strategy=strategy_param,
            )
        )

        if not result:
            return (
                gr.update(value="❌ 连接错误：无法联系路由服务"),
                gr.update(value="", visible=False),  # loading_md
                gr.update(visible=False), gr.update(), gr.update(),
                gr.update(visible=False), gr.update(),
                gr.update(visible=False), gr.update(),
                history_state, gr.update(), gr.update(), gr.update(),
                gr.update(),
            )

        # Parse decision
        decision = result.get("decision", {})
        decision_type = decision.get("type", "direct")
        complexity_score = result.get("complexityScore", 0.5)
        # Use the strategy that was sent in the request, or fallback to result's strategyUsed
        strategy_used = result.get("strategyUsed", strategy)

        # Build KPI Markdown
        if decision_type == "direct":
            agent_name = decision.get("agentName", "—")
            confidence = decision.get("confidence", 0)
            reason = decision.get("reason", "")
            kpi_md = f"""| 字段 | 值 |
|------|-----|
| 任务 ID | `{result.get('taskId', '—')}` |
| 复杂度评分 | **{complexity_score:.2f}** |
| 决策类型 | **直接路由** ✅ |
| 使用策略 | {strategy_used} |
| 分配 Agent | **{agent_name}** |
| 置信度 | **{confidence*100:.0f}%** |
| 决策原因 | {reason} |"""
            dag_visible = False
            subtask_visible = False
            # Return empty figure and dataframe instead of gr.update()
            dag_chart = go.Figure()
            subtask_df = pd.DataFrame()
        else:
            dag = decision.get("dag", {})
            nodes = dag.get("nodes", [])
            edges = dag.get("edges", [])
            reason = decision.get("reason", "")
            node_count = len(nodes)
            edge_count = len(edges)

            kpi_md = f"""| 字段 | 值 |
|------|-----|
| 任务 ID | `{result.get('taskId', '—')}` |
| 复杂度评分 | **{complexity_score:.2f}** |
| 决策类型 | **分解路由** 🔀 |
| 使用策略 | {strategy_used} |
| 子任务数 | **{node_count}** |
| 依赖边数 | **{edge_count}** |
| 决策原因 | {reason} |"""
            dag_visible = True
            subtask_visible = True
            dag_chart = tr_dag_chart(dag)
            subtask_df = _dag_to_dataframe(dag)

        # Add to history
        new_history = history_state.copy() if isinstance(history_state, list) else []
        new_history.append({
            "task": task_input[:50],
            "complexityScore": complexity_score,
            "decisionType": decision_type,
            "strategy": strategy_used,
            "target": decision.get("agentName", "—") if decision_type == "direct" else f"{len(nodes)} nodes",
        })
        new_history = new_history[-20:]  # Keep last 20

        # Update history charts
        hist_timeline = tr_history_timeline(new_history)
        strategy_pie = tr_strategy_pie(new_history)
        history_df = _history_to_df(new_history)

        status_msg = "✅ 路由完成"

        # Fetch updated persistent stats (SR-5)
        updated_stats = fetch_router_stats()

        return (
            gr.update(value=status_msg),
            gr.update(value="", visible=False),  # loading_md hidden
            gr.update(visible=True),
            gr.update(value=kpi_md),
            tr_complexity_gauge(complexity_score, decision_type),
            gr.update(visible=dag_visible),
            dag_chart,
            gr.update(visible=subtask_visible),
            subtask_df,
            new_history,
            hist_timeline,
            strategy_pie,
            history_df,
            updated_stats,
        )
    except Exception as e:
        import traceback
        error_msg = f"❌ 路由出错：{str(e)}"
        print(f"submit_route error: {e}")
        traceback.print_exc()
        return (
            gr.update(value=error_msg),
            gr.update(value="", visible=False),  # loading_md
            gr.update(visible=False), gr.update(), gr.update(),
            gr.update(visible=False), gr.update(),
            gr.update(visible=False), gr.update(),
            history_state, gr.update(), gr.update(), gr.update(),
            gr.update(),
        )


# ── Tab 3: KDN ───────────────────────────────────────────────────────────────

def fetch_router_stats() -> dict:
    """Fetch persistent router stats from the gateway."""
    c = _client()
    return c.router_stats() or {}


def fetch_router_traces() -> tuple[pd.DataFrame, list]:
    """Fetch recent route traces and return (summary_df, raw_traces)."""
    c = _client()
    traces = c.traces(limit=20) or []
    rows = []
    for t in traces:
        ts = datetime.fromtimestamp(t.get("timestampSecs", 0), tz=timezone.utc).strftime("%H:%M:%S")
        dtype = t.get("decisionType", "")
        if dtype == "direct":
            da = t.get("directAgent") or {}
            target = da.get("agentName", "—")
            nodes = "—"
        else:
            nodes = str(len(t.get("dagNodes") or []))
            target = f"{nodes} 节点"
        exec_info = t.get("execution") or {}
        rows.append({
            "时间": ts,
            "任务": t.get("originalTask", "")[:40] + ("…" if len(t.get("originalTask", "")) > 40 else ""),
            "类型": dtype,
            "目标/节点": target,
            "复杂度": f"{t.get('complexityScore', 0):.2f}",
            "路由延迟(ms)": t.get("latencyMs", 0),
            "执行节点": f"{exec_info.get('successNodes', '—')}/{exec_info.get('totalNodes', '—')}",
            "执行延迟(ms)": exec_info.get("executionLatencyMs", "—"),
        })
    df = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["时间", "任务", "类型", "目标/节点", "复杂度", "路由延迟(ms)", "执行节点", "执行延迟(ms)"]
    )
    return df, traces


def refresh_kdn():
    kdn = _kdn_client()
    status_text, _ = _gateway_status()

    # Fetch data from KDN server
    sessions = kdn.sessions() or []
    kdn_stats = kdn.stats() or {}
    cache_entries = kdn.cache_list() or []
    health = kdn.health() or {}

    # Health status line
    if health.get("status") == "ok":
        health_md = (
            f"🟢 **KDN 服务在线** · 缓存条目: **{health.get('cache_entries', 0)}** · "
            f"目录: `{health.get('kvcache_dir', '—')}` · "
            f"目录存在: {'✅' if health.get('kvcache_dir_exists') else '❌'}"
        )
    elif health.get("status") == "mock":
        health_md = (
            f"⚠️ **KDN Mock 模式**（kdn_server 未运行，端口 {_KDN_URL}）· "
            f"显示模拟数据"
        )
    else:
        health_md = f"🔴 **KDN 服务离线** · 无法连接 `{_KDN_URL}`"

    # KPI — session stats
    total_sessions = len(sessions)
    total_turns = sum(s["turn_count"] for s in sessions)
    overlap_sessions = sum(
        1 for s in sessions if s["turn_count"] > len(s["seen_fingerprints"])
    )
    total_overlaps = sum(
        max(0, s["turn_count"] - len(s["seen_fingerprints"])) for s in sessions
    )
    ttft_saved_est = total_overlaps * 200

    # KPI — cache stats from kdn_server
    cache_total = kdn_stats.get("cache_entries_total", len(cache_entries))
    cache_ready = kdn_stats.get("cache_entries_ready", 0)
    cache_pending = kdn_stats.get("cache_entries_pending", 0)
    disk_files = kdn_stats.get("lmcache_disk_files", 0)
    node_hint = kdn_stats.get("node_hint", "(未配置)")

    kpi_md = f"""
| 指标 | 值 |
|------|-----|
| 活跃 Session 数 | **{total_sessions}** |
| 累计对话轮次 | **{total_turns}** |
| 含重叠指纹的 Session | **{overlap_sessions}** |
| 指纹重叠总次数 | **{total_overlaps}** |
| 预计 TTFT 节省 | **{ttft_saved_est} ms** (≈{ttft_saved_est/1000:.1f}s) |
| KV 缓存总条目 | **{cache_total}** (就绪: {cache_ready} · 等待: {cache_pending}) |
| LMCache 磁盘文件 | **{disk_files}** |
| 推理节点 | `{node_hint}` |
"""

    # Sessions table
    rows = []
    for s in sessions:
        overlap = max(0, s["turn_count"] - len(s["seen_fingerprints"]))
        rows.append(
            {
                "Session ID": s["session_id"],
                "路由": s["route_key"],
                "轮次": s["turn_count"],
                "唯一指纹": len(s["seen_fingerprints"]),
                "重叠次数": overlap,
                "KDN潜力": "🔥 高" if overlap >= 2 else ("⚡ 有" if overlap == 1 else "—"),
                "连续失败": s["consecutive_failures"],
                "最后活跃": _ts(s["last_seen_at_secs"]),
            }
        )
    sessions_df = pd.DataFrame(rows) if rows else pd.DataFrame()

    # Cache entries table
    cache_rows = []
    for e in cache_entries:
        created = e.get("created_at")
        created_str = (
            datetime.fromtimestamp(created, tz=timezone.utc).strftime("%m-%d %H:%M:%S")
            if created else "—"
        )
        fp = e.get("fingerprint")
        cache_rows.append(
            {
                "Cache ID": e.get("cache_id", "—"),
                "状态": "✅ 就绪" if e.get("status") == "ready" else "⏳ 等待",
                "模型": e.get("model", "—"),
                "路由": e.get("route_key", "—"),
                "指纹(hex)": f"{fp:#018x}" if fp else "—",
                "TTFT节省(ms)": e.get("ttft_saved_ms", "—"),
                "节点": e.get("node_hint") or "—",
                "创建时间": created_str,
            }
        )
    cache_df = pd.DataFrame(cache_rows) if cache_rows else pd.DataFrame()

    return (
        status_text,
        health_md,
        kpi_md,
        sessions_df,
        kdn_session_overview(sessions),
        kdn_overlap_bar(sessions),
        kdn_fingerprint_heatmap(sessions),
        cache_df,
    )


def kdn_scan_lmcache():
    kdn = _kdn_client()
    result = kdn.scan_lmcache()
    if result:
        return (
            f"✅ 扫描完成：共 **{result['scanned']}** 个 `.pt` 文件，"
            f"导入 **{result['imported']}**，跳过 **{result['skipped']}**\n\n"
            f"目录：`{result.get('kvcache_dir', '—')}`"
        )
    return "❌ 扫描失败（KDN 服务未连接）"


def kdn_evict_entry(cache_id: str):
    if not cache_id.strip():
        return "⚠️ 请输入 Cache ID"
    kdn = _kdn_client()
    result = kdn.evict(cache_id.strip())
    if result:
        return f"✅ 已删除条目：`{result.get('evicted', cache_id)}`"
    return f"❌ 删除失败（ID `{cache_id.strip()}` 不存在，或 KDN 服务未连接）"


def kdn_compute_fingerprint(prompt: str):
    if not prompt.strip():
        return "⚠️ 请输入 Prompt 文本"
    kdn = _kdn_client()
    result = kdn.get_fingerprint(prompt.strip())
    if result:
        suffix = "" if not isinstance(kdn, MockKdnApiClient) else "\n\n_（本地计算，KDN 服务未连接）_"
        return (
            f"**指纹（十进制）**: `{result['fingerprint']}`\n\n"
            f"**指纹（十六进制）**: `{result['fingerprint_hex']}`"
            f"{suffix}"
        )
    return "❌ 计算失败"


# ── KDN Protocol Reference ───────────────────────────────────────────────────

KDN_PROTOCOL_MD = """
## KDN (Knowledge Delivery Network) 协议说明

```
请求到达 AgentGateway
        │
        ├─► 提取 Prompt 前 512 字节
        │        └─ FNV-1a 64-bit 指纹
        │
        ├─► 查询 Session 工作记忆
        │        └─ 同 Session 中是否见过此指纹？
        │                 ├─ YES → session_overlap = true  ← 最强 KDN 信号
        │                 └─ NO  → session_overlap = false
        │
        ├─► (若配置 kdnEndpoint) POST /kdn/query
        │        ├─ fingerprint, model, route_key
        │        ├─ session_id, session_turn_count, session_overlap
        │        └─ 超时 200ms，失败静默降级
        │
        ├─► KDN 响应
        │        ├─ hit: true  → cache_id + node_hint + ttft_saved_ms
        │        │               → 路由至特定推理节点，跳过 KV 重算
        │        └─ hit: false → 正常路由，KDN 可开始缓存此次 KV-state
        │
        └─► 后端调用 + 响应
```

### 指纹重叠的意义

同一 Session 中出现相同 `context_fingerprint`，意味着：
- 用户在多轮对话中**重复使用了相同的 Prompt 前缀**
- 模型上一次生成时产生的 **KV-cache 大概率可复用**
- KDN 节点可将上次的 Intermediate Activations 直接注入，**节省 TTFT**

### 关键参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| 指纹长度 | 512 bytes | Prompt 前 N 字节，FNV-1a 哈希 |
| KDN 超时 | 200ms | 不阻塞热路径 |
| Session TTL | 1800s | 30 分钟无活动后淘汰 |
"""

# ── Layout ───────────────────────────────────────────────────────────────────

CSS = """
body, .gradio-container { background: #0f172a !important; }
.tab-nav button { font-size: 15px !important; font-weight: 600 !important; }
.kpi-table td { font-size: 14px !important; }
#status-bar { border-radius: 8px; padding: 8px 16px; }
#tr-status { padding: 6px 12px; border-radius: 6px; border-left: 3px solid #6366f1; }

/* DataFrame table: force readable dark-on-light cells */
.gr-dataframe { font-size: 13px !important; }
table { border-collapse: collapse; width: 100%; }
table thead tr th {
    background-color: #1e293b !important;
    color: #94a3b8 !important;
    font-weight: 600;
    padding: 8px 12px;
    border-bottom: 1px solid #334155;
}
table tbody tr td {
    background-color: #0f172a !important;
    color: #e2e8f0 !important;
    padding: 6px 12px;
    border-bottom: 1px solid #1e293b;
}
table tbody tr:hover td {
    background-color: #1e293b !important;
}

/* Markdown text color for dark theme */
.markdown { color: #e2e8f0 !important; }
.prose { color: #e2e8f0 !important; }
code { color: #60a5fa !important; background-color: #1e293b !important; }
pre { background-color: #1e293b !important; color: #e2e8f0 !important; }
"""


def build_app(admin_url: str = "http://localhost:15000", proxy_port: int = 3000, kdn_url: str = "http://localhost:9000") -> gr.Blocks:
    global _ADMIN_URL, _PROXY_PORT, _KDN_URL
    _ADMIN_URL = admin_url
    _PROXY_PORT = proxy_port
    _KDN_URL = kdn_url

    with gr.Blocks(
        title="AgentGateway Knowledge Dashboard",
    ) as demo:

        gr.Markdown(
            "# 🧠 AgentGateway · Knowledge Management Dashboard\n"
            "> **演进式知识管理**：工作记忆 · 语义路由 · KDN 协同推理加速"
        )

        status_bar = gr.Markdown(
            _gateway_status()[0],
            elem_id="status-bar",
        )

        # ── Tab 0: Travel Agent Panel ─────────────────────────────────────
        with gr.Tab("🤖 旅行规划 Agent"):

            gr.Markdown(
                "### 多 Agent 旅行规划系统\n"
                "8 个专业 Agent 通过 AgentGateway 统一代理，Host Agent 自动协调子 Agent 响应复杂旅行查询。"
            )

            # ── Agent 状态面板 ───────────────────────────────────────────
            with gr.Row():
                agents_refresh_btn = gr.Button("🔄 刷新状态", variant="primary", scale=0)
                agents_auto_cb = gr.Checkbox(label="自动刷新 (15s)", value=True, scale=0)

            agents_status_table = gr.DataFrame(
                value=refresh_agents(),
                wrap=True,
                label="Agent 运行状态",
            )

            gr.Markdown("---")

            # ── 对话界面 ─────────────────────────────────────────────────
            gr.Markdown("### 💬 与 Agent 对话")

            with gr.Row():
                with gr.Column(scale=3):
                    agent_msg_input = gr.Textbox(
                        label="消息",
                        placeholder="例：帮我查一下明天北京的天气，以及推荐几个景点...",
                        lines=3,
                    )
                with gr.Column(scale=1):
                    agent_selector = gr.Dropdown(
                        choices=_AGENT_CHOICES,
                        value="host",
                        label="目标 Agent",
                        info="建议选 Host Agent 自动路由",
                    )
                    use_gateway_cb = gr.Checkbox(
                        label="经由网关代理",
                        value=True,
                        info="取消勾选则直连 Agent 端口",
                    )
                    agent_send_btn = gr.Button("🚀 发送", variant="primary")

            agent_response_md = gr.Markdown(
                "_选择 Agent 并输入消息后点击发送_",
                label="Agent 响应",
            )

            # ── 快捷测试用例 ──────────────────────────────────────────────
            with gr.Accordion("💡 快捷测试用例（点击填入）", open=True):
                gr.Markdown("""
| 测试场景 | 推荐 Agent | 示例消息 |
|----------|-----------|---------|
| 旅行规划 | 🧠 Host   | `帮我规划5天日本东京旅游行程，包含航班、酒店和景点推荐` |
| 天气查询 | 🌤️ Weather | `东京明天的天气怎么样？` |
| 航班搜索 | ✈️ Flight  | `查询北京到东京12月15日的航班` |
| 酒店查询 | 🏨 Hotel   | `东京银座附近4星级酒店推荐` |
| 景点推荐 | 🗺️ TripAdvisor | `东京最值得去的10个景点` |
| 汇率查询 | 💹 Finance | `今日人民币兑日元汇率` |
| 活动查询 | 🎭 Event   | `东京12月有什么演出或节日活动？` |
""")

            # ── 回调 ─────────────────────────────────────────────────────
            agents_refresh_btn.click(refresh_agents, outputs=[agents_status_table])

            agent_send_btn.click(
                send_agent_message,
                inputs=[agent_msg_input, agent_selector, use_gateway_cb],
                outputs=[agent_response_md],
            )

            agents_timer = gr.Timer(15)

            def _auto_refresh_agents(enabled):
                if enabled:
                    return refresh_agents()
                return gr.update()

            agents_timer.tick(
                lambda: refresh_agents(),
                outputs=[agents_status_table],
            )

        # ── Tab 1: Working Memory ─────────────────────────────────────────────
        with gr.Tab("📋 工作记忆"):
            with gr.Row():
                wm_refresh_btn = gr.Button("🔄 刷新", variant="primary", scale=0)
                wm_auto_cb = gr.Checkbox(label="自动刷新 (5s)", value=True, scale=0)

            gr.Markdown(
                "> 工作记忆实时捕获每一次 AI 交互，自动提炼可复用上下文，"
                "在后续对话中智能召回——让 AI 像人一样「记住」上下文，越用越聪明。"
            )

            wm_kpi = gr.Markdown("加载中…")

            gr.Markdown("### 🗂️ 工作记忆记录了什么")
            with gr.Row():
                wm_timeline = gr.Plot(label="知识记录时间轴")
                wm_donut = gr.Plot(label="知识领域分布")

            gr.Markdown("### 📈 知识积累与召回")
            with gr.Row():
                wm_route = gr.Plot(label="各领域知识条目")
                wm_hist = gr.Plot(label="知识召回记录")

            gr.Markdown("### 📚 知识库内容快照")
            wm_table = gr.DataFrame(wrap=True, label="各领域知识汇总")

            wm_outputs = [status_bar, wm_kpi, wm_timeline, wm_donut, wm_route, wm_hist, wm_table]
            wm_refresh_btn.click(refresh_wm, outputs=wm_outputs)

            wm_timer = gr.Timer(5)
            wm_timer.tick(refresh_wm, outputs=wm_outputs)


        # ── Tab 2: Task Router ───────────────────────────────────────────────
        # ── Tab 2: Task Router ───────────────────────────────────────────────
        with gr.Tab("🌐 KDN 知识分发网络"):
            with gr.Row():
                kdn_refresh_btn = gr.Button("🔄 刷新", variant="primary", scale=0)
                kdn_auto_cb = gr.Checkbox(label="自动刷新 (8s)", value=True, scale=0)

            kdn_health_md = gr.Markdown("加载中…")
            kdn_kpi = gr.Markdown("加载中…")

            gr.Markdown("### 活跃 Session 列表")
            kdn_table = gr.DataFrame(wrap=True)

            with gr.Row():
                kdn_overview = gr.Plot(label="Session 概览气泡图")
                kdn_overlap = gr.Plot(label="KDN 命中潜力")

            kdn_heatmap = gr.Plot(label="指纹命中矩阵")

            gr.Markdown("### 🗄️ KV Cache 条目")
            kdn_cache_table = gr.DataFrame(wrap=True)

            with gr.Row():
                with gr.Column():
                    gr.Markdown("**🔍 扫描磁盘 LMCache 文件**")
                    kdn_scan_btn = gr.Button("扫描 kvcache/ 目录", variant="secondary")
                    kdn_scan_result = gr.Markdown()
                with gr.Column():
                    gr.Markdown("**🗑️ 删除缓存条目**")
                    kdn_evict_input = gr.Textbox(
                        label="Cache ID",
                        placeholder="kv-abc123...",
                    )
                    kdn_evict_btn = gr.Button("删除条目", variant="stop")
                    kdn_evict_result = gr.Markdown()
                with gr.Column():
                    gr.Markdown("**🔢 计算 Prompt 指纹**")
                    kdn_fp_input = gr.Textbox(
                        label="Prompt 文本",
                        placeholder="输入 prompt 前 512 字节…",
                        lines=2,
                    )
                    kdn_fp_btn = gr.Button("计算 FNV-1a 指纹", variant="secondary")
                    kdn_fp_result = gr.Markdown()

            with gr.Accordion("📖 KDN 协议说明", open=False):
                gr.Markdown(KDN_PROTOCOL_MD)

            kdn_outputs = [
                status_bar,
                kdn_health_md,
                kdn_kpi,
                kdn_table,
                kdn_overview,
                kdn_overlap,
                kdn_heatmap,
                kdn_cache_table,
            ]
            kdn_refresh_btn.click(refresh_kdn, outputs=kdn_outputs)

            kdn_timer = gr.Timer(8)
            kdn_timer.tick(refresh_kdn, outputs=kdn_outputs)

            kdn_scan_btn.click(kdn_scan_lmcache, outputs=[kdn_scan_result])
            kdn_evict_btn.click(kdn_evict_entry, inputs=[kdn_evict_input], outputs=[kdn_evict_result])
            kdn_fp_btn.click(kdn_compute_fingerprint, inputs=[kdn_fp_input], outputs=[kdn_fp_result])

        with gr.Tab("🔀 任务路由器"):
            with gr.Row():
                with gr.Column(scale=3):
                    tr_task_input = gr.Textbox(
                        label="任务描述",
                        placeholder="描述你要完成的任务...",
                        lines=3,
                    )
                with gr.Column(scale=1):
                    gr.Markdown("**🎯 路由策略**\n\n`vectorPrefilterLlm` — 向量预筛 Top-3 再 LLM 精选")
                    tr_submit_btn = gr.Button("🚀 提交路由", variant="primary")
                    tr_clear_btn = gr.Button("🗑️ 清空")

            with gr.Accordion("⚙️ Agent 配置", open=True):
                gr.Markdown("""**格式说明**（每行一个 Agent）：
```
名称 | 描述 | URL | 技能1,技能2,...
```
例：`DataFetcher|数据获取|http://api.data.local|sql,http`
""")
                tr_agents_input = gr.Textbox(
                    label="Agent 列表（管道分隔）",
                    lines=15,
                    value="""WeatherAgent|提供全球城市的天气预报、气候信息、气象数据|http://localhost:10001|天气,weather,forecast,climate,temperature
FlightAgent|搜索和比较航班、机票、票价、航线、中转和航班时刻表|http://localhost:10006|flight,airline,airfare,ticket,departure,arrival
HotelAgent|搜索酒店、度假村、汽车旅馆和传统住宿选项，包含评级、设施和价格比较|http://localhost:10007|hotel,resort,accommodation,lodging
FinanceDocumentAgent|从企业财务部门公告中提取报销制度、费用口径、审批节点、票据要件及差旅报销标准|http://localhost:10009|finance,reimbursement,expense,approval,invoice
InfoSecDocumentAgent|从企业信息安全部门公告中提取出境设备要求、数据保护措施、保密要求及信息安全合规规定|http://localhost:10010|infosec,security,confidential,device,compliance
DeptDocReaderAgent|从采购、外事与出入境、安全与海外风险等部门公告中提取审批流程、备案要求和材料清单|http://localhost:10011|dept,doc,procurement,foreign,safety,approval
DataAnalystAgent|对结构化数据集进行统计分析、数据可视化、趋势分析和商业智能报告|http://localhost:20001|data,analysis,statistics,visualization,report,BI,trend
CodeReviewerAgent|审查代码中的漏洞、安全问题、性能问题和编码最佳实践。支持 Python、JavaScript、Go、Rust|http://localhost:20002|code,review,bug,security,programming,debugging,refactor
ContentWriterAgent|创作博客文章、营销文案、产品描述、社交媒体内容和专业写作|http://localhost:20003|writing,content,blog,copy,marketing,article,SEO
TranslationAgent|在中文、英文、日文、法文、德文、西班牙文等多种语言之间互译文本|http://localhost:20004|translation,language,Chinese,English,Japanese,localization,multilingual
CalendarManagerAgent|管理日程、预约、会议、提醒和日历事件。集成 Google Calendar 和 Outlook|http://localhost:20005|calendar,schedule,meeting,appointment,reminder,planning,time
ImageSearchAgent|从网络搜索和检索图片、照片和视觉内容。支持以图搜图和视觉相似度匹配|http://localhost:20006|image,photo,picture,visual,search,gallery
DocumentProcessorAgent|处理、总结和提取文档信息，包括 PDF、Word 和电子表格。支持 OCR 文字识别|http://localhost:20007|document,PDF,summary,extract,OCR,word,spreadsheet""",
                )

            tr_status_md = gr.Markdown("", elem_id="tr-status")
            tr_loading_md = gr.Markdown("", visible=False)  # Loading indicator

            with gr.Group(visible=False) as tr_result_row:
                with gr.Row():
                    with gr.Column():
                        tr_result_kpi = gr.Markdown()
                    with gr.Column():
                        tr_complexity_plot = gr.Plot(label="复杂度评分")

            with gr.Group(visible=False) as tr_dag_group:
                gr.Markdown("### 📊 DAG 任务分解图")
                tr_dag_plot = gr.Plot(label="任务 DAG")

            with gr.Group(visible=False) as tr_subtask_group:
                gr.Markdown("### 📋 子任务详情")
                tr_subtask_table = gr.DataFrame(wrap=True)

            with gr.Accordion("📜 路由历史", open=True):
                with gr.Row():
                    gr.Button("🗑️ 清空历史", variant="secondary", scale=0)
                    gr.Markdown("**最近 20 次路由记录**")

                with gr.Row():
                    tr_hist_timeline = gr.Plot(label="复杂度时序")
                    tr_strategy_pie = gr.Plot(label="决策分布")

                tr_history_table = gr.DataFrame(wrap=True)

            with gr.Accordion("📊 路由统计（聚合）", open=False):
                gr.Markdown("来自网关 `/task-router/stats`，重启 Dashboard 后数据仍在（网关进程内存持久）。")
                tr_router_stats_json = gr.JSON(label="RouterStats")

            with gr.Accordion("🔍 路由追踪记录", open=True):
                gr.Markdown("类别二+三合并视图：路由决策快照 + DAG 执行结果。来自 `/task-router/traces`。")
                with gr.Row():
                    tr_traces_refresh_btn = gr.Button("🔄 刷新追踪", variant="secondary", scale=0)
                tr_traces_table = gr.DataFrame(
                    label="最近路由记录（点击行查看详情）",
                    wrap=True,
                    interactive=False,
                )
                tr_traces_detail = gr.JSON(label="选中追踪完整详情（含 DAG + 节点回复 + Agent 信息传递 + 最终结果）")
                tr_traces_state = gr.State([])

            # State for history
            tr_history_state = gr.State([])

            # Callbacks
            def _refresh_traces():
                df, raw = fetch_router_traces()
                detail = raw[0] if raw else {}
                return df, raw, detail

            tr_submit_btn.click(
                submit_route,
                inputs=[tr_task_input, tr_agents_input, tr_history_state],
                outputs=[
                    tr_status_md,
                    tr_loading_md,
                    tr_result_row,
                    tr_result_kpi,
                    tr_complexity_plot,
                    tr_dag_group,
                    tr_dag_plot,
                    tr_subtask_group,
                    tr_subtask_table,
                    tr_history_state,
                    tr_hist_timeline,
                    tr_strategy_pie,
                    tr_history_table,
                    tr_router_stats_json,
                ],
            ).then(_refresh_traces, outputs=[tr_traces_table, tr_traces_state, tr_traces_detail])

            def clear_form():
                return "", gr.update(value="", visible=False), gr.update(visible=False), gr.update(), gr.update(), \
                       gr.update(visible=False), gr.update(), \
                       gr.update(visible=False), gr.update(), \
                       [], gr.update(), gr.update(), gr.update(), gr.update()

            tr_clear_btn.click(
                clear_form,
                outputs=[
                    tr_task_input,
                    tr_loading_md,
                    tr_result_row,
                    tr_result_kpi,
                    tr_complexity_plot,
                    tr_dag_group,
                    tr_dag_plot,
                    tr_subtask_group,
                    tr_subtask_table,
                    tr_history_state,
                    tr_hist_timeline,
                    tr_strategy_pie,
                    tr_history_table,
                    tr_router_stats_json,
                ],
            )

            tr_traces_refresh_btn.click(
                _refresh_traces,
                outputs=[tr_traces_table, tr_traces_state, tr_traces_detail],
            )

            def show_trace_detail(evt: gr.SelectData, traces):
                idx = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
                if traces and idx < len(traces):
                    return traces[idx]
                return {}

            tr_traces_table.select(
                show_trace_detail,
                inputs=[tr_traces_state],
                outputs=[tr_traces_detail],
            )

        # ── Initial load ─────────────────────────────────────────────────────
        demo.load(refresh_wm, outputs=wm_outputs)
        demo.load(refresh_kdn, outputs=kdn_outputs)
        demo.load(fetch_router_stats, outputs=[tr_router_stats_json])
        demo.load(_refresh_traces, outputs=[tr_traces_table, tr_traces_state, tr_traces_detail])

    return demo


# ── CLI entry point ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AgentGateway Knowledge Dashboard")
    parser.add_argument(
        "--admin",
        default="http://localhost:15000",
        help="AgentGateway admin URL (default: http://localhost:15000)",
    )
    parser.add_argument(
        "--proxy-port",
        type=int,
        default=3000,
        help="AgentGateway LLM proxy port (default: 3000)",
    )
    parser.add_argument(
        "--kdn",
        default="http://localhost:9000",
        help="KDN server URL (default: http://localhost:9000)",
    )
    parser.add_argument(
        "--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=7860, help="Bind port (default: 7860)"
    )
    parser.add_argument(
        "--share", action="store_true", help="Create a public Gradio share link"
    )
    args = parser.parse_args()

    demo = build_app(admin_url=args.admin, proxy_port=args.proxy_port, kdn_url=args.kdn)
    demo.launch(
        server_name=args.host,
        server_port=args.port,
        share=args.share,
        theme=gr.themes.Base(
            primary_hue=gr.themes.colors.indigo,
            secondary_hue=gr.themes.colors.slate,
            neutral_hue=gr.themes.colors.slate,
        ).set(
            body_background_fill="#0f172a",
            body_text_color="#e2e8f0",
            block_background_fill="#1e293b",
            block_border_color="#334155",
            block_title_text_color="#94a3b8",
            input_background_fill="#0f172a",
        ),
        css=CSS,
    )


if __name__ == "__main__":
    main()
