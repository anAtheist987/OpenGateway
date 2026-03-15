<!-- Copyright 2026 Tsinghua University. Licensed under Apache 2.0.
     This file was created by Tsinghua University and is not part of
     the original agentgateway project by Solo.io. -->

# Document Agent

[中文文档](README_CN.md)

## Overview

Document Agent is an A2A agent that extracts **constraints**, **recommendations**, **process steps**, and **material checklist** from enterprise travel and policy documents. It enables the Host (gateway) to "obtain department terms and personal data and turn them into constraints or recommendation conditions" without stuffing full documents into the LLM context.

For A2A service calls, the agent is tuned to return a concise, machine-readable process payload by default (instead of long narrative text):
- `process_steps`
- `material_checklist`
- `warnings`

**Key features:**
- Extract hard constraints (e.g. "6h+ flight → business class", "protocol hotels only") with source references
- Extract soft recommendations (e.g. "prefer protocol hotel rates") for recommendation reasons on the solution page
- Extract approval/filing/reimbursement steps and required materials for the process page
- Output structured JSON with `source` / `source_excerpt` for auditability

**Port:** `10008`

## MCP tools

- **extract_constraints_and_process(doc_text, categories?)**  
  Extract from **plain text**. Returns JSON: `constraints`, `recommendations`, `process_steps`, `material_checklist`, `warnings`. Optional `categories` to limit scope (e.g. `["transport", "accommodation", "approval"]`).
- **extract_from_file(file_content_base64, file_extension, categories?)**  
  Extract from **file**. Supports **PDF (.pdf), Word (.docx), Excel (.xlsx)**. Arguments: file content as Base64 string, extension (e.g. `"pdf"`, `"docx"`, `"xlsx"`), optional categories. Returns the same JSON structure.
- **list_extraction_categories()**  
  Returns supported categories and stages.

## Configuration

1. Copy env and configure LLM:

   ```bash
   cd document_agent
   cp example.env .env
   ```

2. Set at least one of:
   - `LITELLM_MODEL` (e.g. `dashscope/qwen-plus` or `gemini-2.5-flash`) and corresponding API key / base URL, or
   - `GOOGLE_API_KEY` for Gemini.

## Run

```bash
cd document_agent
python __main__.py --port 10008
```

Agent listens on `0.0.0.0:10008` and is typically accessed via `127.0.0.1`.

- Agent card: http://127.0.0.1:10008/.well-known/agent-card.json

If you changed `.env` (for example `APP_URL` or model config), restart the service.

## Quick service test

```bash
cd document_agent
python call_service_demo.py --base-url http://127.0.0.1:10008 --smoke
python call_service_demo.py --base-url http://127.0.0.1:10008 --timeout 220
```

## Integration with Host

The Host should call this agent when the user intent involves travel policy, approval, reimbursement, or department rules. The Host uses the returned JSON to:
- **Solution page:** filter and annotate flight/hotel options with recommendation reasons tied to department clauses
- **Process page:** show approval/reimbursement steps and material list
- **Check page:** verify constraints and show missing items (using `warnings`)

Register the agent in your registry with keywords such as `document`, `policy`, `constraints`, `approval`, `reimbursement` so the Host can discover it.
