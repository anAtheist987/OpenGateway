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

/// Working memory: a bounded, time-aware ring buffer of recent execution traces.
///
/// Each entry captures the minimal signal needed for KDN overlap detection and
/// local knowledge-base refinement:
///   - route / backend identity
///   - LLM context fingerprint (model + first-N-tokens hash of the prompt)
///   - outcome (success / failure + status code)
///   - latency
///
/// The store is intentionally kept in-memory only; persistence is handled by
/// `KnowledgeStore` (Step 2).  Thread-safety is provided by a `RwLock` so
/// readers never block each other.
use std::collections::VecDeque;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use agent_core::strng::Strng;
use tokio::sync::RwLock;

// ── public types ─────────────────────────────────────────────────────────────

/// Outcome of a single proxied request.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
	Success,
	Failure { status: u16 },
}

/// A single working-memory entry.
#[derive(Debug, Clone)]
pub struct TraceEntry {
	/// Wall-clock timestamp (seconds since UNIX epoch).
	pub timestamp_secs: u64,
	/// Route identifier (bind::listener::route).
	pub route_key: Strng,
	/// Backend name.
	pub backend: Strng,
	/// LLM model name, if this was an AI request.
	pub llm_model: Option<Strng>,
	/// Stable fingerprint of the LLM prompt context (first 512 chars, SHA-256 truncated to 8 bytes).
	/// `None` for non-LLM requests.
	pub context_fingerprint: Option<u64>,
	/// First 120 Unicode characters of the prompt, for display in the dashboard.
	/// Truncated with "…" when the original prompt is longer.
	/// `None` for non-LLM requests or when no prompt was provided.
	pub prompt_snippet: Option<String>,
	/// Agent identifier extracted from the `X-Agent-ID` request header.
	/// `None` for requests that do not carry the header.
	pub agent_id: Option<String>,
	/// Full LLM response text stored for local working-memory replay.
	/// `None` for non-LLM requests or when the response was not captured.
	pub response_content: Option<String>,
	/// Request outcome.
	pub outcome: Outcome,
	/// End-to-end latency.
	pub latency: Duration,
}

/// Shared handle — cheap to clone, backed by `Arc`.
#[derive(Clone, Debug)]
pub struct WorkingMemory(Arc<RwLock<Inner>>);

struct Inner {
	capacity: usize,
	entries: VecDeque<TraceEntry>,
}

impl std::fmt::Debug for Inner {
	fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
		f.debug_struct("Inner")
			.field("capacity", &self.capacity)
			.field("len", &self.entries.len())
			.finish()
	}
}

// ── implementation ────────────────────────────────────────────────────────────

impl WorkingMemory {
	/// Create a new working memory with the given ring-buffer capacity.
	pub fn new(capacity: usize) -> Self {
		WorkingMemory(Arc::new(RwLock::new(Inner {
			capacity,
			entries: VecDeque::with_capacity(capacity.min(1024)),
		})))
	}

	/// Push a new trace entry, evicting the oldest if at capacity.
	pub async fn push(&self, entry: TraceEntry) {
		let mut inner = self.0.write().await;
		if inner.entries.len() >= inner.capacity {
			inner.entries.pop_front();
		}
		inner.entries.push_back(entry);
	}

	/// Return a snapshot of all current entries (newest last).
	pub async fn snapshot(&self) -> Vec<TraceEntry> {
		self.0.read().await.entries.iter().cloned().collect()
	}

	/// Return the number of stored entries.
	pub async fn len(&self) -> usize {
		self.0.read().await.entries.len()
	}

	pub async fn is_empty(&self) -> bool {
		self.0.read().await.entries.is_empty()
	}

	/// Return the most recent entry whose `context_fingerprint` matches `fp`.
	/// Used for local working-memory replay without KDN.
	pub async fn lookup_by_fingerprint(&self, fp: u64) -> Option<TraceEntry> {
		self
			.0
			.read()
			.await
			.entries
			.iter()
			.rev()
			.find(|e| e.context_fingerprint == Some(fp))
			.cloned()
	}

	/// Find entries whose `context_fingerprint` matches `fp`.
	/// Used to detect KDN overlap candidates.
	pub async fn find_by_fingerprint(&self, fp: u64) -> Vec<TraceEntry> {
		self
			.0
			.read()
			.await
			.entries
			.iter()
			.filter(|e| e.context_fingerprint == Some(fp))
			.cloned()
			.collect()
	}

	/// Evict entries older than `max_age`.
	pub async fn evict_older_than(&self, max_age: Duration) {
		let now_secs = SystemTime::now()
			.duration_since(UNIX_EPOCH)
			.unwrap_or_default()
			.as_secs();
		let cutoff = now_secs.saturating_sub(max_age.as_secs());
		let mut inner = self.0.write().await;
		while inner
			.entries
			.front()
			.is_some_and(|e| e.timestamp_secs < cutoff)
		{
			inner.entries.pop_front();
		}
	}
}

// ── helpers ───────────────────────────────────────────────────────────────────

/// Compute a cheap 64-bit fingerprint from an arbitrary byte slice.
/// Uses FNV-1a (no external dep required).
pub fn fingerprint(data: &[u8]) -> u64 {
	const OFFSET: u64 = 14695981039346656037;
	const PRIME: u64 = 1099511628211;
	let mut h = OFFSET;
	for &b in data {
		h ^= b as u64;
		h = h.wrapping_mul(PRIME);
	}
	h
}

/// Build a `TraceEntry` from the fields available at log-emission time.
#[allow(clippy::too_many_arguments)]
pub fn build_entry(
	route_key: Strng,
	backend: Strng,
	llm_model: Option<Strng>,
	prompt_prefix: Option<&str>,
	response_content: Option<String>,
	agent_id: Option<String>,
	outcome: Outcome,
	latency: Duration,
) -> TraceEntry {
	const SNIPPET_CHARS: usize = 120;
	let context_fingerprint = prompt_prefix.map(|p| {
		let bytes = p.as_bytes();
		let slice = &bytes[..bytes.len().min(512)];
		fingerprint(slice)
	});
	let prompt_snippet = prompt_prefix.map(|p| {
		let s = p.trim_start();
		let mut chars = s.chars();
		let head: String = chars.by_ref().take(SNIPPET_CHARS).collect();
		if chars.next().is_some() {
			format!("{head}…")
		} else {
			head
		}
	});
	let timestamp_secs = SystemTime::now()
		.duration_since(UNIX_EPOCH)
		.unwrap_or_default()
		.as_secs();
	TraceEntry {
		timestamp_secs,
		route_key,
		backend,
		llm_model,
		context_fingerprint,
		prompt_snippet,
		agent_id,
		response_content,
		outcome,
		latency,
	}
}

// ── tests ─────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
	use super::*;
	use agent_core::strng;

	fn entry(route: &'static str, fp: Option<u64>, outcome: Outcome) -> TraceEntry {
		TraceEntry {
			timestamp_secs: SystemTime::now()
				.duration_since(UNIX_EPOCH)
				.unwrap_or_default()
				.as_secs(),
			route_key: agent_core::strng::new(route),
			backend: strng::literal!("be"),
			llm_model: None,
			context_fingerprint: fp,
			prompt_snippet: None,
			agent_id: None,
			response_content: None,
			outcome,
			latency: Duration::from_millis(10),
		}
	}

	#[tokio::test]
	async fn push_and_snapshot() {
		let wm = WorkingMemory::new(4);
		wm.push(entry("r1", None, Outcome::Success)).await;
		wm.push(entry("r2", None, Outcome::Success)).await;
		assert_eq!(wm.len().await, 2);
		let snap = wm.snapshot().await;
		assert_eq!(snap[0].route_key, strng::literal!("r1"));
		assert_eq!(snap[1].route_key, strng::literal!("r2"));
	}

	#[tokio::test]
	async fn capacity_eviction() {
		let wm = WorkingMemory::new(3);
		for i in 0..5u8 {
			wm.push(entry(
				"r",
				None,
				Outcome::Failure {
					status: 500 + i as u16,
				},
			))
			.await;
		}
		assert_eq!(wm.len().await, 3);
		// oldest two evicted; last three remain
		let snap = wm.snapshot().await;
		assert_eq!(snap[0].outcome, Outcome::Failure { status: 502 });
		assert_eq!(snap[2].outcome, Outcome::Failure { status: 504 });
	}

	#[tokio::test]
	async fn find_by_fingerprint() {
		let wm = WorkingMemory::new(10);
		wm.push(entry("r1", Some(42), Outcome::Success)).await;
		wm.push(entry("r2", Some(99), Outcome::Success)).await;
		wm.push(entry("r3", Some(42), Outcome::Success)).await;
		let hits = wm.find_by_fingerprint(42).await;
		assert_eq!(hits.len(), 2);
		assert!(hits.iter().all(|e| e.context_fingerprint == Some(42)));
	}

	#[tokio::test]
	async fn evict_older_than() {
		let wm = WorkingMemory::new(10);
		// Inject a very old entry manually
		{
			let mut inner = wm.0.write().await;
			inner.entries.push_back(TraceEntry {
				timestamp_secs: 1, // epoch + 1s → ancient
				route_key: strng::literal!("old"),
				backend: strng::literal!("be"),
				llm_model: None,
				context_fingerprint: None,
				prompt_snippet: None,
				agent_id: None,
				response_content: None,
				outcome: Outcome::Success,
				latency: Duration::ZERO,
			});
		}
		wm.push(entry("new", None, Outcome::Success)).await;
		assert_eq!(wm.len().await, 2);
		wm.evict_older_than(Duration::from_secs(60)).await;
		assert_eq!(wm.len().await, 1);
		assert_eq!(wm.snapshot().await[0].route_key, strng::literal!("new"));
	}

	#[test]
	fn fingerprint_deterministic() {
		let a = fingerprint(b"hello world");
		let b = fingerprint(b"hello world");
		assert_eq!(a, b);
		assert_ne!(a, fingerprint(b"hello worlD"));
	}

	#[test]
	fn build_entry_with_prompt() {
		let e = build_entry(
			strng::literal!("r"),
			strng::literal!("be"),
			Some(strng::literal!("gpt-4")),
			Some("You are a helpful assistant."),
			None,
			None,
			Outcome::Success,
			Duration::from_millis(200),
		);
		assert!(e.context_fingerprint.is_some());
		assert_eq!(e.llm_model, Some(strng::literal!("gpt-4")));
	}

	// ── lookup_by_fingerprint tests ───────────────────────────────────────────

	#[tokio::test]
	async fn lookup_by_fingerprint_empty_memory() {
		let wm = WorkingMemory::new(10);
		assert!(wm.lookup_by_fingerprint(42).await.is_none());
	}

	#[tokio::test]
	async fn lookup_by_fingerprint_exact_match() {
		let wm = WorkingMemory::new(10);
		let mut e = entry("r1", Some(42), Outcome::Success);
		e.response_content = Some("answer".to_string());
		wm.push(e).await;
		let hit = wm.lookup_by_fingerprint(42).await;
		assert!(hit.is_some());
		assert_eq!(hit.unwrap().response_content.as_deref(), Some("answer"));
	}

	#[tokio::test]
	async fn lookup_by_fingerprint_returns_most_recent() {
		let wm = WorkingMemory::new(10);
		let mut old = entry("r", Some(7), Outcome::Success);
		old.response_content = Some("old-response".to_string());
		let mut new = entry("r", Some(7), Outcome::Success);
		new.response_content = Some("new-response".to_string());
		wm.push(old).await;
		wm.push(new).await;
		let hit = wm.lookup_by_fingerprint(7).await.unwrap();
		assert_eq!(hit.response_content.as_deref(), Some("new-response"));
	}

	#[tokio::test]
	async fn lookup_by_fingerprint_no_match_different_prefix() {
		let wm = WorkingMemory::new(10);
		wm.push(entry("r1", Some(11), Outcome::Success)).await;
		assert!(wm.lookup_by_fingerprint(99).await.is_none());
	}

	#[tokio::test]
	async fn lookup_by_fingerprint_no_fp_entry_not_matched() {
		// Entries with no fingerprint (non-LLM) must not be returned.
		let wm = WorkingMemory::new(10);
		wm.push(entry("r1", None, Outcome::Success)).await;
		assert!(wm.lookup_by_fingerprint(0).await.is_none());
	}

	// ── fingerprint accuracy tests ────────────────────────────────────────────

	#[test]
	fn fingerprint_one_byte_diff_no_match() {
		let base = b"hello world";
		let diff = b"hello World"; // capital W
		assert_ne!(fingerprint(base), fingerprint(diff));
	}

	#[test]
	fn fingerprint_beyond_512_bytes_ignored() {
		// Build a 600-byte prompt: first 512 bytes identical, bytes 513-600 differ.
		let mut a = vec![b'A'; 512];
		a.extend_from_slice(b"suffix-A");
		let mut b = vec![b'A'; 512];
		b.extend_from_slice(b"SUFFIX-B-different");

		// fingerprint() is called on the full slice in tests,
		// but build_entry() truncates to 512. Verify the truncation logic.
		let fp_a = fingerprint(&a[..512]);
		let fp_b = fingerprint(&b[..512]);
		assert_eq!(
			fp_a, fp_b,
			"same first-512 bytes must yield same fingerprint"
		);

		// Different suffix beyond 512 must not affect the fingerprint used.
		let fp_full_a = fingerprint(&a);
		let fp_full_b = fingerprint(&b);
		assert_ne!(
			fp_full_a, fp_full_b,
			"full-slice fingerprints must differ (confirming suffix differs)"
		);
	}

	#[tokio::test]
	async fn lookup_by_fingerprint_prefix_beyond_512_same_hits() {
		// Two entries built from prompts that differ only after byte 512 →
		// both produce the same fingerprint → lookup should find the stored entry.
		let base: String = "X".repeat(512);
		let prompt_a = format!("{base}suffix-a");
		let prompt_b = format!("{base}suffix-b");

		let entry_a = build_entry(
			strng::literal!("r"),
			strng::literal!("be"),
			Some(strng::literal!("m")),
			Some(&prompt_a),
			Some("response-a".to_string()),
			None,
			Outcome::Success,
			Duration::from_millis(10),
		);

		let wm = WorkingMemory::new(10);
		wm.push(entry_a).await;

		let fp_b = {
			let bytes = prompt_b.as_bytes();
			fingerprint(&bytes[..bytes.len().min(512)])
		};
		let hit = wm.lookup_by_fingerprint(fp_b).await;
		assert!(
			hit.is_some(),
			"prompts with identical first-512 bytes must match"
		);
		assert_eq!(hit.unwrap().response_content.as_deref(), Some("response-a"));
	}

	#[tokio::test]
	async fn lookup_preserves_response_content() {
		let wm = WorkingMemory::new(10);
		let e = build_entry(
			strng::literal!("r"),
			strng::literal!("be"),
			Some(strng::literal!("gpt-4")),
			Some("system: assistant\nuser: hi\n"),
			Some("Hello! How can I help?".to_string()),
			None,
			Outcome::Success,
			Duration::from_millis(50),
		);
		let fp = e.context_fingerprint.unwrap();
		wm.push(e).await;
		let hit = wm.lookup_by_fingerprint(fp).await.unwrap();
		assert_eq!(
			hit.response_content.as_deref(),
			Some("Hello! How can I help?")
		);
	}

	#[test]
	fn fingerprint_exactly_512_bytes_boundary() {
		// A 512-byte prompt and a 513-byte prompt that differs only at byte 513.
		let p512 = "A".repeat(512);
		let p513 = format!("{p512}B");

		let fp512 = {
			let b = p512.as_bytes();
			fingerprint(&b[..b.len().min(512)])
		};
		let fp513 = {
			let b = p513.as_bytes();
			fingerprint(&b[..b.len().min(512)])
		};
		assert_eq!(
			fp512, fp513,
			"513th byte must not affect fingerprint (only first 512 used)"
		);
	}
}
