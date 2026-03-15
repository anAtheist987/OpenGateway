<!-- Copyright 2026 Tsinghua University. Licensed under Apache 2.0.
     This file was created by Tsinghua University and is not part of
     the original agentgateway project by Solo.io. -->

# 保密/信息安全文档 Agent

保密与信息安全专属文档抽取 Agent。  
仅处理「保密与信息安全办公室」公告，输出统一 JSON：
- `process_steps`
- `material_checklist`
- `warnings`

## 数据源

- 默认读取：`/root/reimbursement_portal/reimbursement_mock_data.json`
- 可通过环境变量覆盖：`REIMBURSEMENT_MOCK_DATA_PATH`

## 运行

```bash
conda activate a2a
cd /root/airbnb_planner_multiagent/infosec_document_agent
python __main__.py --port 10010
```

## MCP 工具

- `list_infosec_notices(limit?)`
- `search_infosec_notices(query, limit?)`
- `extract_infosec_notices(notice_ids, categories?, include_attachment?)`
- `extract_constraints_and_process(doc_text, categories?)`
- `extract_from_file(file_content_base64, file_extension, categories?)`
- `extract_from_html(html_content, categories?)`
- `list_extraction_categories()`

