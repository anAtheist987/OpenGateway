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

/// KnowledgeStore — persistent (in-process) knowledge base for Evolutionary Memory.
///
/// Responsibilities:
///   1. Aggregate `TraceEntry` records from `WorkingMemory` into per-route statistics.
///   2. Expose route-level success-rate and latency summaries for routing policy hints.
///   3. Record user corrections (explicit feedback) that override learned signals.
///   4. Persist per-route-call decision snapshots and execution traces.
///
/// Storage is intentionally in-memory for now; a future persistence layer (SQLite /
/// append-log) can be added without changing the public API.
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use agent_core::strng::Strng;
use serde::{Deserialize, Serialize};
use tokio::sync::RwLock;

use crate::knowledge::working_memory::{Outcome, TraceEntry};

// ── 类别一：聚合统计 ──────────────────────────────────────────────────────────

/// Complexity distribution buckets.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct ComplexityHistogram {
	/// complexity_score < 0.3
	pub low: u64,
	/// 0.3 ≤ complexity_score ≤ 0.7
	pub medium: u64,
	/// complexity_score > 0.7
	pub high: u64,
}

/// Summary of a single recent route (used in RouterStats.recent_routes).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct RecentRoute {
	pub task_id: String,
	pub timestamp_secs: u64,
	pub original_task: String,
	pub decision_type: String,
	/// Direct routing: target agent name.
	pub agent_name: Option<String>,
	/// Decomposed routing: number of DAG nodes.
	pub dag_node_count: Option<usize>,
	pub complexity_score: f32,
	pub latency_ms: u64,
}

/// Aggregated statistics derived from all RouterEntry records.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct RouterStats {
	// 1.1 路由决策分布
	pub total_routes: u64,
	pub direct_count: u64,
	pub decomposed_count: u64,
	pub avg_complexity: f64,
	pub complexity_histogram: ComplexityHistogram,
	// 1.2 延迟统计
	pub avg_latency_ms: f64,
	pub p50_latency_ms: f64,
	pub p95_latency_ms: f64,
	pub max_latency_ms: f64,
	// 1.3 Per-Agent 详情（直连路由）
	/// agent_name → route count
	pub per_agent_counts: HashMap<String, u64>,
	/// agent_name → average confidence
	pub per_agent_avg_confidence: HashMap<String, f64>,
	/// agent_name → average latency (ms)
	pub per_agent_avg_latency_ms: HashMap<String, f64>,
	// 1.4 分解路由 DAG 统计
	pub avg_dag_node_count: f64,
	pub max_dag_node_count: usize,
	// strategy → count
	pub strategy_counts: HashMap<String, u64>,
	// 1.5 最近 10 条路由摘要
	pub recent_routes: Vec<RecentRoute>,
}

// ── 类别二：单次路由决策快照 ──────────────────────────────────────────────────

/// Direct-routing agent assignment captured at routing time.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DirectAgentSnapshot {
	pub agent_name: String,
	pub agent_url: String,
	pub confidence: f32,
	pub reason: String,
}

/// DAG node as captured from the routing decision.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DagNodeSnapshot {
	pub node_id: String,
	pub description: String,
	pub assigned_agent: Option<String>,
	pub agent_url: Option<String>,
	pub estimated_complexity: f32,
}

/// DAG edge as captured from the routing decision.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DagEdgeSnapshot {
	pub from: String,
	pub to: String,
}

/// Single routing record appended after each POST /task-router/route call.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct RouterEntry {
	pub task_id: String,
	pub timestamp_secs: u64,
	/// Original user task text.
	pub original_task: String,
	pub complexity_score: f32,
	/// "direct" | "decomposed"
	pub decision_type: String,
	/// Routing strategy used (e.g. "vectorPrefilterLlm").
	pub strategy: String,
	/// Routing decision latency (ms).
	pub latency_ms: u64,
	/// Populated when decision_type == "direct".
	pub direct_agent: Option<DirectAgentSnapshot>,
	/// Populated when decision_type == "decomposed".
	pub dag_nodes: Option<Vec<DagNodeSnapshot>>,
	pub dag_edges: Option<Vec<DagEdgeSnapshot>>,
}

impl RouterEntry {
	fn agent_name(&self) -> Option<&str> {
		self.direct_agent.as_ref().map(|a| a.agent_name.as_str())
	}

	fn dag_node_count(&self) -> Option<usize> {
		self.dag_nodes.as_ref().map(|n| n.len())
	}
}

// ── 类别三：单次执行结果追踪 ──────────────────────────────────────────────────

/// Result from a single DAG node execution.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct NodeResult {
	pub node_id: String,
	pub agent_name: String,
	pub task: String,
	/// "success" | "failed" | "skipped"
	pub status: String,
	/// Agent response (may be truncated to 500 chars by sender).
	pub response: String,
	/// Summary passed to downstream nodes, if any.
	pub summary_to_downstream: Option<String>,
}

/// A single upstream→downstream summary transfer between DAG nodes.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AgentMessage {
	pub from_node_id: String,
	pub to_node_id: String,
	pub summary: String,
}

/// Execution results submitted by the Python agent after the DAG completes.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct RouteExecution {
	pub task_id: String,
	pub node_results: Vec<NodeResult>,
	/// All upstream→downstream summary transfers within this DAG run.
	pub agent_messages: Vec<AgentMessage>,
	/// Final synthesized result from ResultSummarizer.
	pub final_result: String,
	pub total_nodes: usize,
	pub success_nodes: usize,
	/// Wall-clock time for the full DAG execution (ms).
	pub execution_latency_ms: u64,
}

/// Combined routing decision + execution result for a single task.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct RouteTrace {
	#[serde(flatten)]
	pub decision: RouterEntry,
	/// None until Python submits execution results.
	pub execution: Option<RouteExecution>,
}

// ── public types (working memory) ─────────────────────────────────────────────

/// Aggregated statistics for a single route.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct RouteStats {
	pub route_key: Strng,
	pub total_requests: u64,
	pub success_count: u64,
	pub failure_count: u64,
	/// Exponentially-weighted moving average latency (α = 0.1).
	pub ewma_latency_ms: f64,
}

impl RouteStats {
	fn new(route_key: Strng) -> Self {
		Self {
			route_key,
			total_requests: 0,
			success_count: 0,
			failure_count: 0,
			ewma_latency_ms: 0.0,
		}
	}

	fn record(&mut self, outcome: &Outcome, latency: Duration) {
		const ALPHA: f64 = 0.1;
		self.total_requests += 1;
		match outcome {
			Outcome::Success => self.success_count += 1,
			Outcome::Failure { .. } => self.failure_count += 1,
		}
		let ms = latency.as_secs_f64() * 1000.0;
		if self.total_requests == 1 {
			self.ewma_latency_ms = ms;
		} else {
			self.ewma_latency_ms = ALPHA * ms + (1.0 - ALPHA) * self.ewma_latency_ms;
		}
	}

	pub fn success_rate(&self) -> f64 {
		if self.total_requests == 0 {
			1.0
		} else {
			self.success_count as f64 / self.total_requests as f64
		}
	}
}

/// An explicit user correction attached to a route.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Correction {
	pub route_key: Strng,
	/// Human-readable note (e.g. "prefer backend B for this route").
	pub note: String,
	pub timestamp_secs: u64,
}

// ── internal state ─────────────────────────────────────────────────────────────

/// Shared handle — cheap to clone.
#[derive(Clone, Debug)]
pub struct KnowledgeStore(Arc<RwLock<Inner>>);

#[derive(Debug, Default)]
struct Inner {
	stats: HashMap<Strng, RouteStats>,
	corrections: Vec<Correction>,
	router_entries: Vec<RouterEntry>,
	/// task_id → execution results (submitted by Python after DAG completes).
	route_executions: HashMap<String, RouteExecution>,
}

const ROUTER_ENTRY_LIMIT: usize = 1000;

// ── implementation ─────────────────────────────────────────────────────────────

impl KnowledgeStore {
	pub fn new() -> Self {
		KnowledgeStore(Arc::new(RwLock::new(Inner::default())))
	}

	// ── working memory ─────────────────────────────────────────────────────────

	/// Ingest a batch of `TraceEntry` records (typically a working-memory snapshot).
	pub async fn ingest(&self, entries: &[TraceEntry]) {
		let mut inner = self.0.write().await;
		for e in entries {
			inner
				.stats
				.entry(e.route_key.clone())
				.or_insert_with(|| RouteStats::new(e.route_key.clone()))
				.record(&e.outcome, e.latency);
		}
	}

	/// Return stats for a specific route, if any.
	pub async fn route_stats(&self, route_key: &Strng) -> Option<RouteStats> {
		self.0.read().await.stats.get(route_key).cloned()
	}

	/// Return all route stats (for admin dump).
	pub async fn all_stats(&self) -> Vec<RouteStats> {
		self.0.read().await.stats.values().cloned().collect()
	}

	// ── corrections ────────────────────────────────────────────────────────────

	/// Record a user correction.
	pub async fn add_correction(&self, correction: Correction) {
		self.0.write().await.corrections.push(correction);
	}

	/// Return all corrections (for admin dump).
	pub async fn all_corrections(&self) -> Vec<Correction> {
		self.0.read().await.corrections.clone()
	}

	// ── 类别二：路由决策快照 ──────────────────────────────────────────────────

	/// Append a router entry; trims oldest when the ring limit is reached.
	pub async fn add_router_entry(&self, entry: RouterEntry) {
		let mut inner = self.0.write().await;
		inner.router_entries.push(entry);
		if inner.router_entries.len() > ROUTER_ENTRY_LIMIT {
			let excess = inner.router_entries.len() - ROUTER_ENTRY_LIMIT;
			inner.router_entries.drain(..excess);
		}
	}

	// ── 类别三：执行结果追踪 ──────────────────────────────────────────────────

	/// Store execution results submitted by the Python agent.
	pub async fn add_execution(&self, execution: RouteExecution) {
		self
			.0
			.write()
			.await
			.route_executions
			.insert(execution.task_id.clone(), execution);
	}

	/// Return the most recent `limit` traces (decision + execution), newest first.
	pub async fn all_traces(&self, limit: usize) -> Vec<RouteTrace> {
		let inner = self.0.read().await;
		let entries = &inner.router_entries;
		let start = entries.len().saturating_sub(limit);
		entries[start..]
			.iter()
			.rev()
			.map(|e| RouteTrace {
				decision: e.clone(),
				execution: inner.route_executions.get(&e.task_id).cloned(),
			})
			.collect()
	}

	// ── 类别一：聚合统计 ──────────────────────────────────────────────────────

	/// Compute aggregated router statistics from the stored entries.
	pub async fn router_stats(&self) -> RouterStats {
		let inner = self.0.read().await;
		let entries = &inner.router_entries;
		let total = entries.len() as u64;

		if total == 0 {
			return RouterStats {
				total_routes: 0,
				direct_count: 0,
				decomposed_count: 0,
				avg_complexity: 0.0,
				complexity_histogram: ComplexityHistogram::default(),
				avg_latency_ms: 0.0,
				p50_latency_ms: 0.0,
				p95_latency_ms: 0.0,
				max_latency_ms: 0.0,
				per_agent_counts: HashMap::new(),
				per_agent_avg_confidence: HashMap::new(),
				per_agent_avg_latency_ms: HashMap::new(),
				avg_dag_node_count: 0.0,
				max_dag_node_count: 0,
				strategy_counts: HashMap::new(),
				recent_routes: vec![],
			};
		}

		let mut direct_count = 0u64;
		let mut decomposed_count = 0u64;
		let mut complexity_sum = 0f64;
		let mut complexity_histogram = ComplexityHistogram::default();
		let mut latencies: Vec<f64> = Vec::with_capacity(entries.len());

		// Per-agent accumulators (direct routes only)
		let mut per_agent_counts: HashMap<String, u64> = HashMap::new();
		let mut per_agent_confidence_sum: HashMap<String, f64> = HashMap::new();
		let mut per_agent_latency_sum: HashMap<String, f64> = HashMap::new();

		// DAG accumulators
		let mut dag_node_sum = 0usize;
		let mut max_dag_node_count = 0usize;
		let mut decomposed_dag_count = 0usize;

		let mut strategy_counts: HashMap<String, u64> = HashMap::new();

		for e in entries {
			let cs = e.complexity_score as f64;
			complexity_sum += cs;
			if e.complexity_score < 0.3 {
				complexity_histogram.low += 1;
			} else if e.complexity_score <= 0.7 {
				complexity_histogram.medium += 1;
			} else {
				complexity_histogram.high += 1;
			}

			latencies.push(e.latency_ms as f64);
			*strategy_counts.entry(e.strategy.clone()).or_insert(0) += 1;

			if e.decision_type == "direct" {
				direct_count += 1;
				if let Some(da) = &e.direct_agent {
					let cnt = per_agent_counts.entry(da.agent_name.clone()).or_insert(0);
					*cnt += 1;
					*per_agent_confidence_sum
						.entry(da.agent_name.clone())
						.or_insert(0.0) += da.confidence as f64;
					*per_agent_latency_sum
						.entry(da.agent_name.clone())
						.or_insert(0.0) += e.latency_ms as f64;
				}
			} else {
				decomposed_count += 1;
				if let Some(nodes) = &e.dag_nodes {
					let n = nodes.len();
					dag_node_sum += n;
					decomposed_dag_count += 1;
					if n > max_dag_node_count {
						max_dag_node_count = n;
					}
				}
			}
		}

		// Latency percentiles
		latencies.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
		let n = latencies.len();
		let p50 = latencies[n / 2];
		let p95 = latencies[(n * 95).div_ceil(100).min(n) - 1];
		let max_latency = latencies[n - 1];
		let avg_latency = latencies.iter().sum::<f64>() / n as f64;

		// Per-agent averages
		let per_agent_avg_confidence: HashMap<String, f64> = per_agent_counts
			.keys()
			.map(|name| {
				let sum = per_agent_confidence_sum.get(name).copied().unwrap_or(0.0);
				let cnt = per_agent_counts[name] as f64;
				(name.clone(), if cnt > 0.0 { sum / cnt } else { 0.0 })
			})
			.collect();

		let per_agent_avg_latency_ms: HashMap<String, f64> = per_agent_counts
			.keys()
			.map(|name| {
				let sum = per_agent_latency_sum.get(name).copied().unwrap_or(0.0);
				let cnt = per_agent_counts[name] as f64;
				(name.clone(), if cnt > 0.0 { sum / cnt } else { 0.0 })
			})
			.collect();

		// DAG averages
		let avg_dag_node_count = if decomposed_dag_count > 0 {
			dag_node_sum as f64 / decomposed_dag_count as f64
		} else {
			0.0
		};

		// Recent 10 routes (newest first)
		let recent_routes: Vec<RecentRoute> = entries
			.iter()
			.rev()
			.take(10)
			.map(|e| RecentRoute {
				task_id: e.task_id.clone(),
				timestamp_secs: e.timestamp_secs,
				original_task: e.original_task.clone(),
				decision_type: e.decision_type.clone(),
				agent_name: e.agent_name().map(String::from),
				dag_node_count: e.dag_node_count(),
				complexity_score: e.complexity_score,
				latency_ms: e.latency_ms,
			})
			.collect();

		RouterStats {
			total_routes: total,
			direct_count,
			decomposed_count,
			avg_complexity: complexity_sum / total as f64,
			complexity_histogram,
			avg_latency_ms: avg_latency,
			p50_latency_ms: p50,
			p95_latency_ms: p95,
			max_latency_ms: max_latency,
			per_agent_counts,
			per_agent_avg_confidence,
			per_agent_avg_latency_ms,
			avg_dag_node_count,
			max_dag_node_count,
			strategy_counts,
			recent_routes,
		}
	}
}

impl Default for KnowledgeStore {
	fn default() -> Self {
		Self::new()
	}
}

// ── tests ──────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
	use super::*;
	use agent_core::strng;
	use std::time::{SystemTime, UNIX_EPOCH};

	fn trace(route: &'static str, outcome: Outcome, latency_ms: u64) -> TraceEntry {
		TraceEntry {
			timestamp_secs: SystemTime::now()
				.duration_since(UNIX_EPOCH)
				.unwrap_or_default()
				.as_secs(),
			route_key: agent_core::strng::new(route),
			backend: strng::literal!("be"),
			llm_model: None,
			context_fingerprint: None,
			prompt_snippet: None,
			response_content: None,
			agent_id: None,
			outcome,
			latency: Duration::from_millis(latency_ms),
		}
	}

	fn make_entry(task_id: &str, decision_type: &str, latency_ms: u64) -> RouterEntry {
		RouterEntry {
			task_id: task_id.to_string(),
			timestamp_secs: 0,
			original_task: "test task".to_string(),
			complexity_score: if decision_type == "decomposed" {
				0.8
			} else {
				0.2
			},
			decision_type: decision_type.to_string(),
			strategy: "llm".to_string(),
			latency_ms,
			direct_agent: if decision_type == "direct" {
				Some(DirectAgentSnapshot {
					agent_name: "AgentA".to_string(),
					agent_url: "http://localhost:10001".to_string(),
					confidence: 0.9,
					reason: "best match".to_string(),
				})
			} else {
				None
			},
			dag_nodes: if decision_type == "decomposed" {
				Some(vec![
					DagNodeSnapshot {
						node_id: "t1".to_string(),
						description: "step1".to_string(),
						assigned_agent: Some("AgentB".to_string()),
						agent_url: Some("http://localhost:10002".to_string()),
						estimated_complexity: 0.4,
					},
					DagNodeSnapshot {
						node_id: "t2".to_string(),
						description: "step2".to_string(),
						assigned_agent: Some("AgentC".to_string()),
						agent_url: Some("http://localhost:10003".to_string()),
						estimated_complexity: 0.4,
					},
				])
			} else {
				None
			},
			dag_edges: if decision_type == "decomposed" {
				Some(vec![DagEdgeSnapshot {
					from: "t1".to_string(),
					to: "t2".to_string(),
				}])
			} else {
				None
			},
		}
	}

	#[tokio::test]
	async fn ingest_and_stats() {
		let ks = KnowledgeStore::new();
		let entries = vec![
			trace("r1", Outcome::Success, 100),
			trace("r1", Outcome::Success, 200),
			trace("r1", Outcome::Failure { status: 500 }, 50),
		];
		ks.ingest(&entries).await;

		let stats = ks.route_stats(&strng::literal!("r1")).await.unwrap();
		assert_eq!(stats.total_requests, 3);
		assert_eq!(stats.success_count, 2);
		assert_eq!(stats.failure_count, 1);
		let sr = stats.success_rate();
		assert!((sr - 2.0 / 3.0).abs() < 1e-9);
	}

	#[tokio::test]
	async fn ewma_latency() {
		let ks = KnowledgeStore::new();
		ks.ingest(&[trace("r", Outcome::Success, 100)]).await;
		let s1 = ks.route_stats(&strng::literal!("r")).await.unwrap();
		assert!((s1.ewma_latency_ms - 100.0).abs() < 1e-6);

		ks.ingest(&[trace("r", Outcome::Success, 200)]).await;
		let s2 = ks.route_stats(&strng::literal!("r")).await.unwrap();
		assert!((s2.ewma_latency_ms - 110.0).abs() < 1e-6);
	}

	#[tokio::test]
	async fn corrections() {
		let ks = KnowledgeStore::new();
		ks.add_correction(Correction {
			route_key: strng::literal!("r1"),
			note: "prefer backend B".to_string(),
			timestamp_secs: 0,
		})
		.await;
		let corrections = ks.all_corrections().await;
		assert_eq!(corrections.len(), 1);
		assert_eq!(corrections[0].note, "prefer backend B");
	}

	#[tokio::test]
	async fn all_stats_multiple_routes() {
		let ks = KnowledgeStore::new();
		ks.ingest(&[
			trace("r1", Outcome::Success, 10),
			trace("r2", Outcome::Success, 20),
			trace("r2", Outcome::Failure { status: 503 }, 5),
		])
		.await;
		let all = ks.all_stats().await;
		assert_eq!(all.len(), 2);
	}

	#[tokio::test]
	async fn empty_route_returns_none() {
		let ks = KnowledgeStore::new();
		assert!(
			ks.route_stats(&strng::literal!("nonexistent"))
				.await
				.is_none()
		);
	}

	#[tokio::test]
	async fn router_stats_aggregation() {
		let ks = KnowledgeStore::new();
		ks.add_router_entry(make_entry("t1", "direct", 100)).await;
		ks.add_router_entry(make_entry("t2", "direct", 300)).await;
		ks.add_router_entry(make_entry("t3", "decomposed", 200))
			.await;

		let s = ks.router_stats().await;
		assert_eq!(s.total_routes, 3);
		assert_eq!(s.direct_count, 2);
		assert_eq!(s.decomposed_count, 1);
		assert_eq!(s.per_agent_counts.get("AgentA").copied().unwrap_or(0), 2);
		assert_eq!(s.max_dag_node_count, 2);
		assert!(s.avg_dag_node_count > 0.0);
		assert_eq!(s.recent_routes.len(), 3);
	}

	#[tokio::test]
	async fn execution_trace_round_trip() {
		let ks = KnowledgeStore::new();
		ks.add_router_entry(make_entry("task-1", "decomposed", 500))
			.await;
		ks.add_execution(RouteExecution {
			task_id: "task-1".to_string(),
			node_results: vec![NodeResult {
				node_id: "t1".to_string(),
				agent_name: "AgentB".to_string(),
				task: "do something".to_string(),
				status: "success".to_string(),
				response: "done".to_string(),
				summary_to_downstream: Some("key info".to_string()),
			}],
			agent_messages: vec![AgentMessage {
				from_node_id: "t1".to_string(),
				to_node_id: "t2".to_string(),
				summary: "key info".to_string(),
			}],
			final_result: "Final answer.".to_string(),
			total_nodes: 1,
			success_nodes: 1,
			execution_latency_ms: 45000,
		})
		.await;

		let traces = ks.all_traces(10).await;
		assert_eq!(traces.len(), 1);
		assert!(traces[0].execution.is_some());
		let exec = traces[0].execution.as_ref().unwrap();
		assert_eq!(exec.final_result, "Final answer.");
		assert_eq!(exec.agent_messages.len(), 1);
	}
}
