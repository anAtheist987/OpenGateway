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

"""A2A service tester for document_agent.

Examples:
1) Smoke test:
   python call_service_demo.py --smoke

2) Send one sentence task:
   python call_service_demo.py --task "我下周要去新加坡...审批/报销步骤清单"

3) Read task from file:
   python call_service_demo.py --task-file /tmp/task.txt
"""

import argparse
import asyncio
import json
import uuid
from typing import Any

import httpx
from a2a.client import A2AClient
from a2a.types import (
    AgentCard,
    MessageSendParams,
    SendMessageRequest,
    SendMessageResponse,
    SendMessageSuccessResponse,
    Task,
)


def _extract_text(response: Any) -> str:
    if isinstance(response, Task):
        if response.artifacts:
            texts: list[str] = []
            for artifact in response.artifacts:
                for part in artifact.parts:
                    text = getattr(getattr(part, "root", None), "text", None)
                    if text:
                        texts.append(text)
            if texts:
                return "\n\n".join(texts)
        if response.history:
            for msg in reversed(response.history):
                if getattr(msg, "role", None) == "agent":
                    for part in msg.parts:
                        text = getattr(getattr(part, "root", None), "text", None)
                        if text:
                            return text
        return str(response)

    if isinstance(response, SendMessageResponse):
        root = getattr(response, "root", None)
        if isinstance(root, SendMessageSuccessResponse):
            return _extract_text(root.result)
        return str(response)

    return str(response)


def _strip_code_fence(text: str) -> str:
    clean = (text or "").strip()
    if clean.startswith("```"):
        lines = clean.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        clean = "\n".join(lines).strip()
    return clean


def _build_task(raw_task: str, checklist_only: bool) -> str:
    if not checklist_only:
        return raw_task
    return (
        "你是文档抽取Agent，只能基于公告制度做抽取，不要做航班/酒店/天气规划。"
        "请仅提取“审批/备案/报销步骤清单”，并且只输出一个JSON对象。"
        "JSON键仅允许：process_steps, material_checklist, warnings。"
        "process_steps仅保留stage=approval、filing、reimbursement。"
        "如果未抽取到备案步骤，请在warnings明确写出“未找到备案步骤”。"
        "不要输出markdown、不要输出解释文字。"
        f"\n\n用户原始问题：{raw_task}"
    )


def _post_filter_result(data: dict[str, Any], checklist_only: bool) -> dict[str, Any]:
    if not checklist_only:
        return data
    steps = data.get("process_steps") or []
    filtered_steps = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        stage = str(step.get("stage", "")).lower()
        if stage in {"approval", "filing", "reimbursement"}:
            filtered_steps.append(step)
    return {
        "process_steps": filtered_steps,
        "material_checklist": data.get("material_checklist", []),
        "warnings": data.get("warnings", []),
    }


async def _load_card(client: httpx.AsyncClient, base_url: str) -> AgentCard:
    resp = await client.get(f"{base_url}/.well-known/agent-card.json")
    resp.raise_for_status()
    return AgentCard.model_validate(resp.json())


async def _send_task(
    client: httpx.AsyncClient, base_url: str, card: AgentCard, task_text: str
) -> Any:
    a2a_client = A2AClient(client, card, url=base_url)
    context_id = str(uuid.uuid4())
    message_id = str(uuid.uuid4())
    payload = {
        "message": {
            "role": "user",
            "parts": [{"type": "text", "text": task_text}],
            "messageId": message_id,
            "contextId": context_id,
        }
    }
    req = SendMessageRequest(id=message_id, params=MessageSendParams.model_validate(payload))
    return await a2a_client.send_message(req)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Call document_agent service via A2A")
    parser.add_argument("--base-url", default="http://127.0.0.1:10008", help="A2A base URL")
    parser.add_argument("--timeout", type=float, default=180.0, help="HTTP timeout seconds")
    parser.add_argument("--smoke", action="store_true", help="Only check agent-card health")
    parser.add_argument("--print-card", action="store_true", help="Print agent card json")
    parser.add_argument(
        "--task",
        default=(
            "我下周要去新加坡参加两天会议（周三到周五），从上海出发。"
            "请给我：航班 + 酒店 + 天气提醒 + 审批/报销步骤清单。"
            "偏好：不坐红眼；酒店离会场 30 分钟内；更重视准点与安全。"
        ),
        help="Task text",
    )
    parser.add_argument("--task-file", default="", help="Read task text from file path")
    parser.add_argument(
        "--checklist-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep only approval/filing/reimbursement process output",
    )
    args = parser.parse_args()

    task_text = args.task
    if args.task_file:
        with open(args.task_file, "r", encoding="utf-8") as f:
            task_text = f.read().strip()
    task_text = _build_task(task_text, args.checklist_only)

    timeout = httpx.Timeout(args.timeout)
    async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
        card = await _load_card(client, args.base_url)
        if args.print_card:
            print(json.dumps(card.model_dump(exclude_none=True), ensure_ascii=False, indent=2))
        if args.smoke:
            print(f"[OK] agent-card reachable: {args.base_url}")
            return

        resp = await _send_task(client, args.base_url, card, task_text)

    text = _strip_code_fence(_extract_text(resp))
    try:
        data = json.loads(text)
        data = _post_filter_result(data, args.checklist_only)
        print(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception:
        print(text)


if __name__ == "__main__":
    asyncio.run(main())
