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
import traceback  # Import the traceback module
import sys
import io
import os
from a2a.types import Task, AgentCard, AgentSkill, AgentCapabilities
from collections.abc import AsyncIterator
from pprint import pformat

import gradio as gr
from gradio import ChatMessage

from google.adk.events import Event

# AgentCard configuration (exposed at /.well-known/agent-card.json)
app_url = os.environ.get("APP_URL", "http://127.0.0.1:8083")

skill = AgentSkill(
    id="user_interact",
    name="User interact",
    description="Just enter your question",
    tags=["answer", "question"],
    examples=["please help me to plan a trip to Paris"],
)

agent_card = AgentCard(
    name="User Agent",
    description="Just enter your question",
    url=app_url,
    version="1.0.0",
    default_input_modes=["text"],
    default_output_modes=["text"],
    capabilities=AgentCapabilities(streaming=True),
    skills=[skill],
)
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from google.genai.errors import ClientError

# Fix Windows console encoding issue
if sys.platform == 'win32':
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass  # Already wrapped or not needed

# from routing_agent import (
#     root_agent as routing_agent,
# )

from orchestrator import orchestrator as routing_agent


APP_NAME = 'routing_app'
USER_ID = 'default_user'
SESSION_ID = 'default_session'

SESSION_SERVICE = InMemorySessionService()
ROUTING_AGENT_RUNNER = Runner(
    agent=routing_agent,
    app_name=APP_NAME,
    session_service=SESSION_SERVICE,
)

from a2a.utils.message import new_agent_text_message

class HostAgentExecutor:
    def __init__(self, runner, session_service):
        self.runner = runner
        self.session_service = session_service

    async def execute(self, context, queue):
        """Execute a user's request by streaming events via get_response_from_agent,
        then enqueue the final_response and POST it to the frontend (localhost:15000 by default).
        """
        user_text = context.get_user_input()
        if not user_text:
            return

        session_id = context.context_id
        user_id = USER_ID
        app_name = APP_NAME

        # 确保 ADK session 存在
        try:
            await self.session_service.create_session(
                app_name=app_name,
                user_id=user_id,
                session_id=session_id,
            )
        except Exception:
            pass

        # Use the existing get_response_from_agent stream to collect events
        last_final_text = ''
        # process_message_and_forward runs the agent and forwards events to port 8084 (agent-listener)
        # It returns the final text so we don't need a second runner invocation
        try:
            last_final_text = await process_message_and_forward(user_text, [], session_id=session_id)
        except Exception as e:
            print(f"Error in process_message_and_forward: {e}")
            traceback.print_exc()

        # If we have a final text, enqueue it and send to frontend
        if last_final_text:
            message = new_agent_text_message(
                last_final_text,
                context_id=context.context_id,
                task_id=getattr(context, 'task_id', None),
            )

            # Enqueue for A2A result delivery
            try:
                await queue.enqueue_event(message)
            except Exception as e:
                print(f"Failed to enqueue event: {e}")

            # Post final text to frontend (default http://127.0.0.1:15000/)
            frontend_url = os.environ.get('FRONTEND_URL', 'http://127.0.0.1:15000/')
            payload = {
                'final_text': last_final_text,
                'context_id': context.context_id,
                'task_id': getattr(context, 'task_id', None),
            }
            try:
                async with aiohttp.ClientSession() as http_session:
                    async with http_session.post(frontend_url, json=payload) as resp:
                        text = await resp.text()
                        print(f"Posted final response to {frontend_url} -> {resp.status}: {text}")
            except Exception as e:
                print(f"Error posting final response to frontend {frontend_url}: {e}")

async def get_response_from_agent(
    message: str,
    history: list[ChatMessage],
    session_id: str = SESSION_ID,
):
    """Get response from host agent."""
    messages_buffer = []  # Buffer to accumulate all messages
    agent_call_id2messages_idx_map = {}  # Map agent_call_id to message index

    try:
        event_iterator: AsyncIterator[Event] = ROUTING_AGENT_RUNNER.run_async(
            user_id=USER_ID,
            session_id=session_id,
            new_message=types.Content(
                role='user', parts=[types.Part(text=message)]
            ),
        )
        processing_message_id = "0"
        processing_message = ChatMessage(
            role='assistant', 
            content="",
            metadata={"title": "Processing", "status": "pending", "id": processing_message_id}
        )
        messages_buffer.append(processing_message)

        async for event in event_iterator:
            print('***'*10)
            print(f'Event received: {event}')
            print('***'*10)
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.function_call:
                        agent_name = part.function_call.args.get('agent_name')
                        author = event.author
                        task = part.function_call.args.get('task')
                        title_name = agent_name if agent_name else f"{author} 为以下任务寻找Agent：{task}"

                        agent_call_id = part.function_call.id
                        
                        formatted_call = f'```python\n{pformat(part.function_call.model_dump(exclude_none=True), indent=2, width=80)}\n```'
                        
                        # 创建新消息,显示正在调用的 agent
                        new_message = ChatMessage(
                            content=f'🤔 **Calling {title_name}**\n{formatted_call}',
                            metadata={"title": f"⏳ {title_name}", "id": agent_call_id, "status": "pending", 'parent_id': processing_message_id}
                        )
                        
                        messages_buffer.append(new_message)
                        agent_call_id2messages_idx_map[agent_call_id] = len(messages_buffer) - 1
                        
                        # 立即 yield 以显示思考过程
                        yield messages_buffer
                        
                    elif part.function_response:
                        agent_call_id = part.function_response.id
                        
                        if agent_call_id in agent_call_id2messages_idx_map:
                            idx = agent_call_id2messages_idx_map[agent_call_id]
                            old_message = messages_buffer[idx]
                            
                            # 提取 agent 名称
                            agent_name = old_message.metadata.get('title', 'Agent').replace('⏳', '').strip()
                            old_message.metadata['title'] = f'✅ {agent_name}'
                            
                            response_content = part.function_response.response
                            if response_content.get('result'):
                                result_object = response_content.get('result')
                                # result_object as a2a Task object
                                if isinstance(result_object, Task):
                                    text_output = result_object.artifacts[0].parts[0].root.text
                                    formatted_response = f'```markdown\n{text_output}\n```'
                                else:
                                    formatted_response = f'```json\n{pformat(response_content['response'], indent=2, width=80)}\n```'
                            else:
                                formatted_response = f'```json\n{pformat(response_content, indent=2, width=80)}\n```'

                            old_message.content += f'\n\n💬 **Response from {agent_name}**\n{formatted_response}'
                            yield messages_buffer
                            await asyncio.sleep(5)
                            old_message.metadata["status"] = "done"
                            yield messages_buffer

            if event.is_final_response():
                final_response_text = ''
                if event.content and event.content.parts:
                    final_response_text = ''.join(
                        [p.text for p in event.content.parts if p.text]
                    )
                elif event.actions and event.actions.escalate:
                    final_response_text = f'Agent escalated: {event.error_message or "No specific message."}'
                if final_response_text:
                    event_author = event.author
                    if event_author != "ResultSummarizer":
                        new_message = ChatMessage(
                            role='assistant', content=final_response_text,
                            metadata={"title": event_author, "id": event.id,
                                      "status": "pending", 'parent_id': processing_message_id}
                        )
                        messages_buffer.append(new_message)
                    else:
                        new_message = gr.ChatMessage(
                            role='assistant', content=final_response_text
                        )
                        for message in messages_buffer:
                            if message.metadata and "status" in message.metadata:
                                message.metadata["status"] = "done"
                        messages_buffer.append(new_message)
                    # Yield all accumulated messages including the final one
                    yield messages_buffer
                # Do not break here for SequentialAgent to continue
                # break
    except ClientError as e:
        if e.code == 429 or "RESOURCE_EXHAUSTED" in str(e):
            print(f"\n⚠️ API Rate Limit Exceeded (429). Please wait a moment before retrying.\nError details: {e}")
            error_message = ChatMessage(
                role='assistant',
                content='⚠️ **System Busy**: The AI service is currently receiving too many requests (Rate Limit Exceeded). Please wait a minute and try again.',
            )
            messages_buffer.append(error_message)
            yield messages_buffer
        else:
            print(f'GenAI ClientError: {e}')
            traceback.print_exc()
            yield messages_buffer
    except Exception as e:
        print(f'Error in get_response_from_agent (Type: {type(e)}): {e}')
        traceback.print_exc()  # This will print the full traceback
        error_message = gr.ChatMessage(
            role='assistant',
            content='An error occurred while processing your request. Please check the server logs for details.',
        )
        messages_buffer.append(error_message)
        yield messages_buffer

import aiohttp

GATEWAY_ADMIN_URL = os.environ.get("GATEWAY_ADMIN_URL", "http://localhost:15000")


async def _post_json(session: aiohttp.ClientSession, path: str, payload: dict):
    url = f'http://localhost:8084{path}'
    try:
        async with session.post(url, json=payload) as resp:
            text = await resp.text()
            print(f"POST {url} -> {resp.status}: {text}")
            return resp.status, text
    except Exception as e:
        print(f"Error posting to {url}: {e}")


async def _post_to_gateway(session: aiohttp.ClientSession, path: str, payload: dict):
    """POST to the gateway admin API (port 15000)."""
    url = f'{GATEWAY_ADMIN_URL}{path}'
    try:
        async with session.post(url, json=payload) as resp:
            text = await resp.text()
            print(f"POST {url} -> {resp.status}: {text}")
    except Exception as e:
        print(f"Error posting to gateway {url}: {e}")


async def process_message_and_forward(message: str, history: list[dict], session_id: str = SESSION_ID) -> str:
    """Run the agent, forward events to port 8084 (agent-listener), and return the final text.

    - function_call events -> POST /chat/call with all content.parts
    - function_response events -> POST /chat/function with the function_response
    - final responses -> POST /chat/final with the final text
    """
    print('Processing message and forwarding events...')
    collected_final_text = ''
    async with aiohttp.ClientSession() as http_session:
        # Reset listener so each new conversation starts with a clean tree
        await _post_json(http_session, '/reset', {})
        try:
            event_iterator = ROUTING_AGENT_RUNNER.run_async(
                user_id=USER_ID,
                session_id=session_id,
                new_message=types.Content(
                    role='user', parts=[types.Part(text=message)]
                ),
            )

            async for event in event_iterator:
                print('***'*10)
                print(f'Event received: {event}')
                print('***'*10)

                if event.content and event.content.parts:
                    # Serialize all parts for forwarding when needed
                    parts_serialized = [p.model_dump(exclude_none=True) for p in event.content.parts]

                    for part in event.content.parts:
                        if part.function_call:
                            # Forward all parts for a function call
                            payload = {
                                'event_id': event.id,
                                'author': event.author,
                                'parts': parts_serialized,
                            }
                            await _post_json(http_session, '/chat/call', payload)

                        elif part.function_response:
                            payload = {
                                'event_id': event.id,
                                'author': event.author,
                                'function_response': part.function_response.model_dump(exclude_none=True),
                            }
                            await _post_json(http_session, '/chat/function', payload)

                if event.is_final_response():
                    final_response_text = ''
                    if event.content and event.content.parts:
                        final_response_text = ''.join([p.text for p in event.content.parts if p.text])
                    elif event.actions and event.actions.escalate:
                        final_response_text = f'Agent escalated: {event.error_message or "No specific message."}'

                    if final_response_text:
                        collected_final_text = final_response_text

                    # GatewayPlanner already posts its decomp plan directly from the
                    # before_model_callback; skip here to avoid a duplicate "final" node
                    # that would be misidentified as the DAG Complete summary.
                    if event.author == 'GatewayPlanner':
                        continue

                    payload = {
                        'event_id': event.id,
                        'author': event.author,
                        'final_text': final_response_text,
                    }
                    await _post_json(http_session, '/chat/final', payload)

        except ClientError as e:
            print(f'GenAI ClientError: {e}')
            async with aiohttp.ClientSession() as err_session:
                await _post_json(err_session, '/chat/final', {'error': str(e)})
        except Exception as e:
            print(f'Error in process_message_and_forward (Type: {type(e)}): {e}')
            traceback.print_exc()
            async with aiohttp.ClientSession() as err_session:
                await _post_json(err_session, '/chat/final', {'error': str(e)})

        # ── 事件循环结束后，把执行数据提交给网关 (/task-router/execution) ──
        try:
            session_obj = await SESSION_SERVICE.get_session(
                app_name=APP_NAME, user_id=USER_ID, session_id=session_id
            )
            if session_obj:
                task_id = session_obj.state.get("task_id")
                exec_data = session_obj.state.get("execution_data")
                if task_id and exec_data:
                    exec_data["taskId"] = task_id
                    exec_data["finalResult"] = collected_final_text
                    await _post_to_gateway(http_session, "/task-router/execution", exec_data)
        except Exception as e:
            print(f'[process_message_and_forward] Failed to submit execution trace: {e}')

    return collected_final_text


async def main():
    print('Creating ADK session...')
    await SESSION_SERVICE.create_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID
    )
    print('ADK session created successfully.')

    from a2a.server.apps import A2AStarletteApplication
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.tasks import InMemoryTaskStore
    import uvicorn

    executor = HostAgentExecutor(ROUTING_AGENT_RUNNER, SESSION_SERVICE)

    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
    )

    a2a_app = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    )

    app = a2a_app.build()  # ✅ Starlette ASGI app

    # CORS 中间件：允许浏览器（运行在 localhost:15000）跨域访问 localhost:8083
    from starlette.middleware.cors import CORSMiddleware
    app = CORSMiddleware(
        app,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ✅ async-friendly uvicorn 启动方式
    config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=8083,
        loop="asyncio",
        log_level="info",
    )
    server = uvicorn.Server(config)

    print('Host agent listening on http://localhost:8083')
    await server.serve()   # 🚨 注意：await，不是 run()



if __name__ == '__main__':
    asyncio.run(main())