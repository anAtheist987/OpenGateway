<!-- Copyright 2026 Tsinghua University. Licensed under Apache 2.0.
     This file was created by Tsinghua University and is not part of
     the original agentgateway project by Solo.io. -->

# AgentGateway Knowledge Dashboard — 使用文档

## 概述

Knowledge Dashboard 是 AgentGateway 演进式知识管理模块的可视化前端，使用 [Gradio](https://gradio.app/) 构建，分三个页面：

| 页面 | 内容 |
|------|------|
| 📋 工作记忆 | 请求轨迹环形缓冲区、延迟时序、成功率分布 |
| 🔀 语义路由 | 各路由 EWMA 延迟对比、成功率仪表、人工纠正 |
| 🌐 KDN | Session 指纹重叠矩阵、KV-cache 命中潜力估算 |

---

## 快速启动

### 前置条件

```bash
Python >= 3.10
pip install -r dashboard/requirements.txt
```

### 运行（Mock 模式，无需网关）

```bash
cd Agentgateway-thu/dashboard
python app.py
# 浏览器访问 http://localhost:7860
```

Mock 模式会自动生成 80 条历史轨迹并模拟实时流量，无需启动网关。

### 连接真实网关

```bash
# 1. 先启动网关（以 qwen-demo 为例）
export DASHSCOPE_API_KEY="sk-xxxx"
cargo build -p agentgateway-app
./target/debug/agentgateway -f examples/knowledge-qwen-demo/config.yaml

# 2. 启动 Dashboard，指向 Admin API
cd dashboard
python app.py --admin http://localhost:15000
```

### 命令行参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--admin` | `http://localhost:15000` | AgentGateway Admin API 地址 |
| `--host` | `0.0.0.0` | Dashboard 监听地址 |
| `--port` | `7860` | Dashboard 监听端口 |
| `--share` | `false` | 生成公开 Gradio Share 链接 |

---

## 页面说明

### 📋 工作记忆

展示 Working Memory 环形缓冲区中的请求轨迹，聚焦于**优化效果可视化**：

**KPI 指标卡**
- 总请求数、LLM 请求占比、成功/失败数、平均延迟、含指纹条目数

**延迟时序图（核心）**
- 灰色散点：原始延迟（可能有尖峰）
- 彩色折线：各路由 EWMA 平滑延迟（α=0.1）
- **效果展示**：EWMA 曲线明显比原始值平滑，体现网关的学习能力

**结果分布饼图**
- 实时展示成功率，失败高亮为红色

**延迟直方图**
- 成功/失败请求的延迟分布对比，直观呈现异常

**最近 30 条请求表**
- 时间戳、路由、后端、模型、结果（✅/❌）、延迟、指纹

**刷新方式**
- 手动点击「🔄 刷新」按钮
- 勾选「自动刷新 (5s)」自动轮询

---

### 🔀 语义路由

展示 KnowledgeStore 中各路由的聚合统计，突出**学习到的路由性能差异**：

**路由统计表**
- 总请求数、成功/失败数、成功率（%）、EWMA 延迟（ms）

**EWMA 延迟对比柱状图**
- 延迟按路由分组，颜色梯度（绿→黄→红）直观呈现性能优劣

**成功率仪表盘**
- 每条路由一个仪表盘，背景区域红/黄/绿三段式警示

**成功/失败堆积柱图**
- 快速判断哪条路由失败数多

**人工纠正**
- 查看所有历史纠正记录（时间、路由、说明）
- 通过折叠面板「➕ 提交路由纠正」提交新的运维建议

---

### 🌐 KDN 知识分发网络

展示 Session 工作记忆和 KDN 指纹复用机会，体现**KV-cache 加速潜力**：

**KPI 指标卡**
- 活跃 Session 数、累计对话轮次、含重叠指纹 Session 数、总重叠次数、预计 TTFT 节省（ms）

**Session 列表表**
- Session ID、路由、轮次、唯一指纹数、重叠次数、KDN 潜力（🔥高/⚡有/—）

**气泡图：对话轮次 vs 指纹多样性**
- X轴：turn_count，Y轴：唯一指纹数，气泡大小=重叠次数
- 气泡大 → KDN 命中潜力高

**KDN 命中潜力柱状图 + TTFT 估算**
- 左轴：每个 Session 的指纹重叠次数
- 右轴（菱形点）：预计 TTFT 节省（每次重叠 ≈ 200ms）

**指纹命中矩阵（热力图）**
- 行=Session，列=Prompt 指纹，蓝色格子=曾见过该指纹
- 同一列多行蓝色 → 不同 Session 复用同一 Prompt 前缀 → KDN 全局缓存机会

**KDN 协议说明（折叠）**
- 请求流程图（文本格式）、指纹重叠的意义、关键参数表

---

## 连接状态

Dashboard 顶部显示网关连接状态：
- `✅ 已连接 AgentGateway Admin API` — 使用真实数据
- `⚠️ Mock 模式（网关未连接）` — 使用合成数据演示

Mock 模式下 Dashboard 与真实网关功能完全一致，适合离线演示。

---

## 文件结构

```
dashboard/
├── app.py              # Gradio 主应用（3 个 Tab、回调、自动刷新）
├── api_client.py       # Admin API HTTP 客户端 + Mock 数据生成器
├── charts.py           # Plotly 图表构建（10 个图表函数）
├── requirements.txt    # Python 依赖
└── tests/
    ├── __init__.py
    └── test_dashboard.py  # 70 个测试（Mock客户端/HTTP客户端/图表/回调/边界情况）
```

---

## 运行测试

```bash
cd dashboard
pip install -r requirements.txt pytest requests-mock
pytest tests/ -v
# 期望：70 passed
```

---

## Admin API 端点（由网关提供）

| 端点 | 方法 | 说明 |
|------|------|------|
| `GET /knowledge/working_memory` | GET | 工作记忆快照 |
| `GET /knowledge/stats` | GET | 各路由统计 |
| `GET /knowledge/sessions` | GET | 活跃 Session 列表 |
| `GET /knowledge/corrections` | GET | 人工纠正列表 |
| `POST /knowledge/corrections` | POST | 提交新纠正 |

详见 `docs/knowledge-management.md` 和 `docs/openapi-kdn.yaml`。
