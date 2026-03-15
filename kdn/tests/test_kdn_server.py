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
Tests for kdn_server.py — KDN (Knowledge Delivery Network) server.

Tests cover:
  - POST /kdn/query  (KDN Client Protocol, called by AgentGateway)
  - POST /kdn/store  (internal: register a KV-cache entry)
  - POST /kdn/warmup (internal: pre-register fingerprint)
  - GET  /kdn/list   (internal: list all entries)
  - GET  /kdn/stats  (internal: statistics)
  - GET  /kdn/sessions  (internal: active sessions)
  - GET  /kdn/health (internal: health check)
  - GET  /kdn/fingerprint (internal: compute FNV-1a fingerprint)
  - DELETE /kdn/evict/{cache_id} (internal: evict an entry)
  - FNV-1a fingerprint consistency with AgentGateway
  - Session overlap detection
  - TTL expiry
"""

from __future__ import annotations

import sys
import time
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Add kdn/ to sys.path so we can import kdn_server directly.
# ---------------------------------------------------------------------------
_KDN_DIR = Path(__file__).resolve().parent.parent
if str(_KDN_DIR) not in sys.path:
    sys.path.insert(0, str(_KDN_DIR))

# We need a temporary kvcache dir before importing kdn_server.
_TMP_ROOT = Path(tempfile.mkdtemp())

# Patch module-level path constants before the module creates the directory.
import os
os.environ.setdefault("_KDN_TEST_TMPDIR", str(_TMP_ROOT))

# Monkeypatch the Path resolution so that kdn_server uses our tmpdir.
import unittest.mock as mock

_orig_path_init = Path.__init__

# Override KVCACHE_DIR to our temp dir by patching the constant just before import.
# Easiest: replace the project root that the server computes from __file__.
with mock.patch.object(
    Path,
    "resolve",
    autospec=True,
    side_effect=lambda self, **kw: (
        _TMP_ROOT / "kdn_server.py"
        if self == Path(__file__).parent.parent / "kdn_server.py"
        else self.absolute()
    ),
):
    pass  # We'll use a simpler approach below.

# Simpler approach: just import, then replace the global dicts.
import kdn_server as _kdn_orig
from fastapi.testclient import TestClient


def _make_client(tmpdir: Path) -> tuple:
    """Create fresh PersistentDicts in tmpdir, return (client, index, sessions)."""
    index = _kdn_orig.PersistentDict(tmpdir / "index.json")
    sessions_dict = _kdn_orig.PersistentDict(tmpdir / "sessions.json")
    return index, sessions_dict


@pytest.fixture(autouse=True)
def reset_state(tmp_path):
    """Replace module-global _index and _sessions with fresh, isolated instances."""
    old_index = _kdn_orig._index
    old_sessions = _kdn_orig._sessions
    old_kvcache = _kdn_orig.KVCACHE_DIR

    _kdn_orig._index = _kdn_orig.PersistentDict(tmp_path / "index.json")
    _kdn_orig._sessions = _kdn_orig.PersistentDict(tmp_path / "sessions.json")
    _kdn_orig.KVCACHE_DIR = tmp_path

    yield

    _kdn_orig._index = old_index
    _kdn_orig._sessions = old_sessions
    _kdn_orig.KVCACHE_DIR = old_kvcache


@pytest.fixture()
def client():
    return TestClient(_kdn_orig.app)


# ---------------------------------------------------------------------------
# FNV-1a fingerprint
# ---------------------------------------------------------------------------


class TestFnv1a:
    def test_known_value(self):
        """FNV-1a on empty bytes equals the offset basis."""
        assert _kdn_orig.fnv1a_64(b"") == 0xCBF29CE484222325

    def test_deterministic(self):
        assert _kdn_orig.fnv1a_64(b"hello") == _kdn_orig.fnv1a_64(b"hello")

    def test_different_inputs(self):
        assert _kdn_orig.fnv1a_64(b"abc") != _kdn_orig.fnv1a_64(b"abd")

    def test_compute_fingerprint_truncates_at_512(self):
        long_prompt = "x" * 1000
        short_prompt = "x" * 512
        assert (
            _kdn_orig.compute_fingerprint(long_prompt)
            == _kdn_orig.compute_fingerprint(short_prompt)
        )

    def test_compute_fingerprint_returns_int(self):
        fp = _kdn_orig.compute_fingerprint("hello world")
        assert isinstance(fp, int)
        assert 0 <= fp < 2**64

    def test_fingerprint_endpoint(self, client):
        resp = client.get("/kdn/fingerprint", params={"prompt": "hello"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["fingerprint"] == _kdn_orig.compute_fingerprint("hello")
        assert data["fingerprint_hex"].startswith("0x")


# ---------------------------------------------------------------------------
# /kdn/query — KDN Client Protocol
# ---------------------------------------------------------------------------


class TestKdnQuery:
    def test_miss_when_empty(self, client):
        resp = client.post(
            "/kdn/query",
            json={"fingerprint": 12345, "model": "Qwen/Qwen3-8B", "route_key": "test/route"},
        )
        assert resp.status_code == 200
        assert resp.json()["hit"] is False

    def test_hit_after_store(self, client):
        fp = _kdn_orig.compute_fingerprint("system: helpful\nuser: hello\n")

        store_resp = client.post(
            "/kdn/store",
            json={
                "fingerprint": fp,
                "model": "Qwen/Qwen3-8B",
                "route_key": "test/route",
                "ttft_saved_ms": 300,
            },
        )
        assert store_resp.status_code == 200
        cache_id = store_resp.json()["cache_id"]

        resp = client.post(
            "/kdn/query",
            json={"fingerprint": fp, "model": "Qwen/Qwen3-8B", "route_key": "test/route"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["hit"] is True
        assert data["cache_id"] == cache_id
        assert data["ttft_saved_ms"] == 300

    def test_miss_different_model(self, client):
        fp = _kdn_orig.compute_fingerprint("prompt text")
        client.post("/kdn/store", json={"fingerprint": fp, "model": "ModelA", "route_key": "r"})
        resp = client.post(
            "/kdn/query",
            json={"fingerprint": fp, "model": "ModelB", "route_key": "r"},
        )
        assert resp.json()["hit"] is False

    def test_miss_different_fingerprint(self, client):
        fp1 = _kdn_orig.compute_fingerprint("prompt A")
        fp2 = _kdn_orig.compute_fingerprint("prompt B")
        client.post("/kdn/store", json={"fingerprint": fp1, "model": "m", "route_key": "r"})
        resp = client.post(
            "/kdn/query",
            json={"fingerprint": fp2, "model": "m", "route_key": "r"},
        )
        assert resp.json()["hit"] is False

    def test_node_hint_returned_when_configured(self, client):
        fp = _kdn_orig.compute_fingerprint("p")
        client.post(
            "/kdn/store",
            json={
                "fingerprint": fp,
                "model": "m",
                "route_key": "r",
                "node_hint": "192.168.1.5:8080",
            },
        )
        resp = client.post(
            "/kdn/query",
            json={"fingerprint": fp, "model": "m", "route_key": "r"},
        )
        data = resp.json()
        assert data["hit"] is True
        assert data["node_hint"] == "192.168.1.5:8080"

    def test_ttl_expiry_returns_miss(self, client, tmp_path):
        fp = _kdn_orig.compute_fingerprint("expiring prompt")
        # Inject an entry directly with a past created_at and ttl_secs=1.
        key = _kdn_orig._idx_key(fp, "m")
        _kdn_orig._index.put(
            key,
            {
                "cache_id": "kv-expired",
                "fingerprint": fp,
                "model": "m",
                "route_key": "r",
                "node_hint": None,
                "ttft_saved_ms": 200,
                "lmcache_file": None,
                "ttl_secs": 1,
                "created_at": time.time() - 10,  # 10 seconds ago
                "status": "ready",
            },
        )
        resp = client.post(
            "/kdn/query",
            json={"fingerprint": fp, "model": "m", "route_key": "r"},
        )
        assert resp.json()["hit"] is False

    def test_session_fields_accepted(self, client):
        """Query with session fields should not fail even on a miss."""
        resp = client.post(
            "/kdn/query",
            json={
                "fingerprint": 99999,
                "model": "m",
                "route_key": "r",
                "session_id": "sess-abc",
                "session_turn_count": 3,
                "session_overlap": True,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["hit"] is False

    def test_query_via_prompt_prefix(self, client):
        """Store using prompt_prefix, query using the computed fingerprint."""
        prompt = "user: what is the weather today?\n"
        fp = _kdn_orig.compute_fingerprint(prompt)

        client.post(
            "/kdn/store",
            json={"prompt_prefix": prompt, "model": "m", "route_key": "r"},
        )
        resp = client.post(
            "/kdn/query",
            json={"fingerprint": fp, "model": "m", "route_key": "r"},
        )
        assert resp.json()["hit"] is True


# ---------------------------------------------------------------------------
# /kdn/store
# ---------------------------------------------------------------------------


class TestKdnStore:
    def test_store_returns_cache_id(self, client):
        resp = client.post(
            "/kdn/store",
            json={"fingerprint": 1, "model": "m", "route_key": "r"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["stored"] is True
        assert data["cache_id"].startswith("kv-")
        assert data["fingerprint"] == 1

    def test_store_missing_both_fingerprint_and_prefix(self, client):
        resp = client.post(
            "/kdn/store",
            json={"model": "m", "route_key": "r"},
        )
        assert resp.status_code == 400

    def test_store_deduplicates_by_fingerprint_model(self, client):
        body = {"fingerprint": 42, "model": "m", "route_key": "r"}
        r1 = client.post("/kdn/store", json=body)
        r2 = client.post("/kdn/store", json=body)
        assert r1.status_code == 200
        assert r2.status_code == 200
        entries = client.get("/kdn/list").json()
        assert len(entries) == 1


# ---------------------------------------------------------------------------
# /kdn/warmup
# ---------------------------------------------------------------------------


class TestKdnWarmup:
    def test_warmup_creates_pending_entry(self, client):
        resp = client.post(
            "/kdn/warmup",
            json={"fingerprint": 7, "model": "m", "route_key": "r"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"
        assert data["cache_id"].startswith("kv-pending-")

    def test_warmup_skips_if_already_ready(self, client):
        fp = 8
        client.post("/kdn/store", json={"fingerprint": fp, "model": "m", "route_key": "r"})
        resp = client.post(
            "/kdn/warmup",
            json={"fingerprint": fp, "model": "m", "route_key": "r"},
        )
        assert resp.json()["status"] == "already_ready"


# ---------------------------------------------------------------------------
# /kdn/list
# ---------------------------------------------------------------------------


class TestKdnList:
    def test_empty_list(self, client):
        assert client.get("/kdn/list").json() == []

    def test_list_after_store(self, client):
        client.post("/kdn/store", json={"fingerprint": 1, "model": "m", "route_key": "r"})
        client.post("/kdn/store", json={"fingerprint": 2, "model": "m", "route_key": "r"})
        entries = client.get("/kdn/list").json()
        assert len(entries) == 2


# ---------------------------------------------------------------------------
# /kdn/stats
# ---------------------------------------------------------------------------


class TestKdnStats:
    def test_stats_structure(self, client):
        resp = client.get("/kdn/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "cache_entries_total" in data
        assert "cache_entries_ready" in data
        assert "cache_entries_pending" in data
        assert "active_sessions" in data

    def test_stats_counts_correctly(self, client):
        client.post("/kdn/store", json={"fingerprint": 1, "model": "m", "route_key": "r"})
        client.post("/kdn/warmup", json={"fingerprint": 99, "model": "m", "route_key": "r"})
        data = client.get("/kdn/stats").json()
        assert data["cache_entries_total"] == 2
        assert data["cache_entries_ready"] == 1
        assert data["cache_entries_pending"] == 1


# ---------------------------------------------------------------------------
# /kdn/health
# ---------------------------------------------------------------------------


class TestKdnHealth:
    def test_health_ok(self, client):
        resp = client.get("/kdn/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# /kdn/evict
# ---------------------------------------------------------------------------


class TestKdnEvict:
    def test_evict_existing(self, client):
        r = client.post("/kdn/store", json={"fingerprint": 5, "model": "m", "route_key": "r"})
        cache_id = r.json()["cache_id"]
        resp = client.delete(f"/kdn/evict/{cache_id}")
        assert resp.status_code == 200
        assert resp.json()["evicted"] == cache_id
        assert client.get("/kdn/list").json() == []

    def test_evict_nonexistent(self, client):
        resp = client.delete("/kdn/evict/nonexistent-id")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /kdn/sessions — session overlap detection
# ---------------------------------------------------------------------------


class TestKdnSessions:
    def test_sessions_empty_initially(self, client):
        assert client.get("/kdn/sessions").json() == []

    def test_session_created_on_first_query(self, client):
        fp = _kdn_orig.compute_fingerprint("prompt")
        client.post(
            "/kdn/query",
            json={
                "fingerprint": fp,
                "model": "m",
                "route_key": "r",
                "session_id": "sess-1",
                "session_turn_count": 0,
            },
        )
        sessions = client.get("/kdn/sessions").json()
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "sess-1"

    def test_session_overlap_detected_on_second_query(self, client):
        fp = _kdn_orig.compute_fingerprint("repeated prompt")

        def query(turn):
            return client.post(
                "/kdn/query",
                json={
                    "fingerprint": fp,
                    "model": "m",
                    "route_key": "r",
                    "session_id": "sess-x",
                    "session_turn_count": turn,
                },
            )

        query(0)
        query(1)

        sessions = client.get("/kdn/sessions").json()
        sess = next(s for s in sessions if s["session_id"] == "sess-x")
        assert sess["turn_count"] == 2
        # fingerprint should appear only once (deduplicated)
        assert len(sess["seen_fingerprints"]) == 1
        assert fp in sess["seen_fingerprints"]

    def test_multiple_sessions_tracked_independently(self, client):
        fp = _kdn_orig.compute_fingerprint("shared prompt")
        for sid in ("alice", "bob"):
            client.post(
                "/kdn/query",
                json={"fingerprint": fp, "model": "m", "route_key": "r", "session_id": sid},
            )

        sessions = {s["session_id"]: s for s in client.get("/kdn/sessions").json()}
        assert "alice" in sessions
        assert "bob" in sessions

    def test_session_overlap_flag_from_session_update(self):
        """_session_update returns True when fingerprint is seen a second time."""
        fp = _kdn_orig.compute_fingerprint("same context")
        ov1 = _kdn_orig._session_update("s", fp, "r")
        assert ov1 is False  # first time: no overlap
        ov2 = _kdn_orig._session_update("s", fp, "r")
        assert ov2 is True  # second time: overlap

    def test_session_evict_expired(self):
        """Sessions with stale last_seen_at_secs are evicted."""
        # Insert a session directly with an old timestamp.
        old_ts = int(time.time()) - 7200  # 2 hours ago
        _kdn_orig._sessions.put(
            "sess:old-session",
            {
                "session_id": "old-session",
                "route_key": "r",
                "turn_count": 1,
                "seen_fingerprints": [],
                "consecutive_failures": 0,
                "created_at_secs": old_ts,
                "last_seen_at_secs": old_ts,
            },
        )
        # Default TTL is 1800 s; 7200 > 1800, so this session should be evicted.
        evicted = _kdn_orig._session_evict_expired()
        assert evicted >= 1
        # Confirm it's gone.
        assert _kdn_orig._sessions.get("sess:old-session") is None


# ---------------------------------------------------------------------------
# PersistentDict
# ---------------------------------------------------------------------------


class TestPersistentDict:
    def test_get_put_delete(self, tmp_path):
        d = _kdn_orig.PersistentDict(tmp_path / "test.json")
        d.put("k1", {"val": 1})
        assert d.get("k1") == {"val": 1}
        assert d.get("missing") is None
        assert d.delete("k1") is True
        assert d.get("k1") is None

    def test_delete_missing_returns_false(self, tmp_path):
        d = _kdn_orig.PersistentDict(tmp_path / "test.json")
        assert d.delete("no-such-key") is False

    def test_persistence_across_reload(self, tmp_path):
        path = tmp_path / "idx.json"
        d1 = _kdn_orig.PersistentDict(path)
        d1.put("k", {"x": 42})

        d2 = _kdn_orig.PersistentDict(path)
        assert d2.get("k") == {"x": 42}

    def test_find_key_by(self, tmp_path):
        d = _kdn_orig.PersistentDict(tmp_path / "t.json")
        d.put("k1", {"cache_id": "abc"})
        d.put("k2", {"cache_id": "xyz"})
        assert d.find_key_by("cache_id", "abc") == "k1"
        assert d.find_key_by("cache_id", "missing") is None

    def test_len(self, tmp_path):
        d = _kdn_orig.PersistentDict(tmp_path / "l.json")
        assert len(d) == 0
        d.put("a", {})
        d.put("b", {})
        assert len(d) == 2
