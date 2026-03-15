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

import logging
import os

import click
import uvicorn

from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
)
from dotenv import load_dotenv
from google.adk.artifacts import InMemoryArtifactService
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from document_agent import create_document_agent
from document_executor import DocumentExecutor

# Env loading strategy for document agent:
# 1) Load project root .env (shared defaults)
# 2) Load document_agent/.env with override=True (document-agent specific overrides)
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
ROOT_ENV_PATH = os.path.join(PROJECT_ROOT, '.env')
LOCAL_ENV_PATH = os.path.join(CURRENT_DIR, '.env')

if os.path.exists(ROOT_ENV_PATH):
    load_dotenv(ROOT_ENV_PATH)
if os.path.exists(LOCAL_ENV_PATH):
    load_dotenv(LOCAL_ENV_PATH, override=True)
if not os.path.exists(ROOT_ENV_PATH) and not os.path.exists(LOCAL_ENV_PATH):
    load_dotenv()

logging.basicConfig()

DEFAULT_HOST = '0.0.0.0'
DEFAULT_PORT = 10008


def main(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
    if os.getenv('GOOGLE_GENAI_USE_VERTEXAI') != 'TRUE' and not os.getenv(
        'GOOGLE_API_KEY'
    ):
        has_litellm = bool(os.getenv('LITELLM_MODEL'))
        mode = os.getenv('DOCUMENT_LLM_MODE', 'api').strip().lower()
        has_document_mode_cfg = (
            bool(os.getenv('DOCUMENT_LOCAL_MODEL'))
            if mode == 'local'
            else bool(os.getenv('DOCUMENT_API_MODEL'))
        )
        if not has_litellm and not has_document_mode_cfg:
            raise ValueError(
                'GOOGLE_API_KEY or LITELLM_MODEL or DOCUMENT_* model config must be set.'
            )

    skill = AgentSkill(
        id='document_extraction',
        name='Extract policy constraints',
        description='Extract department requirements, constraints, process steps and material checklist from travel/policy documents',
        tags=['document', 'policy', 'constraints', 'approval', 'reimbursement'],
        examples=[
            '从差旅制度中抽取交通和住宿约束',
            'Extract approval and reimbursement steps from policy doc',
        ],
    )

    app_url = os.environ.get('APP_URL', f'http://{host}:{port}')

    agent_card = AgentCard(
        name='Document Agent',
        description='Extracts department requirements and key clauses from travel/policy documents into constraints, recommendations, process steps and material checklist for the gateway.',
        url=app_url,
        version='1.0.0',
        default_input_modes=['text'],
        default_output_modes=['text'],
        capabilities=AgentCapabilities(streaming=True),
        skills=[skill],
    )

    adk_agent = create_document_agent()
    runner = Runner(
        app_name=agent_card.name,
        agent=adk_agent,
        artifact_service=InMemoryArtifactService(),
        session_service=InMemorySessionService(),
        memory_service=InMemoryMemoryService(),
    )
    agent_executor = DocumentExecutor(runner, agent_card)

    request_handler = DefaultRequestHandler(
        agent_executor=agent_executor, task_store=InMemoryTaskStore()
    )

    a2a_app = A2AStarletteApplication(
        agent_card=agent_card, http_handler=request_handler
    )

    uvicorn.run(a2a_app.build(), host=host, port=port)


@click.command()
@click.option('--host', 'host', default=DEFAULT_HOST)
@click.option('--port', 'port', default=DEFAULT_PORT)
def cli(host: str, port: int):
    main(host, port)


if __name__ == '__main__':
    cli()
