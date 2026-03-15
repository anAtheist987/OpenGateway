<!-- Copyright 2026 Tsinghua University. Licensed under Apache 2.0.
     This file was created by Tsinghua University and is not part of
     the original agentgateway project by Solo.io. -->

# 共享部门文档读取 Agent

共享文档读取型 Agent，仅服务以下部门：
- `procurement`（采购/集采）
- `foreign`（外事/出入境）
- `safety`（安全/海外安全）

不处理：
- `finance`
- `infosec`

输出统一 JSON：
- `process_steps`
- `material_checklist`
- `warnings`

## 数据源

- 默认读取：`/root/reimbursement_portal/reimbursement_mock_data.json`
- 可通过环境变量覆盖：`REIMBURSEMENT_MOCK_DATA_PATH`

## 运行

```bash
conda activate a2a
cd /root/airbnb_planner_multiagent/dept_doc_reader_agent
python __main__.py --port 10011
```

## MCP 工具

- `list_supported_departments()`
- `list_department_notices(department_id?, limit?)`
- `search_department_notices(query, department_id, limit?)`
- `extract_department_notices(department_id, notice_ids, categories?, include_attachment?)`
- `extract_constraints_and_process(doc_text, categories?)`
- `extract_from_file(file_content_base64, file_extension, categories?)`
- `extract_from_html(html_content, categories?)`
- `list_extraction_categories()`

