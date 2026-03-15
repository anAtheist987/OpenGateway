<!-- Copyright 2026 Tsinghua University. Licensed under Apache 2.0.
     This file was created by Tsinghua University and is not part of
     the original agentgateway project by Solo.io. -->

# 旅行规划 Multi-Agent 系统部署指南

本文档说明在新机器上部署该系统时需要准备的环境、API Key 和配置修改内容。

---

## 一、必要环境

### 1. Conda 环境

需要创建两个 conda 环境：

| 环境名 | Python 版本 | 用途 |
|--------|------------|------|
| `Agentgateway` | 3.11+ | 网关二进制编译（cargo）+ Gradio Dashboard |
| `travel-agents` | **3.12** | 所有 Python Agent（必须是 3.12） |

```bash
# 创建 travel-agents 环境
conda create -n travel-agents python=3.12
conda activate travel-agents
cd agents/airbnb_planner_multiagent-main
pip install -r requirements.txt   # 若有
# 或手动安装主要依赖：
pip install google-adk a2a-sdk litellm mcp aiohttp starlette uvicorn gradio requests python-dotenv
pip install serpapi tripadvisor-python    # 可选，按需
```

### 2. Rust 工具链

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
rustup update stable   # 需要 Rust >= 1.90
```

### 3. Node.js（构建前端 UI）

```bash
# 需要 Node.js >= 18
node --version   # 确认版本
```

### 4. 系统工具

```bash
apt install -y lsof curl   # 端口清理和健康检查依赖
```

---

## 二、API Key 配置

将 `agents.env.example` 复制为 `agents.env` 并填写：

```bash
cp agents.env.example agents.env
vim agents.env
```

### Key 清单

| 变量名 | 必填 | 用途 | 获取地址 |
|--------|------|------|---------|
| `DASHSCOPE_API_KEY` | **必填** | Host Agent (Qwen-Plus 协调器) + Flight/Event/Finance Agent LLM | https://dashscope.console.aliyun.com/apiKey |
| `DOCUMENT_API_KEY` | **强烈建议** | Weather/Hotel/Doc Agent 使用 Claude Sonnet 4.6 | https://console.anthropic.com/ |
| `SERPAPI_KEY` | 可选 | 航班/酒店/活动/金融 实时搜索（缺失则跳过这 4 个 Agent） | https://serpapi.com/dashboard（100次/月免费） |
| `TRIPADVISOR_API_KEY` | 可选 | 景点信息（缺失则跳过 TripAdvisor Agent） | https://www.tripadvisor.com/developers |
| `GOOGLE_API_KEY` | 可选 | Airbnb Agent（缺失则跳过） | https://makersuite.google.com/app/apikey |

### 最小配置示例（仅运行核心功能）

```bash
DASHSCOPE_API_KEY="sk-xxxx"    # DashScope API Key
DOCUMENT_API_KEY="sk-yyyy"     # Anthropic API Key
SERPAPI_KEY=""                  # 跳过 Flight/Hotel/Event/Finance
TRIPADVISOR_API_KEY=""
GOOGLE_API_KEY=""
```

---

## 三、Agent LLM 后端分配

| Agent | 使用模型 | API 端点 | Key |
|-------|---------|---------|-----|
| Host Agent (协调器) | `openai/qwen-plus` | DashScope | `DASHSCOPE_API_KEY` |
| Flight Agent | `openai/qwen-plus` | DashScope | `DASHSCOPE_API_KEY` |
| Event Agent | `openai/qwen-plus` | DashScope | `DASHSCOPE_API_KEY` |
| Finance Agent (金融) | `openai/qwen-plus` | DashScope | `DASHSCOPE_API_KEY` |
| **Weather Agent** | `anthropic/claude-sonnet-4-6` | Anthropic API | `DOCUMENT_API_KEY` |
| **Hotel Agent** | `anthropic/claude-sonnet-4-6` | Anthropic API | `DOCUMENT_API_KEY` |
| **Finance Doc Agent** | `anthropic/claude-sonnet-4-6` | Anthropic API | `DOCUMENT_API_KEY` |
| **Infosec Doc Agent** | `anthropic/claude-sonnet-4-6` | Anthropic API | `DOCUMENT_API_KEY` |
| **Dept Doc Agent** | `anthropic/claude-sonnet-4-6` | Anthropic API | `DOCUMENT_API_KEY` |

> **注意**：Weather 和 Hotel Agent 使用 Claude 版本（`weather_agent_claude`、`hotel_agent_claude`）。
> start-all.sh 通过 `env LITELLM_MODEL=anthropic/claude-sonnet-4-6` 覆盖全局 Qwen 配置，确保这两个 agent 使用 Claude。

---

## 四、start-all.sh 修改指南

迁移到新机器后，需要修改以下内容：

### 1. Python 路径（必改）

```bash
# 文件顶部，约第 49-50 行
UI_PY="/root/miniconda3/envs/Agentgateway/bin/python3"       # ← 改为新机器路径
TA_PY="/root/miniconda3/envs/travel-agents/bin/python3.12"   # ← 改为新机器路径
```

查找实际路径：
```bash
conda activate Agentgateway && which python3
conda activate travel-agents && which python3.12
```

### 2. 网关二进制路径（如需）

```bash
# 第 41 行
BINARY="$REPO_ROOT/target/debug/agentgateway"   # 默认 debug build，如用 release 改为：
# BINARY="$REPO_ROOT/target/release/agentgateway"
```

### 3. 文档数据路径（如需）

Doc Agents 读取的 mock 数据文件默认路径：
```python
# finance_document_mcp.py / infosec_document_mcp.py / dept_doc_reader_mcp.py
DEFAULT_PORTAL_DATA_PATH = '/mnt/ssd2/cyh/Agentgateway-thu/reimbursement_portal/reimbursement_mock_data.json'
```

如果新机器路径不同，在 `agents.env` 中添加：
```bash
REIMBURSEMENT_MOCK_DATA_PATH="/新路径/reimbursement_mock_data.json"
```

### 4. 可选：更换 LLM 模型

如需使用不同的 Qwen 模型（默认 `qwen-plus`）：
```bash
# agents.env 中添加：
LITELLM_MODEL="openai/qwen-turbo"      # 更快更便宜
# LITELLM_MODEL="openai/qwen-max"      # 更强
LITELLM_API_BASE="https://dashscope.aliyuncs.com/compatible-mode/v1"
```

如需更换 Claude 模型（默认 `anthropic/claude-sonnet-4-6`）：
```bash
# agents.env 中添加（会覆盖 start-all.sh 的默认值）：
DOCUMENT_API_MODEL="anthropic/claude-opus-4-6"    # 更强
DOCUMENT_API_BASE="https://api.anthropic.com"     # Anthropic API 端点
```

---

## 五、端口占用清单

启动后以下端口必须全部可用：

| 端口 | 组件 |
|------|------|
| 8083 | Host Agent (Orchestrator) |
| 8084 | Agent Listener (SSE 调用树) |
| 8090 | Mock Registry |
| 10001 | Weather Agent |
| 10002 | Airbnb Agent |
| 10003 | TripAdvisor Agent |
| 10004 | Event Agent |
| 10005 | Finance Agent (金融) |
| 10006 | Flight Agent |
| 10007 | Hotel Agent |
| 10009 | Finance Document Agent |
| 10010 | Infosec Document Agent |
| 10011 | Dept Doc Reader Agent |
| 15000 | AgentGateway Admin API + Travel UI |
| 15020 | AgentGateway Prometheus metrics |
| 15021 | AgentGateway readiness probe |
| 3000-3011 | AgentGateway 代理端口（对外暴露） |
| 7860 | Gradio Knowledge Dashboard |

---

## 六、启动命令

```bash
# 完整启动（含编译）
export DASHSCOPE_API_KEY="sk-xxxx"
bash start-all.sh

# 跳过编译（已编译过，快速重启）
SKIP_BUILD=1 bash start-all.sh

# 推荐：配置 agents.env 后无需手动 export
cp agents.env.example agents.env
# 编辑 agents.env 填入 key
bash start-all.sh
```

访问入口：
- **Travel 对话 UI**：http://localhost:15000/ui/travel
- **Admin UI**：http://localhost:15000/ui
- **Knowledge Dashboard**：http://localhost:7860

---

## 七、常见问题

### WeatherAgent / HotelAgent 启动失败：`ValueError: ANTHROPIC_API_KEY not set`

原因：`DOCUMENT_API_KEY` 未填入 `agents.env`，导致 `ANTHROPIC_API_KEY` 为空。
修复：在 `agents.env` 中填入 `DOCUMENT_API_KEY`。

### Doc Agents 返回 405：`litellm.APIError: OpenAIException - <html>`

原因：`DOCUMENT_API_KEY` 未填或填错，Anthropic API 返回 HTML 错误页。
修复：同上。

### EventAgent / FinanceAgent 启动失败：MCP session error

原因：MCP subprocess 使用了错误的 Python 路径（`python` 而非 `sys.executable`）。
修复：已在源码中修复（`event_agent.py` 和 `finance_agent.py` 使用 `sys.executable`）。

### 网关 bind 失败：`Address already in use`

原因：端口已被占用。
修复：start-all.sh 会自动清理已知端口，如仍失败手动运行：
```bash
for port in 8083 8084 8090 10001 10002 10003 10004 10005 10006 10007 10009 10010 10011; do
    lsof -ti :$port | xargs kill -9 2>/dev/null; done
```
