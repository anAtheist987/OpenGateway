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

"""Finance-specific document extraction agent."""
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


def _resolve_llm_config() -> tuple[str, str | None, str | None]:
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


def create_finance_document_agent() -> LlmAgent:
    model_name, api_base, api_key = _resolve_llm_config()
    model_lower = model_name.lower()
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
    mcp_server_script = os.path.join(base_dir, 'finance_document_mcp.py')

    instruction = """You are the Finance Department Document Agent in an A2A system.

You only process finance-related policy extraction and checklist generation.
Your source data is reimbursement portal notices limited to finance department.

You MUST use MCP tools:
- list_finance_notices(limit?)
- search_finance_notices(query, limit?)
- extract_finance_notices(notice_ids, categories?, include_attachment?)
- extract_constraints_and_process(doc_text, categories?)
- extract_from_file(file_content_base64, file_extension, categories?)
- extract_from_html(html_content, categories?)
- list_extraction_categories()

Default workflow:
1) Search notices with finance keywords from user query.
2) Extract from top relevant notices (<= 3 by default).
3) Focus on finance compliance: reimbursement scope, grade-based standards, invoice requirements, approval and audit checkpoints.

Mandatory output:
- Return ONLY one JSON object (no markdown/prose).
- Keys MUST be exactly:
  {
    "process_steps": [...],
    "material_checklist": [...],
    "warnings": [...]
  }
- If user asks non-document tasks (flight/hotel/weather), mention out-of-scope in warnings only.

Respond in the same language as the user."""

    return LlmAgent(
        model=LiteLlm(model=model_name),
        name='finance_document_agent',
        description='Extracts finance-department policy process and reimbursement material checklist from portal notices.',
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

