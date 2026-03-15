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

use anyhow::Context;

use crate::task_router::dag::{DagEdge, DagNode, TaskDAG};
use crate::task_router::embedding_client::EmbeddingClient;
use crate::task_router::llm_client::InternalLLMClient;
use crate::task_router::types::{
	AgentAssignment, AgentInfo, RouteTaskRequest, RoutingDecision, RoutingResult, RoutingStrategy,
	TaskRouterConfig,
};

pub struct TaskRouter {
	llm: InternalLLMClient,
	embedding: Option<EmbeddingClient>,
	cfg: TaskRouterConfig,
}

impl TaskRouter {
	pub fn new(cfg: TaskRouterConfig) -> anyhow::Result<Self> {
		let llm = InternalLLMClient::new(&cfg)?;
		let embedding = if cfg.routing_strategy == RoutingStrategy::Vector
			|| cfg.routing_strategy == RoutingStrategy::LlmEnhancedVector
			|| cfg.routing_strategy == RoutingStrategy::VectorPrefilterLlm
		{
			let model = cfg
				.embedding_model
				.clone()
				.context("routingStrategy requires embeddingModel to be configured")?;
			let endpoint = format!("{}/embeddings", cfg.base_url.trim_end_matches('/'));
			Some(EmbeddingClient::new(endpoint, model, cfg.api_key.clone())?)
		} else {
			None
		};
		Ok(Self {
			llm,
			embedding,
			cfg,
		})
	}

	/// Build a human-readable agent list for LLM prompts.
	fn agents_description(agents: &[AgentInfo]) -> String {
		agents
			.iter()
			.map(|a| {
				format!(
					"- {}: {} (skills: {})",
					a.name,
					a.description,
					a.skills.join(", ")
				)
			})
			.collect::<Vec<_>>()
			.join("\n")
	}

	/// Assign an agent to a (sub)task description using the provided strategy.
	async fn assign_agent(
		&self,
		task_desc: &str,
		agents: &[AgentInfo],
		strategy: &RoutingStrategy,
	) -> anyhow::Result<AgentAssignment> {
		match strategy {
			RoutingStrategy::Vector => {
				let ec = self
					.embedding
					.as_ref()
					.expect("embedding client must exist for vector strategy");
				let (agent, score) = ec.find_best_agent(task_desc, agents).await?;
				Ok(AgentAssignment {
					agent_name: agent.name.clone(),
					agent_url: agent.url.clone(),
					confidence: score,
				})
			},
			RoutingStrategy::LlmEnhancedVector => {
				let ec = self
					.embedding
					.as_ref()
					.expect("embedding client must exist for llmEnhancedVector strategy");
				let enhanced = self
					.llm
					.enhance_task_for_embedding(task_desc)
					.await
					.context("LLM task enhancement failed")?;
				let (agent, score) = ec.find_best_agent(&enhanced, agents).await?;
				Ok(AgentAssignment {
					agent_name: agent.name.clone(),
					agent_url: agent.url.clone(),
					confidence: score,
				})
			},
			RoutingStrategy::VectorPrefilterLlm => {
				let ec = self
					.embedding
					.as_ref()
					.expect("embedding client must exist for vectorPrefilterLlm strategy");
				// Top-k = 3 (configurable in the future)
				let top_candidates = ec.find_top_k_agents(task_desc, agents, 3).await?;
				let candidates_desc = top_candidates
					.iter()
					.map(|(a, score)| {
						format!(
							"- {} (score: {:.3}): {} (skills: {})",
							a.name,
							score,
							a.description,
							a.skills.join(", ")
						)
					})
					.collect::<Vec<_>>()
					.join("\n");
				let sel = self
					.llm
					.select_from_candidates(task_desc, &candidates_desc)
					.await?;
				let agent = agents
					.iter()
					.find(|a| a.name == sel.agent_name)
					.with_context(|| format!("LLM selected unknown agent '{}'", sel.agent_name))?;
				Ok(AgentAssignment {
					agent_name: agent.name.clone(),
					agent_url: agent.url.clone(),
					confidence: sel.confidence,
				})
			},
			RoutingStrategy::Llm => {
				let agents_desc = Self::agents_description(agents);
				let sel = self.llm.select_agent(task_desc, &agents_desc).await?;
				let agent = agents
					.iter()
					.find(|a| a.name == sel.agent_name)
					.with_context(|| format!("LLM selected unknown agent '{}'", sel.agent_name))?;
				Ok(AgentAssignment {
					agent_name: agent.name.clone(),
					agent_url: agent.url.clone(),
					confidence: sel.confidence,
				})
			},
		}
	}

	/// Route a task, returning a full RoutingResult.
	pub async fn route(&self, req: RouteTaskRequest) -> anyhow::Result<RoutingResult> {
		let task_id = req
			.task_id
			.unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
		let effective_strategy = req
			.strategy_override
			.as_ref()
			.unwrap_or(&self.cfg.routing_strategy);

		// Step 1: assess complexity
		let complexity = self
			.llm
			.assess_complexity(&req.task)
			.await
			.context("complexity assessment failed")?;

		let agents_desc = Self::agents_description(&req.agents);

		let decision = if complexity.score >= self.cfg.complexity_threshold {
			// Step 2a: decompose into sub-tasks (task only, no agent list)
			let decomp = self
				.llm
				.decompose_task(&req.task, self.cfg.max_subtasks)
				.await
				.context("task decomposition failed")?;

			// Step 2b: assign each node using the effective strategy
			let mut dag_nodes: Vec<DagNode> = Vec::with_capacity(decomp.nodes.len());
			for node in &decomp.nodes {
				let assignment = self
					.assign_agent(&node.description, &req.agents, effective_strategy)
					.await
					.ok();

				dag_nodes.push(DagNode {
					id: node.id.clone(),
					description: node.description.clone(),
					required_capabilities: node.required_capabilities.clone(),
					assigned_agent: assignment,
					estimated_complexity: node.estimated_complexity,
				});
			}

			let dag = TaskDAG {
				nodes: dag_nodes,
				edges: decomp
					.edges
					.into_iter()
					.map(|e| DagEdge {
						from: e.from,
						to: e.to,
					})
					.collect(),
			};

			dag
				.validate_acyclic()
				.map_err(|e| anyhow::anyhow!("LLM produced cyclic DAG: {e}"))?;

			RoutingDecision::Decomposed {
				dag,
				reason: decomp.reason,
			}
		} else {
			// Direct routing via effective strategy
			let (agent_name, agent_url, confidence, reason) = match effective_strategy {
				RoutingStrategy::Vector => {
					let ec = self
						.embedding
						.as_ref()
						.expect("embedding client must exist for vector strategy");
					let (agent, score) = ec
						.find_best_agent(&req.task, &req.agents)
						.await
						.context("vector agent selection failed")?;
					(
						agent.name.clone(),
						agent.url.clone(),
						score,
						format!("vector similarity: {:.4}", score),
					)
				},
				RoutingStrategy::LlmEnhancedVector => {
					let ec = self
						.embedding
						.as_ref()
						.expect("embedding client must exist for llmEnhancedVector strategy");
					let enhanced = self
						.llm
						.enhance_task_for_embedding(&req.task)
						.await
						.context("LLM task enhancement failed")?;
					let (agent, score) = ec
						.find_best_agent(&enhanced, &req.agents)
						.await
						.context("vector agent selection failed after LLM enhancement")?;
					(
						agent.name.clone(),
						agent.url.clone(),
						score,
						format!("llm-enhanced vector similarity: {:.4}", score),
					)
				},
				RoutingStrategy::VectorPrefilterLlm => {
					let ec = self
						.embedding
						.as_ref()
						.expect("embedding client must exist for vectorPrefilterLlm strategy");
					// Top-k = 3
					let top_candidates = ec
						.find_top_k_agents(&req.task, &req.agents, 3)
						.await
						.context("vector prefiltering failed")?;
					let candidates_desc = top_candidates
						.iter()
						.map(|(a, score)| {
							format!(
								"- {} (similarity: {:.3}): {} (skills: {})",
								a.name,
								score,
								a.description,
								a.skills.join(", ")
							)
						})
						.collect::<Vec<_>>()
						.join("\n");
					let sel = self
						.llm
						.select_from_candidates(&req.task, &candidates_desc)
						.await
						.context("LLM selection from candidates failed")?;
					let agent = req
						.agents
						.iter()
						.find(|a| a.name == sel.agent_name)
						.with_context(|| {
							format!(
								"LLM selected unknown agent '{}'; available: {}",
								sel.agent_name,
								req
									.agents
									.iter()
									.map(|a| a.name.as_str())
									.collect::<Vec<_>>()
									.join(", ")
							)
						})?;
					(
						agent.name.clone(),
						agent.url.clone(),
						sel.confidence,
						sel.reason,
					)
				},
				RoutingStrategy::Llm => {
					let sel = self
						.llm
						.select_agent(&req.task, &agents_desc)
						.await
						.context("agent selection failed")?;
					let agent = req
						.agents
						.iter()
						.find(|a| a.name == sel.agent_name)
						.with_context(|| {
							format!(
								"LLM selected unknown agent '{}'; available: {}",
								sel.agent_name,
								req
									.agents
									.iter()
									.map(|a| a.name.as_str())
									.collect::<Vec<_>>()
									.join(", ")
							)
						})?;
					(
						agent.name.clone(),
						agent.url.clone(),
						sel.confidence,
						sel.reason,
					)
				},
			};

			RoutingDecision::Direct {
				agent_name,
				agent_url,
				confidence,
				reason,
			}
		};

		Ok(RoutingResult {
			task_id,
			complexity_score: complexity.score,
			decision,
		})
	}
}
