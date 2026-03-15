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

/// Integration tests for the task_router module.
///
/// These tests cover types, DAG validation, config deserialization, and
/// `TaskRouter::new()` error paths — all without requiring a live LLM API.
use agentgateway::task_router::{
	dag::{DagEdge, DagNode, TaskDAG},
	router::TaskRouter,
	types::{AgentInfo, RoutingStrategy, TaskRouterConfig},
};

// ── helpers ───────────────────────────────────────────────────────────────────

fn minimal_config() -> TaskRouterConfig {
	TaskRouterConfig {
		base_url: "http://localhost:11434/v1".to_string(),
		planner_model: "llama3".to_string(),
		api_key: None,
		complexity_threshold: 0.6,
		max_subtasks: 8,
		routing_strategy: RoutingStrategy::Llm,
		embedding_model: None,
	}
}

fn make_dag(node_ids: &[&str], edges: &[(&str, &str)]) -> TaskDAG {
	TaskDAG {
		nodes: node_ids
			.iter()
			.map(|id| DagNode {
				id: id.to_string(),
				description: format!("task {id}"),
				required_capabilities: vec![],
				assigned_agent: None,
				estimated_complexity: 0.3,
			})
			.collect(),
		edges: edges
			.iter()
			.map(|(f, t)| DagEdge {
				from: f.to_string(),
				to: t.to_string(),
			})
			.collect(),
	}
}

// ── DAG validation ────────────────────────────────────────────────────────────

#[test]
fn dag_empty_is_acyclic() {
	assert!(make_dag(&[], &[]).validate_acyclic().is_ok());
}

#[test]
fn dag_single_node_no_edges_is_acyclic() {
	assert!(make_dag(&["t1"], &[]).validate_acyclic().is_ok());
}

#[test]
fn dag_linear_chain_is_acyclic() {
	let dag = make_dag(&["t1", "t2", "t3"], &[("t1", "t2"), ("t2", "t3")]);
	assert!(dag.validate_acyclic().is_ok());
}

#[test]
fn dag_diamond_is_acyclic() {
	// t1 → t2, t1 → t3, t2 → t4, t3 → t4
	let dag = make_dag(
		&["t1", "t2", "t3", "t4"],
		&[("t1", "t2"), ("t1", "t3"), ("t2", "t4"), ("t3", "t4")],
	);
	assert!(dag.validate_acyclic().is_ok());
}

#[test]
fn dag_self_loop_is_cyclic() {
	let dag = make_dag(&["t1"], &[("t1", "t1")]);
	assert!(dag.validate_acyclic().is_err());
}

#[test]
fn dag_simple_cycle_detected() {
	let dag = make_dag(
		&["t1", "t2", "t3"],
		&[("t1", "t2"), ("t2", "t3"), ("t3", "t1")],
	);
	let err = dag.validate_acyclic().unwrap_err();
	assert!(err.contains("cycle"), "error should mention 'cycle': {err}");
}

#[test]
fn dag_two_node_back_edge_is_cyclic() {
	let dag = make_dag(&["a", "b"], &[("a", "b"), ("b", "a")]);
	assert!(dag.validate_acyclic().is_err());
}

// ── config & types ────────────────────────────────────────────────────────────

#[test]
fn task_router_config_defaults() {
	let cfg = minimal_config();
	assert!((cfg.complexity_threshold - 0.6).abs() < f32::EPSILON);
	assert_eq!(cfg.max_subtasks, 8);
	assert_eq!(cfg.routing_strategy, RoutingStrategy::Llm);
	assert!(cfg.embedding_model.is_none());
}

#[test]
fn routing_strategy_default_is_llm() {
	let s: RoutingStrategy = Default::default();
	assert_eq!(s, RoutingStrategy::Llm);
}

#[test]
fn task_router_config_roundtrip_json() {
	let cfg = minimal_config();
	let json = serde_json::to_string(&cfg).unwrap();
	let decoded: TaskRouterConfig = serde_json::from_str(&json).unwrap();
	assert_eq!(decoded.base_url, cfg.base_url);
	assert_eq!(decoded.planner_model, cfg.planner_model);
	assert_eq!(decoded.routing_strategy, cfg.routing_strategy);
}

#[test]
fn task_router_config_deserialization_camel_case() {
	let json = r#"{
		"baseUrl": "http://llm:8080/v1",
		"plannerModel": "qwen",
		"complexityThreshold": 0.5,
		"maxSubtasks": 4,
		"routingStrategy": "vector",
		"embeddingModel": "text-embed-v1"
	}"#;
	let cfg: TaskRouterConfig = serde_json::from_str(json).unwrap();
	assert_eq!(cfg.base_url, "http://llm:8080/v1");
	assert_eq!(cfg.planner_model, "qwen");
	assert!((cfg.complexity_threshold - 0.5).abs() < f32::EPSILON);
	assert_eq!(cfg.max_subtasks, 4);
	assert_eq!(cfg.routing_strategy, RoutingStrategy::Vector);
	assert_eq!(cfg.embedding_model.as_deref(), Some("text-embed-v1"));
}

#[test]
fn agent_info_roundtrip_json() {
	let agent = AgentInfo {
		name: "flight-agent".to_string(),
		description: "books flights".to_string(),
		url: "http://flight:8080".to_string(),
		skills: vec!["booking".to_string(), "search".to_string()],
	};
	let json = serde_json::to_string(&agent).unwrap();
	let decoded: AgentInfo = serde_json::from_str(&json).unwrap();
	assert_eq!(decoded.name, agent.name);
	assert_eq!(decoded.skills.len(), 2);
}

// ── TaskRouter::new() error paths ─────────────────────────────────────────────

#[test]
fn task_router_new_llm_strategy_ok() {
	let cfg = minimal_config();
	assert!(TaskRouter::new(cfg).is_ok());
}

#[test]
fn task_router_new_vector_strategy_requires_embedding_model() {
	let cfg = TaskRouterConfig {
		routing_strategy: RoutingStrategy::Vector,
		embedding_model: None, // missing
		..minimal_config()
	};
	let result = TaskRouter::new(cfg);
	assert!(result.is_err());
	let err = result.err().unwrap();
	assert!(
		err.to_string().contains("embeddingModel"),
		"error should mention embeddingModel: {err}"
	);
}

#[test]
fn task_router_new_llm_enhanced_vector_requires_embedding_model() {
	let cfg = TaskRouterConfig {
		routing_strategy: RoutingStrategy::LlmEnhancedVector,
		embedding_model: None,
		..minimal_config()
	};
	assert!(TaskRouter::new(cfg).is_err());
}
#[test]
fn task_router_new_vector_strategy_with_model_ok() {
	let cfg = TaskRouterConfig {
		routing_strategy: RoutingStrategy::Vector,
		embedding_model: Some("text-embed-v1".to_string()),
		..minimal_config()
	};
	assert!(TaskRouter::new(cfg).is_ok());
}

// ── DAG serialization ─────────────────────────────────────────────────────────

#[test]
fn dag_serializes_to_json() {
	let dag = make_dag(&["t1", "t2"], &[("t1", "t2")]);
	let json = serde_json::to_string(&dag).unwrap();
	assert!(json.contains("t1"));
	assert!(json.contains("t2"));
}

#[test]
fn dag_roundtrip_json() {
	let dag = make_dag(&["a", "b", "c"], &[("a", "b"), ("b", "c")]);
	let json = serde_json::to_string(&dag).unwrap();
	let decoded: TaskDAG = serde_json::from_str(&json).unwrap();
	assert_eq!(decoded.nodes.len(), 3);
	assert_eq!(decoded.edges.len(), 2);
	assert!(decoded.validate_acyclic().is_ok());
}
