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
Plotly chart builders for the AgentGateway Knowledge Dashboard.
All functions accept raw API data and return Plotly figures.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import plotly.graph_objects as go
import plotly.express as px

# ── colour palette ────────────────────────────────────────────────────────────
PALETTE = {
    "success": "#10b981",   # emerald-500
    "failure": "#ef4444",   # red-500
    "ewma":    "#6366f1",   # indigo-500
    "raw":     "#94a3b8",   # slate-400
    "route0":  "#3b82f6",   # blue-500
    "route1":  "#f59e0b",   # amber-500
    "route2":  "#8b5cf6",   # violet-500
    "bg":      "#0f172a",   # slate-900
    "paper":   "#1e293b",   # slate-800
    "grid":    "#334155",   # slate-700
    "text":    "#e2e8f0",   # slate-200
}

ROUTE_COLORS = ["#3b82f6", "#f59e0b", "#8b5cf6", "#06b6d4", "#ec4899"]


def _dark_layout(**kwargs) -> dict:
    base = dict(
        paper_bgcolor=PALETTE["paper"],
        plot_bgcolor=PALETTE["bg"],
        font=dict(color=PALETTE["text"], family="Inter, system-ui, sans-serif"),
        margin=dict(l=48, r=24, t=40, b=40),
        legend=dict(
            bgcolor="rgba(30,41,59,0.8)",
            bordercolor=PALETTE["grid"],
            borderwidth=1,
        ),
        xaxis=dict(
            gridcolor=PALETTE["grid"],
            zerolinecolor=PALETTE["grid"],
        ),
        yaxis=dict(
            gridcolor=PALETTE["grid"],
            zerolinecolor=PALETTE["grid"],
        ),
    )
    base.update(kwargs)
    return base


def _ts_label(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")


# ── Working Memory charts ─────────────────────────────────────────────────────

def wm_latency_timeline(entries: list[dict]) -> go.Figure:
    """Raw latency scatter + per-route EWMA trend lines."""
    fig = go.Figure()

    routes = sorted({e["route_key"] for e in entries})
    for i, route in enumerate(routes):
        color = ROUTE_COLORS[i % len(ROUTE_COLORS)]
        sub = [e for e in entries if e["route_key"] == route]
        xs = [_ts_label(e["timestamp_secs"]) for e in sub]
        raw_ys = [e["latency_ms"] for e in sub]
        ewma_ys = [e.get("ewma_latency_ms", e["latency_ms"]) for e in sub]

        # Raw latency (faint scatter)
        fig.add_trace(
            go.Scatter(
                x=xs,
                y=raw_ys,
                mode="markers",
                marker=dict(size=5, color=color, opacity=0.35),
                name=f"{route} (raw)",
                showlegend=True,
                legendgroup=route,
            )
        )
        # EWMA trend
        fig.add_trace(
            go.Scatter(
                x=xs,
                y=ewma_ys,
                mode="lines",
                line=dict(width=2.5, color=color),
                name=f"{route} (EWMA α=0.1)",
                legendgroup=route,
            )
        )

    fig.update_layout(
        **_dark_layout(
            title=dict(text="延迟时序：原始值 vs EWMA平滑", font=dict(size=15)),
            xaxis_title="时间 (UTC)",
            yaxis_title="延迟 (ms)",
            hovermode="x unified",
        )
    )
    return fig


def wm_outcome_donut(entries: list[dict]) -> go.Figure:
    """Success / failure donut."""
    success = sum(1 for e in entries if e["outcome"] == "success")
    failure = len(entries) - success
    fig = go.Figure(
        go.Pie(
            labels=["成功", "失败"],
            values=[success, failure],
            hole=0.55,
            marker=dict(colors=[PALETTE["success"], PALETTE["failure"]]),
            textfont=dict(size=13),
            hovertemplate="%{label}: %{value} (%{percent})<extra></extra>",
        )
    )
    fig.update_layout(
        **_dark_layout(
            title=dict(text="请求结果分布", font=dict(size=15)),
            showlegend=True,
        )
    )
    return fig


def wm_route_bar(entries: list[dict]) -> go.Figure:
    """Request count per route."""
    from collections import Counter
    counts = Counter(e["route_key"] for e in entries)
    routes = sorted(counts)
    fig = go.Figure(
        go.Bar(
            x=routes,
            y=[counts[r] for r in routes],
            marker=dict(
                color=[ROUTE_COLORS[i % len(ROUTE_COLORS)] for i in range(len(routes))],
                line=dict(width=0),
            ),
            text=[counts[r] for r in routes],
            textposition="outside",
        )
    )
    fig.update_layout(
        **_dark_layout(
            title=dict(text="各路由请求量", font=dict(size=15)),
            xaxis_title="路由",
            yaxis_title="请求数",
            showlegend=False,
        )
    )
    return fig


def wm_latency_hist(entries: list[dict]) -> go.Figure:
    """Latency histogram split by outcome."""
    fig = go.Figure()
    for outcome, color in [("success", PALETTE["success"]), ("failure", PALETTE["failure"])]:
        vals = [e["latency_ms"] for e in entries if e["outcome"] == outcome]
        if vals:
            fig.add_trace(
                go.Histogram(
                    x=vals,
                    name="成功" if outcome == "success" else "失败",
                    marker_color=color,
                    opacity=0.75,
                    nbinsx=24,
                )
            )
    fig.update_layout(
        **_dark_layout(
            title=dict(text="延迟分布直方图", font=dict(size=15)),
            xaxis_title="延迟 (ms)",
            yaxis_title="频次",
            barmode="overlay",
        )
    )
    return fig


# ── Working Memory charts (customer-facing) ───────────────────────────────────

def wm_knowledge_timeline(entries: list[dict]) -> go.Figure:
    """Swim-lane timeline: when each domain's knowledge was recorded."""
    fig = go.Figure()
    if not entries:
        fig.update_layout(**_dark_layout(title=dict(text="暂无知识记录", font=dict(size=15))))
        return fig

    domains = sorted({e.get("domain_label", e["route_key"]) for e in entries})
    for domain in domains:
        sub = [e for e in entries if e.get("domain_label", e["route_key"]) == domain]
        xs = [_ts_label(e["timestamp_secs"]) for e in sub]
        colors = [PALETTE["success"] if e["outcome"] == "success" else PALETTE["failure"] for e in sub]
        sizes = [12 if e.get("context_fingerprint") else 7 for e in sub]
        symbols = ["star" if e.get("context_fingerprint") else "circle" for e in sub]
        hover = [
            f"{domain}<br>时间: {_ts_label(e['timestamp_secs'])}<br>"
            f"{'★ 可复用上下文' if e.get('context_fingerprint') else '普通记录'}<br>"
            f"{'✅ 有效' if e['outcome'] == 'success' else '❌ 无效'}"
            for e in sub
        ]
        fig.add_trace(
            go.Scatter(
                x=xs,
                y=[domain] * len(sub),
                mode="markers",
                marker=dict(
                    size=sizes,
                    color=colors,
                    symbol=symbols,
                    opacity=0.85,
                    line=dict(width=1, color=PALETTE["grid"]),
                ),
                name=domain,
                hovertext=hover,
                hoverinfo="text",
            )
        )
    fig.update_layout(
        **_dark_layout(
            title=dict(text="知识记录时间轴（★ = 可复用上下文 · 绿 = 有效 · 红 = 无效）", font=dict(size=14)),
            xaxis_title="记录时间",
            yaxis_title="知识领域",
            hovermode="closest",
            height=260,
        )
    )
    return fig


def wm_domain_donut(entries: list[dict]) -> go.Figure:
    """Knowledge distribution by domain — customer-facing donut."""
    from collections import Counter
    labels = [e.get("domain_label", e["route_key"]) for e in entries]
    counts = Counter(labels)
    if not counts:
        fig = go.Figure()
        fig.update_layout(**_dark_layout(title=dict(text="暂无知识记录", font=dict(size=15))))
        return fig
    fig = go.Figure(
        go.Pie(
            labels=list(counts.keys()),
            values=list(counts.values()),
            hole=0.55,
            marker=dict(colors=ROUTE_COLORS[: len(counts)]),
            textfont=dict(size=12),
            hovertemplate="%{label}<br>%{value} 条 (%{percent})<extra></extra>",
        )
    )
    fig.update_layout(
        **_dark_layout(
            title=dict(text="知识领域分布", font=dict(size=15)),
            showlegend=True,
        )
    )
    return fig


def wm_knowledge_bar(entries: list[dict]) -> go.Figure:
    """Stacked bar: reusable vs ordinary knowledge entries per domain."""
    from collections import defaultdict

    domain_data: dict[str, dict[str, int]] = defaultdict(lambda: {"reusable": 0, "ordinary": 0})
    for e in entries:
        domain = e.get("domain_label", e["route_key"])
        if e.get("context_fingerprint"):
            domain_data[domain]["reusable"] += 1
        else:
            domain_data[domain]["ordinary"] += 1

    domains = sorted(domain_data.keys())
    if not domains:
        fig = go.Figure()
        fig.update_layout(**_dark_layout(title=dict(text="暂无知识记录", font=dict(size=15))))
        return fig

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            name="★ 可复用上下文",
            x=domains,
            y=[domain_data[d]["reusable"] for d in domains],
            marker_color=PALETTE["ewma"],
            hovertemplate="%{x}<br>可复用: %{y} 条<extra></extra>",
        )
    )
    fig.add_trace(
        go.Bar(
            name="普通记录",
            x=domains,
            y=[domain_data[d]["ordinary"] for d in domains],
            marker_color=PALETTE["raw"],
            opacity=0.7,
            hovertemplate="%{x}<br>普通记录: %{y} 条<extra></extra>",
        )
    )
    fig.update_layout(
        **_dark_layout(
            title=dict(text="各领域知识积累量（★ 可复用上下文占比）", font=dict(size=14)),
            xaxis_title="知识领域",
            yaxis_title="知识条目数",
            barmode="stack",
        )
    )
    return fig


def wm_reuse_events(entries: list[dict]) -> go.Figure:
    """Scatter: moments when stored knowledge was recalled (fingerprint reuse)."""
    fp_first_seen: dict[int, dict] = {}
    reuse_points: list[dict] = []

    for e in sorted(entries, key=lambda x: x["timestamp_secs"]):
        fp = e.get("context_fingerprint")
        if fp is None:
            continue
        domain = e.get("domain_label", e["route_key"])
        ts = e["timestamp_secs"]
        if fp not in fp_first_seen:
            fp_first_seen[fp] = {"ts": ts, "domain": domain}
        else:
            reuse_points.append(
                {
                    "ts": ts,
                    "domain": domain,
                    "delay_s": max(0, ts - fp_first_seen[fp]["ts"]),
                }
            )

    if not reuse_points:
        fig = go.Figure()
        fig.add_annotation(
            text="尚无知识召回事件<br><sub>当 AI 再次遇到相似上下文时，召回记录将出现在这里</sub>",
            xref="paper",
            yref="paper",
            x=0.5,
            y=0.5,
            showarrow=False,
            font=dict(size=13, color=PALETTE["text"]),
        )
        fig.update_layout(**_dark_layout(title=dict(text="知识召回记录（暂无）", font=dict(size=15))))
        return fig

    domains = sorted({p["domain"] for p in reuse_points})
    fig = go.Figure()
    for i, domain in enumerate(domains):
        color = ROUTE_COLORS[i % len(ROUTE_COLORS)]
        sub = [p for p in reuse_points if p["domain"] == domain]
        fig.add_trace(
            go.Scatter(
                x=[_ts_label(p["ts"]) for p in sub],
                y=[p["delay_s"] for p in sub],
                mode="markers",
                marker=dict(
                    size=14,
                    color=color,
                    symbol="star",
                    opacity=0.9,
                    line=dict(width=1.5, color=PALETTE["text"]),
                ),
                name=domain,
                hovertemplate=(
                    f"<b>{domain}</b><br>"
                    "召回时间: %{x}<br>"
                    "距首次记录: %{y} s<br>"
                    "<extra></extra>"
                ),
            )
        )
    fig.update_layout(
        **_dark_layout(
            title=dict(text="知识召回时间轴（★ = 记忆被成功唤起）", font=dict(size=14)),
            xaxis_title="召回时间",
            yaxis_title="距首次记录 (秒)",
        )
    )
    return fig


# ── Semantic Routing charts ───────────────────────────────────────────────────

def sr_latency_bar(stats: list[dict]) -> go.Figure:
    """EWMA latency per route with a reference baseline."""
    routes = [s["route_key"] for s in stats]
    latencies = [s["ewma_latency_ms"] for s in stats]
    fig = go.Figure(
        go.Bar(
            x=routes,
            y=latencies,
            marker=dict(
                color=latencies,
                colorscale=[[0, "#10b981"], [0.5, "#f59e0b"], [1, "#ef4444"]],
                showscale=True,
                colorbar=dict(title="ms", tickfont=dict(color=PALETTE["text"])),
                line=dict(width=0),
            ),
            text=[f"{v:.0f} ms" for v in latencies],
            textposition="outside",
            hovertemplate="路由: %{x}<br>EWMA延迟: %{y:.1f} ms<extra></extra>",
        )
    )
    fig.update_layout(
        **_dark_layout(
            title=dict(text="各路由 EWMA 延迟（α=0.1 指数平滑）", font=dict(size=15)),
            xaxis_title="路由",
            yaxis_title="EWMA 延迟 (ms)",
            showlegend=False,
        )
    )
    return fig


def sr_success_rate_gauge(stats: list[dict]) -> go.Figure:
    """Success rate per route as bullet gauges."""
    fig = go.Figure()
    for i, s in enumerate(stats):
        total = s["total_requests"]
        rate = (s["success_count"] / total * 100) if total > 0 else 100.0
        color = (
            PALETTE["success"] if rate >= 95
            else PALETTE["route1"] if rate >= 80
            else PALETTE["failure"]
        )
        fig.add_trace(
            go.Indicator(
                mode="gauge+number+delta",
                value=round(rate, 1),
                delta={"reference": 100, "relative": False, "suffix": "%"},
                title={"text": s["route_key"], "font": {"size": 12, "color": PALETTE["text"]}},
                gauge={
                    "axis": {"range": [0, 100], "tickcolor": PALETTE["text"]},
                    "bar": {"color": color},
                    "bgcolor": PALETTE["bg"],
                    "borderwidth": 1,
                    "bordercolor": PALETTE["grid"],
                    "steps": [
                        {"range": [0, 80], "color": "#7f1d1d"},
                        {"range": [80, 95], "color": "#78350f"},
                        {"range": [95, 100], "color": "#064e3b"},
                    ],
                },
                number={"suffix": "%", "font": {"size": 20, "color": PALETTE["text"]}},
                domain={
                    "row": 0,
                    "column": i,
                },
            )
        )
    n = max(len(stats), 1)
    fig.update_layout(
        **_dark_layout(
            title=dict(text="各路由成功率", font=dict(size=15)),
            grid={"rows": 1, "columns": n, "pattern": "independent"},
            height=220,
        )
    )
    return fig


def sr_requests_stacked(stats: list[dict]) -> go.Figure:
    """Stacked bar: success count + failure count per route."""
    routes = [s["route_key"] for s in stats]
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            name="成功",
            x=routes,
            y=[s["success_count"] for s in stats],
            marker_color=PALETTE["success"],
        )
    )
    fig.add_trace(
        go.Bar(
            name="失败",
            x=routes,
            y=[s["failure_count"] for s in stats],
            marker_color=PALETTE["failure"],
        )
    )
    fig.update_layout(
        **_dark_layout(
            title=dict(text="各路由成功/失败请求数", font=dict(size=15)),
            xaxis_title="路由",
            yaxis_title="请求数",
            barmode="stack",
        )
    )
    return fig


# ── KDN charts ────────────────────────────────────────────────────────────────

def kdn_session_overview(sessions: list[dict]) -> go.Figure:
    """Bubble chart: turn_count vs fingerprint diversity, bubble = overlap potential."""
    if not sessions:
        fig = go.Figure()
        fig.update_layout(**_dark_layout(title=dict(text="暂无活跃 Session")))
        return fig

    sids = [s["session_id"] for s in sessions]
    turns = [s["turn_count"] for s in sessions]
    unique_fps = [len(s["seen_fingerprints"]) for s in sessions]
    # overlap potential = turns - unique_fps (how many turns reused a fingerprint)
    overlap_counts = [max(0, t - u) for t, u in zip(turns, unique_fps)]

    fig = go.Figure(
        go.Scatter(
            x=turns,
            y=unique_fps,
            mode="markers+text",
            text=sids,
            textposition="top center",
            marker=dict(
                size=[20 + oc * 10 for oc in overlap_counts],
                color=overlap_counts,
                colorscale=[[0, "#334155"], [0.5, "#6366f1"], [1, "#a855f7"]],
                showscale=True,
                colorbar=dict(title="指纹重叠次数", tickfont=dict(color=PALETTE["text"])),
                opacity=0.85,
                line=dict(width=1, color=PALETTE["text"]),
            ),
            customdata=list(zip(sids, turns, unique_fps, overlap_counts)),
            hovertemplate=(
                "<b>%{customdata[0]}</b><br>"
                "对话轮次: %{customdata[1]}<br>"
                "唯一指纹数: %{customdata[2]}<br>"
                "指纹重叠次数: %{customdata[3]}<br>"
                "<extra></extra>"
            ),
        )
    )
    fig.update_layout(
        **_dark_layout(
            title=dict(text="Session 概览：对话轮次 vs 指纹多样性（气泡大小=KDN命中潜力）", font=dict(size=14)),
            xaxis_title="对话轮次 (turn_count)",
            yaxis_title="唯一指纹数",
        )
    )
    return fig


def kdn_overlap_bar(sessions: list[dict]) -> go.Figure:
    """Overlap count per session — direct KDN cache reuse signal."""
    if not sessions:
        fig = go.Figure()
        fig.update_layout(**_dark_layout(title=dict(text="暂无活跃 Session")))
        return fig

    sids = [s["session_id"] for s in sessions]
    overlaps = [max(0, s["turn_count"] - len(s["seen_fingerprints"])) for s in sessions]
    savings_est = [oc * 200 for oc in overlaps]  # ~200ms TTFT per overlap

    colors = [PALETTE["success"] if oc > 0 else PALETTE["grid"] for oc in overlaps]
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=sids,
            y=overlaps,
            name="指纹重叠次数",
            marker_color=colors,
            text=overlaps,
            textposition="outside",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=sids,
            y=savings_est,
            name="预计 TTFT 节省 (ms)",
            yaxis="y2",
            mode="markers",
            marker=dict(size=10, color="#6366f1", symbol="diamond"),
        )
    )
    fig.update_layout(
        **_dark_layout(
            title=dict(text="KDN 缓存命中潜力：指纹重叠 → TTFT 节省", font=dict(size=14)),
            xaxis_title="Session ID",
            yaxis_title="指纹重叠次数",
            yaxis2=dict(
                title="预计 TTFT 节省 (ms)",
                overlaying="y",
                side="right",
                gridcolor=PALETTE["grid"],
                color=PALETTE["text"],
            ),
            barmode="group",
        )
    )
    return fig


def kdn_fingerprint_heatmap(sessions: list[dict]) -> go.Figure:
    """Heatmap: sessions × fingerprint slots — visualises KV-cache reuse."""
    if not sessions:
        fig = go.Figure()
        fig.update_layout(**_dark_layout(title=dict(text="暂无活跃 Session")))
        return fig

    all_fps = sorted({fp for s in sessions for fp in s["seen_fingerprints"]})
    if not all_fps:
        fig = go.Figure()
        fig.update_layout(**_dark_layout(title=dict(text="暂无 LLM 指纹数据")))
        return fig

    # fp labels: last 8 hex chars
    fp_labels = [f"fp…{fp & 0xFFFFFFFF:08x}" for fp in all_fps]
    sids = [s["session_id"] for s in sessions]

    z = []
    for s in sessions:
        row = [1 if fp in s["seen_fingerprints"] else 0 for fp in all_fps]
        z.append(row)

    fig = go.Figure(
        go.Heatmap(
            z=z,
            x=fp_labels,
            y=sids,
            colorscale=[[0, PALETTE["bg"]], [1, "#6366f1"]],
            showscale=False,
            text=[[("✓" if v else "") for v in row] for row in z],
            texttemplate="%{text}",
            hovertemplate="Session: %{y}<br>指纹: %{x}<br><extra></extra>",
        )
    )
    fig.update_layout(
        **_dark_layout(
            title=dict(text="Session × 指纹命中矩阵（相同指纹 = KDN 复用机会）", font=dict(size=14)),
            xaxis_title="Prompt 指纹 (FNV-1a 64-bit)",
            yaxis_title="Session",
        )
    )
    return fig


# ── Task Router charts ────────────────────────────────────────────────────

def _dag_hierarchical_layout(nodes: list[dict], edges: list[dict]) -> dict[str, tuple[float, float]]:
    """
    Compute hierarchical layout for DAG using Kahn + Barycenter algorithm.
    Returns: {node_id: (x, y)}
    """
    if not nodes:
        return {}

    # Handle single-node DAG
    if len(nodes) == 1:
        return {nodes[0]["id"]: (0.5, 0.5)}

    # Build adjacency: in/out edges per node
    node_ids = {n["id"] for n in nodes}
    in_edges: dict[str, list[str]] = {nid: [] for nid in node_ids}
    out_edges: dict[str, list[str]] = {nid: [] for nid in node_ids}

    for edge in edges:
        if edge["from"] in node_ids and edge["to"] in node_ids:
            out_edges[edge["from"]].append(edge["to"])
            in_edges[edge["to"]].append(edge["from"])

    # Kahn's algorithm: topological sort + layer assignment
    in_degree = {nid: len(in_edges[nid]) for nid in node_ids}
    queue = [nid for nid in node_ids if in_degree[nid] == 0]
    layer: dict[str, int] = {}
    processed = []

    while queue:
        node = queue.pop(0)
        processed.append(node)
        # Layer = max(predecessor layers) + 1
        preds = in_edges[node]
        if preds:
            layer[node] = max(layer.get(p, 0) for p in preds) + 1
        else:
            layer[node] = 0
        for succ in out_edges[node]:
            in_degree[succ] -= 1
            if in_degree[succ] == 0:
                queue.append(succ)

    # Unprocessed nodes (disconnected) go to layer 0
    for nid in node_ids - set(processed):
        layer[nid] = 0

    # Group by layer
    layers: dict[int, list[str]] = {}
    for nid, lyr in layer.items():
        if lyr not in layers:
            layers[lyr] = []
        layers[lyr].append(nid)

    # Barycenter sort within layer (reduce edge crossings)
    def barycenter_y(node_id: str) -> float:
        """Average Y of predecessors for sorting."""
        preds = in_edges[node_id]
        if not preds:
            return 0.0
        return sum(node_y.get(p, 0.5) for p in preds) / len(preds)

    node_y: dict[str, float] = {}
    for lyr_idx in sorted(layers.keys()):
        nodes_in_layer = sorted(layers[lyr_idx], key=barycenter_y)
        num_nodes = len(nodes_in_layer)
        for i, nid in enumerate(nodes_in_layer):
            node_y[nid] = (i + 0.5) / max(num_nodes, 1)

    # Assign X based on layer
    positions: dict[str, tuple[float, float]] = {}
    for nid in node_ids:
        x = layer[nid] * 1.2
        y = node_y[nid]
        positions[nid] = (x, y)

    return positions


def tr_dag_chart(dag_data: dict) -> go.Figure:
    """Visualize task decomposition DAG with hierarchical layout."""
    nodes = dag_data.get("nodes", [])
    edges = dag_data.get("edges", [])

    if not nodes:
        fig = go.Figure()
        fig.update_layout(**_dark_layout(title=dict(text="无任务分解数据")))
        return fig

    # Compute layout
    positions = _dag_hierarchical_layout(nodes, edges)

    fig = go.Figure()

    # Draw edges with arrows
    for edge in edges:
        from_id = edge["from"]
        to_id = edge["to"]
        if from_id in positions and to_id in positions:
            x0, y0 = positions[from_id]
            x1, y1 = positions[to_id]
            # Edge line
            fig.add_trace(
                go.Scatter(
                    x=[x0, x1],
                    y=[y0, y1],
                    mode="lines",
                    line=dict(width=2, color=PALETTE["grid"]),
                    hoverinfo="skip",
                    showlegend=False,
                )
            )
            # Arrow annotation
            fig.add_annotation(
                x=x1, y=y1,
                ax=x0, ay=y0,
                xref="x", yref="y",
                axref="x", ayref="y",
                arrowhead=3,
                arrowsize=1,
                arrowwidth=2,
                arrowcolor=PALETTE["grid"],
                showarrow=True,
            )

    # Draw nodes
    node_xs = []
    node_ys = []
    node_ids = []
    node_colors = []
    node_texts = []

    for node in nodes:
        nid = node["id"]
        if nid in positions:
            x, y = positions[nid]
            node_xs.append(x)
            node_ys.append(y)
            node_ids.append(nid)

            # Color by complexity
            complexity = node.get("estimatedComplexity", 0.5)
            if complexity < 0.6:
                color = "#10b981"  # green
            elif complexity < 0.8:
                color = "#f59e0b"  # amber
            else:
                color = "#ef4444"  # red
            node_colors.append(color)

            # Label: ID + Agent name (first 12 chars)
            agent_name = node.get("assignedAgent", {}).get("agentName", "—")[:12]
            label = f"<b>{nid}</b><br>{agent_name}"
            node_texts.append(label)

    fig.add_trace(
        go.Scatter(
            x=node_xs,
            y=node_ys,
            mode="markers+text",
            marker=dict(
                size=20,
                color=node_colors,
                line=dict(width=2, color=PALETTE["text"]),
            ),
            text=node_texts,
            textposition="middle center",
            textfont=dict(size=10, color=PALETTE["text"]),
            customdata=node_ids,
            hovertemplate=(
                "<b>%{customdata}</b><br>"
                "%{text}<br>"
                "<extra></extra>"
            ),
            showlegend=False,
        )
    )

    fig.update_layout(
        **_dark_layout(
            title=dict(text="任务分解 DAG", font=dict(size=15)),
            height=420,
            dragmode="pan",
            xaxis=dict(showticklabels=False, showgrid=False, zeroline=False),
            yaxis=dict(showticklabels=False, showgrid=False, zeroline=False),
        )
    )
    fig.update_xaxes(range=[-0.5, max(pos[0] for pos in positions.values()) + 0.5] if positions else [0, 1])
    fig.update_yaxes(range=[-0.2, 1.2])

    return fig


def tr_complexity_gauge(score: float, decision_type: str) -> go.Figure:
    """Complexity gauge indicator with threshold line and decision label."""
    fig = go.Figure(
        go.Indicator(
            mode="gauge+number",
            value=score * 100,
            title={
                "text": "复杂度评分",
                "font": {"size": 16},
            },
            gauge={
                "axis": {"range": [0, 100], "tickcolor": PALETTE["text"]},
                "bar": {"color": "#6366f1"},
                "bgcolor": PALETTE["bg"],
                "borderwidth": 1,
                "bordercolor": PALETTE["grid"],
                "steps": [
                    {"range": [0, 60], "color": "#1f2937"},
                    {"range": [60, 80], "color": "#7f1d1d"},
                    {"range": [80, 100], "color": "#7f1d1d"},
                ],
                "threshold": {
                    "line": {"color": "#fbbf24", "width": 3},
                    "thickness": 0.75,
                    "value": 60,
                },
            },
            number={"font": {"size": 28, "color": PALETTE["text"]}},
        )
    )

    # Decision label
    decision_label = "直接路由 ✅" if decision_type == "direct" else "分解路由 🔀"
    decision_color = "#10b981" if decision_type == "direct" else "#ef4444"

    fig.add_annotation(
        text=decision_label,
        x=0.5, y=-0.15,
        xref="paper", yref="paper",
        showarrow=False,
        font=dict(size=13, color=decision_color),
    )

    fig.update_layout(
        **_dark_layout(
            height=300,
        )
    )
    return fig


def tr_history_timeline(history: list[dict]) -> go.Figure:
    """Timeline of routing decisions with complexity scores."""
    if not history:
        fig = go.Figure()
        fig.update_layout(**_dark_layout(title=dict(text="无路由历史")))
        return fig

    xs = list(range(len(history)))
    ys = [h.get("complexityScore", 0.5) for h in history]
    colors = [
        "#10b981" if h.get("decisionType") == "direct" else "#ef4444"
        for h in history
    ]
    symbols = [
        "circle" if h.get("decisionType") == "direct" else "diamond"
        for h in history
    ]

    fig = go.Figure()

    # Threshold line
    fig.add_hline(
        y=0.6,
        line_dash="dash",
        line_color=PALETTE["grid"],
        annotation_text="阈值",
        annotation_position="right",
    )

    # Trace with markers
    fig.add_trace(
        go.Scatter(
            x=xs,
            y=ys,
            mode="markers+lines",
            marker=dict(
                size=10,
                color=colors,
                symbol=symbols,
                line=dict(width=1, color=PALETTE["text"]),
            ),
            line=dict(width=1.5, color=PALETTE["grid"]),
            hovertemplate=(
                "序号: %{x}<br>"
                "复杂度: %{y:.3f}<br>"
                "<extra></extra>"
            ),
            showlegend=False,
        )
    )

    fig.update_layout(
        **_dark_layout(
            title=dict(text="路由决策历史", font=dict(size=15)),
            xaxis_title="序号",
            yaxis_title="复杂度评分",
            height=320,
        )
    )
    return fig


def tr_strategy_pie(history: list[dict]) -> go.Figure:
    """Donut chart: distribution of direct vs decomposed decisions."""
    if not history:
        return go.Figure()

    direct_count = sum(1 for h in history if h.get("decisionType") == "direct")
    decomposed_count = len(history) - direct_count

    fig = go.Figure(
        go.Pie(
            labels=["直接路由", "分解路由"],
            values=[direct_count, decomposed_count],
            hole=0.55,
            marker=dict(colors=["#10b981", "#ef4444"]),
            textfont=dict(size=12),
            hovertemplate="%{label}: %{value}<extra></extra>",
        )
    )

    center_text = f"总计<br><b>{len(history)}</b>"
    fig.add_annotation(
        text=center_text,
        x=0.5, y=0.5,
        xref="paper", yref="paper",
        showarrow=False,
        font=dict(size=14, color=PALETTE["text"]),
    )

    fig.update_layout(
        **_dark_layout(
            title=dict(text="路由决策分布", font=dict(size=15)),
            showlegend=True,
            height=320,
        )
    )
    return fig
