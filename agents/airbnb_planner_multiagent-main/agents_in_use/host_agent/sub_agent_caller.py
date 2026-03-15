# Copyright 2026 Tsinghua University
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# This file was created by Tsinghua University and is not part of
# the original agentgateway project by Solo.io.

# pylint: disable=logging-fstring-interpolation
"""
Sub Agent Caller — DAG-aware executor
职责：
  1. 读取 state["dag"] 和 state["discovered_agents"]
  2. 按拓扑排序执行 DAG 节点，同层节点并行
  3. 上游节点输出摘要自动注入下游节点 prompt
  4. 所有结果写入 state["results"]，跳过 LLM 调用
  5. 每个节点执行状态实时上报到 agent-listener (port 8084)
"""
import asyncio
import re
import uuid
from collections import defaultdict, deque
from typing import Any

import aiohttp
import httpx
from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_response import LlmResponse
from google.genai import types

_LISTENER_URL = "http://localhost:8084"


async def _report(path: str, payload: dict) -> None:
    """向 agent-listener 上报事件，失败静默忽略。"""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{_LISTENER_URL}{path}", json=payload) as r:
                await r.text()
    except Exception:
        pass

from a2a.types import (
    AgentCard,
    MessageSendParams,
    SendMessageRequest,
    SendMessageResponse,
    SendMessageSuccessResponse,
    Task,
)

from remote_agent_connection import RemoteAgentConnections

# 用于从 agent 回复中提取下游摘要的标签
_SUMMARY_TAG_RE = re.compile(
    r"<SUMMARY_FOR_DOWNSTREAM>(.*?)</SUMMARY_FOR_DOWNSTREAM>",
    re.DOTALL,
)


def _strip_summary_tag(text: str) -> tuple[str, str]:
    """返回 (正文, 摘要)，摘要不存在时为空字符串。"""
    m = _SUMMARY_TAG_RE.search(text)
    if not m:
        return text, ""
    summary = m.group(1).strip()
    body = _SUMMARY_TAG_RE.sub("", text).strip()
    return body, summary


class DagExecutor:
    """
    按拓扑排序执行 DAG，同层节点并行，上游摘要注入下游 prompt。
    """

    def __init__(self):
        self.remote_agent_connections: dict[str, RemoteAgentConnections] = {}
        self.agent_cards: dict[str, AgentCard] = {}

    # ── 连接管理 ──────────────────────────────────────────────────────────────

    async def _connect(self, agent_name: str, agent_url: str) -> None:
        if agent_name in self.remote_agent_connections:
            return
        if not agent_url or not agent_url.startswith("http"):
            raise ValueError(f"Invalid URL for {agent_name}: {agent_url}")
        async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
            resp = await client.get(f"{agent_url.rstrip('/')}/.well-known/agent-card.json")
            resp.raise_for_status()
            card = AgentCard.model_validate(resp.json())
        self.remote_agent_connections[agent_name] = RemoteAgentConnections(
            agent_card=card, agent_url=agent_url
        )
        self.agent_cards[agent_name] = card

    # ── 发送消息 ──────────────────────────────────────────────────────────────

    async def _send(self, agent_name: str, task_text: str, context_id: str) -> str:
        conn = self.remote_agent_connections.get(agent_name)
        if not conn:
            return f"[ERROR] Agent '{agent_name}' not connected."
        msg_id = str(uuid.uuid4())
        req = SendMessageRequest(
            id=msg_id,
            params=MessageSendParams.model_validate({
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": task_text}],
                    "messageId": msg_id,
                    "contextId": context_id,
                }
            }),
        )
        try:
            resp = await conn.send_message(req)
            return self._extract_text(resp)
        except Exception as e:
            return f"[ERROR] {e}"

    # ── 文本提取 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _try_text(part: Any) -> str:
        root = getattr(part, "root", part)
        if hasattr(root, "text") and root.text:
            return root.text
        if hasattr(part, "text") and part.text:
            return part.text
        return ""

    def _extract_text(self, response: Any) -> str:
        if isinstance(response, Task):
            if response.artifacts:
                for art in response.artifacts:
                    for part in (art.parts or []):
                        t = self._try_text(part)
                        if t:
                            return t
            if response.history:
                for msg in reversed([m for m in response.history if getattr(m, "role", None) == "agent"]):
                    for part in (msg.parts or []):
                        t = self._try_text(part)
                        if t:
                            return t
        elif isinstance(response, SendMessageResponse):
            root = getattr(response, "root", None)
            if isinstance(root, SendMessageSuccessResponse) and isinstance(root.result, Task):
                return self._extract_text(root.result)
        return str(response)

    # ── DAG 执行 ──────────────────────────────────────────────────────────────

    async def execute(
        self,
        dag: dict,
        discovered_agents: list[dict],
        context_id: str,
    ) -> tuple[list[dict], list[dict]]:
        """
        按拓扑排序执行 DAG，返回 (node_results, agent_messages)。
        node_results: [{"node_id", "agent", "task", "response", "status"}]
        agent_messages: [{"fromNodeId", "toNodeId", "summary"}]
        """
        nodes: list[dict] = dag.get("nodes", [])
        edges: list[dict] = dag.get("edges", [])

        # node_id → agent info
        agent_by_node: dict[str, dict] = {}
        for agent_info in discovered_agents:
            # discovered_agents 里每项有 node_id（由 orchestrator 写入）
            nid = agent_info.get("node_id", "")
            if nid:
                agent_by_node[nid] = agent_info

        # 建立邻接表和入度表
        successors: dict[str, list[str]] = defaultdict(list)   # from → [to]
        predecessors: dict[str, list[str]] = defaultdict(list) # to → [from]
        in_degree: dict[str, int] = {n["id"]: 0 for n in nodes}
        for edge in edges:
            f, t = edge["from"], edge["to"]
            successors[f].append(t)
            predecessors[t].append(f)
            in_degree[t] = in_degree.get(t, 0) + 1

        # node_id → description
        desc_by_id = {n["id"]: n.get("description", "") for n in nodes}

        # 连接所有 agent
        for agent_info in discovered_agents:
            try:
                await self._connect(agent_info["name"], agent_info["url"])
            except Exception as e:
                print(f"[DagExecutor] ❌ Connect failed {agent_info['name']}: {e}")

        # 拓扑排序执行
        results: list[dict] = []
        node_summaries: dict[str, str] = {}   # node_id → 摘要（供下游使用）
        node_bodies: dict[str, str] = {}      # node_id → 正文
        _call_ids: dict[str, str] = {}        # node_id → call_id（用于上报）

        # 初始队列：入度为 0 的节点
        ready = deque([nid for nid, deg in in_degree.items() if deg == 0])
        remaining_in_degree = dict(in_degree)

        while ready:
            # 取出当前所有就绪节点，并行执行
            batch = []
            while ready:
                batch.append(ready.popleft())

            print(f"[DagExecutor] Executing batch: {batch}")

            # 批次开始：上报所有节点为 pending
            batch_parent_id = f"batch-{uuid.uuid4()}"
            for node_id in batch:
                agent_info = agent_by_node.get(node_id)
                agent_name = agent_info["name"] if agent_info else "N/A"
                call_id = f"dag-{node_id}-{uuid.uuid4()}"
                # 把 call_id 存起来供完成时使用
                _call_ids[node_id] = call_id
                await _report("/chat/call", {
                    "event_id": call_id,
                    "author": "DagExecutor",
                    "parts": [{
                        "function_call": {
                            "id": call_id,
                            "name": f"send_message [{agent_name}]",
                            "args": {
                                "agent_name": agent_name,
                                "task": desc_by_id.get(node_id, ""),
                                "node_id": node_id,
                                "parent_id": batch_parent_id if len(batch) > 1 else None,
                            },
                        }
                    }],
                })

            async def run_node(node_id: str) -> dict:
                agent_info = agent_by_node.get(node_id)
                task_desc = desc_by_id.get(node_id, "")
                call_id = _call_ids.get(node_id, f"dag-{node_id}")

                if not agent_info:
                    await _report("/chat/function", {
                        "event_id": call_id,
                        "author": "DagExecutor",
                        "function_response": {
                            "id": call_id,
                            "name": f"send_message [N/A]",
                            "response": {"status": "skipped", "response": "[SKIPPED] No agent assigned."},
                        },
                    })
                    return {
                        "node_id": node_id,
                        "agent": "N/A",
                        "task": task_desc,
                        "response": "[SKIPPED] No agent assigned.",
                        "status": "skipped",
                    }

                # 构建 prompt：基础任务 + 上游摘要 + 下游提示
                prompt_parts = [task_desc]

                # 注入上游摘要
                upstream_summaries = []
                for pred_id in predecessors.get(node_id, []):
                    s = node_summaries.get(pred_id, "")
                    if s:
                        upstream_summaries.append(
                            f"[来自上游节点 {pred_id} 的摘要]\n{s}"
                        )
                if upstream_summaries:
                    prompt_parts.insert(0, "【上游信息】\n" + "\n\n".join(upstream_summaries) + "\n\n【当前任务】")

                # 告知下游需求，要求输出摘要
                succ_ids = successors.get(node_id, [])
                if succ_ids:
                    succ_descs = [f"- {desc_by_id[s]}" for s in succ_ids if s in desc_by_id]
                    downstream_hint = (
                        "\n\n【下游依赖提示】\n"
                        "以下任务依赖你的输出，请在回复正文末尾额外输出一段精炼摘要，"
                        "格式为：\n"
                        "<SUMMARY_FOR_DOWNSTREAM>\n"
                        "（此处写给下游任务使用的关键信息摘要，不超过200字）\n"
                        "</SUMMARY_FOR_DOWNSTREAM>\n\n"
                        "下游任务：\n" + "\n".join(succ_descs)
                    )
                    prompt_parts.append(downstream_hint)

                full_prompt = "\n".join(prompt_parts)

                raw_response = await self._send(
                    agent_info["name"], full_prompt, context_id
                )
                body, summary = _strip_summary_tag(raw_response)
                status = "success" if not body.startswith("[ERROR]") else "failed"

                # 上报节点完成
                await _report("/chat/function", {
                    "event_id": call_id,
                    "author": "DagExecutor",
                    "function_response": {
                        "id": call_id,
                        "name": f"send_message [{agent_info['name']}]",
                        "response": {
                            "agent": agent_info["name"],
                            "node_id": node_id,
                            "task": task_desc,
                            "status": status,
                            "response": body[:500] + ("…" if len(body) > 500 else ""),
                            **({"summary": summary} if summary else {}),
                        },
                    },
                })

                return {
                    "node_id": node_id,
                    "agent": agent_info["name"],
                    "task": task_desc,
                    "response": body,
                    "summary": summary,
                    "status": status,
                }

            batch_results = await asyncio.gather(*[run_node(nid) for nid in batch])

            for res in batch_results:
                nid = res["node_id"]
                node_bodies[nid] = res["response"]
                node_summaries[nid] = res.pop("summary", "")
                results.append(res)

                # 更新入度，将新就绪节点加入队列
                for succ in successors.get(nid, []):
                    remaining_in_degree[succ] -= 1
                    if remaining_in_degree[succ] == 0:
                        ready.append(succ)

        # DAG 执行完毕，上报汇总 final
        summary_lines = [f"- [{r['agent']}] {r['task'][:60]} → {r['status']}" for r in results]
        await _report("/chat/final", {
            "event_id": f"dag-final-{uuid.uuid4()}",
            "author": "DagExecutor",
            "final_text": f"DAG 执行完成，共 {len(results)} 个节点\n" + "\n".join(summary_lines),
        })

        # 构建 agent 间信息传递记录（upstream→downstream summary）
        agent_messages = []
        for nid, summary in node_summaries.items():
            if summary:
                for succ in successors.get(nid, []):
                    agent_messages.append({
                        "fromNodeId": nid,
                        "toNodeId": succ,
                        "summary": summary,
                    })

        return results, agent_messages


class SubAgentCaller:
    """
    ADK LlmAgent 包装器。
    before_model_callback 中直接执行 DAG，跳过 LLM 调用。
    """

    def __init__(self):
        self.executor = DagExecutor()

    def create_agent(self, model) -> LlmAgent:
        return LlmAgent(
            model=model,
            name="SubAgentCaller",
            instruction="Execute sub-agent tasks.",
            before_model_callback=self.before_model_callback,
            description="Executes DAG of sub-agent tasks with parallel execution and dependency passing.",
            output_key="results",
        )

    async def before_model_callback(
        self,
        callback_context: CallbackContext,
        llm_request,
    ):
        state = callback_context.state
        dag = state.get("dag", {})
        discovered_agents = state.get("discovered_agents", [])
        context_id = state.get("context_id", str(uuid.uuid4()))

        if not dag or not discovered_agents:
            print("[SubAgentCaller] No DAG or agents in state, falling back to LLM.")
            return None

        print(f"[SubAgentCaller] Executing DAG with {len(dag.get('nodes', []))} nodes, "
              f"{len(dag.get('edges', []))} edges")

        import time as _time
        dag_start = _time.time()
        results, agent_messages = await self.executor.execute(dag, discovered_agents, context_id)
        dag_elapsed_ms = int((_time.time() - dag_start) * 1000)

        # 格式化结果写入 state
        result_lines = []
        for r in results:
            result_lines.append(
                f"### {r['agent']} — {r['task']}\n"
                f"状态: {r['status']}\n\n"
                f"{r['response']}"
            )
        results_text = "\n\n---\n\n".join(result_lines)
        state["results"] = results_text

        # 存执行数据，供 process_message_and_forward 在事件循环结束后提交给网关
        # 先建立 nodeId → summary 映射（每个节点最多一条，取第一条）
        node_summary_map = {m["fromNodeId"]: m["summary"] for m in agent_messages}
        state["execution_data"] = {
            "nodeResults": [
                {
                    "nodeId": r["node_id"],
                    "agentName": r["agent"],
                    "task": r["task"],
                    "status": r["status"],
                    "response": r["response"][:500] + ("…" if len(r["response"]) > 500 else ""),
                    "summaryToDownstream": node_summary_map.get(r["node_id"]),
                }
                for r in results
            ],
            "agentMessages": agent_messages,
            "totalNodes": len(results),
            "successNodes": sum(1 for r in results if r["status"] == "success"),
            "executionLatencyMs": dag_elapsed_ms,
        }

        print(f"[SubAgentCaller] DAG execution complete. {len(results)} nodes executed.")

        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(text=results_text)],
            )
        )


def create_sub_agent_caller(model) -> LlmAgent:
    caller = SubAgentCaller()
    return caller.create_agent(model)
