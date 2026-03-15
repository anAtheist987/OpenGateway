use std::fmt::{Display, Formatter};
use std::{env, fmt};

const BUILD_VERSION: &str = env!("AGENTGATEWAY_BUILD_buildVersion");
const BUILD_GIT_REVISION: &str = env!("AGENTGATEWAY_BUILD_buildGitRevision");
const BUILD_RUST_VERSION: &str = env!("AGENTGATEWAY_BUILD_RUSTC_VERSION");
const BUILD_RUST_PROFILE: &str = env!("AGENTGATEWAY_BUILD_PROFILE_NAME");
const BUILD_RUST_TARGET: &str = env!("AGENTGATEWAY_BUILD_TARGET");

#[derive(serde::Serialize, Clone, Debug, Default)]
pub struct BuildInfo {
	pub version: &'static str,
	pub git_revision: &'static str,
	pub rust_version: &'static str,
	pub build_profile: &'static str,
	pub build_target: &'static str,
}

impl BuildInfo {
	pub const fn new() -> Self {
		BuildInfo {
			version: BUILD_VERSION,
			git_revision: BUILD_GIT_REVISION,
			rust_version: BUILD_RUST_VERSION,
			build_profile: BUILD_RUST_PROFILE,
			build_target: BUILD_RUST_TARGET,
		}
	}
}

impl Display for BuildInfo {
	fn fmt(&self, f: &mut Formatter<'_>) -> fmt::Result {
		let s = serde_json::to_string_pretty(self).map_err(|_| fmt::Error)?;
		write!(f, "{s}")
	}
}
