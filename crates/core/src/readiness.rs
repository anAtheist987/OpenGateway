// Modified by Tsinghua University, 2026
// Original source: https://github.com/agentgateway/agentgateway
// Licensed under the Apache License, Version 2.0

// Originally derived from https://github.com/istio/ztunnel (Apache 2.0 licensed)

use std::collections::HashSet;
use std::sync::{Arc, Mutex};

use tracing::info;

use crate::telemetry;

/// Ready tracks whether the process is ready.
#[derive(Clone, Debug, Default)]
pub struct Ready(Arc<Mutex<HashSet<String>>>);

impl Ready {
	pub fn new() -> Ready {
		Ready(Default::default())
	}

	/// register_task allows a caller to add a dependency to be marked "ready".
	pub fn register_task(&self, name: &str) -> BlockReady {
		self.0.lock().unwrap().insert(name.to_string());
		BlockReady {
			parent: self.to_owned(),
			name: name.to_string(),
		}
	}

	pub fn pending(&self) -> HashSet<String> {
		self.0.lock().unwrap().clone()
	}
}

/// BlockReady blocks readiness until it is dropped.
pub struct BlockReady {
	parent: Ready,
	name: String,
}

impl BlockReady {
	pub fn subtask(&self, name: &str) -> BlockReady {
		self.parent.register_task(name)
	}
}

impl Drop for BlockReady {
	fn drop(&mut self) {
		let mut pending = self.parent.0.lock().unwrap();
		let removed = pending.remove(&self.name);
		debug_assert!(removed); // It is a bug to somehow remove something twice
		let left = pending.len();
		let dur = telemetry::APPLICATION_START_TIME.elapsed();
		if left == 0 {
			info!(
				"Task '{}' complete ({dur:?}), marking server ready",
				self.name
			);
		} else {
			info!(
				"Task '{}' complete ({dur:?}), still awaiting {left} tasks",
				self.name
			);
		}
	}
}
