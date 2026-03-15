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

#!/usr/bin/env python3
"""
KDN Server — Knowledge Delivery Network node

实现两大功能:

【一】KDN Client Protocol (AgentGateway → KDN)
    根据 openapi-kdn.yaml 中 "KDN Client Protocol" 部分的格式，
    实现 AgentGateway 向 KDN 发起的接口，捕获 AgentGateway 发来的信息:
        POST /kdn/query  —— AgentGateway 在路由 LLM 请求前查询 KV 缓存

【二】Qwen KV Cache 跨重启持久化
    Qwen (vLLM + LMCache) 生成的 KV cache 写入磁盘:
        KVCACHE_DIR = /mnt/ssd2/dh/Agent/airbnb_planner_multiagent/kvcache/
    重启 Qwen 后，之前保存的 KV cache 可被直接复用。

    持久化机制:
    • 实际 KV tensor  由 LMCache 管理，写入 KVCACHE_DIR/*.pt
    • fingerprint 索引 由本服务维护，写入 KVCACHE_DIR/index.json
    • 会话状态        写入 KVCACHE_DIR/sessions.json

内部管理接口 (供 vLLM/脚本调用):
    POST /kdn/store          —— 推理完成后注册 KV 缓存条目
    POST /kdn/warmup         —— 推理开始前预注册 fingerprint
    POST /kdn/scan_lmcache   —— 扫描磁盘已有 LMCache 文件，更新索引
    GET  /kdn/list           —— 列出所有缓存条目
    GET  /kdn/stats          —— 统计信息
    GET  /kdn/sessions       —— 活跃会话
    GET  /kdn/health         —— 健康检查
    GET  /kdn/fingerprint    —— 计算 prompt fingerprint (调试用)
    DELETE /kdn/evict/{id}   —— 删除缓存条目

启动 Qwen/vLLM 时的配置 (确保 KV cache 落盘到 KVCACHE_DIR):
    export LMCACHE_LOCAL_DISK=file:///mnt/ssd2/dh/Agent/airbnb_planner_multiagent/kvcache
    export LMCACHE_MAX_LOCAL_DISK_SIZE=200
    export LMCACHE_LOCAL_CPU=True
    export LMCACHE_MAX_LOCAL_CPU_SIZE=5
    export LMCACHE_CHUNK_SIZE=8
    vllm serve Qwen/Qwen3-8B \\
        --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}'

用法:
    python kdn_server.py [--port 9000] [--host 0.0.0.0] [--node-hint 127.0.0.1:8000]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# ─────────────────────────────────────────────────────────────────────────────
# 路径常量
# ─────────────────────────────────────────────────────────────────────────────

# kdn_server.py 在 kdn/ 目录，父目录是 airbnb_planner_multiagent/
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
KVCACHE_DIR   = _PROJECT_ROOT / "kvcache"
INDEX_FILE    = KVCACHE_DIR / "index.json"
SESSION_FILE  = KVCACHE_DIR / "sessions.json"

KVCACHE_DIR.mkdir(parents=True, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# 日志
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [KDN] %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("kdn")

# ─────────────────────────────────────────────────────────────────────────────
# FNV-1a 64-bit（与 AgentGateway 实现完全一致）
# ─────────────────────────────────────────────────────────────────────────────

_FNV_PRIME  = 0x00000100000001B3
_FNV_OFFSET = 0xCBF29CE484222325


def fnv1a_64(data: bytes) -> int:
    """FNV-1a 64-bit hash，结果为无符号 64-bit 整数。"""
    h = _FNV_OFFSET
    for b in data:
        h ^= b
        h = (h * _FNV_PRIME) & 0xFFFFFFFFFFFFFFFF
    return h


def compute_fingerprint(prompt: str) -> int:
    """对 prompt 前 512 字节计算 FNV-1a 64-bit fingerprint。"""
    return fnv1a_64(prompt.encode("utf-8", errors="replace")[:512])


# ─────────────────────────────────────────────────────────────────────────────
# 线程安全的 JSON 持久化字典
# ─────────────────────────────────────────────────────────────────────────────

class PersistentDict:
    """
    将一个 Python dict 持久化到 JSON 文件。
    - 所有写操作先写 .tmp 再 rename（原子写，防止崩溃损坏文件）
    - 所有操作线程安全
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: Dict[str, dict] = {}
        self._load()

    # ── 磁盘 IO ──────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
            log.info(f"加载索引: {len(self._data)} 条 ← {self._path}")
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"加载 {self._path} 失败 ({e})，重置为空索引")
            self._data = {}

    def _flush(self) -> None:
        """原子写入磁盘（调用方须持有锁）。"""
        tmp = self._path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
            tmp.replace(self._path)
        except OSError as e:
            log.error(f"写入 {self._path} 失败: {e}")

    # ── 公共接口 ─────────────────────────────────────────────────────────────

    def get(self, key: str) -> Optional[dict]:
        with self._lock:
            return self._data.get(key)

    def put(self, key: str, value: dict) -> None:
        with self._lock:
            self._data[key] = value
            self._flush()

    def delete(self, key: str) -> bool:
        with self._lock:
            if key not in self._data:
                return False
            del self._data[key]
            self._flush()
            return True

    def values(self) -> List[dict]:
        with self._lock:
            return list(self._data.values())

    def find_key_by(self, field: str, value) -> Optional[str]:
        with self._lock:
            for k, v in self._data.items():
                if v.get(field) == value:
                    return k
        return None

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


# 全局单例
_index    = PersistentDict(INDEX_FILE)
_sessions = PersistentDict(SESSION_FILE)

# ─────────────────────────────────────────────────────────────────────────────
# 辅助：生成索引 key
# ─────────────────────────────────────────────────────────────────────────────

def _idx_key(fingerprint: int, model: str) -> str:
    return f"{fingerprint:016x}:{model}"

# ─────────────────────────────────────────────────────────────────────────────
# LMCache 磁盘文件校验
# ─────────────────────────────────────────────────────────────────────────────

def _file_still_valid(entry: dict) -> bool:
    """
    校验条目对应的 LMCache 磁盘文件是否仍然存在。

    规则:
    - 若 lmcache_file 字段记录了绝对路径 → 直接检查文件是否存在
    - 否则无法主动校验 → 乐观返回 True（LMCache 自己管理文件生命周期）
    """
    path = entry.get("lmcache_file")
    if path:
        return Path(path).exists()
    return True   # 无路径信息时信任索引

# ─────────────────────────────────────────────────────────────────────────────
# 会话状态管理（多轮对话 overlap 检测）
# ─────────────────────────────────────────────────────────────────────────────

_SESSION_TTL = int(os.getenv("KDN_SESSION_TTL_SECS", "1800"))  # 默认 30 分钟


def _session_update(session_id: str, fingerprint: int, route_key: str) -> bool:
    """
    更新会话状态，返回 session_overlap。
    session_overlap = True 当且仅当该 fingerprint 在本 session 中已出现过。
    """
    key = f"sess:{session_id}"
    now = int(time.time())
    entry = _sessions.get(key)

    if entry is None:
        entry = {
            "session_id":          session_id,
            "route_key":           route_key,
            "turn_count":          0,
            "seen_fingerprints":   [],
            "consecutive_failures": 0,
            "created_at_secs":     now,
            "last_seen_at_secs":   now,
        }

    seen: List[int] = entry["seen_fingerprints"]
    overlap = fingerprint in seen
    if not overlap:
        seen.append(fingerprint)

    entry["seen_fingerprints"] = seen
    entry["turn_count"] += 1
    entry["last_seen_at_secs"] = now
    _sessions.put(key, entry)
    return overlap


def _session_evict_expired() -> int:
    """清理过期 session，返回清理数量。"""
    now = int(time.time())
    expired = [
        f"sess:{e['session_id']}"
        for e in _sessions.values()
        if "session_id" in e
        and e.get("last_seen_at_secs", 0) + _SESSION_TTL < now
    ]
    for k in expired:
        _sessions.delete(k)
    return len(expired)


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic 模型 ── KDN Client Protocol
# （完全对照 openapi-kdn.yaml 中 KdnQueryRequest / KdnQueryResponse 定义）
# ─────────────────────────────────────────────────────────────────────────────

class KdnQueryRequest(BaseModel):
    """
    AgentGateway → KDN 的查询请求体。
    对应 openapi-kdn.yaml § components/schemas/KdnQueryRequest
    """
    fingerprint: int = Field(
        ...,
        description=(
            "FNV-1a 64-bit hash of the first 512 bytes of the LLM prompt. "
            "Used as the primary cache lookup key."
        ),
        examples=[14695981039346656037],
    )
    model: str = Field(
        ...,
        description='LLM model identifier (e.g. "gpt-4o", "Qwen/Qwen3-8B").',
        examples=["Qwen/Qwen3-8B"],
    )
    route_key: str = Field(
        ...,
        description="Opaque gateway route identifier in the form bind/listener/route.",
        examples=["prod/api/chat"],
    )
    session_id: Optional[str] = Field(
        None,
        description=(
            "Client-supplied session identifier from the X-Session-ID header. "
            "Omitted when the request carries no session header."
        ),
        examples=["sess-a1b2c3d4"],
    )
    session_turn_count: Optional[int] = Field(
        None,
        description=(
            "Number of turns completed in this session before the current request. "
            "Omitted when session_id is absent."
        ),
        examples=[4],
    )
    session_overlap: Optional[bool] = Field(
        None,
        description=(
            "True when fingerprint was already observed in an earlier turn of the "
            "same session. Strongest signal that cached KV-state can be reused."
        ),
        examples=[True],
    )


class KdnQueryResponse(BaseModel):
    """
    KDN → AgentGateway 的响应体。
    对应 openapi-kdn.yaml § components/schemas/KdnQueryResponse
    """
    hit: bool = Field(
        ...,
        description="true if a matching KV-state is available in cache.",
    )
    cache_id: Optional[str] = Field(
        None,
        description="Opaque identifier of the cached KV-state. Present only when hit=true.",
        examples=["kv-abc123"],
    )
    ttft_saved_ms: Optional[int] = Field(
        None,
        description=(
            "KDN's estimate of TTFT savings (ms) from reusing the cached state. "
            "Present only when hit=true."
        ),
        examples=[280],
    )
    node_hint: Optional[str] = Field(
        None,
        description=(
            "host:port of the inference node holding the cached KV-state. "
            "When present, AgentGateway will route to this specific node."
        ),
        examples=["127.0.0.1:8000"],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic 模型 ── 内部管理接口
# ─────────────────────────────────────────────────────────────────────────────

class KvStoreRequest(BaseModel):
    """
    注册一个新的 KV 缓存条目（由 vLLM 推理完成后调用）。
    fingerprint 和 prompt_prefix 二选一。
    """
    fingerprint:        Optional[int]  = Field(None, description="FNV-1a 64-bit fingerprint")
    prompt_prefix:      Optional[str]  = Field(None, description="Prompt 前 512 字节，KDN 自动计算 fingerprint")
    model:              str            = Field(..., description="LLM model identifier")
    route_key:          str            = Field(..., description="Gateway route identifier")
    node_hint:          Optional[str]  = Field(None, description="推理节点 host:port")
    ttft_saved_ms:      Optional[int]  = Field(200,  description="预估 TTFT 节省 (ms)")
    lmcache_chunk_hash: Optional[str]  = Field(None, description="LMCache 文件中的 chunk_hash (十六进制)")
    lmcache_file:       Optional[str]  = Field(None, description="LMCache 磁盘文件绝对路径")
    session_id:         Optional[str]  = Field(None)
    ttl_secs:           Optional[int]  = Field(None, description="TTL 秒数，None 表示永不过期")


class KvWarmupRequest(BaseModel):
    """推理开始前预注册 fingerprint（占位，推理完成后再调 /kdn/store）。"""
    fingerprint:   Optional[int] = None
    prompt_prefix: Optional[str] = None
    model:         str
    route_key:     str
    session_id:    Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI 应用
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="KDN Server",
    version="1.0",
    description=(
        "Knowledge Delivery Network — 实现 AgentGateway↔KDN 协议，"
        "并持久化 Qwen/vLLM KV cache 使其跨重启可复用。"
    ),
)

# 通过环境变量或命令行参数配置推理节点地址
_NODE_HINT: str = os.getenv("KDN_NODE_HINT", "")


# ─────────────────────────────────────────────────────────────────────────────
# ① POST /kdn/query  —— KDN Client Protocol (AgentGateway 调用)
# ─────────────────────────────────────────────────────────────────────────────

@app.post(
    "/kdn/query",
    response_model=KdnQueryResponse,
    tags=["kdn-client"],
    summary="Query KDN for a cached inference KV-state",
    description=(
        "AgentGateway calls this endpoint **before** routing an LLM request upstream. "
        "It fingerprints the prompt prefix (first 512 bytes, FNV-1a 64-bit hash) and asks "
        "the KDN whether a matching KV-state is already cached.\n\n"
        "**Timeout**: AgentGateway enforces a hard 200 ms client timeout."
    ),
)
async def kdn_query(req: KdnQueryRequest) -> KdnQueryResponse:
    """
    捕获 AgentGateway 发来的查询信息，返回缓存命中/未命中结果。

    AgentGateway 发来的字段:
      - fingerprint       : FNV-1a 64-bit hash (prompt 前 512 字节)
      - model             : LLM 模型名称
      - route_key         : 网关路由标识
      - session_id        : 会话 ID（可选）
      - session_turn_count: 会话已完成轮次（可选）
      - session_overlap   : AgentGateway 侧的 overlap 信号（可选）
    """

    # ── 记录接收到的信息 ──────────────────────────────────────────────────────
    log.info(
        f"[RECV] fingerprint={req.fingerprint:#018x}  model={req.model!r}  "
        f"route={req.route_key!r}  session={req.session_id}  "
        f"turn={req.session_turn_count}  overlap={req.session_overlap}"
    )

    # ── 本地 session 状态更新（KDN 侧 overlap 检测）──────────────────────────
    local_overlap = False
    if req.session_id:
        local_overlap = _session_update(req.session_id, req.fingerprint, req.route_key)
        if local_overlap:
            log.info(
                f"[SESS]  session={req.session_id!r}  fingerprint={req.fingerprint:#018x}  "
                f"→ OVERLAP（本轮与历史轮次 prompt 前缀相同）"
            )

    # ── 在持久化索引中查找 ────────────────────────────────────────────────────
    key   = _idx_key(req.fingerprint, req.model)
    entry = _index.get(key)

    if entry is None:
        log.info(f"[MISS]  fingerprint={req.fingerprint:#018x}  model={req.model!r}")
        return KdnQueryResponse(hit=False)

    # ── 检查 TTL ──────────────────────────────────────────────────────────────
    ttl_secs   = entry.get("ttl_secs")
    created_at = entry.get("created_at", 0.0)
    if ttl_secs and (time.time() > created_at + ttl_secs):
        log.info(f"[EXPIRED]  cache_id={entry['cache_id']}  — 已过期，清除条目")
        _index.delete(key)
        return KdnQueryResponse(hit=False)

    # ── 检查 LMCache 磁盘文件是否仍然存在 ────────────────────────────────────
    if not _file_still_valid(entry):
        log.warning(
            f"[STALE]  cache_id={entry['cache_id']}  "
            f"lmcache_file={entry.get('lmcache_file')}  — 文件已删除，清除条目"
        )
        _index.delete(key)
        return KdnQueryResponse(hit=False)

    # ── 命中 ─────────────────────────────────────────────────────────────────
    node = entry.get("node_hint") or _NODE_HINT or None
    log.info(
        f"[HIT]   fingerprint={req.fingerprint:#018x}  cache_id={entry['cache_id']}  "
        f"node={node}  ttft_saved={entry.get('ttft_saved_ms')}ms"
    )
    return KdnQueryResponse(
        hit          = True,
        cache_id     = entry["cache_id"],
        ttft_saved_ms= entry.get("ttft_saved_ms", 200),
        node_hint    = node,
    )


# ─────────────────────────────────────────────────────────────────────────────
# ② POST /kdn/store  —— 注册 KV 缓存条目（推理完成后调用）
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/kdn/store", tags=["internal"], summary="注册新的 KV 缓存条目")
async def kdn_store(req: KvStoreRequest) -> dict:
    """
    vLLM/Qwen 完成一次推理后，调用本接口将 fingerprint 与 LMCache 磁盘文件
    信息写入持久化索引，使得下次 AgentGateway 查询时可以命中。

    fingerprint 来源（二选一）:
      • 直接传入 fingerprint 字段
      • 传入 prompt_prefix，由 KDN 自动计算 FNV-1a fingerprint
    """
    fp = _resolve_fingerprint(req.fingerprint, req.prompt_prefix)

    # 若 lmcache_file 未指定，在 kvcache/ 目录中尝试按 chunk_hash 查找
    lmcache_file = req.lmcache_file
    if lmcache_file is None and req.lmcache_chunk_hash:
        candidates = sorted(KVCACHE_DIR.glob(f"*@{req.lmcache_chunk_hash}@*.pt"))
        if candidates:
            lmcache_file = str(candidates[0])

    cache_id  = f"kv-{uuid.uuid4().hex[:12]}"
    index_key = _idx_key(fp, req.model)

    entry = {
        "cache_id":           cache_id,
        "fingerprint":        fp,
        "model":              req.model,
        "route_key":          req.route_key,
        "node_hint":          req.node_hint or _NODE_HINT or None,
        "ttft_saved_ms":      req.ttft_saved_ms,
        "lmcache_chunk_hash": req.lmcache_chunk_hash,
        "lmcache_file":       lmcache_file,
        "session_id":         req.session_id,
        "ttl_secs":           req.ttl_secs,
        "created_at":         time.time(),
        "status":             "ready",
    }
    _index.put(index_key, entry)

    log.info(
        f"[STORE]  fingerprint={fp:#018x}  cache_id={cache_id}  "
        f"model={req.model!r}  lmcache_file={lmcache_file}"
    )
    return {
        "cache_id":   cache_id,
        "fingerprint": fp,
        "index_key":  index_key,
        "stored":     True,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ③ POST /kdn/warmup  —— 推理开始前预注册（占位）
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/kdn/warmup", tags=["internal"], summary="推理开始前预注册 fingerprint")
async def kdn_warmup(req: KvWarmupRequest) -> dict:
    """
    推理开始前预注册 fingerprint，状态为 pending。
    推理完成后需调用 /kdn/store 更新为 ready。
    """
    fp        = _resolve_fingerprint(req.fingerprint, req.prompt_prefix)
    index_key = _idx_key(fp, req.model)

    existing = _index.get(index_key)
    if existing and existing.get("status") == "ready":
        return {"status": "already_ready", "cache_id": existing["cache_id"], "fingerprint": fp}

    placeholder_id = f"kv-pending-{uuid.uuid4().hex[:8]}"
    _index.put(index_key, {
        "cache_id":    placeholder_id,
        "fingerprint": fp,
        "model":       req.model,
        "route_key":   req.route_key,
        "node_hint":   _NODE_HINT or None,
        "session_id":  req.session_id,
        "created_at":  time.time(),
        "status":      "pending",
    })
    log.info(f"[WARMUP] fingerprint={fp:#018x}  model={req.model!r}  status=pending")
    return {"status": "pending", "cache_id": placeholder_id, "fingerprint": fp}


# ─────────────────────────────────────────────────────────────────────────────
# ④ POST /kdn/scan_lmcache  —— 扫描磁盘文件，更新索引
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/kdn/scan_lmcache", tags=["internal"], summary="扫描 kvcache/ 目录同步 LMCache 文件")
async def scan_lmcache(
    model:         str = "Qwen/Qwen3-8B",
    route_key:     str = "default/api/chat",
    ttft_saved_ms: int = 300,
) -> dict:
    """
    扫描 kvcache/ 目录下的 LMCache 磁盘文件（*.pt），将尚未在索引中出现的文件
    作为新条目导入。

    Qwen 重启后可调用此接口，确保磁盘上已有的 KV cache 重新被 KDN 感知。

    注意：LMCache 文件名中的 chunk_hash 与 AgentGateway 的 FNV-1a fingerprint
    是不同的哈希，因此这里只能按 chunk_hash 建立记录；真正的 fingerprint 映射
    需通过 /kdn/store 在推理完成后注册。
    """
    pt_files = list(KVCACHE_DIR.glob("*.pt"))
    imported = 0
    skipped  = 0

    for f in pt_files:
        # LMCache 文件名格式: vllm@<model>@<world_size>@<worker_id>@<chunk_hash>@<dtype>.pt
        parts = f.stem.split("@")
        chunk_hash = parts[4] if len(parts) >= 5 else f.stem

        ck_key = f"lmcache_chunk:{chunk_hash}"
        if _index.get(ck_key) is not None:
            skipped += 1
            continue

        cache_id = f"kv-disk-{uuid.uuid4().hex[:8]}"
        _index.put(ck_key, {
            "cache_id":           cache_id,
            "fingerprint":        None,
            "model":              model,
            "route_key":          route_key,
            "node_hint":          _NODE_HINT or None,
            "ttft_saved_ms":      ttft_saved_ms,
            "lmcache_chunk_hash": chunk_hash,
            "lmcache_file":       str(f),
            "session_id":         None,
            "created_at":         f.stat().st_mtime,
            "status":             "ready",
            "source":             "scan_lmcache",
        })
        imported += 1

    log.info(f"[SCAN]  扫描 {len(pt_files)} 个文件  导入 {imported}  跳过 {skipped}")
    return {
        "scanned":    len(pt_files),
        "imported":   imported,
        "skipped":    skipped,
        "kvcache_dir": str(KVCACHE_DIR),
    }


# ─────────────────────────────────────────────────────────────────────────────
# ⑤ GET /kdn/list
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/kdn/list", tags=["internal"], summary="列出所有缓存条目")
async def kdn_list() -> List[dict]:
    return _index.values()


# ─────────────────────────────────────────────────────────────────────────────
# ⑥ GET /kdn/stats
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/kdn/stats", tags=["internal"], summary="缓存统计信息")
async def kdn_stats() -> dict:
    entries  = _index.values()
    total    = len(entries)
    ready    = sum(1 for e in entries if e.get("status") == "ready")
    pending  = sum(1 for e in entries if e.get("status") == "pending")
    pt_files = list(KVCACHE_DIR.glob("*.pt"))
    sessions = [e for e in _sessions.values() if "session_id" in e]
    return {
        "cache_entries_total":   total,
        "cache_entries_ready":   ready,
        "cache_entries_pending": pending,
        "lmcache_disk_files":    len(pt_files),
        "active_sessions":       len(sessions),
        "kvcache_dir":           str(KVCACHE_DIR),
        "node_hint":             _NODE_HINT or "(未配置)",
    }


# ─────────────────────────────────────────────────────────────────────────────
# ⑦ GET /kdn/sessions
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/kdn/sessions", tags=["internal"], summary="活跃会话列表")
async def kdn_sessions() -> List[dict]:
    _session_evict_expired()
    return [e for e in _sessions.values() if "session_id" in e]


# ─────────────────────────────────────────────────────────────────────────────
# ⑧ GET /kdn/health
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/kdn/health", tags=["internal"], summary="健康检查")
async def kdn_health() -> dict:
    return {
        "status":             "ok",
        "cache_entries":      len(_index),
        "kvcache_dir":        str(KVCACHE_DIR),
        "kvcache_dir_exists": KVCACHE_DIR.exists(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# ⑨ DELETE /kdn/evict/{cache_id}
# ─────────────────────────────────────────────────────────────────────────────

@app.delete("/kdn/evict/{cache_id}", tags=["internal"], summary="删除缓存条目（仅清除 KDN 索引）")
async def kdn_evict(cache_id: str) -> dict:
    key = _index.find_key_by("cache_id", cache_id)
    if key is None:
        raise HTTPException(status_code=404, detail=f"cache_id '{cache_id}' 不存在")
    _index.delete(key)
    log.info(f"[EVICT]  cache_id={cache_id}")
    return {"evicted": cache_id}


# ─────────────────────────────────────────────────────────────────────────────
# ⑩ GET /kdn/fingerprint  （调试工具）
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/kdn/fingerprint", tags=["internal"], summary="计算 prompt 的 FNV-1a fingerprint")
async def get_fingerprint(prompt: str) -> dict:
    fp = compute_fingerprint(prompt)
    return {"fingerprint": fp, "fingerprint_hex": f"{fp:#018x}"}


# ─────────────────────────────────────────────────────────────────────────────
# 内部工具函数
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_fingerprint(
    fingerprint: Optional[int],
    prompt_prefix: Optional[str],
) -> int:
    """从 fingerprint 或 prompt_prefix 得到 fingerprint，两者都没有则报错。"""
    if fingerprint is not None:
        return fingerprint
    if prompt_prefix is not None:
        return compute_fingerprint(prompt_prefix)
    raise HTTPException(
        status_code=400,
        detail="必须提供 fingerprint 或 prompt_prefix 之一",
    )


# ─────────────────────────────────────────────────────────────────────────────
# 命令行 & 启动
# ─────────────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="KDN Server — AgentGateway KV 缓存元数据服务",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
启动 Qwen/vLLM 时配置 LMCache 落盘到 kvcache/ 目录:
  export LMCACHE_LOCAL_DISK=file:///mnt/ssd2/dh/Agent/airbnb_planner_multiagent/kvcache
  export LMCACHE_MAX_LOCAL_DISK_SIZE=200
  export LMCACHE_LOCAL_CPU=True
  export LMCACHE_MAX_LOCAL_CPU_SIZE=5
  export LMCACHE_CHUNK_SIZE=8
  vllm serve Qwen/Qwen3-8B \\
      --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}'
        """,
    )
    p.add_argument("--host",      default="0.0.0.0",  help="监听地址 (默认 0.0.0.0)")
    p.add_argument("--port",      type=int, default=9000, help="监听端口 (默认 9000)")
    p.add_argument("--node-hint", default="", help="Qwen/vLLM 节点地址 host:port，如 127.0.0.1:8000")
    return p.parse_args()


def _print_banner(host: str, port: int) -> None:
    border = "═" * 64
    print(f"\n╔{border}╗")
    print(f"║{'KDN Server  (AgentGateway → KDN)':^64}║")
    print(f"╠{border}╣")
    print(f"║  监听地址   : http://{host}:{port:<40}║")
    print(f"║  KV 索引    : {str(INDEX_FILE):<50}║")
    print(f"║  会话状态   : {str(SESSION_FILE):<50}║")
    print(f"║  KV 缓存目录: {str(KVCACHE_DIR):<50}║")
    print(f"╠{border}╣")
    print(f"║  {'KDN Client Protocol (AgentGateway 调用):':<62}║")
    print(f"║    POST /kdn/query                                             ║")
    print(f"║  {'内部管理接口:':<62}║")
    print(f"║    POST   /kdn/store          注册新 KV 缓存条目               ║")
    print(f"║    POST   /kdn/warmup         推理前预注册                     ║")
    print(f"║    POST   /kdn/scan_lmcache   扫描磁盘文件导入索引             ║")
    print(f"║    GET    /kdn/list           列出所有缓存                     ║")
    print(f"║    GET    /kdn/stats          统计信息                         ║")
    print(f"║    GET    /kdn/sessions       活跃会话                         ║")
    print(f"║    GET    /kdn/health         健康检查                         ║")
    print(f"║    DELETE /kdn/evict/{{id}}     删除条目                        ║")
    print(f"║  {'API 文档:':<62}║")
    print(f"║    http://{host}:{port}/docs                                      ║")
    print(f"╠{border}╣")
    print(f"║  {'KV Cache 持久化配置（启动 Qwen/vLLM 时设置）:':<62}║")
    print(f"║    LMCACHE_LOCAL_DISK=file://{str(KVCACHE_DIR):<35}║")
    print(f"║    LMCACHE_MAX_LOCAL_DISK_SIZE=200                             ║")
    print(f"║    LMCACHE_CHUNK_SIZE=8                                        ║")
    print(f"╚{border}╝\n")


if __name__ == "__main__":
    args = _parse_args()

    if args.node_hint:
        _NODE_HINT = args.node_hint
        os.environ["KDN_NODE_HINT"] = args.node_hint

    _print_banner(args.host, args.port)
    log.info(f"启动 KDN 服务 — 端口 {args.port}，已加载 {len(_index)} 条索引记录")

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="warning",
    )
