<!-- Modified by Tsinghua University, 2026. Original: https://github.com/agentgateway/agentgateway -->

# Quickstart (GitHub, no local install)

1. Click **Code → Create codespace on main**.
2. In the terminal:
   cargo fmt --all
   cargo clippy --all -- -D warnings
   cargo test --all
3. If you touched the UI:
   cd ui
   npm ci
   npm test

# Local Development

This page contains instructions on how to run everything locally.

## Build from Source

Requirements:
- Rust 1.86+
- npm 10+

Build the agentgateway UI:

```bash
cd ui
npm install
npm run build
```

Build the agentgateway binary:

```bash
cd ..
export CARGO_NET_GIT_FETCH_WITH_CLI=true
export AGENTGATEWAY_BUILD_buildVersion="0.11.2"
export AGENTGATEWAY_BUILD_buildGitRevision=$(git rev-parse HEAD)
make build
```

Run the agentgateway binary:

```bash
./target/release/agentgateway
```
Open your browser and navigate to `http://localhost:15000/ui` to see the agentgateway UI.

