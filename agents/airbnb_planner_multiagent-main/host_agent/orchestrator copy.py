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

# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import base64
import json
import logging
import os
import re
import warnings
from typing import Any, AsyncGenerator, Dict, Generator, Iterable, List, Literal, Optional, Tuple, TypedDict, Union, cast

from google.genai import types
import litellm
from litellm import acompletion, completion
from litellm import (
    ChatCompletionAssistantMessage,
    ChatCompletionAssistantToolCall,
    ChatCompletionDeveloperMessage,
    ChatCompletionMessageToolCall,
    ChatCompletionToolMessage,
    ChatCompletionUserMessage,
    CustomStreamWrapper,
    Function,
    Message,
    ModelResponse,
    OpenAIMessageContent,
)
from pydantic import BaseModel, Field
from typing_extensions import override

from .base_llm import BaseLlm
from .llm_request import LlmRequest
from .llm_response import LlmResponse

# This will add functions to prompts if functions are provided.
litellm.add_function_to_prompt = True

logger = logging.getLogger("google_adk." + __name__)

_NEW_LINE = "\n"
_EXCLUDED_PART_FIELD = {"inline_data": {"data"}}


# ---------------------------
# ✅ Added: local OpenAI/vLLM defaults + model normalization
# ---------------------------

_DEFAULT_API_BASE = os.getenv("LITELLM_API_BASE", "http://127.0.0.1:8000/v1")
_DEFAULT_API_KEY = os.getenv("LITELLM_API_KEY", "EMPTY")
_DEFAULT_PROVIDER = os.getenv("LITELLM_PROVIDER", "openai")


def _looks_like_local_path_model(model: str) -> bool:
  # e.g. "/mnt/ssd2/..." or "./Qwen..." or "C:\..."
  return (
      model.startswith("/")
      or model.startswith("./")
      or model.startswith("../")
      or (len(model) >= 3 and model[1:3] == ":\\")
  )


def _normalize_model_string_for_litellm(model: str) -> str:
  """
  LiteLLM typically expects 'provider/model' strings.
  If user passes a raw local path (like vLLM served id that is a path),
  we rewrite to 'openai/<that_path>' so LiteLLM routes to OpenAI-compatible client.
  """
  if "/" in model:
    # Already has provider/model OR it's a path.
    # If it's a path, it starts with "/" or "./" etc.
    if _looks_like_local_path_model(model):
      return f"{_DEFAULT_PROVIDER}/{model}"
    return model

  # No slash: treat as model name, prepend provider
  return f"{_DEFAULT_PROVIDER}/{model}"


def _maybe_inject_openai_compatible_defaults(model: str, kwargs: Dict[str, Any]) -> None:
  """
  If model is routed to OpenAI-compatible provider, ensure api_base/api_key exist.
  This enables: LiteLlm(model="openai/llama-3.2-1b") to hit local vLLM.
  """
  # Provider is before first slash
  provider = model.split("/", 1)[0] if "/" in model else _DEFAULT_PROVIDER
  if provider in ("openai", "azure", "openai_chat"):
    kwargs.setdefault("api_base", _DEFAULT_API_BASE)
    kwargs.setdefault("api_key", _DEFAULT_API_KEY)


# ---------------------------
# Original code
# ---------------------------

class ChatCompletionFileUrlObject(TypedDict):
  file_data: str
  format: str


class FunctionChunk(BaseModel):
  id: Optional[str]
  name: Optional[str]
  args: Optional[str]
  index: Optional[int] = 0


class TextChunk(BaseModel):
  text: str


class UsageMetadataChunk(BaseModel):
  prompt_tokens: int
  completion_tokens: int
  total_tokens: int


class LiteLLMClient:
  """Provides acompletion method (for better testability)."""

  async def acompletion(
      self, model, messages, tools, **kwargs
  ) -> Union[ModelResponse, CustomStreamWrapper]:
    return await acompletion(
        model=model,
        messages=messages,
        tools=tools,
        **kwargs,
    )

  def completion(
      self, model, messages, tools, stream=False, **kwargs
  ) -> Union[ModelResponse, CustomStreamWrapper]:
    return completion(
        model=model,
        messages=messages,
        tools=tools,
        stream=stream,
        **kwargs,
    )


def _safe_json_serialize(obj) -> str:
  try:
    return json.dumps(obj, ensure_ascii=False)
  except (TypeError, OverflowError):
    return str(obj)


def _content_to_message_param(
    content: types.Content,
) -> Union[Message, list[Message]]:
  tool_messages = []
  for part in content.parts:
    if part.function_response:
      tool_messages.append(
          ChatCompletionToolMessage(
              role="tool",
              tool_call_id=part.function_response.id,
              content=_safe_json_serialize(part.function_response.response),
          )
      )
  if tool_messages:
    return tool_messages if len(tool_messages) > 1 else tool_messages[0]

  role = _to_litellm_role(content.role)
  message_content = _get_content(content.parts) or None

  if role == "user":
    return ChatCompletionUserMessage(role="user", content=message_content)
  else:
    tool_calls = []
    content_present = False
    for part in content.parts:
      if part.function_call:
        tool_calls.append(
            ChatCompletionAssistantToolCall(
                type="function",
                id=part.function_call.id,
                function=Function(
                    name=part.function_call.name,
                    arguments=_safe_json_serialize(part.function_call.args),
                ),
            )
        )
      elif part.text or part.inline_data:
        content_present = True

    final_content = message_content if content_present else None
    if final_content and isinstance(final_content, list):
      final_content = (
          final_content[0].get("text", "")
          if final_content[0].get("type", None) == "text"
          else final_content
      )

    return ChatCompletionAssistantMessage(
        role=role,
        content=final_content,
        tool_calls=tool_calls or None,
    )


def _get_content(
    parts: Iterable[types.Part],
) -> Union[OpenAIMessageContent, str]:
  content_objects = []
  for part in parts:
    if part.text:
      if len(parts) == 1:
        return part.text
      content_objects.append({"type": "text", "text": part.text})
    elif (
        part.inline_data
        and part.inline_data.data
        and part.inline_data.mime_type
    ):
      base64_string = base64.b64encode(part.inline_data.data).decode("utf-8")
      data_uri = f"data:{part.inline_data.mime_type};base64,{base64_string}"

      if part.inline_data.mime_type.startswith("image"):
        format_type = part.inline_data.mime_type
        content_objects.append({
            "type": "image_url",
            "image_url": {"url": data_uri, "format": format_type},
        })
      elif part.inline_data.mime_type.startswith("video"):
        format_type = part.inline_data.mime_type
        content_objects.append({
            "type": "video_url",
            "video_url": {"url": data_uri, "format": format_type},
        })
      elif part.inline_data.mime_type.startswith("audio"):
        format_type = part.inline_data.mime_type
        content_objects.append({
            "type": "audio_url",
            "audio_url": {"url": data_uri, "format": format_type},
        })
      elif part.inline_data.mime_type == "application/pdf":
        format_type = part.inline_data.mime_type
        content_objects.append({
            "type": "file",
            "file": {"file_data": data_uri, "format": format_type},
        })
      else:
        raise ValueError("LiteLlm(BaseLlm) does not support this content part.")

  return content_objects


def _to_litellm_role(role: Optional[str]) -> Literal["user", "assistant"]:
  if role in ["model", "assistant"]:
    return "assistant"
  return "user"


TYPE_LABELS = {
    "STRING": "string",
    "NUMBER": "number",
    "BOOLEAN": "boolean",
    "OBJECT": "object",
    "ARRAY": "array",
    "INTEGER": "integer",
}


def _schema_to_dict(schema: types.Schema) -> dict:
  schema_dict = schema.model_dump(exclude_none=True)

  if "type" in schema_dict:
    t = schema_dict["type"]
    schema_dict["type"] = (t.value if isinstance(t, types.Type) else t).lower()

  if "items" in schema_dict:
    schema_dict["items"] = _schema_to_dict(
        schema.items
        if isinstance(schema.items, types.Schema)
        else types.Schema.model_validate(schema_dict["items"])
    )

  if "properties" in schema_dict:
    new_props = {}
    for key, value in schema_dict["properties"].items():
      if isinstance(value, dict):
        new_props[key] = _schema_to_dict(types.Schema.model_validate(value))
      elif isinstance(value, types.Schema):
        new_props[key] = _schema_to_dict(value)
      else:
        new_props[key] = value
        if "type" in new_props[key]:
          new_props[key]["type"] = new_props[key]["type"].lower()
    schema_dict["properties"] = new_props

  return schema_dict


def _function_declaration_to_tool_param(
    function_declaration: types.FunctionDeclaration,
) -> dict:
  assert function_declaration.name

  properties = {}
  if (
      function_declaration.parameters
      and function_declaration.parameters.properties
  ):
    for key, value in function_declaration.parameters.properties.items():
      properties[key] = _schema_to_dict(value)

  tool_params = {
      "type": "function",
      "function": {
          "name": function_declaration.name,
          "description": function_declaration.description or "",
          "parameters": {
              "type": "object",
              "properties": properties,
          },
      },
  }

  if (
      function_declaration.parameters
      and function_declaration.parameters.required
  ):
    tool_params["function"]["parameters"]["required"] = (
        function_declaration.parameters.required
    )

  return tool_params


def _model_response_to_chunk(
    response: ModelResponse,
) -> Generator[
    Tuple[
        Optional[Union[TextChunk, FunctionChunk, UsageMetadataChunk]],
        Optional[str],
    ],
    None,
    None,
]:
  message = None
  if response.get("choices", None):
    message = response["choices"][0].get("message", None)
    finish_reason = response["choices"][0].get("finish_reason", None)
    if message is None and response["choices"][0].get("delta", None):
      message = response["choices"][0]["delta"]

    if message.get("content", None):
      yield TextChunk(text=message.get("content")), finish_reason

    if message.get("tool_calls", None):
      for tool_call in message.get("tool_calls"):
        if tool_call.type == "function":
          func_name = tool_call.function.name
          func_args = tool_call.function.arguments
          if not func_name and not func_args:
            continue
          yield FunctionChunk(
              id=tool_call.id,
              name=func_name,
              args=func_args,
              index=tool_call.index,
          ), finish_reason

    if finish_reason and not (
        message.get("content", None) or message.get("tool_calls", None)
    ):
      yield None, finish_reason

  if not message:
    yield None, None

  if response.get("usage", None):
    yield UsageMetadataChunk(
        prompt_tokens=response["usage"].get("prompt_tokens", 0),
        completion_tokens=response["usage"].get("completion_tokens", 0),
        total_tokens=response["usage"].get("total_tokens", 0),
    ), None


def _model_response_to_generate_content_response(
    response: ModelResponse,
) -> LlmResponse:
  message = None
  if response.get("choices", None):
    message = response["choices"][0].get("message", None)

  if not message:
    raise ValueError("No message in response")

  llm_response = _message_to_generate_content_response(message)
  if response.get("usage", None):
    llm_response.usage_metadata = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=response["usage"].get("prompt_tokens", 0),
        candidates_token_count=response["usage"].get("completion_tokens", 0),
        total_token_count=response["usage"].get("total_tokens", 0),
    )
  return llm_response


def _message_to_generate_content_response(
    message: Message, is_partial: bool = False
) -> LlmResponse:
  parts = []
  if message.get("content", None):
    parts.append(types.Part.from_text(text=message.get("content")))

  if message.get("tool_calls", None):
    for tool_call in message.get("tool_calls"):
      if tool_call.type == "function":
        part = types.Part.from_function_call(
            name=tool_call.function.name,
            args=json.loads(tool_call.function.arguments or "{}"),
        )
        part.function_call.id = tool_call.id
        parts.append(part)

  return LlmResponse(
      content=types.Content(role="model", parts=parts), partial=is_partial
  )


def _get_completion_inputs(
    llm_request: LlmRequest,
) -> Tuple[
    List[Message],
    Optional[List[Dict]],
    Optional[types.SchemaUnion],
    Optional[Dict],
]:
  messages: List[Message] = []
  for content in llm_request.contents or []:
    message_param_or_list = _content_to_message_param(content)
    if isinstance(message_param_or_list, list):
      messages.extend(message_param_or_list)
    elif message_param_or_list:
      messages.append(message_param_or_list)

  if llm_request.config.system_instruction:
    messages.insert(
        0,
        ChatCompletionDeveloperMessage(
            role="developer",
            content=llm_request.config.system_instruction,
        ),
    )

  tools: Optional[List[Dict]] = None
  if (
      llm_request.config
      and llm_request.config.tools
      and llm_request.config.tools[0].function_declarations
  ):
    tools = [
        _function_declaration_to_tool_param(tool)
        for tool in llm_request.config.tools[0].function_declarations
    ]

  response_format: Optional[types.SchemaUnion] = None
  if llm_request.config and llm_request.config.response_schema:
    response_format = llm_request.config.response_schema

  generation_params: Optional[Dict] = None
  if llm_request.config:
    config_dict = llm_request.config.model_dump(exclude_none=True)
    generation_params = {}
    param_mapping = {
        "max_output_tokens": "max_completion_tokens",
        "stop_sequences": "stop",
    }
    for key in (
        "temperature",
        "max_output_tokens",
        "top_p",
        "top_k",
        "stop_sequences",
        "presence_penalty",
        "frequency_penalty",
    ):
      if key in config_dict:
        mapped_key = param_mapping.get(key, key)
        generation_params[mapped_key] = config_dict[key]

    if not generation_params:
      generation_params = None

  return messages, tools, response_format, generation_params


def _build_function_declaration_log(
    func_decl: types.FunctionDeclaration,
) -> str:
  param_str = "{}"
  if func_decl.parameters and func_decl.parameters.properties:
    param_str = str({
        k: v.model_dump(exclude_none=True)
        for k, v in func_decl.parameters.properties.items()
    })
  return_str = "None"
  if func_decl.response:
    return_str = str(func_decl.response.model_dump(exclude_none=True))
  return f"{func_decl.name}: {param_str} -> {return_str}"


def _build_request_log(req: LlmRequest) -> str:
  function_decls: list[types.FunctionDeclaration] = cast(
      list[types.FunctionDeclaration],
      req.config.tools[0].function_declarations if req.config.tools else [],
  )
  function_logs = (
      [
          _build_function_declaration_log(func_decl)
          for func_decl in function_decls
      ]
      if function_decls
      else []
  )
  contents_logs = [
      content.model_dump_json(
          exclude_none=True,
          exclude={
              "parts": {
                  i: _EXCLUDED_PART_FIELD for i in range(len(content.parts))
              }
          },
      )
      for content in req.contents
  ]

  return f"""
LLM Request:
-----------------------------------------------------------
System Instruction:
{req.config.system_instruction}
-----------------------------------------------------------
Contents:
{_NEW_LINE.join(contents_logs)}
-----------------------------------------------------------
Functions:
{_NEW_LINE.join(function_logs)}
-----------------------------------------------------------
"""


def _is_litellm_gemini_model(model_string: str) -> bool:
  pattern = r"^(gemini|vertex_ai)/gemini-"
  return bool(re.match(pattern, model_string))


def _extract_gemini_model_from_litellm(litellm_model: str) -> str:
  if "/" in litellm_model:
    return litellm_model.split("/", 1)[1]
  return litellm_model


def _warn_gemini_via_litellm(model_string: str) -> None:
  if not _is_litellm_gemini_model(model_string):
    return

  if os.environ.get(
      "ADK_SUPPRESS_GEMINI_LITELLM_WARNINGS", ""
  ).strip().lower() in ("1", "true", "yes", "on"):
    return

  warnings.warn(
      f"[GEMINI_VIA_LITELLM] {model_string}: You are using Gemini via LiteLLM."
      " For better performance, reliability, and access to latest features,"
      " consider using Gemini directly through ADK's native Gemini"
      f" integration. Replace LiteLlm(model='{model_string}') with"
      f" Gemini(model='{_extract_gemini_model_from_litellm(model_string)}')."
      " Set ADK_SUPPRESS_GEMINI_LITELLM_WARNINGS=true to suppress this"
      " warning.",
      category=UserWarning,
      stacklevel=3,
  )


class LiteLlm(BaseLlm):
  """Wrapper around litellm."""

  llm_client: LiteLLMClient = Field(default_factory=LiteLLMClient)
  _additional_args: Dict[str, Any] = None

  def __init__(self, model: str, **kwargs):
    """
    ✅ Modified:
    - Normalize model string to 'provider/model'
    - If provider is openai-like, inject api_base/api_key defaults for local vLLM
      (LITELLM_API_BASE, LITELLM_API_KEY)
    """
    normalized_model = _normalize_model_string_for_litellm(model)
    _maybe_inject_openai_compatible_defaults(normalized_model, kwargs)

    super().__init__(model=normalized_model, **kwargs)

    _warn_gemini_via_litellm(normalized_model)

    self._additional_args = kwargs
    self._additional_args.pop("llm_client", None)
    self._additional_args.pop("messages", None)
    self._additional_args.pop("tools", None)
    self._additional_args.pop("stream", None)

  async def generate_content_async(
      self, llm_request: LlmRequest, stream: bool = False
  ) -> AsyncGenerator[LlmResponse, None]:
    self._maybe_append_user_content(llm_request)
    logger.debug(_build_request_log(llm_request))

    messages, tools, response_format, generation_params = (
        _get_completion_inputs(llm_request)
    )

    if "functions" in self._additional_args:
      tools = None

    completion_args = {
        "model": self.model,
        "messages": messages,
        "tools": tools,
        "response_format": response_format,
    }
    completion_args.update(self._additional_args)

    if generation_params:
      completion_args.update(generation_params)

    if stream:
      text = ""
      function_calls = {}
      completion_args["stream"] = True
      aggregated_llm_response = None
      aggregated_llm_response_with_tool_call = None
      usage_metadata = None
      fallback_index = 0

      async for part in await self.llm_client.acompletion(**completion_args):
        for chunk, finish_reason in _model_response_to_chunk(part):
          if isinstance(chunk, FunctionChunk):
            index = chunk.index or fallback_index
            if index not in function_calls:
              function_calls[index] = {"name": "", "args": "", "id": None}

            if chunk.name:
              function_calls[index]["name"] += chunk.name
            if chunk.args:
              function_calls[index]["args"] += chunk.args
              try:
                json.loads(function_calls[index]["args"])
                fallback_index += 1
              except json.JSONDecodeError:
                pass

            function_calls[index]["id"] = (
                chunk.id or function_calls[index]["id"] or str(index)
            )

          elif isinstance(chunk, TextChunk):
            text += chunk.text
            yield _message_to_generate_content_response(
                ChatCompletionAssistantMessage(
                    role="assistant",
                    content=chunk.text,
                ),
                is_partial=True,
            )

          elif isinstance(chunk, UsageMetadataChunk):
            usage_metadata = types.GenerateContentResponseUsageMetadata(
                prompt_token_count=chunk.prompt_tokens,
                candidates_token_count=chunk.completion_tokens,
                total_token_count=chunk.total_tokens,
            )

          if (
              finish_reason == "tool_calls" or finish_reason == "stop"
          ) and function_calls:
            tool_calls = []
            for index, func_data in function_calls.items():
              if func_data["id"]:
                tool_calls.append(
                    ChatCompletionMessageToolCall(
                        type="function",
                        id=func_data["id"],
                        function=Function(
                            name=func_data["name"],
                            arguments=func_data["args"],
                            index=index,
                        ),
                    )
                )
            aggregated_llm_response_with_tool_call = (
                _message_to_generate_content_response(
                    ChatCompletionAssistantMessage(
                        role="assistant",
                        content=text,
                        tool_calls=tool_calls,
                    )
                )
            )
            text = ""
            function_calls.clear()

          elif finish_reason == "stop" and text:
            aggregated_llm_response = _message_to_generate_content_response(
                ChatCompletionAssistantMessage(role="assistant", content=text)
            )
            text = ""

      if aggregated_llm_response:
        if usage_metadata:
          aggregated_llm_response.usage_metadata = usage_metadata
          usage_metadata = None
        yield aggregated_llm_response

      if aggregated_llm_response_with_tool_call:
        if usage_metadata:
          aggregated_llm_response_with_tool_call.usage_metadata = usage_metadata
        yield aggregated_llm_response_with_tool_call

    else:
      response = await self.llm_client.acompletion(**completion_args)
      yield _model_response_to_generate_content_response(response)

  @classmethod
  @override
  def supported_models(cls) -> list[str]:
    return []