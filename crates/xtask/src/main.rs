// Modified by Tsinghua University, 2026
// Original source: https://github.com/agentgateway/agentgateway
// Licensed under the Apache License, Version 2.0

mod schema;
use std::env::args;

use anyhow::{Context, Result, bail};

enum Task {
	Schema,
}

fn get_task() -> Result<Task> {
	let message = "argument is missing. Example usage: \ncargo xtask schema";
	let arg = args().nth(1).context(message)?;
	match arg.as_str() {
		"schema" => Ok(Task::Schema),
		arg => bail!("unknown task: {}", arg),
	}
}

fn main() -> Result<()> {
	match get_task()? {
		Task::Schema => schema::generate_schema(),
	}
}
