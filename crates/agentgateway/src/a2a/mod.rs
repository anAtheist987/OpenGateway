// Modified by Tsinghua University, 2026
// Original source: https://github.com/agentgateway/agentgateway
// Licensed under the Apache License, Version 2.0

use http::{Request, Uri, header};
use serde_json::Value;
use tracing::warn;

use crate::http::{Body, Response, filters};
use crate::json;
use crate::types::agent::A2aPolicy;

pub async fn apply_to_request(
	_: &A2aPolicy,
	req: &mut Request<Body>,
) -> (RequestType, Option<String>) {
	// Possible options are POST a JSON-RPC message or GET /.well-known/agent.json
	// For agent card, we will process only on the response
	classify_request(req).await
}

async fn classify_request(req: &mut Request<Body>) -> (RequestType, Option<String>) {
	// Possible options are POST a JSON-RPC message or GET /.well-known/agent.json
	// For agent card, we will process only on the response
	match (req.method(), req.uri().path()) {
		// agent-card.json: v0.3.0+
		// agent.json: older versions
		(m, "/.well-known/agent.json" | "/.well-known/agent-card.json") if m == http::Method::GET => {
			// In case of rewrite, use the original so we know where to send them back to
			let uri = req
				.extensions()
				.get::<filters::OriginalUrl>()
				.map(|u| u.0.clone())
				.unwrap_or_else(|| req.uri().clone());
			(RequestType::AgentCard(uri), None)
		},
		(m, _) if m == http::Method::POST => {
			let (method, task_text) = match crate::http::classify_content_type(req.headers()) {
				crate::http::WellKnownContentTypes::Json => {
					match json::inspect_body::<a2a_sdk::A2aRequest>(req).await {
						Ok(call) => {
							let text = extract_task_text(&call);
							(call.method(), text)
						},
						Err(e) => {
							warn!("failed to read a2a request: {e}");
							("unknown", None)
						},
					}
				},
				_ => {
					warn!("unknown content type from A2A");
					("unknown", None)
				},
			};
			(RequestType::Call(method), task_text)
		},
		_ => (RequestType::Unknown, None),
	}
}

fn extract_task_text(call: &a2a_sdk::A2aRequest) -> Option<String> {
	let msg = match call {
		a2a_sdk::A2aRequest::SendTaskRequest(r) => &r.params.message,
		a2a_sdk::A2aRequest::SendSubscribeTaskRequest(r) => &r.params.message,
		a2a_sdk::A2aRequest::SendMessageRequest(r) => &r.params.message,
		a2a_sdk::A2aRequest::SendStreamingMessageRequest(r) => &r.params.message,
		_ => return None,
	};
	let parts: &[a2a_sdk::Part] = if !msg.content.is_empty() {
		&msg.content
	} else if let Some(legacy) = &msg.content_legacy {
		legacy
	} else {
		return None;
	};
	let texts: Vec<&str> = parts
		.iter()
		.filter_map(|p| {
			if let a2a_sdk::Part::Text(t) = p {
				Some(t.text.as_str())
			} else {
				None
			}
		})
		.collect();
	if texts.is_empty() {
		None
	} else {
		Some(texts.join("\n"))
	}
}

/// Extract task text from a raw JSON A2A request body.
///
/// Handles both the Python A2A SDK field name (`parts`) and the Rust SDK name
/// (`content` / `content_legacy`), so it works regardless of which client is sending.
pub fn try_extract_text_from_value(v: &serde_json::Value) -> Option<String> {
	let message = v.get("params")?.get("message")?;
	for field in &["parts", "content", "content_legacy"] {
		if let Some(arr) = message.get(field).and_then(|f| f.as_array()) {
			let texts: Vec<&str> = arr
				.iter()
				.filter_map(|p| p.get("text").and_then(|t| t.as_str()))
				.collect();
			if !texts.is_empty() {
				return Some(texts.join("\n"));
			}
		}
	}
	None
}

#[derive(Debug, Clone, Default)]
pub enum RequestType {
	#[default]
	Unknown,
	AgentCard(http::Uri),
	Call(&'static str),
}

pub async fn apply_to_response(
	pol: Option<&A2aPolicy>,
	a2a_type: RequestType,
	resp: &mut Response,
) -> anyhow::Result<()> {
	if pol.is_none() {
		return Ok(());
	};
	match a2a_type {
		RequestType::AgentCard(uri) => {
			// For agent card, we need to mutate the request to insert the proper URL to reach it
			// through the gateway.
			let buffer_limit = crate::http::response_buffer_limit(resp);
			let body = std::mem::replace(resp.body_mut(), Body::empty());
			let Ok(mut agent_card) = json::from_body_with_limit::<Value>(body, buffer_limit).await else {
				anyhow::bail!("agent card invalid JSON");
			};
			let Some(url_field) = json::traverse_mut(&mut agent_card, &["url"]) else {
				anyhow::bail!("agent card missing URL");
			};
			let new_uri = build_agent_path(uri);

			*url_field = Value::String(new_uri);

			resp.headers_mut().remove(header::CONTENT_LENGTH);
			*resp.body_mut() = json::to_body(agent_card)?;
			Ok(())
		},
		RequestType::Call(_) => {
			//TODO
			// match crate::http::classify_content_type(resp.headers()) {
			// 	crate::http::WellKnownContentTypes::Json => {
			// 		let buffer_limit = crate::http::response_buffer_limit(resp);
			// 		let body = std::mem::replace(resp.body_mut(), Body::empty());
			// 		let Ok(mut v) = json::from_body_with_limit::<Value>(body, buffer_limit).await else {
			// 			warn!("failed to parse JSON-RPC message from A2A response");
			// 			return Ok(());
			// 		};
			// 		if let Some(kind) = json::traverse(&v, &["result", "kind"]).and_then(|k| k.as_str()) {
			// 			if kind == "message" {
			// 				let mut texts = Vec::new();
			// 				if let Some(parts) = json::traverse(&v, &["result", "parts"]).and_then(|p| p.as_array()) {
			// 					for part in parts {
			// 						if let Some(text) = part.get("text").and_then(|t| t.as_str()) {
			// 							texts.push(text.to_string());
			// 						}
			// 					}
			// 				}
			// 				let joined = texts.join("\n");
			// 				resp.headers_mut().remove(header::CONTENT_LENGTH);
			// 				resp.headers_mut().insert(
			// 					header::CONTENT_TYPE,
			// 					header::HeaderValue::from_static("text/plain; charset=utf-8"),
			// 				);
			// 				*resp.body_mut() = Body::from(joined.into_bytes());
			// 			}
			// 		}
			// 		Ok(())
			// 	},
			// 	_ => {
			// 		warn!("unknown content type from A2A");
			// 		Ok(())
			// 	}
			Ok(())
		},
		RequestType::Unknown => Ok(()),
	}
}

fn build_agent_path(uri: Uri) -> String {
	// Keep the original URL the found the agent at, but strip the agent card suffix.
	// Note: this won't work in the case they are hosting their agent in other locations.
	let path = uri.path();
	let path = path.strip_suffix("/.well-known/agent.json").unwrap_or(path);
	let path = path
		.strip_suffix("/.well-known/agent-card.json")
		.unwrap_or(path);

	uri.to_string().replace(uri.path(), path)
}

#[cfg(test)]
#[path = "tests.rs"]
mod tests;
