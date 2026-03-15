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
use serde::{Deserialize, Serialize};

use crate::task_router::types::TaskRouterConfig;

pub struct InternalLLMClient {
	client: reqwest::Client,
	endpoint: String,
	model: String,
	api_key: Option<String>,
}

#[derive(Deserialize)]
struct ChatResponse {
	choices: Vec<Choice>,
}

#[derive(Deserialize)]
struct Choice {
	message: Message,
}

#[derive(Deserialize)]
struct Message {
	content: Option<String>,
}

#[derive(Serialize)]
struct ChatRequest<'a> {
	model: &'a str,
	messages: Vec<ChatMessage<'a>>,
	temperature: f32,
}

#[derive(Serialize)]
struct ChatMessage<'a> {
	role: &'a str,
	content: &'a str,
}

impl InternalLLMClient {
	pub fn new(cfg: &TaskRouterConfig) -> anyhow::Result<Self> {
		let client = reqwest::Client::builder()
			.timeout(std::time::Duration::from_secs(100))
			.build()
			.context("failed to build reqwest client")?;
		let endpoint = format!("{}/chat/completions", cfg.base_url.trim_end_matches('/'));
		Ok(Self {
			client,
			endpoint,
			model: cfg.planner_model.clone(),
			api_key: cfg.api_key.clone(),
		})
	}

	/// Send a single system+user prompt and return the raw text content.
	pub async fn chat(&self, system: &str, user: &str) -> anyhow::Result<String> {
		let body = ChatRequest {
			model: &self.model,
			messages: vec![
				ChatMessage {
					role: "system",
					content: system,
				},
				ChatMessage {
					role: "user",
					content: user,
				},
			],
			temperature: 0.0,
		};

		let mut req = self.client.post(&self.endpoint).json(&body);
		if let Some(key) = &self.api_key {
			req = req.bearer_auth(key);
		}

		let resp = req.send().await.context("LLM request failed")?;
		let status = resp.status();
		if !status.is_success() {
			let text = resp.text().await.unwrap_or_default();
			anyhow::bail!("LLM returned {}: {}", status, text);
		}

		let chat: ChatResponse = resp.json().await.context("failed to parse LLM response")?;
		chat
			.choices
			.into_iter()
			.next()
			.and_then(|c| c.message.content)
			.context("LLM response had no content")
	}

	/// Parse JSON from LLM output, stripping markdown fences if present.
	pub fn extract_json(raw: &str) -> &str {
		let trimmed = raw.trim();
		// Strip ```json ... ``` or ``` ... ```
		if let Some(inner) = trimmed
			.strip_prefix("```json")
			.or_else(|| trimmed.strip_prefix("```"))
			&& let Some(end) = inner.rfind("```")
		{
			return inner[..end].trim();
		}
		trimmed
	}
}

/// Parsed complexity assessment from LLM.
#[derive(Deserialize)]
pub struct ComplexityResult {
	pub score: f32,
	pub reason: String,
}

/// Parsed routing selection from LLM.
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct RouteSelection {
	pub agent_name: String,
	pub confidence: f32,
	pub reason: String,
}

/// Parsed DAG decomposition from LLM.
#[derive(Deserialize)]
pub struct DecompositionResult {
	pub nodes: Vec<DecomposedNode>,
	pub edges: Vec<DecomposedEdge>,
	pub reason: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DecomposedNode {
	pub id: String,
	pub description: String,
	#[serde(default)]
	pub required_capabilities: Vec<String>,
	#[serde(default = "default_complexity")]
	pub estimated_complexity: f32,
}

fn default_complexity() -> f32 {
	0.5
}

#[derive(Deserialize)]
pub struct DecomposedEdge {
	pub from: String,
	pub to: String,
}

impl InternalLLMClient {
	pub async fn assess_complexity(&self, task: &str) -> anyhow::Result<ComplexityResult> {
		let system = r#"You are a task complexity assessor. Evaluate if the task requires multiple steps/stages or different skill sets.

Complexity factors (increase score):
- Multiple sequential steps (fetch → analyze → visualize)
- Different required skills (SQL, ML, charting)
- Task decomposition needed (one agent cannot complete all steps)
- Cross-domain requirements (data + analysis + presentation)

Complexity scale:
0.0-0.3 = Single-step, one skill, one agent can do all
0.4-0.6 = Slightly complex but one agent might handle it
0.7-1.0 = Multi-step, multiple skills, needs decomposition and multiple agents

Output JSON only:
{"score": <float 0.0-1.0>, "reason": "<brief reason>"}

IMPORTANT: If task mentions "first...then...finally" or similar sequential keywords, score should be >= 0.7"#;

		let raw = self.chat(system, task).await?;
		let json_str = Self::extract_json(&raw);
		serde_json::from_str(json_str)
			.with_context(|| format!("failed to parse complexity JSON: {json_str}"))
	}

	pub async fn decompose_task(
		&self,
		task: &str,
		max_subtasks: usize,
	) -> anyhow::Result<DecompositionResult> {
		let system = format!(
			r#"Decompose the complex task into a DAG of independent sub-tasks. Output JSON only:
{{
  "nodes": [{{"id":"t1","description":"...","requiredCapabilities":["cap1"],"estimatedComplexity":0.3}}],
  "edges": [{{"from":"t1","to":"t2"}}],
  "reason": "..."
}}
Max {max_subtasks} nodes. No cycles.
Focus on WHAT needs to be done for each sub-task, not which agent to use.
requiredCapabilities should describe the skill domain needed for the sub-task."#
		);

		let user = format!("Task: {task}");
		let raw = self.chat(&system, &user).await?;
		let json_str = Self::extract_json(&raw);
		serde_json::from_str(json_str)
			.with_context(|| format!("failed to parse decomposition JSON: {json_str}"))
	}

	pub async fn select_agent(
		&self,
		task_desc: &str,
		agents_desc: &str,
	) -> anyhow::Result<RouteSelection> {
		let system = r#"Select the best agent for the task. Output JSON only:
{"agentName": "...", "confidence": 0.0-1.0, "reason": "..."}"#;

		let user = format!("Agents:\n{agents_desc}\n\nTask: {task_desc}");
		let raw = self.chat(system, &user).await?;
		let json_str = Self::extract_json(&raw);
		serde_json::from_str(json_str)
			.with_context(|| format!("failed to parse route selection JSON: {json_str}"))
	}

	/// Polish the task description into a keyword-rich phrase for embedding matching.
	pub async fn enhance_task_for_embedding(&self, task: &str) -> anyhow::Result<String> {
		let system = r#"Extract the core intent and key capabilities required from the task description.
Output a concise, keyword-rich phrase (1-2 sentences max) that best represents what kind of agent is needed.
Output plain text only, no JSON, no markdown."#;

		self.chat(system, task).await
	}

	/// Select best agent from candidates. Used for vector-prefiltered candidates.
	pub async fn select_from_candidates(
		&self,
		task_desc: &str,
		candidates_desc: &str,
	) -> anyhow::Result<RouteSelection> {
		let system = r#"Select the best agent from the provided candidates for the task. Output JSON only:
{"agentName": "...", "confidence": 0.0-1.0, "reason": "..."}"#;

		let user = format!("Candidate agents:\n{candidates_desc}\n\nTask: {task_desc}");
		let raw = self.chat(system, &user).await?;
		let json_str = Self::extract_json(&raw);
		serde_json::from_str(json_str)
			.with_context(|| format!("failed to parse route selection JSON: {json_str}"))
	}
}
