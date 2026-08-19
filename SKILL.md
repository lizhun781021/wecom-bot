---
name: wecom-bot
description: Enterprise WeChat (企微) + QQ dual-channel bot management. Send messages/files/images to WeChat groups or individuals, create WeChat documents/smart sheets/todos via MCP, manage bot services (start/stop/restart/status), and troubleshoot issues. Use when user mentions 企微机器人, 企业微信推送, wecom-bot, QQ机器人, 群消息推送, 企微待办, 企微文档, 企微表格, 8505面板, or needs to push notifications/files to WeChat contacts or groups.
name_cn: "企微QQ双通道机器人"
description_cn: "管理企微长连接机器人与QQ官方机器人，支持消息/文件/图片推送、企微文档/智能表格/待办创建、服务启停与面板管理。"
create_source: super-agent-skill-creator
---

# 企微QQ双通道机器人

## Overview

Manage `wecom-bot` project: a dual-channel bot integrating Enterprise WeChat (WebSocket long-connection) and QQ (official botpy SDK) into a unified AI pipeline via the local 8088 OpenAI-compatible proxy → TeleAgent.

**Project root**: `<your_project_dir>/wecom-bot/`
**Dashboard**: http://127.0.0.1:8505
**launchd services**: `com.<your_name>.wecom-bot` (企微), `com.<your_name>.qq-adapter` (QQ)

## Architecture

```
企微用户/QQ用户 → 企微WebSocket / QQ botpy → server.py / qq_official_adapter.py
  → 禁用环境代理 → 8088 proxy (OpenAI兼容) → TeleAgent AI
  → 回复: 企微WebSocket原路返回 / QQ API post_message
  → 后处理: 配餐台账 / 待办创建 / 文档生成 (wecom_api.py MCP)
  → 主动推送: push.py (Webhook群聊 / 应用消息1v1)
```

## Core Capabilities

### 1. Push Messages to WeChat

Use `push.py` for active push. Import from project root or run as CLI.

**Push to group (Webhook)**:
```python
from push import push_to_group, push_to_group_markdown, push_to_group_image, push_to_group_news

# Text
push_to_group("收入数据已更新")
# Markdown (group only)
push_to_group_markdown("## 收入日报\n- 总收入：1.2亿\n- 同比：+5.2%")
# Image (auto-compress if >2MB)
push_to_group_image("/path/to/chart.png")
# News link
push_to_group_news("标题", "描述", "https://url.com")
```

**Push to individual (App Message API)**:
```python
from push import push_to_user, push_markdown_to_user, push_textcard_to_user, push_image_to_user, push_file_to_user

# Text (userid from 通讯录API or WECOM_USER_MAP in config.py)
push_to_user("<userid>", "你有一份配餐方案待查看")
# Markdown (企微内可见, 微信端不可见)
push_markdown_to_user("<userid>", "## 方案\n内容...")
# Text card
push_textcard_to_user("<userid>", "配餐方案", "客户139xxx推荐129元套餐", "https://url.com")
# Image
push_image_to_user("<userid>", "/path/to/image.png")
# File (auto upload media → send)
push_file_to_user("<userid>", "/path/to/report.docx")
```

**CLI**:
```bash
cd <your_project_dir>/wecom-bot
python push.py group "消息内容"
python push.py group_md "## 标题\n内容"
python push.py group_img /path/to/image.jpg
python push.py user <userid> "消息内容"
python push.py user_file <userid> /path/to/file.docx
```

### 2. Create WeChat Documents / Smart Sheets / Todos

Use `wecom_api.py` via MCP (streamableHTTP). Requires backend authorization (管理后台 → 智能机器人 → 文档权限, 7-day expiry).

**Create document with Markdown content**:
```python
from wecom_api import create_wecom_doc
result = create_wecom_doc("配餐方案_20260819", "# 配餐方案\n\n## 当前套餐\n99元不限量\n\n## 推荐套餐\n129元5G融合")
# Returns: {"success": True, "url": "https://doc.weixin.qq.com/...", "docid": "..."}
```

**Create smart sheet with headers + append records**:
```python
from wecom_api import create_smart_sheet_with_headers, add_smart_sheet_records, append_peican_record

# Create sheet (one-step: doc_create with fields + sheet_title)
result = create_smart_sheet_with_headers("配餐台账_202608", ["时间","处理人","客户号码","出账金额","推荐套餐"])
docid = result["docid"]   # Cache in peican_sheet_cache.json
sheet_id = result["sheet_id"]

# Append record (auto-ensure sheet exists)
append_peican_record({
    "时间": "2026-08-19 10:00", "处理人": "张三", "客户号码": "139xxxx",
    "当前套餐": "99元不限量", "出账金额": "120元",
    "推荐套餐": "129元5G融合", "套餐月费": "129元",
    "配餐路径": "平替升级", "提值空间": "30元/月", "备注": "客户同意"
})
```

**Create / manage todos**:
```python
from wecom_api import create_todo, get_todo_list, get_todo_detail, update_todo, delete_todo, change_todo_user_status, search_todo_userid

# Create todo (default: 3 days later, remind on due)
create_todo(content="跟进客户139xxx签约", follower_userid="<userid>")
# List todos (only bot-created ones)
get_todo_list(follower_userid="<userid>", todo_status=1, limit=10)
# Complete todo
change_todo_user_status(todo_id="td-xxx", follower_userid="<userid>", todo_status=0)
# Search user by name/pinyin (for adding followers)
search_todo_userid("张三")
```

### 3. Service Management

Manage via launchd. Both services are always-on with auto-restart.

```bash
# 企微机器人 (WebSocket long-connection, port 8505 dashboard)
launchctl list | grep wecom-bot
launchctl kickstart -k gui/$(id -u)/com.<your_name>.wecom-bot

# QQ 适配器 (separate process, shares server.py pipeline)
launchctl list | grep qq-adapter
launchctl kickstart -k gui/$(id -u)/com.<your_name>.qq-adapter

# Check logs
tail -50 <your_project_dir>/wecom-bot/wecom-bot.log
tail -50 <your_project_dir>/wecom-bot/qq-adapter-app.log
```

### 4. Session Management

Sessions are fixed per user/group to maintain conversation context:

- **企微私聊**: fixed by `userid` (e.g. `wo-xxx`), reuse across messages
- **企微群聊**: fixed by `chatid`
- **QQ群聊**: fixed by `group_openid`
- **QQ私聊**: fixed by `user_openid`

Session title format: `QQ | 群聊 | 昵称 | YYYY-MM-DD HH:mm` or `企微 | 私聊 | 姓名 | 时间`.

Sessions are created via 8088 proxy's `/v1/chat/completions` with a `session_title` field. The proxy maintains session→messages mapping and reuses the same conversation thread.

### 5. Config Management

Edit `config.py` (copy from `config_example.py` for new deployments):

- `BOT_ID` / `BOT_SECRET`: 企微长连接凭证 (管理后台 → 智能机器人 → API模式)
- `WEBHOOK_URL`: 群机器人推送地址
- `CORP_ID` / `CORP_SECRET` / `AGENT_ID`: 企微通讯录API + 应用消息推送
- `TELEAGENT_PROXY_URL`: `http://127.0.0.1:8088/v1/chat/completions`
- `TELEAGENT_MODEL`: `NewApi/chat-pro`
- `QQ_ENABLED` / `QQ_APPID` / `QQ_SECRET`: QQ官方机器人开关与凭证
- `WECOM_USER_MAP`: 手动 userid→姓名映射 (自动查询结果会补充到本地缓存)
- `WECOM_MCP_URL` / `WECOM_TODO_MCP_URL`: MCP服务地址 (不配则自动解密 wecom-cli 配置兜底)

### 6. Dashboard (8505)

Web panel embedded in `server.py` (port 8505). Shows:
- 企微连接状态、消息记录、AI回复日志
- QQ适配器运行状态、消息记录
- 推送历史、配餐台账链接
- 用户姓名映射管理

Access: http://127.0.0.1:8505

## Key Implementation Details

- **Proxy bypass**: `server.py` clears all `http_proxy`/`HTTPS_PROXY` env vars at import time to prevent 8088 loopback requests being forwarded to external proxy (causes hang).
- **Image auto-compress**: `push.py` auto-compresses images exceeding 2MB base64 limit (PIL, quality 88→40, then resize 90%→30%).
- **MCP config fallback**: `wecom_api.py` reads MCP URLs from `config.py` first, falls back to decrypting `~/.config/wecom/mcp_config.enc` (AES-256-GCM).
- **Smart sheet creation**: Use `doc_create` with `doc_type="smartsheet"` + `fields` array + `sheet_title` (all required). Not `create_doc` or integer type codes.
- **Sheet cache**: `peican_sheet_cache.json` stores docid/sheet_id to avoid recreating the ledger each time. Auto-rebuilds if sheet is deleted.
- **Todo scope**: Bot can only query/manage todos it created. `follower_userid` must be specified.
- **QQ group push**: Official API disabled group proactive push. Use passive reply within 5 min of last @mention (reuse `msg_id`).

## Troubleshooting

| Symptom | Check |
|---------|-------|
| AI不回复 | 8088代理是否运行 (`launchctl list \| grep openai-proxy`), 日志是否有429/token过期 |
| 推送失败 | `WEBHOOK_URL`/`CORP_SECRET`是否正确, access_token获取是否成功 (查push日志) |
| MCP调用失败 | 企微后台文档权限是否过期(7天), `WECOM_MCP_URL`是否配置, wecom-cli配置是否可解密 |
| QQ不响应 | `QQ_ENABLED`是否True, botpy是否安装, qq-adapter日志 |
| 图片发送失败 | 文件大小, 格式(jpg/png), 自动压缩日志 |
| 会话不固定 | 8088代理 `/v1/chat/completions` 是否传了 `session_title` |

## references/

- `api_reference.md` — Complete function signatures for `push.py` and `wecom_api.py`
