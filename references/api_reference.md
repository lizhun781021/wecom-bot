---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '5211da60-7f40-480c-9d9d-ee1d1719b6cc'
  PropagateID: '5211da60-7f40-480c-9d9d-ee1d1719b6cc'
  ReservedCode1: 'f7c0f208-85b7-4bcc-8abf-312de35ae0b0'
  ReservedCode2: 'f7c0f208-85b7-4bcc-8abf-312de35ae0b0'
---

# API Reference — push.py & wecom_api.py

Complete function signatures for the wecom-bot project's two main API modules.

## Table of Contents

- [push.py — Active Push](#pushpy--active-push)
  - [Group Push (Webhook)](#group-push-webhook)
  - [Individual Push (App Message)](#individual-push-app-message)
  - [Utilities](#utilities)
- [wecom_api.py — MCP Document/Sheet/Todo](#wecom_apipy--mcp-documentsheettodo)
  - [Document](#document)
  - [Smart Sheet](#smart-sheet)
  - [Todo](#todo)
  - [MCP Low-level](#mcp-low-level)

---

## push.py — Active Push

### Group Push (Webhook)

| Function | Params | Returns | Notes |
|----------|--------|---------|-------|
| `push_to_group(content, mentioned_list=None, mentioned_mobile_list=None)` | `content: str`, `mentioned_list: [userid]` (`["@all"]` = @全体), `mentioned_mobile_list: [phone]` | `dict` (API response) | Webhook text |
| `push_to_group_markdown(content)` | `content: str` (Markdown) | `dict` | Webhook markdown, 企微内可见 |
| `push_to_group_image(image_path)` | `image_path: str` (jpg/png) | `dict` | Auto-compress if base64 >2MB |
| `push_to_group_news(title, description, url, pic_url="")` | All `str` | `dict` | Webhook news link |

### Individual Push (App Message)

| Function | Params | Returns | Notes |
|----------|--------|---------|-------|
| `push_to_user(userid, content)` | `userid: str` (multiples `\|`-separated, `"@all"` = 全员), `content: str` (≤2048 bytes) | `dict` | App text message |
| `push_markdown_to_user(userid, content)` | Same as above | `dict` | 企微内可见, 微信端不可见 |
| `push_textcard_to_user(userid, title, description, url, btntxt="详情")` | All `str` | `dict` | Card with button |
| `push_image_to_user(userid, image_path)` | `userid: str`, `image_path: str` | `dict` | Uploads media first |
| `push_file_to_user(userid, file_path)` | `userid: str`, `file_path: str` | `dict` | Uploads media first |

### Utilities

| Function | Params | Returns | Notes |
|----------|--------|---------|-------|
| `upload_media(file_path, media_type="file")` | `file_path: str`, `media_type: "image"/"voice"/"video"/"file"` | `str` (media_id) or `None` | Upload temporary media |
| `push_notification(title, content, to_user=None, to_group=True)` | `title: str`, `content: str`, `to_user: userid or None`, `to_group: bool` | `(group_result, user_result)` | Convenience: push to both |

---

## wecom_api.py — MCP Document/Sheet/Todo

### Document

| Function | Params | Returns | Notes |
|----------|--------|---------|-------|
| `create_wecom_doc(doc_name, markdown_content)` | `doc_name: str`, `markdown_content: str` | `{"success": bool, "url": str, "docid": str, "error": str}` | Creates doc with Markdown content; falls back to empty doc + overwrite |
| `mcp_create_doc(doc_name, doc_type="doc", content="", fields=None, sheet_title=None)` | See left | `{"success": bool, "data": dict, "error": str}` | Low-level MCP doc_create |
| `mcp_overwrite_doc_content(docid, content, content_type="markdown")` | `docid: str`, `content: str`, `content_type: "markdown"/"text"` | `dict` | Full overwrite of doc content |

### Smart Sheet

| Function | Params | Returns | Notes |
|----------|--------|---------|-------|
| `create_smart_sheet_with_headers(doc_name, headers, field_types=None)` | `doc_name: str`, `headers: [str]`, `field_types: {header: "text"/"number"}` (optional, auto-guess if omitted) | `{"success": bool, "docid": str, "sheet_id": str, "url": str, "error": str}` | One-step: doc_create with fields+sheet_title |
| `add_smart_sheet_records(docid, sheet_id, records)` | `docid: str`, `sheet_id: str`, `records: [{field: value, ...}]` (text fields: `[{"type":"text","text":"..."}]`) | `dict` | Auto-wraps to `{"values": {...}}` |
| `append_peican_record(record_data)` | `record_data: {field: value}` (keys match `PEICAN_SHEET_HEADERS`) | `{"success": bool, "url": str, "error": str}` | High-level: auto-ensure sheet, append row |
| `mcp_smartsheet_get_sheets(docid)` | `docid: str` | `dict` | List sub-sheets |
| `mcp_smartsheet_get_fields(docid, sheet_id)` | Both `str` | `dict` | List fields |
| `mcp_smartsheet_add_fields(docid, sheet_id, fields)` | `fields: [{"field_title": str, "field_type": "text"/"number"}]` | `dict` | Add fields |

### Todo

| Function | Params | Returns | Notes |
|----------|--------|---------|-------|
| `create_todo(content, follower_userid, end_time=None, remind_type_list=None)` | `content: str`, `follower_userid: str` (企微userid), `end_time: "YYYY-MM-DD HH:mm:ss"` (default: 3 days), `remind_type_list: [int]` (default: `[1]` on due; 0=none, 3=15min, 5=1h, 7=1day, 9=1week) | `{"success": bool, "todo_id": str, "error": str}` | Create todo |
| `get_todo_list(follower_userid, todo_status=None, limit=10, cursor=None, ...)` | `todo_status: 0/1/None`, `limit: ≤20`, time filters optional | `{"success": bool, "data": {"todo_list": [...], "next_cursor": str}, "error": str}` | Only bot-created todos |
| `get_todo_detail(todo_ids)` | `todo_ids: [str]` or `str` (max 20) | `dict` | Batch get details |
| `update_todo(todo_id, content=None, end_time=None, remind_type_list=None)` | See left | `dict` | Update fields |
| `delete_todo(todo_id)` | `todo_id: str` | `dict` | Delete |
| `change_todo_user_status(todo_id, follower_userid, todo_status)` | `todo_status: 0=完成, 1=进行中` | `dict` | Mark complete/incomplete |
| `search_todo_userid(keyword)` | `keyword: str` (name/pinyin) | `dict` | Find users for todo followers |

### MCP Low-level

| Function | Params | Returns | Notes |
|----------|--------|---------|-------|
| `_mcp_call(tool_name, arguments, timeout=60, service="doc")` | `tool_name: str`, `arguments: dict`, `service: "doc"/"todo"` | `{"success": bool, "data": dict, "error": str}` | Core MCP JSON-RPC call |
| `_get_access_token()` | None | `str` or `None` | Token cache (2h, refresh 5min early) |
| `_get_mcp_url(biz_type)` | `biz_type: "doc"/"todo"` | `str` | config.py → wecom-cli decrypt fallback |
| `_load_mcp_config()` | None | `list` | Decrypt `~/.config/wecom/mcp_config.enc` (AES-256-GCM) |

### Constants

- `PEICAN_SHEET_HEADERS`: `["时间", "处理人", "客户号码", "当前套餐", "出账金额", "推荐套餐", "套餐月费", "配餐路径", "提值空间", "备注"]`
- `WECOM_API_BASE`: `"https://qyapi.weixin.qq.com/cgi-bin"`
- `WECOM_CLI_CONFIG_DIR`: `"~/.config/wecom"`
- Sheet cache file: `peican_sheet_cache.json` (stores `docid` + `sheet_id`)