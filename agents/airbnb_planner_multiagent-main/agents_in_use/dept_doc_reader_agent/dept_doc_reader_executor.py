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

"""Shared department document reader executor (A2A, deterministic flow)."""
import json
import logging
import re
from typing import TYPE_CHECKING

from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    AgentCard,
    FilePart,
    FileWithBytes,
    FileWithUri,
    Part,
    TaskState,
    TextPart,
    UnsupportedOperationError,
)
from a2a.utils.errors import ServerError
from google.adk import Runner
from google.genai import types

from dept_doc_reader_mcp import (
    extract_department_notices,
    list_department_notices,
    search_department_notices,
)

if TYPE_CHECKING:
    from google.adk.sessions.session import Session

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

DEFAULT_USER_ID = 'self'


def convert_a2a_part_to_genai(part: Part) -> types.Part:
    part = part.root
    if isinstance(part, TextPart):
        return types.Part(text=part.text)
    if isinstance(part, FilePart):
        if isinstance(part.file, FileWithUri):
            return types.Part(
                file_data=types.FileData(
                    file_uri=part.file.uri, mime_type=part.file.mime_type
                )
            )
        if isinstance(part.file, FileWithBytes):
            return types.Part(
                inline_data=types.Blob(
                    data=part.file.bytes, mime_type=part.file.mime_type
                )
            )
        raise ValueError(f'Unsupported file type: {type(part.file)}')
    raise ValueError(f'Unsupported part type: {type(part)}')


def convert_genai_part_to_a2a(part: types.Part) -> Part:
    if part.text:
        return TextPart(text=part.text)
    if part.file_data:
        return FilePart(
            file=FileWithUri(
                uri=part.file_data.file_uri,
                mime_type=part.file_data.mime_type,
            )
        )
    if part.inline_data:
        return Part(
            root=FilePart(
                file=FileWithBytes(
                    bytes=part.inline_data.data,
                    mime_type=part.inline_data.mime_type,
                )
            )
        )
    raise ValueError(f'Unsupported part type: {part}')


class DeptDocReaderExecutor(AgentExecutor):
    """Deterministic shared department reader with fixed MCP call path."""

    def __init__(self, runner: Runner, card: AgentCard):
        # runner is kept for compatibility with existing bootstrap.
        self.runner = runner
        self._card = card
        self._active_sessions: set[str] = set()

    @staticmethod
    def _json_loads(raw: str) -> dict:
        try:
            return json.loads(raw)
        except Exception:
            return {}

    @staticmethod
    def _pick_notice_ids(data: dict, max_count: int = 3) -> list[str]:
        notices = data.get('notices') or []
        ids: list[str] = []
        for n in notices:
            nid = n.get('id')
            if nid:
                ids.append(str(nid))
        return ids[:max_count]

    @staticmethod
    def _extract_department_id(user_text: str) -> str:
        """Extract department_id from explicit hint, or infer from keywords."""
        m = re.search(r'department_id\s*=\s*([a-zA-Z_]+)', user_text or '')
        if m:
            return m.group(1).strip().lower()
        # Infer from Chinese / English keywords in the task description
        text_lower = (user_text or '').lower()
        if any(k in text_lower for k in ('foreign', '外事', '出境', '出国', '因公出国', '涉外')):
            return 'foreign'
        if any(k in text_lower for k in ('safety', '安全', '消防', '应急')):
            return 'safety'
        if any(k in text_lower for k in ('procurement', '采购', '集采', '招标')):
            return 'procurement'
        # Default: try foreign (most common for overseas travel scenarios)
        return 'foreign'

    @staticmethod
    def _strip_department_hint(user_text: str) -> str:
        if not user_text:
            return ''
        return re.sub(r'department_id\s*=\s*[a-zA-Z_]+\s*', '', user_text).strip()

    @staticmethod
    def _to_output_schema(extracted: dict, route_note: str) -> str:
        warnings = extracted.get('warnings') or []
        if route_note:
            warnings = [route_note, *warnings]
        out = {
            'process_steps': extracted.get('process_steps') or [],
            'material_checklist': extracted.get('material_checklist') or [],
            'warnings': warnings,
        }
        return json.dumps(out, ensure_ascii=False)

    async def _build_response(self, user_text: str) -> str:
        try:
            department_id = self._extract_department_id(user_text)
            query = self._strip_department_hint(user_text)

            search_data = self._json_loads(
                await search_department_notices(
                    query=query or department_id,
                    department_id=department_id,
                    limit=3,
                )
            )
            notice_ids = self._pick_notice_ids(search_data)
            search_warnings = search_data.get('warnings') or []
            if search_warnings:
                return json.dumps(
                    {
                        'process_steps': [],
                        'material_checklist': [],
                        'warnings': search_warnings,
                    },
                    ensure_ascii=False,
                )

            if not notice_ids:
                list_data = self._json_loads(
                    await list_department_notices(
                        department_id=department_id,
                        limit=3,
                    )
                )
                list_warnings = list_data.get('warnings') or []
                if list_warnings:
                    return json.dumps(
                        {
                            'process_steps': [],
                            'material_checklist': [],
                            'warnings': list_warnings,
                        },
                        ensure_ascii=False,
                    )
                notice_ids = self._pick_notice_ids(list_data)

            if not notice_ids:
                return json.dumps(
                    {
                        'process_steps': [],
                        'material_checklist': [],
                        'warnings': ['未找到指定部门可抽取公告'],
                    },
                    ensure_ascii=False,
                )

            extracted = self._json_loads(
                await extract_department_notices(
                    department_id=department_id,
                    notice_ids=notice_ids,
                    categories=None,
                    include_attachment=True,
                )
            )
            route_note = (
                f'dept_doc_reader_agent department_id={department_id} '
                f'notices={",".join(notice_ids)}'
            )
            return self._to_output_schema(extracted, route_note)
        except Exception as e:
            return json.dumps(
                {
                    'process_steps': [],
                    'material_checklist': [],
                    'warnings': [f'department reader execution failed: {e!s}'],
                },
                ensure_ascii=False,
            )

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ):
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        if not context.current_task:
            await updater.update_status(TaskState.submitted)
        await updater.update_status(TaskState.working)
        session_id = context.context_id
        self._active_sessions.add(session_id)
        try:
            user_text = ''
            for part in context.message.parts:
                root = part.root
                if isinstance(root, TextPart):
                    user_text += (root.text or '') + '\n'
            result_text = await self._build_response(user_text.strip())
            await updater.add_artifact([TextPart(text=result_text)])
            await updater.update_status(TaskState.completed, final=True)
        finally:
            self._active_sessions.discard(session_id)

    async def cancel(self, context: RequestContext, event_queue: EventQueue):
        session_id = context.context_id
        if session_id in self._active_sessions:
            self._active_sessions.discard(session_id)
        raise ServerError(error=UnsupportedOperationError())

