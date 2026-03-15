// Copyright 2026 Tsinghua University
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// This file was created by Tsinghua University and is not part of
// the original agentgateway project by Solo.io.

/// KDN (Knowledge Delivery Network) client.
///
/// Protocol (HTTP/JSON):
///   POST /kdn/query
///   {
///     "fingerprint":        <u64>,
///     "model":              "<str>",
///     "route_key":          "<str>",
///     "session_id":         "<str>",   // optional — omitted for stateless requests
///     "session_turn_count": <u32>,     // optional — omitted when no session
///     "session_overlap":    <bool>     // optional — omitted when false
///   }
///
///   200 → { "hit": true,  "cache_id": "<str>", "ttft_saved_ms": <u64>, "node_hint": "<addr>" }
///   200 → { "hit": false }
///   non-200 → treated as miss
use std::sync::Arc;
use std::time::Duration;

use agent_core::strng::Strng;
use http_body_util::{BodyExt, Full};
use hyper::body::Bytes;
use hyper_util::client::legacy::Client as HyperClient;
use hyper_util::client::legacy::connect::HttpConnector;
use hyper_util::rt::TokioExecutor;
use serde::{Deserialize, Serialize};
use tracing::debug;

// ── request / response types ──────────────────────────────────────────────────

#[derive(Debug, Serialize)]
pub struct KdnQueryRequest {
	pub fingerprint: u64,
	pub model: String,
	pub route_key: String,
	/// Session identifier for multi-turn tracking (omitted for stateless requests).
	#[serde(skip_serializing_if = "Option::is_none")]
	pub session_id: Option<String>,
	/// Number of turns completed in this session before the current request.
	#[serde(skip_serializing_if = "Option::is_none")]
	pub session_turn_count: Option<u32>,
	/// `true` when this exact fingerprint was already seen earlier in the same
	/// session — the strongest signal for KDN cache reuse.
	#[serde(skip_serializing_if = "Option::is_none")]
	pub session_overlap: Option<bool>,
}

#[derive(Debug, Deserialize, PartialEq)]
pub struct KdnQueryResponse {
	pub hit: bool,
	#[serde(default)]
	pub cache_id: Option<String>,
	/// Estimated time-to-first-token savings from using the cached KV state.
	#[serde(default)]
	pub ttft_saved_ms: Option<u64>,
	/// Address of the inference node holding the cached KV state
	/// (`host:port`).  When present, AgentGateway SHOULD route the upstream
	/// request to this node instead of load-balancing normally.
	#[serde(default)]
	pub node_hint: Option<String>,
}

// ── client ────────────────────────────────────────────────────────────────────

#[derive(Clone, Debug)]
pub struct KdnClient {
	inner: Arc<Inner>,
}

#[derive(Debug)]
struct Inner {
	base_url: String,
	http: HyperClient<HttpConnector, Full<Bytes>>,
}

impl KdnClient {
	pub fn new(base_url: impl Into<String>) -> Self {
		let http = HyperClient::builder(TokioExecutor::new()).build_http();
		KdnClient {
			inner: Arc::new(Inner {
				base_url: base_url.into(),
				http,
			}),
		}
	}

	/// Query the KDN for a cached inference state matching `fingerprint`.
	/// Returns `None` on any error or miss.
	pub async fn query(
		&self,
		fingerprint: u64,
		model: &Strng,
		route_key: &Strng,
		session_id: Option<&str>,
		session_turn_count: Option<u32>,
		session_overlap: Option<bool>,
	) -> Option<KdnQueryResponse> {
		let body_bytes = serde_json::to_vec(&KdnQueryRequest {
			fingerprint,
			model: model.to_string(),
			route_key: route_key.to_string(),
			session_id: session_id.map(ToOwned::to_owned),
			session_turn_count,
			session_overlap,
		})
		.ok()?;

		let url = format!("{}/kdn/query", self.inner.base_url);
		let req = hyper::Request::builder()
			.method(hyper::Method::POST)
			.uri(&url)
			.header(hyper::header::CONTENT_TYPE, "application/json")
			.body(Full::new(Bytes::from(body_bytes)))
			.ok()?;

		let resp = tokio::time::timeout(Duration::from_millis(200), self.inner.http.request(req))
			.await
			.map_err(|_| debug!("KDN query timeout"))
			.ok()?
			.map_err(|e| debug!("KDN request error: {e}"))
			.ok()?;

		if !resp.status().is_success() {
			debug!("KDN returned non-2xx: {}", resp.status());
			return None;
		}

		let body = resp
			.into_body()
			.collect()
			.await
			.map_err(|e| debug!("KDN body error: {e}"))
			.ok()?
			.to_bytes();

		serde_json::from_slice::<KdnQueryResponse>(&body)
			.map_err(|e| debug!("KDN parse error: {e}"))
			.ok()
	}
}

// ── tests ─────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
	use super::*;
	use agent_core::strng;
	use wiremock::matchers::{body_json, method, path};
	use wiremock::{Mock, MockServer, ResponseTemplate};

	#[tokio::test]
	async fn cache_hit() {
		let server = MockServer::start().await;
		Mock::given(method("POST"))
			.and(path("/kdn/query"))
			.and(body_json(serde_json::json!({
					"fingerprint": 42u64,
					"model": "gpt-4",
					"route_key": "r1"
			})))
			.respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
					"hit": true,
					"cache_id": "abc123",
					"ttft_saved_ms": 150,
					"node_hint": "192.168.1.5:8080"
			})))
			.mount(&server)
			.await;

		let client = KdnClient::new(server.uri());
		let resp = client
			.query(
				42,
				&strng::literal!("gpt-4"),
				&strng::literal!("r1"),
				None,
				None,
				None,
			)
			.await
			.unwrap();

		assert!(resp.hit);
		assert_eq!(resp.cache_id.as_deref(), Some("abc123"));
		assert_eq!(resp.ttft_saved_ms, Some(150));
		assert_eq!(resp.node_hint.as_deref(), Some("192.168.1.5:8080"));
	}

	#[tokio::test]
	async fn cache_hit_with_session() {
		let server = MockServer::start().await;
		Mock::given(method("POST"))
			.and(path("/kdn/query"))
			.and(body_json(serde_json::json!({
					"fingerprint": 77u64,
					"model": "claude-3",
					"route_key": "r2",
					"session_id": "sess-abc",
					"session_turn_count": 3u32,
					"session_overlap": true
			})))
			.respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
					"hit": true,
					"cache_id": "kv-xyz",
					"ttft_saved_ms": 300
			})))
			.mount(&server)
			.await;

		let client = KdnClient::new(server.uri());
		let resp = client
			.query(
				77,
				&strng::literal!("claude-3"),
				&strng::literal!("r2"),
				Some("sess-abc"),
				Some(3),
				Some(true),
			)
			.await
			.unwrap();

		assert!(resp.hit);
		assert_eq!(resp.cache_id.as_deref(), Some("kv-xyz"));
		assert!(resp.node_hint.is_none());
	}

	#[tokio::test]
	async fn cache_miss() {
		let server = MockServer::start().await;
		Mock::given(method("POST"))
			.and(path("/kdn/query"))
			.respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
					"hit": false
			})))
			.mount(&server)
			.await;

		let client = KdnClient::new(server.uri());
		let resp = client
			.query(
				99,
				&strng::literal!("gpt-4"),
				&strng::literal!("r1"),
				None,
				None,
				None,
			)
			.await
			.unwrap();

		assert!(!resp.hit);
		assert!(resp.cache_id.is_none());
	}

	#[tokio::test]
	async fn server_error_returns_none() {
		let server = MockServer::start().await;
		Mock::given(method("POST"))
			.and(path("/kdn/query"))
			.respond_with(ResponseTemplate::new(500))
			.mount(&server)
			.await;

		let client = KdnClient::new(server.uri());
		let resp = client
			.query(
				1,
				&strng::literal!("m"),
				&strng::literal!("r"),
				None,
				None,
				None,
			)
			.await;
		assert!(resp.is_none());
	}

	#[tokio::test]
	async fn unreachable_server_returns_none() {
		let client = KdnClient::new("http://127.0.0.1:1");
		let resp = client
			.query(
				1,
				&strng::literal!("m"),
				&strng::literal!("r"),
				None,
				None,
				None,
			)
			.await;
		assert!(resp.is_none());
	}
}
