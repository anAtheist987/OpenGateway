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
import asyncio
import json
import os
import uuid
from typing import Any, Optional, List, Tuple
from datetime import datetime
from typing import Any

import httpx

from a2a.client import A2ACardResolver
from a2a.types import (
    AgentCard,
    MessageSendParams,
    Part,
    SendMessageRequest,
    SendMessageResponse,
    SendMessageSuccessResponse,
    Task,
)
from dotenv import load_dotenv
from google.adk import Agent
from google.adk.agents import LlmAgent
from google.adk.planners import BuiltInPlanner
from google.genai import types
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.tool_context import ToolContext
from remote_agent_connection import (
    # RemoteAgentConnections,
    TaskUpdateCallback,
)

from routing_remote_agent_connection import RemoteAgentConnections
from registry_client import RegistryClient
from registry_models import RegistryListReq, RegistryListResp

load_dotenv()


def convert_part(part: Part, tool_context: ToolContext):
    """Convert a part to text. Only text parts are supported."""
    if part.type == 'text':
        return part.text

    return f'Unknown type: {part.type}'


def convert_parts(parts: list[Part], tool_context: ToolContext):
    """Convert parts to text."""
    rval = []
    for p in parts:
        rval.append(convert_part(p, tool_context))
    return rval


def create_send_message_payload(
    text: str, task_id: str | None = None, context_id: str | None = None
) -> dict[str, Any]:
    """Helper function to create the payload for sending a task."""
    payload: dict[str, Any] = {
        'message': {
            'role': 'user',
            'parts': [{'type': 'text', 'text': text}],
            'messageId': uuid.uuid4().hex,
        },
    }

    if task_id:
        payload['message']['taskId'] = task_id

    if context_id:
        payload['message']['contextId'] = context_id
    return payload


class RoutingAgent:
    """The Routing agent.

    This is the agent responsible for choosing which remote seller agents to send
    tasks to and coordinate their work.
    """

    def __init__(
        self,
        registry_base_url: Optional[str] = None,
        task_callback: TaskUpdateCallback | None = None,
    ):
        # self.task_callback = task_callback
        # self.remote_agent_connections: dict[str, RemoteAgentConnections] = {}
        # self.cards: dict[str, AgentCard] = {}
        # self.agents: str = ''
        registry_url = registry_base_url or os.getenv("REGISTRY_BASE_URL", "http://localhost:8000")
        print(f"\n📡 Initializing RoutingAgent with Registry URL: {registry_url}")
        print(f"   To change, set REGISTRY_BASE_URL environment variable.\n")
        
        self.registry = RegistryClient(
            registry_url,
            timeout=60.0  # 增加超时时间
        )
        self._connections: dict[str, RemoteAgentConnections] = {}

    # -------- 路由：调 Registry 并构造直连连接 --------
    async def resolve_client(
        self, keyword: str, task: str, top_k: int
    ) -> Tuple[List[Tuple[str, str]], List[dict]]:
        """
        调用 Registry，按 score 降序选取前 k 个候选，
        返回:
            results: [(agent_name, url)]
            agents_clean: 去掉 score / agent_id 的完整 agent 信息
        """
        # === 构造请求（纯字典） ===
        req = {
            "request_id": f"req-{uuid.uuid4()}",
            "task": task,
            "top_k": top_k,
        }

        resp = None
        resp: dict = (await self.registry.list_agents(keyword, req)).model_dump()
        if not resp or resp.get("status") != "success" or not resp.get("agents"):
            raise LookupError(
                f"No agent candidates for keyword='{keyword}', task='{task[:80]}...'"
            )

        agents = resp["agents"]

        # === 按分数降序排序（缺 score 的按 0 处理） ===
        agents_sorted = sorted(agents, key=lambda a: float(a.get("score", 0.0)), reverse=True)

        # === 限制 top_k ===
        k = min(int(top_k), len(agents_sorted))
        if k <= 0:
            raise LookupError("No valid candidates after sorting.")

        selected = agents_sorted[:k]

        # === 构造 results & connections ===
        results: List[Tuple[str, str]] = []
        for item in selected:
            name = item.get("name", "")
            url = item.get("url", "")
            if not name or not url:
                continue
            results.append((name, url))
            self._connections[name] = url

        if not results:
            raise LookupError("Candidates exist but none has valid name/url.")

        # === 构造 agents_clean（去掉 score 和 agent_id） ===
        agents_clean: List[dict] = []
        for agent in selected:
            clean = {k: v for k, v in agent.items() if k not in {"agent_id", "score"}}
            agents_clean.append(clean)

        return results, agents_clean

    # -------- 入口：路由 + 发送消息 + 解析 --------
    async def send_message_to_agent(
            self,
            keyword: str,
            task: str,
            tool_context: ToolContext,
            top_k: int = 3,
    ) -> Optional[Task]:
        """
        先路由（取 Top-k），再按得分从高到低依次直连 /messages。
        返回第一个成功的 Task；若都失败，返回 None（并在异常中提供聚合信息也可选）。
        """
        state = tool_context.state

        # 1) 路由：拿前 k 个候选
        candidates, _ = await self.resolve_client(keyword=keyword, task=task, top_k=top_k)

        # 2) 固定一次 context_id（多次重试保持同一会话）
        context_id = state.get("context_id") or str(uuid.uuid4())
        state["context_id"] = context_id

        input_meta = state.get("input_message_metadata") or {}

        # 3) 逐个候选尝试发送
        errors: list[str] = []
        for agent_name, connection in candidates:
            state["active_agent"] = agent_name
            message_id = input_meta.get("message_id") or uuid.uuid4().hex

            payload: dict[str, Any] = {
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": task}],
                    "messageId": message_id,
                    "contextId": context_id,
                    # "metadata": input_meta,  # 远端支持时再打开
                }
            }

            message_request = SendMessageRequest(
                id=message_id, params=MessageSendParams.model_validate(payload)
            )

            try:
                send_response: SendMessageResponse = await connection.send_message(message_request)
            except Exception as e:
                errors.append(f"{agent_name}: request failed ({e})")
                continue

            # 校验 A2A 响应
            if not isinstance(send_response.root, SendMessageSuccessResponse):
                errors.append(f"{agent_name}: non-success response")
                continue
            if not isinstance(send_response.root.result, Task):
                errors.append(f"{agent_name}: success wrapper but no Task")
                continue

            #  首个成功即返回
            return send_response.root.result

        # 所有候选都失败
        # print 或 log 一下便于排查
        if errors:
            print("All candidates failed:\n" + "\n".join(errors))
        return None



