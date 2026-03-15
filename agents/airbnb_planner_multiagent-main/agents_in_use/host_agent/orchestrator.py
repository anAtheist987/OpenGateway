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

import asyncio
import os
import uuid
from datetime import datetime
from typing import Optional

import aiohttp
from dotenv import load_dotenv

load_dotenv()

from google.adk.agents import SequentialAgent, LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.lite_llm import LiteLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from sub_agent_caller import create_sub_agent_caller

_LISTENER_URL = "http://localhost:8084"


async def _report_to_listener(path: str, payload: dict) -> None:
    """Post an event to agent-listener (port 8084), silently ignoring failures."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{_LISTENER_URL}{path}", json=payload) as r:
                await r.text()
    except Exception:
        pass


# =========================
# Local vLLM (OpenAI-compatible) settings
# =========================
VLLM_BASE_URL = os.getenv("VLLM_BASE_URL", "http://127.0.0.1:8000/v1").rstrip("/")
VLLM_API_KEY = os.getenv("VLLM_API_KEY", "EMPTY")
VLLM_MODEL_ID = os.getenv("VLLM_MODEL_ID", "").strip()

GATEWAY_ADMIN_URL = os.getenv("GATEWAY_ADMIN_URL", "http://localhost:15000")
REGISTRY_BASE_URL = os.getenv("REGISTRY_BASE_URL", "http://localhost:8090")

# Agent name → keyword mapping（供 SubAgentCaller 识别）
AGENT_NAME_TO_KEYWORD = {
    "WeatherAgent": "weather",
    "FlightAgent": "transport",
    "HotelAgent": "hotel",
    "FinanceDocumentAgent": "finance",
    "InfoSecDocumentAgent": "infosec",
    "Dept Doc Reader Agent": "dept_doc",
}


def _build_local_llm(model_id: str) -> LiteLlm:
    return LiteLlm(
        model=model_id,
        custom_llm_provider="openai",
        base_url=VLLM_BASE_URL,
        api_key=VLLM_API_KEY,
    )


class GatewayPlannerAgent:
    """
    替代 PlanActions + SubAgentSearcher。
    在 before_model_callback 中调用网关 /task-router/route 完成：
      1. 任务复杂度评估
      2. 任务分解（DAG）
      3. 每个子任务的 Agent 分配（vectorPrefilterLlm）
    解析结果后直接存入 state["plan"] 和 state["discovered_agents"]，
    返回 types.Content 跳过 LLM 调用。
    """

    def create_agent(self, model) -> LlmAgent:
        return LlmAgent(
            model=model,
            name="GatewayPlanner",
            instruction="Present the decomposed travel plan.",
            before_model_callback=self.before_model_callback,
            output_key="plan",
        )

    async def before_model_callback(
        self,
        callback_context: CallbackContext,
        llm_request,
    ) -> Optional[types.Content]:
        # ── 1. 从 LLM 请求历史中提取用户输入 ─────────────────────────────────
        user_task = ""
        for content in (llm_request.contents or []):
            if getattr(content, "role", None) == "user":
                for part in (content.parts or []):
                    text = getattr(part, "text", None)
                    if text:
                        user_task = text
                        break
            if user_task:
                break

        if not user_task:
            print("[GatewayPlanner] No user task found, falling back to LLM.")
            return None

        print(f"[GatewayPlanner] User task: {user_task[:80]}...")

        # ── 2. 从 Registry 获取可用 Agent 列表 ────────────────────────────────
        agents = await self._fetch_agents()
        if not agents:
            print("[GatewayPlanner] No agents from registry, falling back to LLM.")
            return None

        # ── 3. 调用网关 /task-router/route ────────────────────────────────────
        try:
            result = await self._call_gateway(user_task, agents)
        except Exception as e:
            print(f"[GatewayPlanner] Gateway call failed: {e}, falling back to LLM.")
            return None

        print(f"[GatewayPlanner] Gateway result: complexity={result.get('complexityScore', '?')}, "
              f"type={result.get('decision', {}).get('type', '?')}")

        # ── 4. 解析网关响应，构建 discovered_agents ───────────────────────────
        decision = result.get("decision", {})
        decision_type = decision.get("type", "")
        discovered_agents: list[dict] = []
        plan_lines: list[str] = []

        if decision_type == "decomposed":
            dag = decision.get("dag", {})
            nodes = dag.get("nodes", [])
            for i, node in enumerate(nodes, 1):
                desc = node.get("description", "")
                node_id = node.get("id", f"t{i}")
                assigned = node.get("assignedAgent") or {}
                agent_name = assigned.get("agentName", "")
                agent_url = assigned.get("agentUrl", "")
                keyword = AGENT_NAME_TO_KEYWORD.get(agent_name, "unknown")

                if agent_name and agent_url:
                    discovered_agents.append({
                        "node_id": node_id,
                        "name": agent_name,
                        "url": agent_url,
                        "keyword": keyword,
                        "task": desc,
                    })
                    plan_lines.append(f"{i}. {desc}  (agent: {keyword})")
                else:
                    plan_lines.append(f"{i}. {desc}  (no agent assigned)")

        elif decision_type == "direct":
            agent_name = decision.get("agentName", "")
            agent_url = decision.get("agentUrl", "")
            keyword = AGENT_NAME_TO_KEYWORD.get(agent_name, "unknown")
            if agent_name and agent_url:
                discovered_agents.append({
                    "node_id": "t1",
                    "name": agent_name,
                    "url": agent_url,
                    "keyword": keyword,
                    "task": user_task,
                })
                plan_lines.append(f"1. {user_task}  (agent: {keyword})")

        else:
            print(f"[GatewayPlanner] Unknown decision type: {decision_type}, falling back to LLM.")
            return None

        if not discovered_agents:
            print("[GatewayPlanner] No agents assigned by gateway, falling back to LLM.")
            return None

        # ── 5. 写入 state ─────────────────────────────────────────────────────
        state = callback_context.state
        state["task_id"] = result.get("taskId", str(uuid.uuid4()))
        state["discovered_agents"] = discovered_agents
        # 保存完整 DAG（含 edges）供 DagExecutor 使用
        if decision_type == "decomposed":
            state["dag"] = decision.get("dag", {})
        else:
            # direct 路由：构造单节点 DAG
            state["dag"] = {
                "nodes": [{"id": "t1", "description": user_task}],
                "edges": [],
            }
        today = datetime.now().strftime("%A, %B %d, %Y")
        plan_text = (
            f"[Gateway Decomposition — {today}]\n\n"
            + "\n".join(plan_lines)
            + f"\n\nReason: {decision.get('reason', '')}"
        )
        state["plan"] = plan_text

        print(f"[GatewayPlanner] Assigned {len(discovered_agents)} agents: "
              f"{[a['name'] for a in discovered_agents]}")

        # ── 6. Post decomposition plan to agent-listener so the UI can render it ─
        await _report_to_listener("/chat/final", {
            "event_id": f"gw-plan-{uuid.uuid4()}",
            "author": "GatewayPlanner",
            "final_text": plan_text,
        })

        # ── 7. 返回 LlmResponse 跳过 LLM 调用 ───────────────────────────────
        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(text=plan_text)],
            )
        )

    async def _fetch_agents(self) -> list[dict]:
        """从 mock_registry GET /agents 获取真实 Agent 列表。"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{REGISTRY_BASE_URL}/agents",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    data = await resp.json()
                    return [a for a in data if a.get("is_real", True)]
        except Exception as e:
            print(f"[GatewayPlanner] Failed to fetch agents from registry: {e}")
            return []

    async def _call_gateway(self, task: str, agents: list[dict]) -> dict:
        """调用网关 /task-router/route，使用 vectorPrefilterLlm 策略。"""
        agent_infos = [
            {
                "name": a["name"],
                "description": a["description"],
                "url": a["url"],
                "skills": a.get("tags", []),
            }
            for a in agents
        ]
        payload = {
            "task": task,
            "agents": agent_infos,
            "strategyOverride": "vectorPrefilterLlm",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{GATEWAY_ADMIN_URL}/task-router/route",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                resp.raise_for_status()
                return await resp.json()


class OrchestratorAgent:
    """Orchestrator agent that coordinates multiple LlmAgents in sequence."""

    def __init__(self):
        self.gateway_planner = None
        self.caller = None
        self.summarizer = None
        self.orchestrator = None

    async def _async_init_components(self) -> None:
        print("Initializing Orchestrator components...")

        model_id = VLLM_MODEL_ID or "default"
        print(f"[vLLM] base_url={VLLM_BASE_URL}")
        print(f"[vLLM] model_id={model_id}")

        local_llm = _build_local_llm(model_id)

        # GatewayPlanner: 替代 PlanActions + SubAgentSearcher
        print("Creating Gateway Planner Agent...")
        planner_agent = GatewayPlannerAgent()
        self.gateway_planner = planner_agent.create_agent(local_llm)
        print("Gateway Planner Agent created successfully.")

        print("Creating Sub-Agent Caller...")
        self.caller = create_sub_agent_caller(local_llm)
        print("Sub-Agent Caller created successfully.")

        self.summarizer = LlmAgent(
            name="ResultSummarizer",
            instruction=lambda c: f"""You are a Result Synthesis and Presentation Agent. Your role is to transform raw data from multiple agents into a comprehensive, user-friendly final report.

**LANGUAGE INSTRUCTION:**
- ALWAYS respond in the SAME language as the original user input
- If the user wrote in Chinese (中文), respond in Chinese
- If the user wrote in English, respond in English
- Match the language used in the user's original query

**IMPORTANT CONTEXT:**
- Today's date is: {datetime.now().strftime("%A, %B %d, %Y")}
- Use this date context when interpreting and presenting temporal information in the results

**Raw Results from Sub-Agents:**
{c.state.get('results', 'No results available.')}

**Your Responsibilities:**
1. **Synthesize** information from all sub-tasks executed by the sub-agent caller
2. **Identify** connections and patterns across different data sources, especially linking travel options to company policy requirements
3. **Highlight** key insights, important details, and actionable recommendations
4. **Organize** information into the three-page structure defined below
5. **Enrich** the response with context and explanations where helpful
6. **Present** data in a visually appealing format (use markdown formatting)

**REQUIRED OUTPUT STRUCTURE — Three Pages:**

When the results include travel planning (flights, hotels, weather) AND document/policy data (finance, infosec, dept_doc), structure the output as follows:

---

### 📋 第一页：方案页（出行方案）

Present multiple candidate options for each category:

**航班方案**（多套候选）:
- 列出每个候选航班的：出发/到达时间、航司、价格、准点率
- 标注是否满足用户偏好（如：不含红眼、准点安全优先）
- 关联财务/部门政策条款，给出推荐理由（例如：符合差旅标准、在报销限额内）

**酒店方案**（多套候选）:
- 列出每个候选酒店的：名称、价格/晚、距会场距离/时间、评分
- 标注是否满足用户偏好（如：30分钟内到达会场）
- 关联财务政策的住宿标准，给出推荐理由

**天气提醒**:
- 出行日期的天气预报摘要
- 对行程的影响提示

---

### 📝 第二页：流程页（审批/报销流程）

Based on finance, infosec, and dept_doc agent results:

**审批流程**（按步骤列出）:
1. 步骤一：[来源：哪个部门文件]
2. 步骤二：...
...

**备案要求**:
- 列出需要提前备案的事项及截止时间

**报销流程与材料清单**:
- 需要准备的单据/凭证（逐项列出，注明来源部门）
- 报销金额限制（引用具体政策条款）
- 提交方式与时限

**信息安全要求**（来自 InfoSec 文件）:
- 出境设备要求
- 数据保护措施

---

### ✅ 第三页：检查页（关键约束核验）

For each key constraint, check whether it is satisfied:

| 约束项 | 状态 | 说明 |
|--------|------|------|
| 不坐红眼 | ✅/❌/⚠️ | 推荐方案中是否有符合的选项 |
| 酒店离会场≤30分钟 | ✅/❌/⚠️ | 是否有符合距离要求的酒店 |
| 准点率与安全优先 | ✅/❌/⚠️ | 推荐航班的准点/安全记录 |
| 费用在报销标准内 | ✅/❌/⚠️ | 引用具体报销限额 |
| 审批材料完整 | ✅/❌/⚠️ | 清单是否完整 |
| 信息安全合规 | ✅/❌/⚠️ | 是否满足 InfoSec 要求 |

**缺失信息提示**（如有）:
- ⚠️ 如果某项信息无法从代理结果中获取，明确提示用户需要补充什么

---

**If the request does NOT include approval/reimbursement content** (pure travel planning), use the standard format:
- Executive Summary
- Detailed Sections by topic
- Key Insights and Recommendations

**Formatting Guidelines:**
- Use **bold** for important information
- Use bullet points for lists
- Include relevant emojis for better readability (✈️ 🏨 🌤️ 📋 ✅ ⚠️ ❌)
- Use tables for comparing options
- Add section headers with ###

Your goal is to provide a polished, professional, and actionable final report that exceeds user expectations.""",
            model=local_llm,
        )

        self.orchestrator = SequentialAgent(
            name="Orchestrator",
            sub_agents=[self.gateway_planner, self.caller, self.summarizer],
        )

        print("Orchestrator components initialized successfully.")

    @classmethod
    async def create(cls) -> "OrchestratorAgent":
        instance = cls()
        await instance._async_init_components()
        return instance

    def get_agent(self) -> SequentialAgent:
        return self.orchestrator


def _get_initialized_orchestrator_sync() -> SequentialAgent:
    async def _async_main() -> SequentialAgent:
        orchestrator_instance = await OrchestratorAgent.create()
        return orchestrator_instance.get_agent()

    try:
        return asyncio.run(_async_main())
    except RuntimeError as e:
        if 'asyncio.run() cannot be called from a running event loop' in str(e):
            print(
                f'Warning: Could not initialize Orchestrator with asyncio.run(): {e}. '
                'This can happen if an event loop is already running (e.g., in Jupyter). '
                'Consider initializing Orchestrator within an async function in your application.'
            )
        raise


orchestrator = _get_initialized_orchestrator_sync()
