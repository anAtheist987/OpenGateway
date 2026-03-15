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

use crate::task_router::types::AgentAssignment;

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DagNode {
	pub id: String,
	pub description: String,
	#[serde(default)]
	pub required_capabilities: Vec<String>,
	#[serde(default)]
	pub assigned_agent: Option<AgentAssignment>,
	pub estimated_complexity: f32,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DagEdge {
	pub from: String,
	pub to: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TaskDAG {
	pub nodes: Vec<DagNode>,
	pub edges: Vec<DagEdge>,
}

impl TaskDAG {
	/// Returns an error if the DAG contains a cycle (DFS-based).
	pub fn validate_acyclic(&self) -> Result<(), String> {
		use std::collections::{HashMap, HashSet};

		// Build adjacency list
		let mut adj: HashMap<&str, Vec<&str>> = HashMap::new();
		for node in &self.nodes {
			adj.entry(node.id.as_str()).or_default();
		}
		for edge in &self.edges {
			adj
				.entry(edge.from.as_str())
				.or_default()
				.push(edge.to.as_str());
		}

		let mut visited: HashSet<&str> = HashSet::new();
		let mut in_stack: HashSet<&str> = HashSet::new();

		fn dfs<'a>(
			node: &'a str,
			adj: &HashMap<&'a str, Vec<&'a str>>,
			visited: &mut HashSet<&'a str>,
			in_stack: &mut HashSet<&'a str>,
		) -> bool {
			if in_stack.contains(node) {
				return true; // cycle
			}
			if visited.contains(node) {
				return false;
			}
			visited.insert(node);
			in_stack.insert(node);
			if let Some(neighbors) = adj.get(node) {
				for &next in neighbors {
					if dfs(next, adj, visited, in_stack) {
						return true;
					}
				}
			}
			in_stack.remove(node);
			false
		}

		for node in &self.nodes {
			if dfs(node.id.as_str(), &adj, &mut visited, &mut in_stack) {
				return Err(format!("DAG contains a cycle involving node '{}'", node.id));
			}
		}
		Ok(())
	}
}

#[cfg(test)]
mod tests {
	use super::*;

	fn make_dag(edges: &[(&str, &str)], node_ids: &[&str]) -> TaskDAG {
		TaskDAG {
			nodes: node_ids
				.iter()
				.map(|id| DagNode {
					id: id.to_string(),
					description: String::new(),
					required_capabilities: vec![],
					assigned_agent: None,
					estimated_complexity: 0.0,
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

	#[test]
	fn test_acyclic_dag() {
		let dag = make_dag(&[("t1", "t2"), ("t2", "t3")], &["t1", "t2", "t3"]);
		assert!(dag.validate_acyclic().is_ok());
	}

	#[test]
	fn test_cyclic_dag() {
		let dag = make_dag(
			&[("t1", "t2"), ("t2", "t3"), ("t3", "t1")],
			&["t1", "t2", "t3"],
		);
		assert!(dag.validate_acyclic().is_err());
	}

	#[test]
	fn test_empty_dag() {
		let dag = make_dag(&[], &[]);
		assert!(dag.validate_acyclic().is_ok());
	}
}
