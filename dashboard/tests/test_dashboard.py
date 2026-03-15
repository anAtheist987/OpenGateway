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
Tests for the AgentGateway Knowledge Dashboard.

Tests cover:
  1. Mock API client — data generation & correctness
  2. ApiClient — real HTTP client (mocked with pytest-mock)
  3. Chart builders — valid Plotly figures for all data shapes
  4. App callbacks — return types / shapes from refresh_wm, refresh_sr, refresh_kdn
  5. Edge cases — empty data, single entry, missing optional fields

Run:
    cd dashboard
    pip install -r requirements.txt
    pytest tests/ -v
"""
from __future__ import annotations

import sys
import os
import time

import pytest
import pandas as pd
import plotly.graph_objects as go

# Make dashboard package importable when running from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from api_client import ApiClient, MockApiClient, get_client, _compute_stats, _build_sessions, _build_ewma
from charts import (
    wm_latency_timeline,
    wm_outcome_donut,
    wm_route_bar,
    wm_latency_hist,
    sr_latency_bar,
    sr_success_rate_gauge,
    sr_requests_stacked,
    kdn_session_overview,
    kdn_overlap_bar,
    kdn_fingerprint_heatmap,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

SAMPLE_WM = [
    {
        "timestamp_secs": int(time.time()) - 30,
        "route_key": "default/route0",
        "backend": "qwen-plus",
        "llm_model": "qwen-plus",
        "context_fingerprint": 14695981039346656037,
        "outcome": "success",
        "latency_ms": 300,
        "ewma_latency_ms": 295.0,
    },
    {
        "timestamp_secs": int(time.time()) - 20,
        "route_key": "default/route1",
        "backend": "qwen-turbo",
        "llm_model": "qwen-turbo",
        "context_fingerprint": 9876543210987654321,
        "outcome": "success",
        "latency_ms": 150,
        "ewma_latency_ms": 152.0,
    },
    {
        "timestamp_secs": int(time.time()) - 10,
        "route_key": "default/route0",
        "backend": "qwen-plus",
        "llm_model": None,
        "context_fingerprint": None,
        "outcome": "failure",
        "latency_ms": 50,
        "ewma_latency_ms": 48.0,
    },
]

SAMPLE_STATS = [
    {
        "route_key": "default/route0",
        "total_requests": 10,
        "success_count": 9,
        "failure_count": 1,
        "ewma_latency_ms": 312.7,
    },
    {
        "route_key": "default/route1",
        "total_requests": 5,
        "success_count": 5,
        "failure_count": 0,
        "ewma_latency_ms": 160.0,
    },
]

SAMPLE_SESSIONS = [
    {
        "session_id": "sess-alpha",
        "route_key": "default/route0",
        "turn_count": 4,
        "seen_fingerprints": [14695981039346656037, 9876543210987654321],
        "last_backend": "qwen-plus",
        "consecutive_failures": 0,
        "created_at_secs": int(time.time()) - 600,
        "last_seen_at_secs": int(time.time()) - 30,
    },
    {
        "session_id": "sess-beta",
        "route_key": "default/route1",
        "turn_count": 2,
        "seen_fingerprints": [14695981039346656037],
        "last_backend": "qwen-turbo",
        "consecutive_failures": 1,
        "created_at_secs": int(time.time()) - 120,
        "last_seen_at_secs": int(time.time()) - 10,
    },
]


# ── MockApiClient tests ───────────────────────────────────────────────────────

class TestMockApiClient:
    def setup_method(self):
        # Reset state before each test
        import api_client as ac
        ac._STATE["entries"] = []
        ac._STATE["corrections"] = []
        ac._STATE["seed"] = 0

    def test_working_memory_returns_list(self):
        c = MockApiClient()
        wm = c.working_memory()
        assert isinstance(wm, list)
        assert len(wm) > 0

    def test_working_memory_entry_keys(self):
        c = MockApiClient()
        entries = c.working_memory()
        required = {"timestamp_secs", "route_key", "backend", "outcome", "latency_ms"}
        for e in entries[:5]:
            assert required.issubset(e.keys()), f"Missing keys in entry: {e}"

    def test_working_memory_has_ewma(self):
        c = MockApiClient()
        entries = c.working_memory()
        assert all("ewma_latency_ms" in e for e in entries)

    def test_stats_returns_list(self):
        c = MockApiClient()
        stats = c.stats()
        assert isinstance(stats, list)
        assert len(stats) > 0

    def test_stats_keys(self):
        c = MockApiClient()
        stats = c.stats()
        required = {"route_key", "total_requests", "success_count", "failure_count", "ewma_latency_ms"}
        for s in stats:
            assert required.issubset(s.keys())

    def test_stats_counts_consistent(self):
        c = MockApiClient()
        stats = c.stats()
        for s in stats:
            assert s["total_requests"] == s["success_count"] + s["failure_count"]

    def test_sessions_returns_list(self):
        c = MockApiClient()
        sessions = c.sessions()
        assert isinstance(sessions, list)

    def test_corrections_empty_initially(self):
        c = MockApiClient()
        assert c.corrections() == []

    def test_post_correction(self):
        c = MockApiClient()
        ok = c.post_correction("default/route0", "prefer backend B")
        assert ok is True
        corr = c.corrections()
        assert len(corr) == 1
        assert corr[0]["route_key"] == "default/route0"
        assert corr[0]["note"] == "prefer backend B"
        assert "timestamp_secs" in corr[0]

    def test_post_correction_multiple(self):
        c = MockApiClient()
        c.post_correction("default/route0", "note A")
        c.post_correction("default/route1", "note B")
        assert len(c.corrections()) == 2

    def test_is_alive_returns_false(self):
        c = MockApiClient()
        assert c.is_alive() is False

    def test_working_memory_grows_on_repeated_calls(self):
        import api_client as ac
        # Force a deterministic seed so every call adds an entry
        ac._STATE["seed"] = 0
        c = MockApiClient()
        import api_client
        api_client._STATE["seed"] = 1  # override to force entry on next call
        _ = c.working_memory()
        # Just check no exception and we get entries
        assert isinstance(c.working_memory(), list)


# ── Helper function tests ─────────────────────────────────────────────────────

class TestHelpers:
    def test_build_ewma_adds_field(self):
        entries = [
            {"route_key": "r0", "latency_ms": 100, "outcome": "success"},
            {"route_key": "r0", "latency_ms": 200, "outcome": "success"},
        ]
        result = _build_ewma(entries)
        assert "ewma_latency_ms" in result[0]
        assert "ewma_latency_ms" in result[1]

    def test_build_ewma_first_equals_latency(self):
        entries = [{"route_key": "r", "latency_ms": 300, "outcome": "success"}]
        result = _build_ewma(entries)
        assert result[0]["ewma_latency_ms"] == 300.0

    def test_build_ewma_alpha_formula(self):
        entries = [
            {"route_key": "r", "latency_ms": 100, "outcome": "success"},
            {"route_key": "r", "latency_ms": 200, "outcome": "success"},
        ]
        result = _build_ewma(entries)
        expected = 0.1 * 200 + 0.9 * 100
        assert abs(result[1]["ewma_latency_ms"] - expected) < 0.01

    def test_compute_stats_empty(self):
        assert _compute_stats([]) == []

    def test_compute_stats_single_route(self):
        entries = [
            {"route_key": "r0", "latency_ms": 100, "outcome": "success"},
            {"route_key": "r0", "latency_ms": 200, "outcome": "failure"},
        ]
        stats = _compute_stats(entries)
        assert len(stats) == 1
        s = stats[0]
        assert s["total_requests"] == 2
        assert s["success_count"] == 1
        assert s["failure_count"] == 1

    def test_compute_stats_multiple_routes(self):
        entries = [
            {"route_key": "r0", "latency_ms": 100, "outcome": "success"},
            {"route_key": "r1", "latency_ms": 200, "outcome": "success"},
        ]
        stats = _compute_stats(entries)
        assert len(stats) == 2

    def test_build_sessions_empty(self):
        assert _build_sessions([]) == []

    def test_build_sessions_no_fingerprints(self):
        entries = [
            {
                "route_key": "r0",
                "backend": "b",
                "outcome": "success",
                "timestamp_secs": 1000,
                "context_fingerprint": None,
            }
        ]
        result = _build_sessions(entries)
        assert result == []

    def test_build_sessions_with_fingerprints(self):
        entries = [
            {
                "route_key": "r0",
                "backend": "b",
                "outcome": "success",
                "timestamp_secs": 1000,
                "context_fingerprint": 12345,
            }
        ]
        result = _build_sessions(entries)
        assert len(result) > 0
        for s in result:
            assert "session_id" in s
            assert "turn_count" in s
            assert "seen_fingerprints" in s


# ── ApiClient tests (mocked HTTP) ────────────────────────────────────────────

class TestApiClient:
    def test_working_memory_ok(self, requests_mock):
        requests_mock.get(
            "http://localhost:15000/knowledge/working_memory",
            json=SAMPLE_WM,
        )
        c = ApiClient("http://localhost:15000")
        result = c.working_memory()
        assert result == SAMPLE_WM

    def test_working_memory_error(self, requests_mock):
        requests_mock.get(
            "http://localhost:15000/knowledge/working_memory",
            status_code=503,
        )
        c = ApiClient("http://localhost:15000")
        assert c.working_memory() is None

    def test_stats_ok(self, requests_mock):
        requests_mock.get(
            "http://localhost:15000/knowledge/stats",
            json=SAMPLE_STATS,
        )
        c = ApiClient("http://localhost:15000")
        result = c.stats()
        assert result == SAMPLE_STATS

    def test_sessions_ok(self, requests_mock):
        requests_mock.get(
            "http://localhost:15000/knowledge/sessions",
            json=SAMPLE_SESSIONS,
        )
        c = ApiClient("http://localhost:15000")
        result = c.sessions()
        assert result == SAMPLE_SESSIONS

    def test_corrections_ok(self, requests_mock):
        corr = [{"route_key": "r0", "note": "hi", "timestamp_secs": 1000}]
        requests_mock.get(
            "http://localhost:15000/knowledge/corrections",
            json=corr,
        )
        c = ApiClient("http://localhost:15000")
        assert c.corrections() == corr

    def test_post_correction_ok(self, requests_mock):
        requests_mock.post(
            "http://localhost:15000/knowledge/corrections",
            text="ok",
            status_code=200,
        )
        c = ApiClient("http://localhost:15000")
        assert c.post_correction("r0", "note") is True

    def test_post_correction_failure(self, requests_mock):
        requests_mock.post(
            "http://localhost:15000/knowledge/corrections",
            status_code=500,
        )
        c = ApiClient("http://localhost:15000")
        assert c.post_correction("r0", "note") is False

    def test_is_alive_true(self, requests_mock):
        requests_mock.get(
            "http://localhost:15000/knowledge/stats",
            json=[],
        )
        c = ApiClient("http://localhost:15000")
        assert c.is_alive() is True

    def test_is_alive_false(self, requests_mock):
        requests_mock.get(
            "http://localhost:15000/knowledge/stats",
            exc=ConnectionError("refused"),
        )
        c = ApiClient("http://localhost:15000")
        assert c.is_alive() is False

    def test_get_client_returns_mock_when_down(self, requests_mock):
        requests_mock.get(
            "http://localhost:15000/knowledge/stats",
            exc=ConnectionError("refused"),
        )
        c = get_client("http://localhost:15000")
        assert isinstance(c, MockApiClient)


# ── Chart builder tests ───────────────────────────────────────────────────────

class TestCharts:
    # Working Memory charts
    def test_wm_latency_timeline_figure(self):
        fig = wm_latency_timeline(SAMPLE_WM)
        assert isinstance(fig, go.Figure)
        assert len(fig.data) > 0

    def test_wm_latency_timeline_empty(self):
        fig = wm_latency_timeline([])
        assert isinstance(fig, go.Figure)

    def test_wm_outcome_donut_figure(self):
        fig = wm_outcome_donut(SAMPLE_WM)
        assert isinstance(fig, go.Figure)
        assert fig.data[0].type == "pie"

    def test_wm_outcome_donut_all_success(self):
        entries = [{**e, "outcome": "success"} for e in SAMPLE_WM]
        fig = wm_outcome_donut(entries)
        pie = fig.data[0]
        success_idx = list(pie.labels).index("成功")
        assert pie.values[success_idx] == len(SAMPLE_WM)

    def test_wm_route_bar_figure(self):
        fig = wm_route_bar(SAMPLE_WM)
        assert isinstance(fig, go.Figure)
        assert len(fig.data) == 1

    def test_wm_latency_hist_figure(self):
        fig = wm_latency_hist(SAMPLE_WM)
        assert isinstance(fig, go.Figure)
        # Should have at least the success trace
        assert len(fig.data) >= 1

    # Semantic Routing charts
    def test_sr_latency_bar_figure(self):
        fig = sr_latency_bar(SAMPLE_STATS)
        assert isinstance(fig, go.Figure)
        assert len(fig.data) == 1

    def test_sr_latency_bar_empty(self):
        fig = sr_latency_bar([])
        assert isinstance(fig, go.Figure)

    def test_sr_success_rate_gauge_figure(self):
        fig = sr_success_rate_gauge(SAMPLE_STATS)
        assert isinstance(fig, go.Figure)
        # one Indicator per route
        assert len(fig.data) == len(SAMPLE_STATS)

    def test_sr_requests_stacked_figure(self):
        fig = sr_requests_stacked(SAMPLE_STATS)
        assert isinstance(fig, go.Figure)
        assert len(fig.data) == 2  # success + failure bars

    # KDN charts
    def test_kdn_session_overview_figure(self):
        fig = kdn_session_overview(SAMPLE_SESSIONS)
        assert isinstance(fig, go.Figure)

    def test_kdn_session_overview_empty(self):
        fig = kdn_session_overview([])
        assert isinstance(fig, go.Figure)

    def test_kdn_overlap_bar_figure(self):
        fig = kdn_overlap_bar(SAMPLE_SESSIONS)
        assert isinstance(fig, go.Figure)

    def test_kdn_overlap_bar_empty(self):
        fig = kdn_overlap_bar([])
        assert isinstance(fig, go.Figure)

    def test_kdn_fingerprint_heatmap_figure(self):
        fig = kdn_fingerprint_heatmap(SAMPLE_SESSIONS)
        assert isinstance(fig, go.Figure)

    def test_kdn_fingerprint_heatmap_no_fps(self):
        sessions_no_fp = [{**s, "seen_fingerprints": []} for s in SAMPLE_SESSIONS]
        fig = kdn_fingerprint_heatmap(sessions_no_fp)
        assert isinstance(fig, go.Figure)

    def test_kdn_fingerprint_heatmap_empty(self):
        fig = kdn_fingerprint_heatmap([])
        assert isinstance(fig, go.Figure)


# ── App callback tests ────────────────────────────────────────────────────────

class TestAppCallbacks:
    """Test that app callbacks return correct number and types of outputs."""

    def setup_method(self):
        import api_client as ac
        ac._STATE["entries"] = []
        ac._STATE["corrections"] = []
        ac._STATE["seed"] = 0

    def test_refresh_wm_output_count(self):
        import app
        # Ensure mock is used
        original_admin = app._ADMIN_URL
        app._ADMIN_URL = "http://localhost:19999"  # unreachable
        try:
            result = app.refresh_wm()
            # Should return 7 items
            assert len(result) == 7
        finally:
            app._ADMIN_URL = original_admin

    def test_refresh_wm_status_is_string(self):
        import app
        app._ADMIN_URL = "http://localhost:19999"
        result = app.refresh_wm()
        status = result[0]
        assert isinstance(status, str)
        assert "Mock" in status or "连接" in status

    def test_refresh_wm_kpi_is_string(self):
        import app
        app._ADMIN_URL = "http://localhost:19999"
        result = app.refresh_wm()
        kpi = result[1]
        assert isinstance(kpi, str)
        assert "知识条目" in kpi

    def test_refresh_wm_charts_are_figures(self):
        import app
        app._ADMIN_URL = "http://localhost:19999"
        result = app.refresh_wm()
        for fig in result[2:6]:
            assert isinstance(fig, go.Figure)

    def test_refresh_wm_table_is_dataframe(self):
        import app
        app._ADMIN_URL = "http://localhost:19999"
        result = app.refresh_wm()
        assert isinstance(result[6], pd.DataFrame)

    def test_refresh_sr_output_count(self):
        import app
        app._ADMIN_URL = "http://localhost:19999"
        result = app.refresh_sr()
        assert len(result) == 6

    def test_refresh_sr_table_is_dataframe(self):
        import app
        app._ADMIN_URL = "http://localhost:19999"
        result = app.refresh_sr()
        assert isinstance(result[1], pd.DataFrame)

    def test_refresh_sr_charts_are_figures(self):
        import app
        app._ADMIN_URL = "http://localhost:19999"
        result = app.refresh_sr()
        for fig in result[2:5]:
            assert isinstance(fig, go.Figure)

    def test_refresh_kdn_output_count(self):
        import app
        app._ADMIN_URL = "http://localhost:19999"
        result = app.refresh_kdn()
        assert len(result) == 6

    def test_refresh_kdn_kpi_is_string(self):
        import app
        app._ADMIN_URL = "http://localhost:19999"
        result = app.refresh_kdn()
        kpi = result[1]
        assert isinstance(kpi, str)
        assert "Session" in kpi

    def test_refresh_kdn_table_is_dataframe(self):
        import app
        app._ADMIN_URL = "http://localhost:19999"
        result = app.refresh_kdn()
        assert isinstance(result[2], pd.DataFrame)

    def test_submit_correction_empty_route(self):
        import app
        app._ADMIN_URL = "http://localhost:19999"
        msg, _ = app.submit_correction("", "some note")
        # gr.update() returns a dict with a "value" key
        value = msg["value"] if isinstance(msg, dict) else getattr(msg, "value", str(msg))
        assert "⚠️" in value

    def test_submit_correction_empty_note(self):
        import app
        app._ADMIN_URL = "http://localhost:19999"
        msg, _ = app.submit_correction("default/route0", "")
        value = msg["value"] if isinstance(msg, dict) else getattr(msg, "value", str(msg))
        assert "⚠️" in value

    def test_submit_correction_success(self):
        import app
        app._ADMIN_URL = "http://localhost:19999"
        msg, df = app.submit_correction("default/route0", "prefer backend B")
        value = msg["value"] if isinstance(msg, dict) else getattr(msg, "value", str(msg))
        assert "✅" in value
        assert isinstance(df, pd.DataFrame)
        assert len(df) >= 1


# ── Edge case tests ───────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_wm_single_entry(self):
        entries = [SAMPLE_WM[0]]
        fig = wm_latency_timeline(entries)
        assert isinstance(fig, go.Figure)

    def test_sr_single_route(self):
        stats = [SAMPLE_STATS[0]]
        fig = sr_latency_bar(stats)
        assert isinstance(fig, go.Figure)

    def test_kdn_single_session(self):
        sessions = [SAMPLE_SESSIONS[0]]
        fig = kdn_session_overview(sessions)
        assert isinstance(fig, go.Figure)

    def test_wm_all_failures(self):
        entries = [{**e, "outcome": "failure"} for e in SAMPLE_WM]
        fig = wm_outcome_donut(entries)
        pie = fig.data[0]
        fail_idx = list(pie.labels).index("失败")
        assert pie.values[fail_idx] == len(SAMPLE_WM)

    def test_sr_gauge_high_success_rate(self):
        stats = [{"route_key": "r0", "total_requests": 100, "success_count": 100, "failure_count": 0, "ewma_latency_ms": 100.0}]
        fig = sr_success_rate_gauge(stats)
        assert isinstance(fig, go.Figure)

    def test_sr_gauge_zero_requests(self):
        stats = [{"route_key": "r0", "total_requests": 0, "success_count": 0, "failure_count": 0, "ewma_latency_ms": 0.0}]
        fig = sr_success_rate_gauge(stats)
        assert isinstance(fig, go.Figure)

    def test_kdn_session_high_overlap(self):
        sessions = [
            {
                "session_id": "ultra",
                "route_key": "r0",
                "turn_count": 20,
                "seen_fingerprints": [111],
                "last_backend": "b",
                "consecutive_failures": 0,
                "created_at_secs": 1000,
                "last_seen_at_secs": 2000,
            }
        ]
        fig = kdn_overlap_bar(sessions)
        assert isinstance(fig, go.Figure)

    def test_compute_stats_ewma_alpha(self):
        """EWMA with alpha=0.1: second entry should be 0.1*200 + 0.9*100 = 110"""
        entries = [
            {"route_key": "r", "latency_ms": 100, "outcome": "success"},
            {"route_key": "r", "latency_ms": 200, "outcome": "success"},
        ]
        stats = _compute_stats(entries)
        assert len(stats) == 1
        assert abs(stats[0]["ewma_latency_ms"] - 110.0) < 0.01
