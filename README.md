<!-- Modified by Tsinghua University, 2026. Original: https://github.com/agentgateway/agentgateway -->

<div align="center">
  <h1>OpenGateway</h1>
  <div>
    <a href="https://opensource.org/licenses/Apache-2.0">
      <img src="https://img.shields.io/badge/License-Apache2.0-brightgreen.svg?style=flat" alt="License: Apache 2.0">
    </a>
  </div>
</div>

---

An intelligent agent gateway with built-in **semantic routing**, **working memory**, and **knowledge management** — enabling context-aware request dispatching, runtime observability, and cross-session knowledge reuse for multi-agent systems.

Key capabilities:
- **Semantic Task Router** — LLM-reasoning and embedding-based routing with DAG orchestration
- **Working Memory** — ring-buffer trace capture with FNV-1a fingerprinting and TTL eviction
- **Knowledge Store** — per-route EWMA statistics (latency, success rate) and user corrections
- **Session Intelligence** — multi-turn state tracking, fingerprint deduplication, KV-cache reuse estimation
- **KDN Integration** — Knowledge Delivery Network client for cross-instance knowledge sharing
- **Real-time Dashboard** — Gradio-based monitoring UI with live charts and session heatmaps

## Key Features

- **Highly performant:** Written in Rust, designed for any scale
- **Security First:** Robust MCP/A2A focused RBAC system
- **Multi Tenant:** Multiple tenants with isolated resources
- **Dynamic:** Configuration updates via xDS, zero downtime
- **Run Anywhere:** Single machine to large-scale deployment
- **Legacy API Support:** Transform legacy APIs into MCP resources (OpenAPI)

## Quick Start: Travel Agent Demo

### Prerequisites

- **Rust** (1.90+) — install via [rustup](https://rustup.rs/)
- **Python 3.13+** — required by the A2A agents
- **uv** — fast Python package manager: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Node.js** (18+) — for building the gateway UI
- At least one API key (see below)

### 1. Clone and configure

```bash
git clone https://github.com/anAtheist987/OpenGateway.git
cd OpenGateway
cp agents.env.example agents.env
```

Edit `agents.env` and fill in your API keys:

```bash
# Required: DashScope API Key (for all LLM calls, supports Qwen models)
# Get one at: https://dashscope.console.aliyun.com/apiKey
DASHSCOPE_API_KEY="sk-xxxx"

# Optional: enables more agents
SERPAPI_KEY=""              # https://serpapi.com/dashboard (flight/hotel/event/finance agents)
TRIPADVISOR_API_KEY=""     # https://www.tripadvisor.com/developers
GOOGLE_API_KEY=""          # https://makersuite.google.com/app/apikey (Airbnb agent)
DOCUMENT_API_KEY=""        # Anthropic-compatible API key (Claude-based agents)
```

### 2. One-click start

```bash
bash start-all.sh
```

The script automatically:
1. Builds the Next.js UI (`npm install` + `npm run build`)
2. Compiles the Rust gateway (`cargo build`)
3. Installs Python dependencies (`uv sync`, if `.venv` doesn't exist)
4. Starts the gateway, all agents, and the monitoring dashboard

> If you use conda or venv, you can point the script to a specific Python:
> ```bash
> UI_PY=/path/to/python3 TA_PY=/path/to/python3 bash start-all.sh
> ```

### 3. Access

| Service | URL |
|---------|-----|
| Agent Chat UI | http://localhost:8083 |
| Gateway Admin | http://localhost:15000 |
| Monitoring Dashboard | http://localhost:7860 |
| Gateway UI | http://localhost:15000/ui |

### Environment overrides

```bash
SKIP_BUILD=1          # Skip cargo build (if already built)
ADMIN_PORT=15000      # Gateway admin port
PROXY_PORT=3000       # Gateway proxy port
UI_PORT=7860          # Dashboard port
```

## Architecture

```
User → Host Agent (8083) → AgentGateway (3000) → Specialized Agents
                                                    ├── Weather    (10001)
                                                    ├── Airbnb     (10002)
                                                    ├── TripAdvisor(10003)
                                                    ├── Event      (10004)
                                                    ├── Finance    (10005)
                                                    ├── Flight     (10006)
                                                    ├── Hotel      (10007)
                                                    └── Doc Agents (10009-10011)
```

The gateway proxies all agent-to-agent traffic, providing:
- Working memory capture for all LLM requests
- EWMA-smoothed latency/success-rate statistics per route
- Session-level fingerprint tracking for KV-cache reuse estimation
- A2A protocol support with CORS and policy enforcement

## Example Queries

```
Plan a 5-day trip to Paris including flights, hotel, and attractions
What's the weather in Tokyo?
Find flights from Beijing to Los Angeles on March 20
Search for hotels in New York under $200
Find concerts in San Francisco this weekend
Convert 1000 USD to EUR
```

## Project Structure

```
├── agents.env.example       # API key configuration template
├── start-all.sh             # One-click startup script
├── examples/
│   └── travel-agent-demo/
│       └── config.yaml      # Gateway config for the demo
├── agents/
│   └── airbnb_planner_multiagent-main/
│       ├── agents_in_use/   # Active agent implementations
│       ├── host_agent/      # Coordinator agent (Gradio UI)
│       ├── weather_agent/   # Weather forecasts
│       ├── flight_agent/    # Flight search
│       ├── hotel_agent/     # Hotel search
│       └── ...              # More specialized agents
├── crates/                  # Rust workspace
│   ├── agentgateway/        # Core proxy logic
│   ├── agentgateway-app/    # Binary entry point
│   ├── a2a-sdk/             # A2A protocol SDK
│   └── ...
├── ui/                      # Next.js gateway UI
├── dashboard/               # Gradio monitoring dashboard
└── kdn/                     # Knowledge Delivery Network server
```

## Build from Source

```bash
# Build UI first
cd ui && npm install && npm run build && cd ..

# Build gateway
cargo build --release --features ui

# Run with config
cargo run --release --features ui -- -f examples/travel-agent-demo/config.yaml
```

## Documentation

- [Upstream agentgateway docs](https://agentgateway.dev/docs/)
- [Agent details](agents/airbnb_planner_multiagent-main/README.md)
- [Deployment guide](DEPLOYMENT.md)
- [Contributing](CONTRIBUTION.md)

## License

This project is licensed under the [Apache License 2.0](LICENSE), the same license as the upstream [agentgateway](https://github.com/agentgateway/agentgateway) project.

---

<div align="center">
    <img src="img/lf-stacked-color.png" width="300" alt="Linux Foundation logo"/>
    <p>Based on <a href="https://github.com/agentgateway/agentgateway">agentgateway</a>, a <a href="https://www.linuxfoundation.org/">Linux Foundation</a> project.</p>
</div>

**OpenGateway** is a fork of [agentgateway](https://github.com/agentgateway/agentgateway) — an open source data plane optimized for agentic AI connectivity. It provides drop-in security, observability, and governance for agent-to-agent and agent-to-tool communication, supporting [Agent2Agent (A2A)](https://developers.googleblog.com/en/a2a-a-new-era-of-agent-interoperability/) and [Model Context Protocol (MCP)](https://modelcontextprotocol.io/introduction).
