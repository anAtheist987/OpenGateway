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

"""Document extraction agent: extracts constraints, recommendations, process steps from policy docs."""
import os
import sys

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools.mcp_tool.mcp_toolset import (
    MCPToolset,
    StdioConnectionParams,
)
from mcp import StdioServerParameters


def _normalize_api_base(url: str | None) -> str | None:
    if not url:
        return None
    clean = url.strip().rstrip('/')
    if not clean:
        return None
    return clean


def _resolve_document_llm_config() -> tuple[str, str | None, str | None]:
    mode = os.getenv('DOCUMENT_LLM_MODE', 'api').strip().lower()
    if mode == 'local':
        model = os.getenv(
            'DOCUMENT_LOCAL_MODEL',
            os.getenv('LITELLM_MODEL', 'openai/qwen3.5-27b-fp8'),
        ).strip()
        api_base = _normalize_api_base(
            os.getenv('DOCUMENT_LOCAL_API_BASE', 'http://localhost:8888')
        )
        api_key = os.getenv('DOCUMENT_LOCAL_API_KEY', 'EMPTY').strip()
    else:
        model = os.getenv(
            'DOCUMENT_API_MODEL',
            os.getenv('LITELLM_MODEL', 'openai/qwen3.5-27b-fp8'),
        ).strip()
        api_base = _normalize_api_base(
            os.getenv('DOCUMENT_API_BASE', 'https://api.anthropic.com')
        )
        api_key = os.getenv('DOCUMENT_API_KEY', '').strip()
    if api_base and 'claude' in model.lower() and api_base.endswith('/v1'):
        api_base = api_base[:-3]
    return model, api_base, api_key


def create_document_agent() -> LlmAgent:
    """Constructs the Document Extraction ADK agent."""
    LITELLM_MODEL, api_base, api_key = _resolve_document_llm_config()
    model_lower = LITELLM_MODEL.lower()
    is_anthropic = 'claude' in model_lower or model_lower.startswith('anthropic/')
    if is_anthropic:
        if api_base:
            os.environ['ANTHROPIC_API_BASE'] = api_base
        if api_key:
            os.environ['ANTHROPIC_API_KEY'] = api_key
    else:
        if api_base:
            os.environ['OPENAI_API_BASE'] = api_base
        if api_key:
            os.environ['OPENAI_API_KEY'] = api_key

    base_dir = os.path.dirname(os.path.abspath(__file__))
    mcp_server_script = os.path.join(base_dir, 'document_mcp.py')

    instruction = """You are a Document Extraction agent in an A2A multi-agent system.

Your scope is STRICTLY document/policy extraction. Do NOT generate trip plans.
If user text includes requests like flight/hotel/weather, ignore those parts and only return policy process/checklist extraction.

You MUST use the provided MCP tools:
- extract_constraints_and_process(doc_text, categories?)
- extract_from_file(file_content_base64, file_extension, categories?)
- extract_from_html(html_content, categories?)
- list_portal_notices(department?, category?, limit?)
- search_portal_notices(query, department?, category?, limit?)
- extract_from_portal_notices(notice_ids, categories?, include_attachment?)
- list_extraction_categories()

Default workflow for plain user text:
1) First call search_portal_notices using key terms from the user query.
2) Select top relevant notice IDs (usually <=3), then call extract_from_portal_notices.
3) Focus on process extraction and checklist for approval/filing/reimbursement.
4) Never extract all notices unless explicitly requested.

Output contract (MANDATORY):
- Return ONLY one JSON object (no markdown, no prose, no headings).
- JSON keys MUST be exactly:
  {
    "process_steps": [...],
    "material_checklist": [...],
    "warnings": [...]
  }
- process_steps item format:
  {
    "step_order": 1,
    "stage": "approval|filing|reimbursement",
    "name": "步骤名称",
    "materials": ["材料1", "材料2"],
    "source": "公告ID或标题"
  }
- If some requested parts are out of scope (e.g. flight/hotel/weather), put one concise note in warnings instead of generating extra content.
- Keep response concise and machine-readable for A2A downstream consumption.

Respond in the same language as the user (Chinese or English)."""

    return LlmAgent(
        model=LiteLlm(model=LITELLM_MODEL),
        name='document_agent',
        description='Extracts department requirements and key clauses from travel/policy documents into constraints, recommendations, process steps, and material checklist for the gateway.',
        instruction=instruction,
        tools=[
            MCPToolset(
                connection_params=StdioConnectionParams(
                    server_params=StdioServerParameters(
                        command=sys.executable,
                        args=[mcp_server_script],
                    ),
                    timeout=120.0,
                ),
            )
        ],
    )
