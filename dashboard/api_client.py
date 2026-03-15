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
Admin API client for AgentGateway Knowledge Management.

Falls back to mock data automatically when the gateway is unreachable.
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Any

import requests


@dataclass
class ApiClient:
    admin_url: str = "http://localhost:15000"
    timeout: float = 3.0

    def _get(self, path: str) -> list[dict] | None:
        try:
            r = requests.get(f"{self.admin_url}{path}", timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    def working_memory(self) -> list[dict] | None:
        return self._get("/knowledge/working_memory")

    def stats(self) -> list[dict] | None:
        return self._get("/knowledge/stats")

    def sessions(self) -> list[dict] | None:
        return self._get("/knowledge/sessions")

    def corrections(self) -> list[dict] | None:
        return self._get("/knowledge/corrections")

    def router_stats(self) -> dict | None:
        return self._get("/task-router/stats")

    def traces(self, limit: int = 20) -> list[dict] | None:
        return self._get(f"/task-router/traces?limit={limit}")

    def post_correction(self, route_key: str, note: str) -> bool:
        try:
            r = requests.post(
                f"{self.admin_url}/knowledge/corrections",
                json={"route_key": route_key, "note": note},
                timeout=self.timeout,
            )
            return r.status_code == 200
        except Exception:
            return False

    def route_task(
        self,
        task: str,
        agents: list[dict],
        strategy: str | None = None,
        task_id: str | None = None,
    ) -> dict | None:
        """Route a task to agents using the task router API."""
        payload = {"task": task, "agents": agents}
        if task_id:
            payload["taskId"] = task_id
        if strategy:
            payload["strategyOverride"] = strategy
        try:
            r = requests.post(
                f"{self.admin_url}/task-router/route",
                json=payload,
                timeout=120.0,  # 改成 120 秒（2 分钟），复杂任务可能需要 30-60 秒
            )
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    def is_alive(self) -> bool:
        try:
            requests.get(f"{self.admin_url}/knowledge/stats", timeout=1.0)
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Mock data generator — used when the gateway is not running
# ---------------------------------------------------------------------------

_ROUTES = [
    "default/route0",
    "default/route1",
    "default/route2",
]
_BACKENDS = [
    "qwen-plus",
    "qwen-turbo",
    "qwen-plus-safe",
]
_MODELS = ["qwen-plus", "qwen-turbo", None]
_FPS = [14695981039346656037, 9876543210987654321, 3735928559, None]

_START = int(time.time()) - 300
_STATE: dict[str, Any] = {
    "entries": [],
    "corrections": [],
    "seed": 0,
}

_COMPLEX_KEYWORDS = ["然后", "之后", "pipeline", "流程", "序列", "步骤", "分解", "拆解", "分阶段"]


def _seed_entries(n: int = 60) -> None:
    """Pre-populate working memory with synthetic traces."""
    rng = random.Random(42)
    base_latencies = [320.0, 180.0, 450.0]  # baseline per route
    entries = []
    for i in range(n):
        route_idx = rng.randint(0, 2)
        route = _ROUTES[route_idx]
        backend = _BACKENDS[route_idx]
        model = _MODELS[route_idx]
        fp = _FPS[rng.randint(0, 3)] if model else None
        # Add slight noise + a periodic spike for EWMA demonstration
        base = base_latencies[route_idx]
        noise = rng.gauss(0, base * 0.15)
        spike = base * 1.8 if i % 20 == 0 else 0
        latency = max(30, int(base + noise + spike))
        outcome = "failure" if rng.random() < 0.08 else "success"
        entries.append(
            {
                "timestamp_secs": _START + i * 5,
                "route_key": route,
                "backend": backend,
                "llm_model": model,
                "context_fingerprint": fp,
                "outcome": outcome,
                "latency_ms": latency,
            }
        )
    _STATE["entries"] = entries


def _build_ewma(entries: list[dict]) -> list[dict]:
    """Add per-entry ewma_latency_ms for the timeline chart."""
    alpha = 0.1
    ewma_by_route: dict[str, float] = {}
    result = []
    for e in entries:
        rk = e["route_key"]
        ms = e["latency_ms"]
        if rk not in ewma_by_route:
            ewma_by_route[rk] = float(ms)
        else:
            ewma_by_route[rk] = alpha * ms + (1 - alpha) * ewma_by_route[rk]
        result.append({**e, "ewma_latency_ms": round(ewma_by_route[rk], 2)})
    return result


def _compute_stats(entries: list[dict]) -> list[dict]:
    alpha = 0.1
    stats: dict[str, dict] = {}
    for e in entries:
        rk = e["route_key"]
        if rk not in stats:
            stats[rk] = {
                "route_key": rk,
                "total_requests": 0,
                "success_count": 0,
                "failure_count": 0,
                "ewma_latency_ms": 0.0,
            }
        s = stats[rk]
        s["total_requests"] += 1
        if e["outcome"] == "success":
            s["success_count"] += 1
        else:
            s["failure_count"] += 1
        ms = float(e["latency_ms"])
        if s["total_requests"] == 1:
            s["ewma_latency_ms"] = ms
        else:
            s["ewma_latency_ms"] = round(alpha * ms + (1 - alpha) * s["ewma_latency_ms"], 2)
    return list(stats.values())


def _build_sessions(entries: list[dict]) -> list[dict]:
    """Synthesize session states from entries that have fingerprints."""
    sessions: dict[str, dict] = {}
    session_map = {
        "sess-alpha": "default/route0",
        "sess-beta": "default/route1",
        "sess-gamma": "default/route0",
    }
    rng = random.Random(99)
    for e in entries:
        if e["context_fingerprint"] is None:
            continue
        sid = rng.choice(list(session_map.keys()))
        fp = e["context_fingerprint"]
        if sid not in sessions:
            sessions[sid] = {
                "session_id": sid,
                "route_key": session_map[sid],
                "turn_count": 0,
                "seen_fingerprints": [],
                "last_backend": e["backend"],
                "consecutive_failures": 0,
                "created_at_secs": e["timestamp_secs"],
                "last_seen_at_secs": e["timestamp_secs"],
            }
        s = sessions[sid]
        s["turn_count"] += 1
        if fp not in s["seen_fingerprints"]:
            s["seen_fingerprints"].append(fp)
        s["last_backend"] = e["backend"]
        s["last_seen_at_secs"] = e["timestamp_secs"]
        if e["outcome"] == "failure":
            s["consecutive_failures"] += 1
        else:
            s["consecutive_failures"] = 0
    return list(sessions.values())


class MockApiClient:
    """Identical interface to ApiClient but returns synthesized data."""

    def __init__(self) -> None:
        if not _STATE["entries"]:
            _seed_entries(80)

    def _maybe_add_live(self) -> None:
        """Simulate a new request arriving."""
        _STATE["seed"] += 1
        rng = random.Random(_STATE["seed"])
        if rng.random() > 0.4:
            return
        route_idx = rng.randint(0, 2)
        base = [320.0, 180.0, 450.0][route_idx]
        _STATE["entries"].append(
            {
                "timestamp_secs": int(time.time()),
                "route_key": _ROUTES[route_idx],
                "backend": _BACKENDS[route_idx],
                "llm_model": _MODELS[route_idx],
                "context_fingerprint": _FPS[rng.randint(0, 3)] if _MODELS[route_idx] else None,
                "outcome": "failure" if rng.random() < 0.06 else "success",
                "latency_ms": max(30, int(base + rng.gauss(0, base * 0.15))),
            }
        )
        # cap at 200 entries
        if len(_STATE["entries"]) > 200:
            _STATE["entries"] = _STATE["entries"][-200:]

    def working_memory(self) -> list[dict]:
        self._maybe_add_live()
        return _build_ewma(_STATE["entries"])

    def stats(self) -> list[dict]:
        return _compute_stats(_STATE["entries"])

    def sessions(self) -> list[dict]:
        return _build_sessions(_STATE["entries"])

    def corrections(self) -> list[dict]:
        return list(_STATE["corrections"])

    def post_correction(self, route_key: str, note: str) -> bool:
        _STATE["corrections"].append(
            {
                "route_key": route_key,
                "note": note,
                "timestamp_secs": int(time.time()),
            }
        )
        return True

    def is_alive(self) -> bool:
        return False

    def router_stats(self) -> dict:
        return {
            "totalRoutes": 12,
            "directCount": 7,
            "decomposedCount": 5,
            "avgComplexity": 0.58,
            "complexityHistogram": {"low": 3, "medium": 5, "high": 4},
            "avgLatencyMs": 1340.0,
            "p50LatencyMs": 1100.0,
            "p95LatencyMs": 2800.0,
            "maxLatencyMs": 3500.0,
            "perAgentCounts": {"FlightAgent": 3, "WeatherAgent": 2, "HotelAgent": 4, "FinanceDocumentAgent": 3},
            "perAgentAvgConfidence": {"FlightAgent": 0.88, "WeatherAgent": 0.91, "HotelAgent": 0.85, "FinanceDocumentAgent": 0.79},
            "perAgentAvgLatencyMs": {"FlightAgent": 1200.0, "WeatherAgent": 980.0, "HotelAgent": 1450.0, "FinanceDocumentAgent": 1600.0},
            "avgDagNodeCount": 3.2,
            "maxDagNodeCount": 5,
            "strategyCounts": {"vectorPrefilterLlm": 12},
            "recentRoutes": [
                {"taskId": "mock-001", "timestampSecs": int(time.time()) - 120, "originalTask": "从北京飞洛杉矶开会，帮我规划行程",
                 "decisionType": "decomposed", "agentName": None, "dagNodeCount": 4, "complexityScore": 0.85, "latencyMs": 1800},
                {"taskId": "mock-002", "timestampSecs": int(time.time()) - 60, "originalTask": "查询明天北京天气",
                 "decisionType": "direct", "agentName": "WeatherAgent", "dagNodeCount": None, "complexityScore": 0.15, "latencyMs": 420},
            ],
        }

    def traces(self, limit: int = 20) -> list[dict]:
        now = int(time.time())
        return [
            {
                "taskId": "mock-001",
                "timestampSecs": now - 120,
                "originalTask": "从北京飞洛杉矶开会，帮我规划行程并查询差旅政策",
                "complexityScore": 0.85,
                "decisionType": "decomposed",
                "strategy": "vectorPrefilterLlm",
                "latencyMs": 1800,
                "directAgent": None,
                "dagNodes": [
                    {"nodeId": "t1", "description": "查询北京→洛杉矶航班", "assignedAgent": "FlightAgent", "agentUrl": "http://localhost:10006", "estimatedComplexity": 0.3},
                    {"nodeId": "t2", "description": "查询洛杉矶酒店", "assignedAgent": "HotelAgent", "agentUrl": "http://localhost:10007", "estimatedComplexity": 0.3},
                    {"nodeId": "t3", "description": "查询差旅报销政策", "assignedAgent": "FinanceDocumentAgent", "agentUrl": "http://localhost:10009", "estimatedComplexity": 0.25},
                    {"nodeId": "t4", "description": "汇总行程与政策合规性", "assignedAgent": "FlightAgent", "agentUrl": "http://localhost:10006", "estimatedComplexity": 0.4},
                ],
                "dagEdges": [{"from": "t1", "to": "t4"}, {"from": "t2", "to": "t4"}, {"from": "t3", "to": "t4"}],
                "execution": {
                    "taskId": "mock-001",
                    "nodeResults": [
                        {"nodeId": "t1", "agentName": "FlightAgent", "task": "查询北京→洛杉矶航班", "status": "success",
                         "response": "CA983 北京首都→洛杉矶 09:30出发 次日07:20到达 ¥6800", "summaryToDownstream": "CA983，¥6800，次日07:20到达"},
                        {"nodeId": "t2", "agentName": "HotelAgent", "task": "查询洛杉矶酒店", "status": "success",
                         "response": "Marriott Downtown ¥1200/晚，距会场2.3km，15分钟车程", "summaryToDownstream": "Marriott ¥1200/晚，15分钟车程"},
                        {"nodeId": "t3", "agentName": "FinanceDocumentAgent", "task": "查询差旅报销政策", "status": "success",
                         "response": "境外差旅机票上限经济舱¥8000，酒店上限¥1500/晚，需提前3天审批", "summaryToDownstream": "机票≤¥8000，酒店≤¥1500，需提前审批"},
                        {"nodeId": "t4", "agentName": "FlightAgent", "task": "汇总行程与政策合规性", "status": "success",
                         "response": "行程符合政策：CA983 ¥6800<¥8000，Marriott ¥1200<¥1500，建议明日提交审批", "summaryToDownstream": None},
                    ],
                    "agentMessages": [
                        {"fromNodeId": "t1", "toNodeId": "t4", "summary": "CA983，¥6800，次日07:20到达"},
                        {"fromNodeId": "t2", "toNodeId": "t4", "summary": "Marriott ¥1200/晚，15分钟车程"},
                        {"fromNodeId": "t3", "toNodeId": "t4", "summary": "机票≤¥8000，酒店≤¥1500，需提前审批"},
                    ],
                    "finalResult": "根据您的需求，为您规划如下行程：\n\n✈️ 航班：CA983 北京→洛杉矶（¥6800，符合报销标准）\n🏨 酒店：Marriott Downtown（¥1200/晚，15分钟到会场）\n📋 政策：符合差旅标准，请明日提交审批申请",
                    "totalNodes": 4,
                    "successNodes": 4,
                    "executionLatencyMs": 42000,
                },
            },
            {
                "taskId": "mock-002",
                "timestampSecs": now - 60,
                "originalTask": "查询明天北京天气",
                "complexityScore": 0.15,
                "decisionType": "direct",
                "strategy": "vectorPrefilterLlm",
                "latencyMs": 420,
                "directAgent": {"agentName": "WeatherAgent", "agentUrl": "http://localhost:10001", "confidence": 0.96, "reason": "Task matches weather query capabilities"},
                "dagNodes": None,
                "dagEdges": None,
                "execution": None,
            },
        ]

    def route_task(
        self,
        task: str,
        agents: list[dict],
        strategy: str | None = None,
        task_id: str | None = None,
    ) -> dict:
        """Mock task routing with intelligent decomposition based on task complexity."""
        import hashlib

        # Determine task complexity from keywords and length
        task_lower = task.lower()
        has_complex_keywords = any(kw in task_lower for kw in _COMPLEX_KEYWORDS)
        word_count = len(task.split())
        is_complex = has_complex_keywords or word_count > 8

        # Use task hash for deterministic but variable results
        task_hash = int(hashlib.md5(task.encode()).hexdigest(), 16)
        rng = random.Random(task_hash)

        # Generate complexity score (higher for complex tasks)
        complexity_score = rng.uniform(0.7, 0.95) if is_complex else rng.uniform(0.2, 0.5)

        task_id_final = task_id or f"task-{task_hash % 10000:04d}"

        if complexity_score < 0.6:
            # Direct routing
            agent = rng.choice(agents)
            return {
                "taskId": task_id_final,
                "complexityScore": round(complexity_score, 3),
                "decision": {
                    "type": "direct",
                    "agentName": agent["name"],
                    "agentUrl": agent["url"],
                    "confidence": round(rng.uniform(0.75, 0.98), 3),
                    "reason": f"Task matches agent {agent['name']} capabilities",
                },
                "strategyUsed": strategy or "llm",
            }
        else:
            # Decomposed routing (create linear DAG)
            node_count = min(len(agents), rng.randint(2, 4))
            nodes = []
            for i in range(node_count):
                agent = agents[i % len(agents)]
                nodes.append(
                    {
                        "id": f"node-{i}",
                        "description": f"Subtask {i+1}: Process segment",
                        "requiredCapabilities": agent.get("skills", [])[:2],
                        "assignedAgent": {
                            "agentName": agent["name"],
                            "agentUrl": agent["url"],
                            "confidence": round(rng.uniform(0.75, 0.95), 3),
                        },
                        "estimatedComplexity": round(complexity_score / node_count, 3),
                    }
                )

            edges = []
            for i in range(node_count - 1):
                edges.append({"from": f"node-{i}", "to": f"node-{i+1}"})

            return {
                "taskId": task_id_final,
                "complexityScore": round(complexity_score, 3),
                "decision": {
                    "type": "decomposed",
                    "dag": {
                        "nodes": nodes,
                        "edges": edges,
                    },
                    "reason": "Task complexity exceeds threshold; decomposed into subtasks",
                },
                "strategyUsed": strategy or "llm",
            }


def get_client(admin_url: str = "http://localhost:15000") -> ApiClient | MockApiClient:
    """Return a real client if the gateway is up, else mock."""
    c = ApiClient(admin_url)
    if c.is_alive():
        return c
    return MockApiClient()


# ---------------------------------------------------------------------------
# KDN API client — talks directly to kdn_server.py (default port 9000)
# ---------------------------------------------------------------------------

@dataclass
class KdnApiClient:
    kdn_url: str = "http://localhost:9000"
    timeout: float = 3.0

    def _get(self, path: str):
        try:
            r = requests.get(f"{self.kdn_url}{path}", timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    def _post(self, path: str, **params):
        try:
            r = requests.post(
                f"{self.kdn_url}{path}", params=params, timeout=self.timeout
            )
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    def _delete(self, path: str):
        try:
            r = requests.delete(f"{self.kdn_url}{path}", timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    def sessions(self) -> list[dict] | None:
        return self._get("/kdn/sessions")

    def stats(self) -> dict | None:
        return self._get("/kdn/stats")

    def cache_list(self) -> list[dict] | None:
        return self._get("/kdn/list")

    def health(self) -> dict | None:
        return self._get("/kdn/health")

    def scan_lmcache(
        self,
        model: str = "Qwen/Qwen3-8B",
        route_key: str = "default/api/chat",
        ttft_saved_ms: int = 300,
    ) -> dict | None:
        return self._post(
            "/kdn/scan_lmcache",
            model=model,
            route_key=route_key,
            ttft_saved_ms=ttft_saved_ms,
        )

    def evict(self, cache_id: str) -> dict | None:
        return self._delete(f"/kdn/evict/{cache_id}")

    def get_fingerprint(self, prompt: str) -> dict | None:
        try:
            r = requests.get(
                f"{self.kdn_url}/kdn/fingerprint",
                params={"prompt": prompt},
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    def is_alive(self) -> bool:
        try:
            requests.get(f"{self.kdn_url}/kdn/health", timeout=1.0)
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Mock KDN client — used when kdn_server is not running
# ---------------------------------------------------------------------------

_KDN_STATE: dict[str, Any] = {"cache": [], "sessions": []}


def _seed_kdn_cache(n: int = 6) -> None:
    rng = random.Random(7)
    _MOCK_MODELS = ["Qwen/Qwen3-8B", "Qwen/Qwen3-14B"]
    _MOCK_ROUTES = ["default/api/chat", "prod/api/chat"]
    cache = []
    base_ts = int(time.time()) - 600
    for i in range(n):
        fp = rng.getrandbits(64)
        model = _MOCK_MODELS[i % 2]
        route = _MOCK_ROUTES[i % 2]
        cache.append(
            {
                "cache_id": f"kv-mock{i:04d}",
                "fingerprint": fp,
                "model": model,
                "route_key": route,
                "node_hint": "127.0.0.1:8000",
                "ttft_saved_ms": rng.randint(150, 400),
                "lmcache_chunk_hash": f"{rng.getrandbits(32):08x}",
                "lmcache_file": f"/mnt/ssd2/dh/Agent/airbnb_planner_multiagent/kvcache/vllm@mock@1@0@{rng.getrandbits(32):08x}@bfloat16.pt",
                "session_id": None,
                "ttl_secs": None,
                "created_at": base_ts + i * 60,
                "status": "ready" if i < n - 1 else "pending",
            }
        )
    _KDN_STATE["cache"] = cache


def _seed_kdn_sessions() -> None:
    now = int(time.time())
    rng = random.Random(13)
    sessions = []
    for label in ("sess-alpha", "sess-beta", "sess-gamma"):
        turns = rng.randint(3, 8)
        fps = [rng.getrandbits(64) for _ in range(rng.randint(2, turns))]
        sessions.append(
            {
                "session_id": label,
                "route_key": rng.choice(["default/api/chat", "prod/api/chat"]),
                "turn_count": turns,
                "seen_fingerprints": fps,
                "consecutive_failures": rng.randint(0, 1),
                "created_at_secs": now - rng.randint(300, 1200),
                "last_seen_at_secs": now - rng.randint(0, 120),
            }
        )
    _KDN_STATE["sessions"] = sessions


class MockKdnApiClient:
    """Identical interface to KdnApiClient but returns synthesized data."""

    def __init__(self) -> None:
        if not _KDN_STATE["cache"]:
            _seed_kdn_cache()
        if not _KDN_STATE["sessions"]:
            _seed_kdn_sessions()

    def sessions(self) -> list[dict]:
        return list(_KDN_STATE["sessions"])

    def stats(self) -> dict:
        cache = _KDN_STATE["cache"]
        ready = sum(1 for e in cache if e["status"] == "ready")
        pending = len(cache) - ready
        return {
            "cache_entries_total": len(cache),
            "cache_entries_ready": ready,
            "cache_entries_pending": pending,
            "lmcache_disk_files": ready,
            "active_sessions": len(_KDN_STATE["sessions"]),
            "kvcache_dir": "/mnt/ssd2/dh/Agent/airbnb_planner_multiagent/kvcache",
            "node_hint": "(Mock 模式)",
        }

    def cache_list(self) -> list[dict]:
        return list(_KDN_STATE["cache"])

    def health(self) -> dict:
        return {
            "status": "mock",
            "cache_entries": len(_KDN_STATE["cache"]),
            "kvcache_dir": "/mnt/ssd2/dh/Agent/airbnb_planner_multiagent/kvcache",
            "kvcache_dir_exists": False,
        }

    def scan_lmcache(self, **_) -> dict:
        return {"scanned": 0, "imported": 0, "skipped": 0, "kvcache_dir": "(mock)"}

    def evict(self, cache_id: str) -> dict | None:
        before = len(_KDN_STATE["cache"])
        _KDN_STATE["cache"] = [
            e for e in _KDN_STATE["cache"] if e["cache_id"] != cache_id
        ]
        if len(_KDN_STATE["cache"]) < before:
            return {"evicted": cache_id}
        return None

    def get_fingerprint(self, prompt: str) -> dict:
        FNV_PRIME = 0x00000100000001B3
        FNV_OFFSET = 0xCBF29CE484222325
        h = FNV_OFFSET
        for b in prompt.encode("utf-8", errors="replace")[:512]:
            h ^= b
            h = (h * FNV_PRIME) & 0xFFFFFFFFFFFFFFFF
        return {"fingerprint": h, "fingerprint_hex": f"{h:#018x}"}

    def is_alive(self) -> bool:
        return False


def get_kdn_client(kdn_url: str = "http://localhost:9000") -> KdnApiClient | MockKdnApiClient:
    """Return a real KDN client if kdn_server is up, else mock."""
    c = KdnApiClient(kdn_url)
    if c.is_alive():
        return c
    return MockKdnApiClient()
