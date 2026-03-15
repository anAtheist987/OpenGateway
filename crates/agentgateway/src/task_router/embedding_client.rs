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

use crate::task_router::types::AgentInfo;

pub struct EmbeddingClient {
	client: reqwest::Client,
	endpoint: String,
	model: String,
	api_key: Option<String>,
}

#[derive(Serialize)]
struct EmbedRequest<'a> {
	model: &'a str,
	input: &'a str,
}

#[derive(Deserialize)]
struct EmbedResponse {
	data: Vec<EmbedData>,
}

#[derive(Deserialize)]
struct EmbedData {
	embedding: Vec<f32>,
}

impl EmbeddingClient {
	pub fn new(endpoint: String, model: String, api_key: Option<String>) -> anyhow::Result<Self> {
		let client = reqwest::Client::builder()
			.timeout(std::time::Duration::from_secs(30))
			.build()
			.context("failed to build reqwest client for embeddings")?;
		Ok(Self {
			client,
			endpoint,
			model,
			api_key,
		})
	}

	/// Fetch the embedding vector for a single text.
	pub async fn embed(&self, text: &str) -> anyhow::Result<Vec<f32>> {
		let body = EmbedRequest {
			model: &self.model,
			input: text,
		};
		let mut req = self.client.post(&self.endpoint).json(&body);
		if let Some(key) = &self.api_key {
			req = req.bearer_auth(key);
		}
		let resp = req.send().await.context("embedding request failed")?;
		let status = resp.status();
		if !status.is_success() {
			let text = resp.text().await.unwrap_or_default();
			anyhow::bail!("embedding endpoint returned {}: {}", status, text);
		}
		let parsed: EmbedResponse = resp
			.json()
			.await
			.context("failed to parse embedding response")?;
		parsed
			.data
			.into_iter()
			.next()
			.map(|d| d.embedding)
			.context("embedding response contained no data")
	}

	/// Cosine similarity between two vectors. Returns 0.0 if either is zero-length.
	pub fn cosine_similarity(a: &[f32], b: &[f32]) -> f32 {
		let dot: f32 = a.iter().zip(b.iter()).map(|(x, y)| x * y).sum();
		let norm_a: f32 = a.iter().map(|x| x * x).sum::<f32>().sqrt();
		let norm_b: f32 = b.iter().map(|x| x * x).sum::<f32>().sqrt();
		if norm_a == 0.0 || norm_b == 0.0 {
			return 0.0;
		}
		dot / (norm_a * norm_b)
	}

	/// Find the agent whose text representation is most similar to `task`.
	/// Agent text = "{name}: {description}. Skills: {skills}"
	pub async fn find_best_agent<'a>(
		&self,
		task: &str,
		agents: &'a [AgentInfo],
	) -> anyhow::Result<(&'a AgentInfo, f32)> {
		if agents.is_empty() {
			anyhow::bail!("no agents provided for vector routing");
		}

		let task_vec = self.embed(task).await.context("failed to embed task")?;

		let mut best_agent = &agents[0];
		let mut best_score = f32::NEG_INFINITY;

		for agent in agents {
			let agent_text = format!(
				"{}: {}. Skills: {}",
				agent.name,
				agent.description,
				agent.skills.join(", ")
			);
			let agent_vec = self
				.embed(&agent_text)
				.await
				.with_context(|| format!("failed to embed agent '{}'", agent.name))?;
			let score = Self::cosine_similarity(&task_vec, &agent_vec);
			if score > best_score {
				best_score = score;
				best_agent = agent;
			}
		}

		Ok((best_agent, best_score))
	}

	/// Find top-k agents whose text representation is most similar to `task`.
	/// Returns agents sorted by similarity score (highest first).
	pub async fn find_top_k_agents<'a>(
		&self,
		task: &str,
		agents: &'a [AgentInfo],
		k: usize,
	) -> anyhow::Result<Vec<(&'a AgentInfo, f32)>> {
		if agents.is_empty() {
			anyhow::bail!("no agents provided for vector routing");
		}

		let task_vec = self.embed(task).await.context("failed to embed task")?;

		let mut scores: Vec<(&AgentInfo, f32)> = Vec::new();

		for agent in agents {
			let agent_text = format!(
				"{}: {}. Skills: {}",
				agent.name,
				agent.description,
				agent.skills.join(", ")
			);
			let agent_vec = self
				.embed(&agent_text)
				.await
				.with_context(|| format!("failed to embed agent '{}'", agent.name))?;
			let score = Self::cosine_similarity(&task_vec, &agent_vec);
			scores.push((agent, score));
		}

		// Sort by score descending and take top k
		scores.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
		Ok(scores.into_iter().take(k.min(agents.len())).collect())
	}
}

#[cfg(test)]
mod tests {
	use super::EmbeddingClient;

	#[test]
	fn cosine_same_vector() {
		let v = vec![1.0_f32, 2.0, 3.0];
		let sim = EmbeddingClient::cosine_similarity(&v, &v);
		assert!(
			(sim - 1.0).abs() < 1e-6,
			"same vector should have similarity 1.0, got {sim}"
		);
	}

	#[test]
	fn cosine_orthogonal_vectors() {
		let a = vec![1.0_f32, 0.0];
		let b = vec![0.0_f32, 1.0];
		let sim = EmbeddingClient::cosine_similarity(&a, &b);
		assert!(
			sim.abs() < 1e-6,
			"orthogonal vectors should have similarity 0.0, got {sim}"
		);
	}

	#[test]
	fn cosine_zero_vector() {
		let a = vec![0.0_f32, 0.0];
		let b = vec![1.0_f32, 2.0];
		let sim = EmbeddingClient::cosine_similarity(&a, &b);
		assert_eq!(sim, 0.0, "zero vector should return 0.0");
	}
}
