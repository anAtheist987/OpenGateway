<!-- Copyright 2026 Tsinghua University. Licensed under Apache 2.0.
     This file was created by Tsinghua University and is not part of
     the original agentgateway project by Solo.io. -->

# 文档提取 Agent

[English](README.md)

## 概述

文档提取 Agent 是一个 A2A 智能体，从企业差旅/制度文档中抽取**约束**、**推荐条件**、**流程步骤**和**材料清单**，供网关将「部门条款与个人数据」转化为约束或推荐条件，避免把整份文档塞入大模型上下文。

对于 A2A 服务调用，当前默认会返回简洁、可机读的流程结果（而不是长篇叙述）：
- `process_steps`
- `material_checklist`
- `warnings`

**主要能力：**
- 抽取硬约束（如「6 小时以上航班可乘商务舱」「仅限协议酒店」）并带来源引用
- 抽取软推荐（如「优先选择协议酒店」）用于方案页的推荐理由
- 抽取审批/备案/报销步骤及所需材料，用于流程页
- 输出带 `source` / `source_excerpt` 的结构化 JSON，满足可审计

**端口：** `10008`

## MCP 工具

- **extract_constraints_and_process(doc_text, categories?)**  
  从**纯文本**抽取。返回 JSON：`constraints`、`recommendations`、`process_steps`、`material_checklist`、`warnings`。可选 `categories` 限定维度（如 `["transport", "accommodation", "approval"]`）。
- **extract_from_file(file_content_base64, file_extension, categories?)**  
  从**文件**抽取。支持 **PDF（.pdf）、Word（.docx）、Excel（.xlsx）**。参数：文件内容的 Base64 字符串、扩展名（如 `"pdf"`/`"docx"`/`"xlsx"`）、可选的 categories。返回相同结构的 JSON。
- **extract_from_html(html_content, categories?)**  
  从**HTML 原文**抽取（如公告详情页源码），内部会先清洗为文本再抽取。
- **list_portal_notices(department?, category?, limit?)**  
  列出报销公告站（mock 数据）中的候选公告。
- **search_portal_notices(query, department?, category?, limit?)**  
  按关键词检索候选公告（建议先检索再抽取）。
- **extract_from_portal_notices(notice_ids, categories?, include_attachment?)**  
  对指定公告 ID 执行定向抽取（可选拼接附件摘要）。
- **list_extraction_categories()**  
  返回支持的抽取维度和流程阶段。

> 建议流程：先 `search_portal_notices` 缩小范围，再 `extract_from_portal_notices` 抽取。除非明确要求，不建议一次性全量抽取所有公告。

## 配置

1. 复制环境并配置 LLM：

   ```bash
   cd document_agent
   cp example.env .env
   ```

2. 至少配置其一：
   - `LITELLM_MODEL`（如 `dashscope/qwen-plus` 或 `gemini-2.5-flash`）及对应 API Key / Base URL，或
   - `GOOGLE_API_KEY`（Gemini）。

## 运行

```bash
cd document_agent
python __main__.py --port 10008
```

服务监听 `0.0.0.0:10008`，实际访问建议使用 `127.0.0.1`。

- Agent Card: http://127.0.0.1:10008/.well-known/agent-card.json

如果你修改了 `.env`（如 `APP_URL`、模型配置等），请重启服务后再验证。

## 快速测试服务

```bash
cd document_agent
python call_service_demo.py --base-url http://127.0.0.1:10008 --smoke
python call_service_demo.py --base-url http://127.0.0.1:10008 --timeout 220
```

## 与网关的集成

当用户意图涉及差旅制度、审批、报销或部门条款时，Host 应调用本 Agent。网关使用返回的 JSON 做：
- **方案页：** 用约束与推荐条件过滤、标注航班/酒店方案，并给出关联部门条款的推荐理由
- **流程页：** 展示审批/报销步骤与材料清单
- **检查页：** 校验关键约束是否满足，并提示缺失项（可使用 `warnings`）

在注册中心为本 Agent 配置关键词（如 `document`、`policy`、`constraints`、`approval`、`reimbursement`），便于 Host 发现并调用。
