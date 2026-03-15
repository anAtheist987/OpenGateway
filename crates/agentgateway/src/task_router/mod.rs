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

pub mod dag;
pub mod embedding_client;
pub mod llm_client;
pub mod router;
pub mod types;

pub use router::TaskRouter;
pub use types::{
	AgentInfo, RouteTaskRequest, RoutingDecision, RoutingResult, RoutingStrategy, TaskRouterConfig,
};
