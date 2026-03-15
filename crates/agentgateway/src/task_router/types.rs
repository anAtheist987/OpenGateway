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

use serde::{Deserialize, Serialize};

#[cfg(feature = "schema")]
use schemars::JsonSchema;

use crate::task_router::dag::TaskDAG;

fn default_threshold() -> f32 {
	0.6
}

fn default_max_subtasks() -> usize {
	8
}

/// Routing strategy: LLM-based (default), vector embedding-based, or LLM-enhanced vector.
#[derive(Debug, Clone, Deserialize, Serialize, Default, PartialEq)]
#[cfg_attr(feature = "schema", derive(JsonSchema))]
#[serde(rename_all = "camelCase")]
pub enum RoutingStrategy {
	#[default]
	Llm,
	Vector,
	/// LLM first polishes/extracts keywords from the task, then uses embedding cosine similarity.
	LlmEnhancedVector,
	/// Vector similarity to prefilter top-k agents, then LLM selects from candidates.
	VectorPrefilterLlm,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[cfg_attr(feature = "schema", derive(JsonSchema))]
#[serde(rename_all = "camelCase")]
pub struct TaskRouterConfig {
	/// Base URL for API provider (e.g., "https://dashscope.aliyuncs.com/compatible-mode/v1")
	pub base_url: String,
	pub planner_model: String,
	#[serde(default)]
	pub api_key: Option<String>,
	/// Complexity score threshold above which a task is decomposed. Default 0.6.
	#[serde(default = "default_threshold")]
	pub complexity_threshold: f32,
	/// Maximum number of DAG nodes when decomposing. Default 8.
	#[serde(default = "default_max_subtasks")]
	pub max_subtasks: usize,
	/// Routing strategy: "llm" (default) or "vector".
	#[serde(default)]
	pub routing_strategy: RoutingStrategy,
	/// Embedding model name. Required when strategy = vector.
	#[serde(default)]
	pub embedding_model: Option<String>,
}

/// An agent provided by the caller in the routing request.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AgentInfo {
	pub name: String,
	pub description: String,
	pub url: String,
	#[serde(default)]
	pub skills: Vec<String>,
}

/// Assignment of a single agent to a (sub)task.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AgentAssignment {
	pub agent_name: String,
	pub agent_url: String,
	pub confidence: f32,
}

/// Full result returned by the router.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RoutingResult {
	pub task_id: String,
	pub complexity_score: f32,
	pub decision: RoutingDecision,
}

/// The routing decision for a task.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "type", rename_all = "camelCase")]
pub enum RoutingDecision {
	Direct {
		#[serde(rename = "agentName")]
		agent_name: String,
		#[serde(rename = "agentUrl")]
		agent_url: String,
		confidence: f32,
		reason: String,
	},
	Decomposed {
		dag: TaskDAG,
		reason: String,
	},
}

/// Incoming request body for POST /task-router/route
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct RouteTaskRequest {
	#[serde(default)]
	pub task_id: Option<String>,
	pub task: String,
	pub agents: Vec<AgentInfo>,
	#[serde(default)]
	pub strategy_override: Option<RoutingStrategy>,
}
