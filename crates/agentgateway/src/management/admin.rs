// Modified by Tsinghua University, 2026
// Original source: https://github.com/agentgateway/agentgateway
// Licensed under the Apache License, Version 2.0

// Originally derived from https://github.com/istio/ztunnel (Apache 2.0 licensed)

use std::collections::HashMap;
use std::net::SocketAddr;
use std::str::FromStr;
use std::sync::Arc;
use std::time::Duration;

use agent_core::drain::DrainWatcher;
use agent_core::version::BuildInfo;
use agent_core::{signal, telemetry};
use http_body_util::BodyExt;
use hyper::Request;
use hyper::body::Incoming;
use hyper::header::{CONTENT_TYPE, HeaderValue};
use tokio::runtime::Handle;
use tokio::time;
use tracing::{info, warn};
use tracing_subscriber::filter;

use super::hyper_helpers::{Server, empty_response, plaintext_response};
use crate::Config;
use crate::http::Response;
use crate::task_router::{RouteTaskRequest, TaskRouter};

pub trait ConfigDumpHandler: Sync + Send {
	fn key(&self) -> &'static str;
	// sadly can't use async trait because no Sync
	// see: https://github.com/dtolnay/async-trait/issues/248, https://github.com/dtolnay/async-trait/issues/142
	// we can't use FutureExt::shared because our result is not clonable
	fn handle(&self) -> anyhow::Result<serde_json::Value>;
}

pub type AdminResponse = std::pin::Pin<Box<dyn Future<Output = crate::http::Response> + Send>>;

pub trait AdminFallback: Sync + Send {
	// sadly can't use async trait because no Sync
	// see: https://github.com/dtolnay/async-trait/issues/248, https://github.com/dtolnay/async-trait/issues/142
	// we can't use FutureExt::shared because our result is not clonable
	fn handle(&self, req: http::Request<Incoming>) -> AdminResponse;
}

struct State {
	stores: crate::store::Stores,
	config: Arc<Config>,
	shutdown_trigger: signal::ShutdownTrigger,
	config_dump_handlers: Vec<Arc<dyn ConfigDumpHandler>>,
	admin_fallback: Option<Arc<dyn AdminFallback>>,
	dataplane_handle: Handle,
	knowledge: Option<Arc<crate::knowledge::KnowledgeHandle>>,
}

pub struct Service {
	s: Server<State>,
}

#[derive(serde::Serialize, Clone)]
#[serde(rename_all = "camelCase")]
pub struct ConfigDump {
	#[serde(flatten)]
	stores: crate::store::Stores,
	version: BuildInfo,
	config: Arc<Config>,
}

#[derive(serde::Serialize, Debug, Clone, Default)]
#[serde(rename_all = "camelCase")]
pub struct CertDump {
	// Not available via Envoy, but still useful.
	pem: String,
	serial_number: String,
	valid_from: String,
	expiration_time: String,
}

#[derive(serde::Serialize, Debug, Clone, Default)]
#[serde(rename_all = "camelCase")]
pub struct CertsDump {
	identity: String,
	state: String,
	cert_chain: Vec<CertDump>,
	root_certs: Vec<CertDump>,
}

impl Service {
	pub async fn new(
		config: Arc<Config>,
		stores: crate::store::Stores,
		shutdown_trigger: signal::ShutdownTrigger,
		drain_rx: DrainWatcher,
		dataplane_handle: Handle,
	) -> anyhow::Result<Self> {
		Server::<State>::bind(
			"admin",
			config.admin_addr,
			drain_rx,
			State {
				config,
				stores,
				shutdown_trigger,
				config_dump_handlers: vec![],
				admin_fallback: None,
				dataplane_handle,
				knowledge: None,
			},
		)
		.await
		.map(|s| Service { s })
	}

	pub fn address(&self) -> SocketAddr {
		self.s.address()
	}

	pub fn set_knowledge(&mut self, knowledge: Arc<crate::knowledge::KnowledgeHandle>) {
		self.s.state_mut().knowledge = Some(knowledge);
	}

	pub fn add_config_dump_handler(&mut self, handler: Arc<dyn ConfigDumpHandler>) {
		self.s.state_mut().config_dump_handlers.push(handler);
	}

	pub fn set_admin_handler(&mut self, handler: Arc<dyn AdminFallback>) {
		self.s.state_mut().admin_fallback = Some(handler);
	}

	pub fn spawn(self) {
		self.s.spawn(|state, req| async move {
			match req.uri().path() {
				#[cfg(target_os = "linux")]
				"/debug/pprof/profile" => handle_pprof(req).await,
				#[cfg(target_os = "linux")]
				"/debug/pprof/heap" => handle_jemalloc_pprof_heapgen(req).await,
				"/quitquitquit" => Ok(
					handle_server_shutdown(
						state.shutdown_trigger.clone(),
						req,
						state.config.termination_min_deadline,
					)
					.await,
				),
				"/debug/tasks" => handle_tokio_tasks(req, &state.dataplane_handle).await,
				"/config_dump" => {
					handle_config_dump(
						&state.config_dump_handlers,
						ConfigDump {
							stores: state.stores.clone(),
							version: BuildInfo::new(),
							config: state.config.clone(),
						},
					)
					.await
				},
				"/logging" => Ok(handle_logging(req).await),
				"/task-router/route" => {
					handle_route_task(&state.config, state.knowledge.clone(), req).await
				},
				"/task-router/stats" => Ok(handle_router_stats(state.knowledge.clone()).await),
				"/task-router/traces" => Ok(handle_router_traces(state.knowledge.clone(), req).await),
				"/task-router/execution" => handle_route_execution(state.knowledge.clone(), req).await,
				"/knowledge/stats" => Ok(handle_knowledge_stats(state.knowledge.clone()).await),
				"/knowledge/working_memory" => Ok(handle_knowledge_wm(state.knowledge.clone()).await),
				"/knowledge/sessions" => Ok(handle_knowledge_sessions(state.knowledge.clone()).await),
				"/knowledge/corrections" => {
					Ok(handle_knowledge_corrections(state.knowledge.clone(), req).await)
				},
				_ => {
					if let Some(h) = &state.admin_fallback {
						Ok(h.handle(req).await)
					} else if req.uri().path() == "/" {
						Ok(handle_dashboard(req).await)
					} else {
						Ok(empty_response(hyper::StatusCode::NOT_FOUND))
					}
				},
			}
		})
	}
}

async fn handle_dashboard(_req: Request<Incoming>) -> Response {
	let apis = &[
		(
			"debug/pprof/profile",
			"build profile using the pprof profiler (if supported)",
		),
		(
			"debug/pprof/heap",
			"collect heap profiling data (if supported, requires jmalloc)",
		),
		("quitquitquit", "shut down the server"),
		("config_dump", "dump the current agentgateway configuration"),
		("logging", "query/changing logging levels"),
	];

	let mut api_rows = String::new();

	for (index, (path, description)) in apis.iter().copied().enumerate() {
		api_rows.push_str(&format!(
            "<tr class=\"{row_class}\"><td class=\"home-data\"><a href=\"{path}\">{path}</a></td><td class=\"home-data\">{description}</td></tr>\n",
            row_class = if index % 2 == 1 { "gray" } else { "vert-space" },
            path = path,
            description = description
        ));
	}

	let html_str = include_str!("../assets/dashboard.html");
	let html_str = html_str.replace("<!--API_ROWS_PLACEHOLDER-->", &api_rows);

	let mut response = plaintext_response(hyper::StatusCode::OK, html_str);
	response.headers_mut().insert(
		CONTENT_TYPE,
		HeaderValue::from_static("text/html; charset=utf-8"),
	);

	response
}

#[cfg(target_os = "linux")]
async fn handle_pprof(_req: Request<Incoming>) -> anyhow::Result<Response> {
	use pprof::protos::Message;
	let guard = pprof::ProfilerGuardBuilder::default()
		.frequency(1000)
		// .blocklist(&["libc", "libgcc", "pthread", "vdso"])
		.build()?;

	tokio::time::sleep(Duration::from_secs(10)).await;
	let report = guard.report().build()?;
	let profile = report.pprof()?;

	let body = profile.write_to_bytes()?;

	Ok(
		::http::Response::builder()
			.status(hyper::StatusCode::OK)
			.body(body.into())
			.expect("builder with known status code should not fail"),
	)
}

async fn handle_server_shutdown(
	shutdown_trigger: signal::ShutdownTrigger,
	_req: Request<Incoming>,
	self_term_wait: Duration,
) -> Response {
	match *_req.method() {
		hyper::Method::POST => {
			match time::timeout(self_term_wait, shutdown_trigger.shutdown_now()).await {
				Ok(()) => info!("Shutdown completed gracefully"),
				Err(_) => warn!(
					"Graceful shutdown did not complete in {:?}, terminating now",
					self_term_wait
				),
			}
			plaintext_response(hyper::StatusCode::OK, "shutdown now\n".into())
		},
		_ => empty_response(hyper::StatusCode::METHOD_NOT_ALLOWED),
	}
}

#[cfg(target_os = "linux")]
#[derive(serde::Serialize)]
struct TaskDump {
	admin: Vec<String>,
	workload: Vec<String>,
}

#[cfg(target_os = "linux")]
async fn handle_tokio_tasks(
	_req: Request<Incoming>,
	dataplane_handle: &Handle,
) -> anyhow::Result<Response> {
	let mut task_dump = TaskDump {
		admin: Vec::new(),
		workload: Vec::new(),
	};

	let handle = tokio::runtime::Handle::current();
	if let Ok(dump) = tokio::time::timeout(Duration::from_secs(5), handle.dump()).await {
		for task in dump.tasks().iter() {
			let trace = task.trace();
			task_dump.admin.push(trace.to_string());
		}
	} else {
		task_dump
			.admin
			.push("failed to dump admin workload tasks".to_string());
	}

	if let Ok(dump) = tokio::time::timeout(Duration::from_secs(10), dataplane_handle.dump()).await {
		for task in dump.tasks().iter() {
			let trace = task.trace();
			task_dump.workload.push(trace.to_string());
		}
	} else {
		task_dump
			.workload
			.push("failed to dump workload tasks".to_string());
	}

	let json_body = serde_json::to_string(&task_dump)?;

	Ok(
		::http::Response::builder()
			.status(hyper::StatusCode::OK)
			.header("Content-Type", "application/json")
			.body(json_body.into())
			.expect("builder with known status code should not fail"),
	)
}

#[cfg(not(target_os = "linux"))]
async fn handle_tokio_tasks(
	_req: Request<Incoming>,
	_dataplane_handle: &Handle,
) -> anyhow::Result<Response> {
	Ok(
		::http::Response::builder()
			.status(hyper::StatusCode::INTERNAL_SERVER_ERROR)
			.body("task dump is not available".into())
			.expect("builder with known status code should not fail"),
	)
}

async fn handle_config_dump(
	handlers: &[Arc<dyn ConfigDumpHandler>],
	dump: ConfigDump,
) -> anyhow::Result<Response> {
	let serde_json::Value::Object(mut kv) = serde_json::to_value(&dump)? else {
		anyhow::bail!("config dump is not a key-value pair")
	};

	for h in handlers {
		let x = h.handle()?;
		kv.insert(h.key().to_string(), x);
	}
	let body = serde_json::to_string_pretty(&kv)?;
	Ok(
		::http::Response::builder()
			.status(hyper::StatusCode::OK)
			.header(hyper::header::CONTENT_TYPE, "application/json")
			.body(body.into())
			.expect("builder with known status code should not fail"),
	)
}

// mirror envoy's behavior: https://www.envoyproxy.io/docs/envoy/latest/operations/admin#post--logging
// NOTE: multiple query parameters is not supported, for example
// curl -X POST http://127.0.0.1:15000/logging?"tap=debug&router=debug"
static HELP_STRING: &str = "
usage: POST /logging\t\t\t\t\t\t(To list current level)
usage: POST /logging?level=<level>\t\t\t\t(To change global levels)
usage: POST /logging?level={mod1}:{level1},{mod2}:{level2}\t(To change specific mods' logging level)

hint: loglevel:\terror|warn|info|debug|trace|off
hint: mod_name:\tthe module name, i.e. ztunnel::agentgateway
";
async fn handle_logging(req: Request<Incoming>) -> Response {
	match *req.method() {
		hyper::Method::POST => {
			let qp: HashMap<String, String> = req
				.uri()
				.query()
				.map(|v| {
					url::form_urlencoded::parse(v.as_bytes())
						.into_owned()
						.collect()
				})
				.unwrap_or_default();
			let level = qp.get("level").cloned();
			let reset = qp.get("reset").cloned();
			if level.is_some() || reset.is_some() {
				change_log_level(reset.is_some(), &level.unwrap_or_default())
			} else {
				list_loggers()
			}
		},
		_ => plaintext_response(
			hyper::StatusCode::METHOD_NOT_ALLOWED,
			format!("Invalid HTTP method\n {HELP_STRING}"),
		),
	}
}

fn list_loggers() -> Response {
	match telemetry::get_current_loglevel() {
		Ok(loglevel) => plaintext_response(
			hyper::StatusCode::OK,
			format!("current log level is {loglevel}\n"),
		),
		Err(err) => plaintext_response(
			hyper::StatusCode::INTERNAL_SERVER_ERROR,
			format!("failed to get the log level: {err}\n {HELP_STRING}"),
		),
	}
}

fn validate_log_level(level: &str) -> anyhow::Result<()> {
	for clause in level.split(',') {
		// We support 2 forms, compared to the underlying library
		// <level>: supported, sets the default
		// <scope>:<level>: supported, sets a scope's level
		// <scope>: sets the scope to 'trace' level. NOT SUPPORTED.
		match clause {
			"off" | "error" | "warn" | "info" | "debug" | "trace" => continue,
			s if s.contains('=') => {
				filter::Targets::from_str(s)?;
			},
			s => anyhow::bail!("level {s} is invalid"),
		}
	}
	Ok(())
}

fn change_log_level(reset: bool, level: &str) -> Response {
	if !reset && level.is_empty() {
		return list_loggers();
	}
	if !level.is_empty()
		&& let Err(_e) = validate_log_level(level)
	{
		// Invalid level provided
		return plaintext_response(
			hyper::StatusCode::BAD_REQUEST,
			format!("Invalid level provided: {level}\n{HELP_STRING}"),
		);
	};
	match telemetry::set_level(reset, level) {
		Ok(_) => list_loggers(),
		Err(e) => plaintext_response(
			hyper::StatusCode::BAD_REQUEST,
			format!("Failed to set new level: {e}\n{HELP_STRING}"),
		),
	}
}

#[cfg(all(feature = "jemalloc", target_os = "linux"))]
async fn handle_jemalloc_pprof_heapgen(_req: Request<Incoming>) -> anyhow::Result<Response> {
	let Some(prof_ctrl) = jemalloc_pprof::PROF_CTL.as_ref() else {
		return Ok(
			::http::Response::builder()
				.status(hyper::StatusCode::INTERNAL_SERVER_ERROR)
				.body("jemalloc profiling is not enabled".into())
				.expect("builder with known status code should not fail"),
		);
	};
	let mut prof_ctl = prof_ctrl.lock().await;
	if !prof_ctl.activated() {
		return Ok(
			::http::Response::builder()
				.status(hyper::StatusCode::INTERNAL_SERVER_ERROR)
				.body("jemalloc not enabled".into())
				.expect("builder with known status code should not fail"),
		);
	}
	let pprof = prof_ctl.dump_pprof()?;
	Ok(
		::http::Response::builder()
			.status(hyper::StatusCode::OK)
			.body(bytes::Bytes::from(pprof).into())
			.expect("builder with known status code should not fail"),
	)
}

#[cfg(all(not(feature = "jemalloc"), target_os = "linux"))]
async fn handle_jemalloc_pprof_heapgen(_req: Request<Incoming>) -> anyhow::Result<Response> {
	Ok(
		::http::Response::builder()
			.status(hyper::StatusCode::INTERNAL_SERVER_ERROR)
			.body("jemalloc not enabled".into())
			.expect("builder with known status code should not fail"),
	)
}

async fn handle_route_task(
	config: &Config,
	knowledge: Option<Arc<crate::knowledge::KnowledgeHandle>>,
	req: Request<Incoming>,
) -> anyhow::Result<Response> {
	if *req.method() != hyper::Method::POST {
		return Ok(empty_response(hyper::StatusCode::METHOD_NOT_ALLOWED));
	}

	let Some(task_router_cfg) = &config.task_router else {
		return Ok(
			::http::Response::builder()
				.status(hyper::StatusCode::NOT_IMPLEMENTED)
				.header(hyper::header::CONTENT_TYPE, "application/json")
				.body(r#"{"error":"task_router is not configured"}"#.into())
				.expect("builder with known status code should not fail"),
		);
	};

	// Read body
	let body_bytes = req
		.into_body()
		.collect()
		.await
		.map_err(|e| anyhow::anyhow!("failed to read request body: {e}"))?
		.to_bytes();

	let route_req: RouteTaskRequest = serde_json::from_slice(&body_bytes)
		.map_err(|e| anyhow::anyhow!("invalid request body: {e}"))?;

	let router = TaskRouter::new(task_router_cfg.clone())
		.map_err(|e| anyhow::anyhow!("failed to create task router: {e}"))?;

	// Capture fields before req is moved
	let strategy_override = route_req.strategy_override.clone();
	let config_strategy = &task_router_cfg.routing_strategy;
	let original_task = route_req.task.clone();

	let mut req = route_req;
	if req.task_id.is_none() {
		req.task_id = Some(uuid::Uuid::new_v4().to_string());
	}

	let start = std::time::Instant::now();
	let result = router
		.route(req)
		.await
		.map_err(|e| anyhow::anyhow!("routing failed: {e}"))?;
	let latency_ms = start.elapsed().as_millis() as u64;

	// Persist routing entry to KnowledgeStore (SR-4, extended)
	if let Some(kh) = &knowledge {
		use crate::knowledge::store::{
			DagEdgeSnapshot, DagNodeSnapshot, DirectAgentSnapshot, RouterEntry,
		};
		use crate::task_router::RoutingDecision;

		let strategy_str = strategy_override
			.as_ref()
			.or(Some(config_strategy))
			.and_then(|s| serde_json::to_value(s).ok())
			.and_then(|v| v.as_str().map(String::from))
			.unwrap_or_else(|| "llm".to_string());

		let ts = std::time::SystemTime::now()
			.duration_since(std::time::UNIX_EPOCH)
			.unwrap_or_default()
			.as_secs();

		let (decision_type, direct_agent, dag_nodes, dag_edges) = match &result.decision {
			RoutingDecision::Direct {
				agent_name,
				agent_url,
				confidence,
				reason,
			} => (
				"direct".to_string(),
				Some(DirectAgentSnapshot {
					agent_name: agent_name.clone(),
					agent_url: agent_url.clone(),
					confidence: *confidence,
					reason: reason.clone(),
				}),
				None,
				None,
			),
			RoutingDecision::Decomposed { dag, .. } => {
				let nodes = dag
					.nodes
					.iter()
					.map(|n| DagNodeSnapshot {
						node_id: n.id.clone(),
						description: n.description.clone(),
						assigned_agent: n.assigned_agent.as_ref().map(|a| a.agent_name.clone()),
						agent_url: n.assigned_agent.as_ref().map(|a| a.agent_url.clone()),
						estimated_complexity: n.estimated_complexity,
					})
					.collect();
				let edges = dag
					.edges
					.iter()
					.map(|e| DagEdgeSnapshot {
						from: e.from.clone(),
						to: e.to.clone(),
					})
					.collect();
				("decomposed".to_string(), None, Some(nodes), Some(edges))
			},
		};

		kh.store
			.add_router_entry(RouterEntry {
				task_id: result.task_id.clone(),
				timestamp_secs: ts,
				original_task,
				complexity_score: result.complexity_score,
				decision_type,
				strategy: strategy_str,
				latency_ms,
				direct_agent,
				dag_nodes,
				dag_edges,
			})
			.await;
	}

	let body = serde_json::to_string(&result)?;
	Ok(
		::http::Response::builder()
			.status(hyper::StatusCode::OK)
			.header(hyper::header::CONTENT_TYPE, "application/json")
			.body(body.into())
			.expect("builder with known status code should not fail"),
	)
}

// ── Knowledge admin handlers ──────────────────────────────────────────────────

async fn handle_router_stats(
	knowledge: Option<Arc<crate::knowledge::KnowledgeHandle>>,
) -> Response {
	let Some(kh) = knowledge else {
		return plaintext_response(
			hyper::StatusCode::SERVICE_UNAVAILABLE,
			"knowledge not enabled".into(),
		);
	};
	let stats = kh.store.router_stats().await;
	match serde_json::to_string(&stats) {
		Ok(body) => {
			let mut r = plaintext_response(hyper::StatusCode::OK, body);
			r.headers_mut()
				.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
			r
		},
		Err(e) => plaintext_response(
			hyper::StatusCode::INTERNAL_SERVER_ERROR,
			format!("serialize error: {e}"),
		),
	}
}

/// GET /task-router/traces?limit=N  — return recent route traces (decision + execution).
async fn handle_router_traces(
	knowledge: Option<Arc<crate::knowledge::KnowledgeHandle>>,
	req: Request<Incoming>,
) -> Response {
	let Some(kh) = knowledge else {
		return plaintext_response(
			hyper::StatusCode::SERVICE_UNAVAILABLE,
			"knowledge not enabled".into(),
		);
	};
	let limit: usize = req
		.uri()
		.query()
		.and_then(|q| {
			q.split('&')
				.find(|p| p.starts_with("limit="))
				.and_then(|p| p.trim_start_matches("limit=").parse().ok())
		})
		.unwrap_or(20);
	let traces = kh.store.all_traces(limit).await;
	match serde_json::to_string(&traces) {
		Ok(body) => {
			let mut r = plaintext_response(hyper::StatusCode::OK, body);
			r.headers_mut()
				.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
			r
		},
		Err(e) => plaintext_response(
			hyper::StatusCode::INTERNAL_SERVER_ERROR,
			format!("serialize error: {e}"),
		),
	}
}

/// POST /task-router/execution  — receive DAG execution results from the Python agent.
async fn handle_route_execution(
	knowledge: Option<Arc<crate::knowledge::KnowledgeHandle>>,
	req: Request<Incoming>,
) -> anyhow::Result<Response> {
	if *req.method() != hyper::Method::POST {
		return Ok(empty_response(hyper::StatusCode::METHOD_NOT_ALLOWED));
	}
	let Some(kh) = knowledge else {
		return Ok(plaintext_response(
			hyper::StatusCode::SERVICE_UNAVAILABLE,
			"knowledge not enabled".into(),
		));
	};
	let body_bytes = req
		.into_body()
		.collect()
		.await
		.map_err(|e| anyhow::anyhow!("body read error: {e}"))?
		.to_bytes();
	let execution: crate::knowledge::store::RouteExecution = serde_json::from_slice(&body_bytes)
		.map_err(|e| anyhow::anyhow!("invalid execution body: {e}"))?;
	kh.store.add_execution(execution).await;
	Ok(plaintext_response(hyper::StatusCode::OK, "ok".into()))
}

async fn handle_knowledge_stats(
	knowledge: Option<Arc<crate::knowledge::KnowledgeHandle>>,
) -> Response {
	let Some(kh) = knowledge else {
		return plaintext_response(
			hyper::StatusCode::SERVICE_UNAVAILABLE,
			"knowledge not enabled".into(),
		);
	};
	let stats = kh.store.all_stats().await;
	match serde_json::to_string(&stats) {
		Ok(body) => {
			let mut r = plaintext_response(hyper::StatusCode::OK, body);
			r.headers_mut()
				.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
			r
		},
		Err(e) => plaintext_response(
			hyper::StatusCode::INTERNAL_SERVER_ERROR,
			format!("serialize error: {e}"),
		),
	}
}

async fn handle_knowledge_wm(
	knowledge: Option<Arc<crate::knowledge::KnowledgeHandle>>,
) -> Response {
	let Some(kh) = knowledge else {
		return plaintext_response(
			hyper::StatusCode::SERVICE_UNAVAILABLE,
			"knowledge not enabled".into(),
		);
	};
	let entries = kh.working_memory.snapshot().await;
	// Serialize only the non-sensitive fields
	#[derive(serde::Serialize)]
	struct WmEntry<'a> {
		timestamp_secs: u64,
		route_key: &'a str,
		backend: &'a str,
		llm_model: Option<&'a str>,
		context_fingerprint: Option<u64>,
		prompt_snippet: Option<&'a str>,
		outcome: &'static str,
		latency_ms: u64,
	}
	let out: Vec<_> = entries
		.iter()
		.map(|e| WmEntry {
			timestamp_secs: e.timestamp_secs,
			route_key: e.route_key.as_str(),
			backend: e.backend.as_str(),
			llm_model: e.llm_model.as_deref(),
			context_fingerprint: e.context_fingerprint,
			prompt_snippet: e.prompt_snippet.as_deref(),
			outcome: match &e.outcome {
				crate::knowledge::working_memory::Outcome::Success => "success",
				crate::knowledge::working_memory::Outcome::Failure { .. } => "failure",
			},
			latency_ms: e.latency.as_millis() as u64,
		})
		.collect();
	match serde_json::to_string(&out) {
		Ok(body) => {
			let mut r = plaintext_response(hyper::StatusCode::OK, body);
			r.headers_mut()
				.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
			r
		},
		Err(e) => plaintext_response(
			hyper::StatusCode::INTERNAL_SERVER_ERROR,
			format!("serialize error: {e}"),
		),
	}
}

async fn handle_knowledge_corrections(
	knowledge: Option<Arc<crate::knowledge::KnowledgeHandle>>,
	req: Request<Incoming>,
) -> Response {
	let Some(kh) = knowledge else {
		return plaintext_response(
			hyper::StatusCode::SERVICE_UNAVAILABLE,
			"knowledge not enabled".into(),
		);
	};
	match *req.method() {
		hyper::Method::GET => {
			let corrections = kh.store.all_corrections().await;
			match serde_json::to_string(&corrections) {
				Ok(body) => {
					let mut r = plaintext_response(hyper::StatusCode::OK, body);
					r.headers_mut()
						.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
					r
				},
				Err(e) => plaintext_response(
					hyper::StatusCode::INTERNAL_SERVER_ERROR,
					format!("serialize error: {e}"),
				),
			}
		},
		hyper::Method::POST => {
			use http_body_util::BodyExt;
			use hyper::body::Buf;
			let body = match req.into_body().collect().await {
				Ok(b) => b.aggregate(),
				Err(e) => {
					return plaintext_response(hyper::StatusCode::BAD_REQUEST, format!("read error: {e}"));
				},
			};
			#[derive(serde::Deserialize)]
			struct CorrectionReq {
				route_key: String,
				note: String,
			}
			let cr: CorrectionReq = match serde_json::from_reader(body.reader()) {
				Ok(v) => v,
				Err(e) => {
					return plaintext_response(hyper::StatusCode::BAD_REQUEST, format!("parse error: {e}"));
				},
			};
			let ts = std::time::SystemTime::now()
				.duration_since(std::time::UNIX_EPOCH)
				.unwrap_or_default()
				.as_secs();
			kh.store
				.add_correction(crate::knowledge::store::Correction {
					route_key: agent_core::strng::new(&cr.route_key),
					note: cr.note,
					timestamp_secs: ts,
				})
				.await;
			plaintext_response(hyper::StatusCode::OK, "ok".into())
		},
		_ => plaintext_response(
			hyper::StatusCode::METHOD_NOT_ALLOWED,
			"method not allowed".into(),
		),
	}
}

async fn handle_knowledge_sessions(
	knowledge: Option<Arc<crate::knowledge::KnowledgeHandle>>,
) -> Response {
	let Some(kh) = knowledge else {
		return plaintext_response(
			hyper::StatusCode::SERVICE_UNAVAILABLE,
			"knowledge not enabled".into(),
		);
	};
	let sessions = kh.session_memory.snapshot().await;
	match serde_json::to_string(&sessions) {
		Ok(body) => {
			let mut r = plaintext_response(hyper::StatusCode::OK, body);
			r.headers_mut()
				.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
			r
		},
		Err(e) => plaintext_response(
			hyper::StatusCode::INTERNAL_SERVER_ERROR,
			format!("serialize error: {e}"),
		),
	}
}
