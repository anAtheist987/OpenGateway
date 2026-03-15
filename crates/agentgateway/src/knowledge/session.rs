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

/// Session-level working memory — ESSA "macro state" analogue.
///
/// Tracks multi-turn agent sessions across requests using a client-supplied
/// `X-Session-ID` header.  Each session records:
///   - turn count (how many requests have been proxied in this session)
///   - seen context fingerprints (FNV-1a hashes of LLM prompt prefixes)
///   - last backend used
///   - consecutive failure count (for circuit-breaking hints)
///
/// The `touch()` method returns an overlap flag (`bool`) that signals whether
/// the incoming context fingerprint was already seen in this session.  A `true`
/// overlap is the strongest signal for KDN cache retrieval: the same prompt
/// prefix recurred within the same conversation.
///
/// Sessions are TTL-evicted by `evict_stale()`; callers should schedule this
/// periodically (e.g. every 5 min via a background task).
use std::collections::HashMap;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use agent_core::strng::Strng;
use serde::Serialize;
use tokio::sync::RwLock;

pub const DEFAULT_SESSION_TTL_SECS: u64 = 1800; // 30 minutes

// ── public types ─────────────────────────────────────────────────────────────

/// Tracked state for an active multi-turn agent session.
#[derive(Debug, Clone, Serialize)]
pub struct SessionState {
	pub session_id: String,
	/// Route that owns this session (first route seen).
	pub route_key: Strng,
	/// Total number of turns (requests) in this session.
	pub turn_count: u32,
	/// Ordered list of unique context fingerprints seen in this session.
	pub seen_fingerprints: Vec<u64>,
	/// Backend used by the most recent turn.
	pub last_backend: Strng,
	/// Number of consecutive failures (reset to 0 on any success).
	pub consecutive_failures: u32,
	pub created_at_secs: u64,
	pub last_seen_at_secs: u64,
}

impl SessionState {
	fn new(session_id: String, route_key: Strng, backend: Strng) -> Self {
		let now = now_secs();
		Self {
			session_id,
			route_key,
			turn_count: 0,
			seen_fingerprints: Vec::new(),
			last_backend: backend,
			consecutive_failures: 0,
			created_at_secs: now,
			last_seen_at_secs: now,
		}
	}

	/// Returns `true` if `fp` was already recorded in this session.
	pub fn has_seen(&self, fp: u64) -> bool {
		self.seen_fingerprints.contains(&fp)
	}
}

// ── store ─────────────────────────────────────────────────────────────────────

/// Shared, thread-safe session tracking store.
#[derive(Clone, Debug)]
pub struct SessionWorkingMemory(Arc<RwLock<Inner>>);

struct Inner {
	sessions: HashMap<String, SessionState>,
	ttl_secs: u64,
}

impl std::fmt::Debug for Inner {
	fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
		f.debug_struct("Inner")
			.field("sessions", &self.sessions.len())
			.field("ttl_secs", &self.ttl_secs)
			.finish()
	}
}

impl SessionWorkingMemory {
	pub fn new(ttl_secs: u64) -> Self {
		SessionWorkingMemory(Arc::new(RwLock::new(Inner {
			sessions: HashMap::new(),
			ttl_secs,
		})))
	}

	/// Record a new request turn in the session, creating the entry if needed.
	///
	/// Returns `(updated_state, overlap)` where `overlap` is `true` when
	/// `fingerprint` was already present in `seen_fingerprints` **before** this
	/// call — i.e. the same context prefix recurred within the same session.
	pub async fn touch(
		&self,
		session_id: &str,
		route_key: Strng,
		backend: Strng,
		fingerprint: Option<u64>,
		is_failure: bool,
	) -> (SessionState, bool) {
		let mut inner = self.0.write().await;
		let state = inner
			.sessions
			.entry(session_id.to_string())
			.or_insert_with(|| {
				SessionState::new(session_id.to_string(), route_key.clone(), backend.clone())
			});

		// Check overlap BEFORE updating seen_fingerprints.
		let overlap = fingerprint.is_some_and(|fp| state.has_seen(fp));

		state.last_seen_at_secs = now_secs();
		state.last_backend = backend;
		state.turn_count += 1;

		if is_failure {
			state.consecutive_failures += 1;
		} else {
			state.consecutive_failures = 0;
		}

		if let Some(fp) = fingerprint
			&& !state.has_seen(fp)
		{
			state.seen_fingerprints.push(fp);
		}

		(state.clone(), overlap)
	}

	/// Return the current state for a session, if it is active.
	pub async fn get(&self, session_id: &str) -> Option<SessionState> {
		self.0.read().await.sessions.get(session_id).cloned()
	}

	/// Evict sessions idle longer than their configured TTL.
	pub async fn evict_stale(&self) {
		let now = now_secs();
		let mut inner = self.0.write().await;
		let ttl = inner.ttl_secs;
		inner
			.sessions
			.retain(|_, s| now.saturating_sub(s.last_seen_at_secs) < ttl);
	}

	/// All active sessions — used by the admin `/knowledge/sessions` endpoint.
	pub async fn snapshot(&self) -> Vec<SessionState> {
		self.0.read().await.sessions.values().cloned().collect()
	}

	pub async fn len(&self) -> usize {
		self.0.read().await.sessions.len()
	}

	pub async fn is_empty(&self) -> bool {
		self.0.read().await.sessions.is_empty()
	}
}

// ── helpers ───────────────────────────────────────────────────────────────────

fn now_secs() -> u64 {
	SystemTime::now()
		.duration_since(UNIX_EPOCH)
		.unwrap_or_default()
		.as_secs()
}

// ── tests ─────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
	use super::*;
	use agent_core::strng;

	fn route() -> Strng {
		strng::literal!("bind/listener/route0")
	}
	fn be() -> Strng {
		strng::literal!("backend-a")
	}

	#[tokio::test]
	async fn new_session_turn_count() {
		let swm = SessionWorkingMemory::new(3600);
		let (state, overlap) = swm.touch("s1", route(), be(), None, false).await;
		assert_eq!(state.turn_count, 1);
		assert!(!overlap);
	}

	#[tokio::test]
	async fn turn_count_increments() {
		let swm = SessionWorkingMemory::new(3600);
		swm.touch("s1", route(), be(), None, false).await;
		swm.touch("s1", route(), be(), None, false).await;
		let (state, _) = swm.touch("s1", route(), be(), None, false).await;
		assert_eq!(state.turn_count, 3);
	}

	#[tokio::test]
	async fn overlap_detection() {
		let swm = SessionWorkingMemory::new(3600);
		// First time: fingerprint 42 not yet seen → no overlap
		let (_, ov1) = swm.touch("s1", route(), be(), Some(42), false).await;
		assert!(!ov1);
		// Second time: fingerprint 42 was recorded → overlap
		let (_, ov2) = swm.touch("s1", route(), be(), Some(42), false).await;
		assert!(ov2);
		// Different fingerprint: no overlap
		let (_, ov3) = swm.touch("s1", route(), be(), Some(99), false).await;
		assert!(!ov3);
	}

	#[tokio::test]
	async fn fingerprints_deduplicated() {
		let swm = SessionWorkingMemory::new(3600);
		swm.touch("s1", route(), be(), Some(1), false).await;
		swm.touch("s1", route(), be(), Some(1), false).await;
		swm.touch("s1", route(), be(), Some(2), false).await;
		let state = swm.get("s1").await.unwrap();
		// Only unique fingerprints stored
		assert_eq!(state.seen_fingerprints, vec![1u64, 2u64]);
	}

	#[tokio::test]
	async fn consecutive_failures_reset_on_success() {
		let swm = SessionWorkingMemory::new(3600);
		swm.touch("s1", route(), be(), None, true).await;
		swm.touch("s1", route(), be(), None, true).await;
		let (s, _) = swm.touch("s1", route(), be(), None, false).await;
		assert_eq!(s.consecutive_failures, 0);
	}

	#[tokio::test]
	async fn evict_stale_removes_old_sessions() {
		let swm = SessionWorkingMemory::new(0); // TTL = 0 → immediately stale
		swm.touch("old", route(), be(), None, false).await;
		assert_eq!(swm.len().await, 1);
		swm.evict_stale().await;
		assert_eq!(swm.len().await, 0);
	}

	#[tokio::test]
	async fn multiple_independent_sessions() {
		let swm = SessionWorkingMemory::new(3600);
		swm.touch("alice", route(), be(), Some(10), false).await;
		swm.touch("bob", route(), be(), Some(10), false).await;
		// Each session tracks independently
		let alice = swm.get("alice").await.unwrap();
		let bob = swm.get("bob").await.unwrap();
		assert_eq!(alice.session_id, "alice");
		assert_eq!(bob.session_id, "bob");
		// bob touching fp=10 should have NO overlap (first time for bob)
		assert_eq!(bob.seen_fingerprints, vec![10u64]);
	}
}
