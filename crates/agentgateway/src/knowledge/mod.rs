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

pub mod kdn_client;
pub mod session;
pub mod store;
pub mod working_memory;

use std::sync::Arc;
use std::time::Duration;

use agent_core::strng::Strng;

use crate::knowledge::kdn_client::KdnClient;
use crate::knowledge::session::SessionWorkingMemory;
use crate::knowledge::store::KnowledgeStore;
use crate::knowledge::working_memory::{Outcome, WorkingMemory, build_entry};

pub const DEFAULT_WM_CAPACITY: usize = 1000;
pub const DEFAULT_SESSION_TTL_SECS: u64 = session::DEFAULT_SESSION_TTL_SECS;

/// Arguments for `KnowledgeHandle::capture()`.
pub struct CaptureArgs {
	/// Session identifier from the `X-Session-ID` request header.
	pub session_id: Option<String>,
	pub route_key: Strng,
	pub backend: Strng,
	pub llm_model: Option<Strng>,
	/// First 512 bytes of the LLM prompt, used for fingerprinting.
	pub prompt_prefix: Option<String>,
	/// Full LLM response text for local working-memory replay.
	pub response_content: Option<String>,
	pub agent_id: Option<String>,
	pub outcome: Outcome,
	pub latency: Duration,
}

/// `KnowledgeHandle` is the single shared object placed in `ProxyInputs`.
///
/// It bundles:
///  - `working_memory` — bounded ring-buffer of recent execution traces
///  - `session_memory` — active multi-turn session state (ESSA macro-state analogue)
///  - `store`          — aggregated per-route statistics + user corrections
///  - `kdn`            — optional KDN client for KV-cache retrieval
#[derive(Clone, Debug)]
pub struct KnowledgeHandle {
	pub working_memory: WorkingMemory,
	pub session_memory: SessionWorkingMemory,
	pub store: KnowledgeStore,
	/// Optional KDN client — `None` when KDN integration is not configured.
	pub kdn: Option<KdnClient>,
}

impl KnowledgeHandle {
	pub fn new(wm_capacity: usize, session_ttl_secs: u64) -> Self {
		KnowledgeHandle {
			working_memory: WorkingMemory::new(wm_capacity),
			session_memory: SessionWorkingMemory::new(session_ttl_secs),
			store: KnowledgeStore::new(),
			kdn: None,
		}
	}

	pub fn with_kdn(mut self, client: KdnClient) -> Self {
		self.kdn = Some(client);
		self
	}

	/// Capture a completed request trace — non-blocking (spawns a task).
	///
	/// When `args.session_id` is provided:
	///  1. The session working memory is updated (`turn_count`, `seen_fingerprints`).
	///  2. If the fingerprint was already seen in this session (`session_overlap`),
	///     that is the strongest signal for KDN KV-cache reuse.
	///  3. If a KDN client is configured and a fingerprint is available, the KDN is
	///     queried with the session overlap signal so the KDN can log/prepare future hits.
	pub fn capture(self: &Arc<Self>, args: CaptureArgs) {
		let handle = Arc::clone(self);
		tokio::spawn(async move {
			let is_failure = matches!(args.outcome, Outcome::Failure { .. });
			let entry = build_entry(
				args.route_key.clone(),
				args.backend.clone(),
				args.llm_model.clone(),
				args.prompt_prefix.as_deref(),
				args.response_content,
				args.agent_id,
				args.outcome,
				args.latency,
			);
			let fingerprint = entry.context_fingerprint;

			let mut session_turn_count: Option<u32> = None;
			let mut session_overlap: Option<bool> = None;

			if let Some(ref sid) = args.session_id {
				let (state, overlap) = handle
					.session_memory
					.touch(
						sid,
						args.route_key.clone(),
						args.backend.clone(),
						fingerprint,
						is_failure,
					)
					.await;
				session_turn_count = Some(state.turn_count);
				if overlap {
					session_overlap = Some(true);
				}
			}

			// Post-request KDN notification: inform the KDN about this fingerprint so it
			// can track cache candidates and prepare for future overlap hits.
			if let (Some(kdn), Some(fp), Some(model)) =
				(handle.kdn.as_ref(), fingerprint, args.llm_model.as_ref())
			{
				kdn
					.query(
						fp,
						model,
						&args.route_key,
						args.session_id.as_deref(),
						session_turn_count,
						session_overlap,
					)
					.await;
			}

			handle.working_memory.push(entry.clone()).await;
			handle.store.ingest(&[entry]).await;
		});
	}
}

impl Default for KnowledgeHandle {
	fn default() -> Self {
		Self::new(DEFAULT_WM_CAPACITY, DEFAULT_SESSION_TTL_SECS)
	}
}

/// Look up the most recent working-memory entry whose prompt fingerprint
/// matches the first 512 bytes of `prompt_prefix`.
///
/// Returns `None` when the working memory is empty or no entry matches.
/// This is the primary entry point for local working-memory replay without KDN.
pub async fn lookup(
	working_memory: &working_memory::WorkingMemory,
	prompt_prefix: &str,
) -> Option<working_memory::TraceEntry> {
	let bytes = prompt_prefix.as_bytes();
	let slice = &bytes[..bytes.len().min(512)];
	let fp = working_memory::fingerprint(slice);
	working_memory.lookup_by_fingerprint(fp).await
}

#[cfg(test)]
mod tests {
	use std::sync::Arc;
	use std::time::Duration;

	use agent_core::strng;

	use super::*;
	use crate::knowledge::working_memory::{Outcome, WorkingMemory, build_entry};

	async fn push_entry(wm: &WorkingMemory, prompt: &str, response: Option<&str>) {
		let e = build_entry(
			strng::literal!("r"),
			strng::literal!("be"),
			Some(strng::literal!("gpt-4")),
			Some(prompt),
			response.map(ToOwned::to_owned),
			None,
			Outcome::Success,
			Duration::from_millis(10),
		);
		wm.push(e).await;
	}

	#[tokio::test]
	async fn handle_lookup_empty_returns_none() {
		let kh = Arc::new(KnowledgeHandle::default());
		let result = lookup(&kh.working_memory, "hello").await;
		assert!(result.is_none());
	}

	#[tokio::test]
	async fn handle_lookup_found() {
		let kh = Arc::new(KnowledgeHandle::default());
		push_entry(
			&kh.working_memory,
			"system: you are helpful\nuser: hi\n",
			Some("Hello!"),
		)
		.await;

		let result = lookup(&kh.working_memory, "system: you are helpful\nuser: hi\n").await;
		assert!(result.is_some());
		assert_eq!(result.unwrap().response_content.as_deref(), Some("Hello!"));
	}

	#[tokio::test]
	async fn handle_lookup_not_found() {
		let kh = Arc::new(KnowledgeHandle::default());
		push_entry(
			&kh.working_memory,
			"system: original prompt\n",
			Some("response"),
		)
		.await;

		let result = lookup(&kh.working_memory, "system: different prompt\n").await;
		assert!(result.is_none());
	}

	#[tokio::test]
	async fn handle_lookup_prefix_beyond_512_same() {
		let kh = Arc::new(KnowledgeHandle::default());
		let base = "Z".repeat(512);
		let prompt_stored = format!("{base}suffix-stored");
		let prompt_query = format!("{base}suffix-query");

		push_entry(&kh.working_memory, &prompt_stored, Some("cached-response")).await;

		let result = lookup(&kh.working_memory, &prompt_query).await;
		assert!(
			result.is_some(),
			"prompts with identical first-512 bytes must match regardless of suffix"
		);
		assert_eq!(
			result.unwrap().response_content.as_deref(),
			Some("cached-response")
		);
	}
}
