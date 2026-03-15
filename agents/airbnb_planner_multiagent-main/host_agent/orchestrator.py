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
from datetime import datetime
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

from google.adk.agents import SequentialAgent, LlmAgent
from google.adk.models.lite_llm import LiteLlm
from sub_agent_searcher import create_sub_agent_searcher
from sub_agent_caller import create_sub_agent_caller


# =========================
# Local vLLM (OpenAI-compatible) settings
# =========================
VLLM_BASE_URL = os.getenv("VLLM_BASE_URL", "http://127.0.0.1:8000/v1").rstrip("/")
VLLM_API_KEY = os.getenv("VLLM_API_KEY", "EMPTY")
VLLM_MODEL_ID = os.getenv("VLLM_MODEL_ID", "").strip()


def _fetch_first_model_id(api_base: str) -> str:
    """从 vLLM OpenAI-compatible /v1/models 拿第一个 model id"""
    url = f"{api_base}/models"
    r = requests.get(url, timeout=5)
    r.raise_for_status()
    data = r.json()
    models = data.get("data", [])
    if not models:
        raise RuntimeError(f"No models returned from {url}: {data}")
    mid = models[0].get("id")
    if not mid:
        raise RuntimeError(f"Invalid /models response from {url}: {data}")
    return mid


def _build_local_llm(model_id: str) -> LiteLlm:
    """
    ✅ 关键修复：
    - 用 custom_llm_provider="openai" 告诉 litellm 这是 OpenAI-compatible
    - 用 base_url=... (litellm 内部读取的是 base_url，不是 api_base)
    """
    return LiteLlm(
        model=model_id,                     # vLLM 需要原始 model id（你的 path）
        custom_llm_provider="openai",        # ✅ 告诉 litellm 用 openai provider
        base_url=VLLM_BASE_URL,             # ✅ 注意：必须是 base_url
        api_key=VLLM_API_KEY,
    )


class OrchestratorAgent:
    """Orchestrator agent that coordinates multiple LlmAgents in sequence."""

    def __init__(self):
        self.planner = None
        self.searcher = None
        self.caller = None
        self.summarizer = None
        self.orchestrator = None

    async def _async_init_components(self) -> None:
        print("Initializing Orchestrator components...")

        model_id = VLLM_MODEL_ID or "default"

        print(f"[vLLM] base_url={VLLM_BASE_URL}")
        print(f"[vLLM] model_id={model_id} (no eager check)")

        local_llm = _build_local_llm(model_id)
        print(f"[vLLM] base_url={VLLM_BASE_URL}")
        print(f"[vLLM] model_id={model_id}")

        local_llm = _build_local_llm(model_id)

        self.planner = LlmAgent(
            name="PlanActions",
            instruction=lambda c: f"""You are a Task Decomposition Planner. Your role is to analyze complex user requests and break them down into clear, actionable sub-tasks.

**LANGUAGE INSTRUCTION:**
- ALWAYS respond in the SAME language as the user's input
- If the user writes in Chinese (中文), respond in Chinese
- If the user writes in English, respond in English
- Detect the language from the user's query and match it exactly

**IMPORTANT CONTEXT:
- Today's date is: {datetime.now().strftime("%A, %B %d, %Y")}
- Use this date to interpret relative time expressions like "this weekend", "next week", "tomorrow", etc.
- When planning tasks involving dates, always provide specific dates based on today's date

**Your Responsibilities:**
1. **Analyze** the user's request to understand their complete needs
2. **Identify** all required information: weather, accommodations, flights, hotels, events, attractions, restaurants, or financial data
3. **Decompose** the complex request into multiple independent sub-tasks
4. **Prioritize** sub-tasks in logical order (e.g., check weather before planning activities)
5. **Specify** what information each sub-task should gather
6. **Define** dependencies between sub-tasks if any exist
7. **Include specific dates** in your plan when the user mentions relative time periods

**Output Format:**
Provide a structured plan with:
- List of sub-tasks (numbered)
- For each sub-task: clear objective, required agent type, expected output
- Dependencies between tasks if applicable
- Overall goal of the plan
- Specific dates when applicable

**Example:**
User: "Plan a weekend trip to Los Angeles"
Your output:
1. Check weather forecast for Los Angeles this weekend (Saturday, January 25 and Sunday, January 26)
2. Find available accommodations in Los Angeles for January 25-26
3. Search for local events and attractions during that weekend
4. Get restaurant recommendations
5. Estimate total trip cost

Do NOT execute tasks - only create the plan. The next agent will search for appropriate sub-agents to execute this plan.""",
            output_key="plan",
            model=local_llm,
        )

        print("Creating Sub-Agent Searcher...")
        self.searcher = create_sub_agent_searcher(local_llm)
        print("Sub-Agent Searcher created successfully.")

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

**IMPORTANT CONTEXT:
- Today's date is: {datetime.now().strftime("%A, %B %d, %Y")}
- Use this date context when interpreting and presenting temporal information in the results

**Raw Results from Sub-Agents:**
{c.state.get('results', 'No results available.')}

**Your Responsibilities:**
1. **Synthesize** information from all sub-tasks executed by the sub-agent caller
2. **Identify** connections and patterns across different data sources
3. **Highlight** key insights, important details, and actionable recommendations
4. **Organize** information in a logical, easy-to-follow structure
5. **Enrich** the response with context and explanations where helpful
6. **Present** data in a visually appealing format (use markdown formatting)
7. **Add** practical suggestions based on the gathered information

**Output Format:**
- **Executive Summary**: Brief overview of findings
- **Detailed Sections**: Organized by topic (weather, accommodations, activities, etc.)
  - Use bullet points, numbered lists, and tables where appropriate
  - Highlight prices, dates, and important warnings
- **Key Insights**: Connect information across different agents
- **Recommendations**: Practical next steps for the user
- **Summary**: Final thoughts and suggestions

**Formatting Guidelines:**
- Use **bold** for important information
- Use bullet points for lists
- Include relevant emojis for better readability (🌤️ ☀️ 🏨 🍽️ 💰)
- Use tables for comparing options
- Add section headers with ###

Your goal is to provide a polished, professional, and actionable final report that exceeds user expectations.""",
            model=local_llm,
        )

        self.orchestrator = SequentialAgent(
            name="Orchestrator",
            sub_agents=[self.planner, self.searcher, self.caller, self.summarizer],
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