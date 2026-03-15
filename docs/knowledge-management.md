<!-- Copyright 2026 Tsinghua University. Licensed under Apache 2.0.
     This file was created by Tsinghua University and is not part of
     the original agentgateway project by Solo.io. -->

# Evolutionary Knowledge Management (Feature 6.5)

AgentGateway 的演进式知识管理模块将网关从无状态转发器升级为具备学习能力的基础设施。它从流经的流量中自动捕获执行轨迹，构建本地化知识库，并为 KDN（Knowledge Delivery Network）协同推理加速提供接口。

---

## 架构概览

```
每次请求完成后 (DropOnLog::drop)
        │
        ▼
  KnowledgeHandle.capture()   ← fire-and-forget tokio::spawn
        │
        ├─► WorkingMemory      短期环形缓冲区（可配置容量）
        │       └─ FNV-1a 指纹  用于 KDN 重叠检测
        │
        └─► KnowledgeStore     长期聚合统计
                ├─ RouteStats  EWMA 延迟 + 成功率
                └─ Corrections 用户纠正记录
```

KDN 客户端（`KdnClient`）在检测到指纹重叠时向外部 KDN 服务发起查询，获取可复用的 LLM KV-cache，降低 TTFT。

---

## 配置

在 YAML 配置文件的 `config:` 块中添加 `knowledge` 字段：

```yaml
config:
  knowledge:
    # 工作记忆环形缓冲区容量（条目数），默认 1000
    workingMemoryCapacity: 2000

    # KDN 服务地址，不配置则禁用 KDN 集成
    # kdnEndpoint: "http://kdn-service:9000"
```

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `workingMemoryCapacity` | `usize` | `1000` | 工作记忆最大条目数，超出时淘汰最旧条目 |
| `kdnEndpoint` | `string?` | `null` | KDN HTTP 服务地址，`null` 表示禁用 |

---

## Admin API

所有端点挂载在 Admin 服务（默认 `:15000`）上。

### GET /knowledge/working_memory

返回当前工作记忆快照（最近 N 条请求轨迹）。

**响应示例：**
```json
[
  {
    "timestamp_secs": 1772116364,
    "route_key": "default/route0",
    "backend": "/default/default/listener0/default/route0/backend0",
    "llm_model": null,
    "context_fingerprint": null,
    "outcome": "success",
    "latency_ms": 646
  }
]
```

| 字段 | 说明 |
|------|------|
| `timestamp_secs` | Unix 时间戳（秒） |
| `route_key` | 路由标识符 |
| `backend` | 后端名称 |
| `llm_model` | LLM 模型名（非 AI 请求为 `null`） |
| `context_fingerprint` | Prompt 前缀的 FNV-1a 指纹（非 LLM 请求为 `null`） |
| `outcome` | `"success"` 或 `"failure"` |
| `latency_ms` | 端到端延迟（毫秒） |

---

### GET /knowledge/stats

返回各路由的聚合统计信息。

**响应示例：**
```json
[
  {
    "route_key": "default/route0",
    "total_requests": 10,
    "success_count": 9,
    "failure_count": 1,
    "ewma_latency_ms": 679.24
  }
]
```

| 字段 | 说明 |
|------|------|
| `total_requests` | 总请求数 |
| `success_count` | 成功请求数（HTTP < 500） |
| `failure_count` | 失败请求数（HTTP >= 500） |
| `ewma_latency_ms` | 指数加权移动平均延迟（α=0.1） |

成功率计算：`success_count / total_requests`

---

### GET /knowledge/corrections

返回所有用户纠正记录。

**响应示例：**
```json
[
  {
    "route_key": "default/route0",
    "note": "prefer backend B for low-latency workloads",
    "timestamp_secs": 1772116372
  }
]
```

---

### POST /knowledge/corrections

提交一条用户纠正，用于覆盖或补充学习到的路由策略信号。

**请求体：**
```json
{
  "route_key": "default/route0",
  "note": "prefer backend B for low-latency workloads"
}
```

**响应：** `200 ok`

---

## KDN 集成协议

当 `kdnEndpoint` 已配置，且工作记忆中检测到相同 `context_fingerprint` 的历史条目时，网关向 KDN 发起查询：

**请求：**
```
POST {kdnEndpoint}/kdn/query
Content-Type: application/json

{
  "fingerprint": 12345678901234567,
  "model": "gpt-4",
  "route_key": "default/route0"
}
```

**命中响应：**
```json
{
  "hit": true,
  "cache_id": "abc123",
  "ttft_saved_ms": 150
}
```

**未命中响应：**
```json
{ "hit": false }
```

- 查询超时：**200ms**（不阻塞热路径）
- 任何错误（超时、非 2xx、解析失败）均视为未命中，静默降级

---

## 源码位置

| 文件 | 职责 |
|------|------|
| `crates/agentgateway/src/knowledge/working_memory.rs` | 环形缓冲区、FNV-1a 指纹、时间淘汰 |
| `crates/agentgateway/src/knowledge/store.rs` | EWMA 统计聚合、用户纠正存储 |
| `crates/agentgateway/src/knowledge/kdn_client.rs` | KDN HTTP 客户端（hyper） |
| `crates/agentgateway/src/knowledge/mod.rs` | `KnowledgeHandle` 统一入口 |
| `crates/agentgateway/src/telemetry/log.rs` | `DropOnLog::drop()` 捕获钩子 |
| `crates/agentgateway/src/management/admin.rs` | Admin API 端点 |
| `crates/agentgateway/src/lib.rs` | `ProxyInputs.knowledge`、`RawKnowledgeConfig` |
| `examples/knowledge-demo/` | 端到端演示脚本 |

---

## 快速体验

```bash
cd Agentgateway-thu
bash examples/knowledge-demo/demo.sh
```

脚本会自动构建、启动网关、发送流量，并依次展示工作记忆快照、路由统计、用户纠正的完整流程。
