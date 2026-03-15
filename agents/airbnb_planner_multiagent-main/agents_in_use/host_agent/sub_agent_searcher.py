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
Sub Agent Searcher
负责接收 planner 的计划，分解任务，并从注册中心查找合适的 sub agents
"""
import os
import sys
from typing import Any
from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools.tool_context import ToolContext

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
PARENT_DIR = os.path.dirname(PROJECT_ROOT)
sys.path.insert(0, PARENT_DIR)

from routing_agent import RoutingAgent as RegistryRoutingAgent


ALLOWED_KEYWORDS = {"weather", "transport", "hotel", "finance", "infosec", "dept_doc"}


class SubAgentSearcher:
    """
    Sub Agent Searcher Agent
    职责：
    1. 接收 planner 生成的计划
    2. 将计划分解为多个子任务
    3. 为每个子任务从注册中心查找合适的 sub agent
    4. 将结果保存到 context.state 供后续 agent 使用
    """

    # Explicitly enumerate the 6 permitted sub-agents (keyword → display name)
    AGENT_REGISTRY = {
        "weather":   "Weather Agent        (weather_agent_claude,    port 10001)",
        "transport": "Flight Agent         (flight_agent_claude,     port 10006)",
        "hotel":     "Hotel Agent          (hotel_agent_claude,      port 10007)",
        "finance":   "Finance Document Agent   (finance_document_agent,  port 10009)",
        "infosec":   "InfoSec Document Agent   (infosec_document_agent,  port 10010)",
        "dept_doc":  "Dept Doc Reader Agent    (dept_doc_reader_agent,   port 10011)",
    }

    def __init__(self):
        self.registry_base_url = os.getenv("REGISTRY_BASE_URL")
        if not self.registry_base_url:
            raise ValueError("REGISTRY_BASE_URL environment variable not set")
    
    def create_agent(self, model) -> LlmAgent:
        """Create the Sub Agent Searcher LlmAgent."""
        return LlmAgent(
            model=model,
            name='SubAgentSearcher',
            instruction=self.root_instruction,
            description='Analyzes plan and searches for appropriate sub-agents from registry',
            tools=[self.search_agents],
            output_key="agent_search_results"
        )
    
    def root_instruction(self, context: ReadonlyContext) -> str:
        """Generate instruction for the agent."""
        plan = context.state.get('plan', 'No plan available.')
        
        return f"""You are a Sub-Agent Discovery Specialist. Your role is to analyze task plans and identify which specialized agents are needed.

**LANGUAGE INSTRUCTION:**
- ALWAYS respond in the SAME language as the plan
- If the plan is in Chinese (中文), respond in Chinese
- If the plan is in English, respond in English

**Plan from Planner:**
{plan}

**Your Responsibilities:**
1. **Analyze** the plan and identify distinct sub-tasks
2. **Extract keywords** for each sub-task to search for appropriate agents
3. **Call search_agents** tool for each identified sub-task with appropriate keyword
4. **Aggregate** all search results

**Available Keywords for Agent Search (ONLY these 6 are valid):**
- `"weather"` → for weather forecasts, climate, temperature
- `"transport"` → for flights, transportation
- `"hotel"` → for hotel search, accommodation recommendations
- `"finance"` → for financial reimbursement policy, expense approval, budget documents
- `"infosec"` → for information security, secrecy/confidentiality policy, device/data requirements for travel
- `"dept_doc"` → for procurement, foreign affairs, safety department policy documents and approval processes

**Instructions:**
- For each sub-task in the plan, determine the most appropriate keyword from the list above
- Call `search_agents(keyword, task_description, topk)` for each sub-task
- topk=1 for document agents (finance/infosec/dept_doc), topk=3 for travel agents
- The tool will return agent information (name, url, agent_card)
- After all searches complete, provide a summary of found agents

**Example:**
If plan has:
1. Search non-red-eye flights from Shanghai to Singapore
2. Find hotels near conference area
3. Check weather for Singapore
4. Extract financial reimbursement policy
5. Extract infosec requirements for overseas travel
6. Extract foreign-affairs department approval requirements

You should call:
- search_agents(keyword="transport", task="Search non-red-eye flights from Shanghai to Singapore", topk=3)
- search_agents(keyword="hotel", task="Find hotels near conference area in Singapore within 30 min", topk=3)
- search_agents(keyword="weather", task="Check weather forecast for Singapore", topk=3)
- search_agents(keyword="finance", task="Extract financial reimbursement policy for overseas travel", topk=1)
- search_agents(keyword="infosec", task="Extract infosec requirements for overseas travel", topk=1)
- search_agents(keyword="dept_doc", task="Extract foreign-affairs department approval requirements; department_id=foreign", topk=1)

Start by analyzing the plan and making the necessary search_agents calls."""
    
    async def search_agents(
        self, 
        keyword: str, 
        task: str, 
        topk: int,
        tool_context: ToolContext
    ) -> dict[str, Any]:
        """
        Search for appropriate agents from registry based on keyword and task.
        
        Args:
            keyword: Search keyword (weather, transport, hotel, finance, infosec, dept_doc)
            task: Task description to help registry find best match
            topk: Number of top matching agents to return
            tool_context: ADK tool context for state management
            
        Returns:
            Structured agent information including names, URLs, and agent cards
        """
        # Enforce allowed-keyword constraint: only the 6 designated sub-agents may be called
        if keyword not in ALLOWED_KEYWORDS:
            return {
                "keyword": keyword,
                "task": task,
                "error": (
                    f"Keyword '{keyword}' is not allowed. "
                    f"Only these keywords are permitted: {sorted(ALLOWED_KEYWORDS)}"
                ),
                "found_agents": [],
                "total_candidates": 0,
                "message": f"Rejected keyword '{keyword}' — not in allowed set.",
            }

        # Query registry
        router = RegistryRoutingAgent(self.registry_base_url)
        topk_list, agent_list = await router.resolve_client(keyword, task, topk)
        
        # Extract structured information
        agent_infos = []
        for name, url in topk_list:
            agent_infos.append({
                "name": name,
                "url": url,
                "keyword": keyword,
                "task": task
            })
        
        # Save to state for next agent to use
        state = tool_context.state
        if "discovered_agents" not in state:
            state["discovered_agents"] = []
        
        # Append new discoveries
        state["discovered_agents"].extend(agent_infos)
        
        # Also save raw registry response
        if "registry_responses" not in state:
            state["registry_responses"] = {}
        
        state["registry_responses"][keyword] = {
            "topk_list": topk_list,
            "agent_list": agent_list,
            "task": task
        }
        
        return {
            "keyword": keyword,
            "task": task,
            "found_agents": agent_infos,
            "total_candidates": len(agent_list),
            "message": f"Found {len(agent_infos)} agents for keyword '{keyword}'"
        }


def create_sub_agent_searcher(model) -> LlmAgent:
    """Factory function to create SubAgentSearcher agent."""
    searcher = SubAgentSearcher()
    return searcher.create_agent(model)
