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

from typing import List, Optional
from pydantic import BaseModel, Field


class RegistryListReq(BaseModel):
    """POST /api/v1/{keyword}/list 的请求体"""
    request_id: str
    task: str
    top_k: int = Field(default=3, ge=1, le=50)


class RegistryAgentItem(BaseModel):
    """候选 Agent（Registry 返回）"""
    score: float
    agent_id: str
    name: str
    description: Optional[str] = None
    url: Optional[str] = None
    version: str


class RegistryListResp(BaseModel):
    """POST /api/v1/{keyword}/list 的响应体"""
    status: str                # "success" / "error"
    request_id: str
    count: int
    agents: List[RegistryAgentItem] = []
    ##API格式
