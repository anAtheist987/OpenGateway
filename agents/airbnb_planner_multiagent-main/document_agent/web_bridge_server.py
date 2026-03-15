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

"""Bridge API for reimbursement portal -> document extraction agent.

This server demonstrates the closed-loop:
website detail content + attachment metadata -> document_agent extraction -> natural language requirements.
"""
from __future__ import annotations

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from document_mcp import extract_constraints_and_process

HOST = '0.0.0.0'
PORT = 10009
MOCK_DATA_PATH = Path('/root/reimbursement_mock_data.json')


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Access-Control-Allow-Origin', '*')
    handler.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
    handler.send_header('Access-Control-Allow-Headers', 'Content-Type')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _load_notice(notice_id: str) -> dict[str, Any] | None:
    if not MOCK_DATA_PATH.exists():
        return None
    data = json.loads(MOCK_DATA_PATH.read_text(encoding='utf-8'))
    return next((n for n in data.get('notices', []) if n.get('id') == notice_id), None)


def _compose_web_doc_text(notice: dict[str, Any]) -> str:
    html_block = (
        f"<article>\n"
        f"<h1>{notice.get('title', '')}</h1>\n"
        f"<p>发布时间：{notice.get('publish_date', '')}</p>\n"
        f"<p>部门：{notice.get('department_name', '')}</p>\n"
        f"<p>类别：{notice.get('category', '')}</p>\n"
        f"<p>适用职称：{', '.join(notice.get('job_titles', []))}</p>\n"
        f"<div>{notice.get('content_text', '')}</div>\n"
        f"</article>"
    )

    attachment_block = ''
    if notice.get('content_type') == 'pdf':
        attachment_name = notice.get('attachment_name', '未命名附件.pdf')
        attachment_lines = '\n'.join(notice.get('attachment_lines', []))
        attachment_block = (
            f"\n\n【附件信息】\n"
            f"附件名：{attachment_name}\n"
            f"附件摘要：{attachment_lines}"
        )

    return f"【网页详情HTML】\n{html_block}{attachment_block}"


def _to_natural_language(result: dict[str, Any], notice: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"已完成对《{notice.get('title', '该公告')}》的部门要求提取。")

    constraints = result.get('constraints', [])
    recommendations = result.get('recommendations', [])
    process_steps = result.get('process_steps', [])
    checklist = result.get('material_checklist', [])
    warnings = result.get('warnings', [])

    if constraints:
        lines.append('【硬性约束】')
        for idx, item in enumerate(constraints[:5], start=1):
            lines.append(f"{idx}. {item.get('rule_text', '未提供')}（来源：{item.get('source', '公告内容')}）")

    if recommendations:
        lines.append('【推荐条件】')
        for idx, item in enumerate(recommendations[:5], start=1):
            lines.append(f"{idx}. {item.get('condition_text', '未提供')}（来源：{item.get('source', '公告内容')}）")

    if process_steps:
        lines.append('【流程步骤】')
        sorted_steps = sorted(process_steps, key=lambda x: x.get('step_order', 999))
        for step in sorted_steps[:6]:
            materials = '、'.join(step.get('materials', [])) or '无'
            lines.append(
                f"- 第{step.get('step_order', '?')}步（{step.get('stage', '流程')}）：{step.get('name', '未命名')}；材料：{materials}"
            )

    if checklist:
        lines.append('【材料清单】' + '、'.join(checklist[:12]))

    if warnings:
        lines.append('【注意事项】' + '；'.join(warnings[:3]))

    if len(lines) == 1:
        lines.append('未识别出明确的部门约束，建议补充更完整的公告正文或附件内容。')

    return '\n'.join(lines)


class BridgeHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path == '/health':
            _json_response(self, 200, {'ok': True, 'service': 'document_web_bridge'})
            return
        _json_response(self, 404, {'error': 'Not Found'})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != '/api/extract-notice':
            _json_response(self, 404, {'error': 'Not Found'})
            return

        try:
            content_length = int(self.headers.get('Content-Length', '0'))
            raw = self.rfile.read(content_length)
            payload = json.loads(raw.decode('utf-8')) if raw else {}
        except Exception as exc:  # noqa: BLE001
            _json_response(self, 400, {'error': f'Invalid JSON body: {exc!s}'})
            return

        notice_id = payload.get('notice_id', '')
        categories = payload.get('categories')
        if not notice_id:
            _json_response(self, 400, {'error': 'notice_id is required'})
            return

        notice = _load_notice(notice_id)
        if not notice:
            _json_response(self, 404, {'error': f'Notice not found: {notice_id}'})
            return

        composed_text = _compose_web_doc_text(notice)
        try:
            structured_json_text = asyncio.run(
                extract_constraints_and_process(composed_text, categories)
            )
            structured = json.loads(structured_json_text)
        except Exception as exc:  # noqa: BLE001
            _json_response(self, 500, {'error': f'Extraction failed: {exc!s}'})
            return

        natural_text = _to_natural_language(structured, notice)
        _json_response(
            self,
            200,
            {
                'notice_id': notice_id,
                'notice_title': notice.get('title'),
                'input_preview': composed_text[:1200],
                'natural_language_requirements': natural_text,
                'structured_result': structured,
            },
        )


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), BridgeHandler)
    print(f'document web bridge listening on http://{HOST}:{PORT}')
    server.serve_forever()


if __name__ == '__main__':
    main()
