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

#!/usr/bin/env python3
"""
Mock Agent Registry for Travel Planning Demo
=============================================
实现 Registry API: POST /api/v1/{keyword}/list
- 注册 6 个真实 Agent + 7 个假 Agent（用于测试语义路由）
- 路由策略：优先调用 AgentGateway /task-router/route（vectorPrefilterLlm），
            网关不可用时自动 fallback 到本地 TF-IDF 评分
- 无需外部依赖，纯 Python stdlib
"""
import json
import re
import uuid
import os
import math
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

# ── 网关 Admin URL（用于 vectorPrefilterLlm 路由）────────────────────────────
ADMIN_URL = os.getenv("ADMIN_URL", "http://localhost:15000")

# ── Agent 列表 ────────────────────────────────────────────────────────────────
# 真实 Agent（6 个，当前实际运行）
REAL_AGENTS = [
    {
        "agent_id": "weather-agent-001",
        "name": "WeatherAgent",
        "description": "提供全球城市的天气预报、气候信息、气象数据。处理天气查询、温度、降水量、风速、湿度等信息。",
        "url": "http://localhost:10001",
        "version": "1.0.0",
        "tags": ["天气", "weather", "forecast", "climate", "temperature", "rain", "wind", "humidity", "meteorology", "storm"],
    },
    {
        "agent_id": "flight-agent-001",
        "name": "FlightAgent",
        "description": "搜索和比较航班、机票、票价、航线、中转和航班时刻表。处理出发/到达时间、航司、价格及准点率信息。",
        "url": "http://localhost:10006",
        "version": "1.0.0",
        "tags": ["flight", "airline", "airfare", "ticket", "departure", "arrival", "airport", "booking", "transport", "航班", "机票", "飞机", "交通"],
    },
    {
        "agent_id": "hotel-agent-001",
        "name": "HotelAgent",
        "description": "搜索酒店、度假村、汽车旅馆和传统住宿选项，包含评级、设施和价格比较。",
        "url": "http://localhost:10007",
        "version": "1.0.0",
        "tags": ["hotel", "resort", "motel", "accommodation", "lodging", "check-in", "room", "酒店", "宾馆", "度假村", "住宿"],
    },
    {
        "agent_id": "finance-document-agent-001",
        "name": "FinanceDocumentAgent",
        "description": "从企业财务部门公告中提取报销制度、费用口径、审批节点、票据要件及差旅报销标准。",
        "url": "http://localhost:10009",
        "version": "1.0.0",
        "tags": ["finance", "reimbursement", "expense", "approval", "budget", "invoice", "报销", "财务", "审批", "费用", "发票", "差旅"],
    },
    {
        "agent_id": "infosec-document-agent-001",
        "name": "InfoSecDocumentAgent",
        "description": "从企业信息安全部门公告中提取出境设备要求、数据保护措施、保密要求及信息安全合规规定。",
        "url": "http://localhost:10010",
        "version": "1.0.0",
        "tags": ["infosec", "security", "confidential", "data", "device", "compliance", "信息安全", "保密", "设备", "数据", "合规", "出境"],
    },
    {
        "agent_id": "dept-doc-reader-agent-001",
        "name": "Dept Doc Reader Agent",
        "description": "从采购、外事与出入境、安全与海外风险等部门公告中提取审批流程、备案要求和材料清单。",
        "url": "http://localhost:10011",
        "version": "1.0.0",
        "tags": ["dept", "doc", "procurement", "foreign", "safety", "department", "notice", "approval", "filing", "采购", "外事", "出入境", "安全", "备案", "审批"],
    },
]

# 假 Agent（用于测试语义路由，实际不运行）
FAKE_AGENTS = [
    {
        "agent_id": "data-analyst-001",
        "name": "DataAnalystAgent",
        "description": "对结构化数据集进行统计分析、数据可视化、趋势分析和商业智能报告。",
        "url": "http://localhost:20001",
        "version": "1.0.0",
        "tags": ["data", "analysis", "statistics", "visualization", "report", "BI", "trend", "数据", "分析", "统计"],
    },
    {
        "agent_id": "code-reviewer-001",
        "name": "CodeReviewerAgent",
        "description": "审查代码中的漏洞、安全问题、性能问题和编码最佳实践。支持 Python、JavaScript、Go、Rust。",
        "url": "http://localhost:20002",
        "version": "1.0.0",
        "tags": ["code", "review", "bug", "security", "programming", "debugging", "refactor", "代码", "审查", "编程"],
    },
    {
        "agent_id": "content-writer-001",
        "name": "ContentWriterAgent",
        "description": "创作博客文章、营销文案、产品描述、社交媒体内容和专业写作。",
        "url": "http://localhost:20003",
        "version": "1.0.0",
        "tags": ["writing", "content", "blog", "copy", "marketing", "article", "SEO", "写作", "文案", "内容"],
    },
    {
        "agent_id": "translation-agent-001",
        "name": "TranslationAgent",
        "description": "在中文、英文、日文、法文、德文、西班牙文等多种语言之间互译文本。",
        "url": "http://localhost:20004",
        "version": "1.0.0",
        "tags": ["translation", "language", "Chinese", "English", "Japanese", "localization", "multilingual", "翻译", "语言", "多语言"],
    },
    {
        "agent_id": "calendar-agent-001",
        "name": "CalendarManagerAgent",
        "description": "管理日程、预约、会议、提醒和日历事件。集成 Google Calendar 和 Outlook。",
        "url": "http://localhost:20005",
        "version": "1.0.0",
        "tags": ["calendar", "schedule", "meeting", "appointment", "reminder", "planning", "time", "日程", "会议", "提醒"],
    },
    {
        "agent_id": "image-search-001",
        "name": "ImageSearchAgent",
        "description": "从网络搜索和检索图片、照片和视觉内容。支持以图搜图和视觉相似度匹配。",
        "url": "http://localhost:20006",
        "version": "1.0.0",
        "tags": ["image", "photo", "picture", "visual", "search", "gallery", "图片", "照片", "图像"],
    },
    {
        "agent_id": "document-processor-001",
        "name": "DocumentProcessorAgent",
        "description": "处理、总结和提取文档信息，包括 PDF、Word 和电子表格。支持 OCR 文字识别。",
        "url": "http://localhost:20007",
        "version": "1.0.0",
        "tags": ["document", "PDF", "summary", "extract", "OCR", "word", "spreadsheet", "文档", "PDF", "总结"],
    },
]

ALL_AGENTS = REAL_AGENTS + FAKE_AGENTS


# ── 语义评分 ──────────────────────────────────────────────────────────────────

def _tokenize(text: str) -> list[str]:
    """中英文分词：按标点/空格切分，同时保留中文字符。"""
    # 提取所有词（英文单词 + 中文字符）
    tokens = re.findall(r'[a-zA-Z]+|[\u4e00-\u9fff]', text.lower())
    return tokens


def _idf_score(token: str, all_texts: list[str]) -> float:
    """简单 IDF：出现在越少文档中的词权重越高。"""
    n = len(all_texts)
    df = sum(1 for t in all_texts if token in t.lower())
    return math.log((n + 1) / (df + 1)) + 1.0


# 预计算所有 agent 描述文本
_ALL_TEXTS = [f"{a['name']} {a['description']} {' '.join(a['tags'])}" for a in ALL_AGENTS]


def score_agent(agent: dict, task: str, keyword: str) -> float:
    """TF-IDF 风格语义评分。"""
    query = f"{keyword} {task}"
    query_tokens = set(_tokenize(query))
    if not query_tokens:
        return 0.0

    agent_text = f"{agent['name']} {agent['description']} {' '.join(agent['tags'])}"
    agent_tokens = _tokenize(agent_text)
    agent_token_set = set(agent_tokens)

    score = 0.0
    for token in query_tokens:
        if token in agent_token_set:
            idf = _idf_score(token, _ALL_TEXTS)
            # Tag 匹配权重 2x，名称 1.5x，描述 1x
            tag_match = any(token in t.lower() for t in agent.get("tags", []))
            name_match = token in agent["name"].lower()
            weight = 2.0 if tag_match else (1.5 if name_match else 1.0)
            score += idf * weight

    # 归一化到 [0, 1]
    max_possible = sum(_idf_score(t, _ALL_TEXTS) * 2.0 for t in query_tokens)
    return min(score / max_possible, 1.0) if max_possible > 0 else 0.0


def route_via_gateway(task: str, agents: list[dict], top_k: int) -> list[dict] | None:
    """
    调用 AgentGateway /task-router/route 使用 vectorPrefilterLlm 策略选择 Agent。
    返回按置信度排序的候选列表（格式与 TF-IDF scored 一致），失败时返回 None。
    """
    # 将 agents 转换为网关 AgentInfo 格式（skills = tags）
    agent_infos = [
        {
            "name": a["name"],
            "description": a["description"],
            "url": a["url"],
            "skills": a.get("tags", []),
        }
        for a in agents
    ]
    payload = json.dumps({
        "task": task,
        "agents": agent_infos,
        "strategyOverride": "vectorPrefilterLlm",
    }).encode()

    req = urllib.request.Request(
        f"{ADMIN_URL}/task-router/route",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except (urllib.error.URLError, OSError) as e:
        print(f"[Registry] 网关不可达，fallback 到 TF-IDF: {e}")
        return None
    except Exception as e:
        print(f"[Registry] 网关路由出错，fallback 到 TF-IDF: {e}")
        return None

    decision = result.get("decision", {})
    if decision.get("type") != "direct":
        print(f"[Registry] 网关返回非 direct 决策（{decision.get('type')}），fallback 到 TF-IDF")
        return None

    selected_name = decision.get("agentName", "")
    confidence = float(decision.get("confidence", 1.0))

    # 按置信度构造结果列表：选中的排第一（score=confidence），其余 score=0
    scored = []
    for a in agents:
        s = confidence if a["name"] == selected_name else 0.0
        scored.append({
            "score": round(s, 4),
            "agent_id": a["agent_id"],
            "name": a["name"],
            "description": a["description"],
            "url": a["url"],
            "version": a.get("version", "1.0.0"),
        })
    scored.sort(key=lambda x: x["score"], reverse=True)

    reason = decision.get("reason", "")
    print(f"[Registry] vectorPrefilterLlm → {selected_name} (confidence={confidence:.3f}) reason={reason[:60]!r}")
    return scored[:top_k]

class RegistryHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        # 只打印错误，不打印每次请求
        pass

    def _send_json(self, code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except Exception:
            return None

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/health":
            self._send_json(200, {
                "status": "ok",
                "total_agents": len(ALL_AGENTS),
                "real_agents": len(REAL_AGENTS),
                "fake_agents": len(FAKE_AGENTS),
            })

        elif path == "/agents":
            # 返回所有注册 agent 列表（含分类标记）
            result = []
            for a in REAL_AGENTS:
                result.append({**a, "is_real": True})
            for a in FAKE_AGENTS:
                result.append({**a, "is_real": False})
            self._send_json(200, result)

        elif path == "/api/v1/agents/list":
            # 兼容性端点：返回全部 agent
            self._send_json(200, {
                "status": "success",
                "count": len(ALL_AGENTS),
                "agents": [{"score": 1.0, **a} for a in ALL_AGENTS],
            })

        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")

        # POST /api/v1/{keyword}/list
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "v1" and parts[3] == "list":
            keyword = parts[2]
            body = self._read_body()
            if body is None:
                self._send_json(400, {"error": "invalid JSON"})
                return

            task = body.get("task", "")
            top_k = min(int(body.get("top_k", 3)), len(REAL_AGENTS))
            request_id = body.get("request_id", str(uuid.uuid4()))

            # 优先使用网关 vectorPrefilterLlm 路由；网关不可用则 fallback 到 TF-IDF
            top = route_via_gateway(task, REAL_AGENTS, top_k)
            if top is None:
                # TF-IDF fallback
                scored = []
                for agent in REAL_AGENTS:
                    s = score_agent(agent, task, keyword)
                    scored.append({
                        "score": round(s, 4),
                        "agent_id": agent["agent_id"],
                        "name": agent["name"],
                        "description": agent["description"],
                        "url": agent["url"],
                        "version": agent["version"],
                    })
                scored.sort(key=lambda x: x["score"], reverse=True)
                top = scored[:top_k]
                print(f"[Registry][TF-IDF] keyword={keyword!r} task={task[:40]!r}... → top{top_k}: {[a['name'] for a in top]}")
            else:
                print(f"[Registry][GW]     keyword={keyword!r} task={task[:40]!r}... → top{top_k}: {[a['name'] for a in top]}")

            self._send_json(200, {
                "status": "success",
                "request_id": request_id,
                "count": len(top),
                "agents": top,
            })

        else:
            self._send_json(404, {"error": "unknown endpoint"})


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    port = int(os.environ.get("REGISTRY_PORT", "8090"))
    server = ThreadingHTTPServer(("0.0.0.0", port), RegistryHandler)
    real_n = len(REAL_AGENTS)
    fake_n = len(FAKE_AGENTS)
    print(f"[Mock Registry] 启动在 http://0.0.0.0:{port}")
    print(f"[Mock Registry] 注册 Agent: {real_n} 个真实 + {fake_n} 个假 Agent = {real_n + fake_n} 总计")
    print(f"[Mock Registry] 端点: POST /api/v1/{{keyword}}/list")
    server.serve_forever()


if __name__ == "__main__":
    main()
