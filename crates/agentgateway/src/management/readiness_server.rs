// Originally derived from https://github.com/istio/ztunnel (Apache 2.0 licensed)

use std::net::SocketAddr;

use agent_core::drain::DrainWatcher;
use agent_core::readiness;
use hyper::Request;
use hyper::body::Incoming;
use itertools::Itertools;

use super::hyper_helpers;
use crate::Address;
use crate::http::Response;

pub struct Server {
	s: hyper_helpers::Server<readiness::Ready>,
	ready: readiness::Ready,
}

impl Server {
	pub async fn new(
		address: Address,
		drain_rx: DrainWatcher,
		ready: readiness::Ready,
	) -> anyhow::Result<Self> {
		hyper_helpers::Server::<readiness::Ready>::bind("readiness", address, drain_rx, ready.clone())
			.await
			.map(|s| Server { s, ready })
	}

	pub fn ready(&self) -> readiness::Ready {
		self.ready.clone()
	}

	pub fn address(&self) -> SocketAddr {
		self.s.address()
	}

	pub fn spawn(self) {
		self.s.spawn(|ready, req| async move {
			match req.uri().path() {
				"/healthz/ready" => Ok(handle_ready(&ready, req).await),
				_ => Ok(hyper_helpers::empty_response(hyper::StatusCode::NOT_FOUND)),
			}
		})
	}
}

async fn handle_ready(ready: &readiness::Ready, req: Request<Incoming>) -> Response {
	match *req.method() {
		hyper::Method::GET => {
			let pending = ready.pending();
			if pending.is_empty() {
				return hyper_helpers::plaintext_response(hyper::StatusCode::OK, "ready\n".into());
			}
			hyper_helpers::plaintext_response(
				hyper::StatusCode::INTERNAL_SERVER_ERROR,
				format!(
					"not ready, pending: {}\n",
					pending.into_iter().sorted().join(", ")
				),
			)
		},
		_ => hyper_helpers::empty_response(hyper::StatusCode::METHOD_NOT_ALLOWED),
	}
}
