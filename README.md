---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '0194e91a-28a8-4e03-a07e-d516df28ffc4'
  PropagateID: '0194e91a-28a8-4e03-a07e-d516df28ffc4'
  ReservedCode1: 'b54ae02b-1427-404a-8ec2-5e2bca58e08a'
  ReservedCode2: 'b54ae02b-1427-404a-8ec2-5e2bca58e08a'
---

# 企微QQ量子三通道机器人

![version](https://img.shields.io/badge/version-1.15.0-blue)

## 简介

整合**企业微信**（WebSocket 长连接）、**QQ 官方机器人**（botpy SDK）、**量子密信**（webhook 回调）的三通道智能机器人，统一接入本地 8088 OpenAI 兼容代理 → TeleAgent AI 处理，支持群聊/私聊自动回复、主动推送、企微文档/智能表格/待办创建，配备 8505 Web 管理面板。

```
企微用户/QQ用户 → 企微WebSocket / QQ botpy → server.py / qq_official_adapter.py
  → 禁用环境代理 → 8088 proxy (OpenAI兼容) → TeleAgent AI
  → 回复: 企微WebSocket原路返回 / QQ API post_message
  → 后处理: 配餐台账 / 待办创建 / 文档生成 (wecom_api.py MCP)
  → 主动推送: push.py (Webhook群聊 / 应用消息1v1)

量子密信群 @机器人 → 平台回调(HTTPS POST) → 公网入口(SSH反向隧道) → zmx_adapter.py(:1011)
  → 复用 server.call_teleagent / build_prompt / get_session_title
  → 回复: POST callBackUrl (量子密信webhook send API)
```

## 三通道对比

| 通道 | 连接方向 | 公网需求 | 适配器 | 消息类型 |
|---|---|---|---|---|
| 企微 | 机器人主动连平台（WebSocket长连接） | 不需要 | server.py（主进程） | 文字/图片/文件/语音/视频 |
| QQ | 机器人主动连平台（反向连接） | 不需要 | qq_official_adapter.py（独立进程） | 文字/图片/文件 |
| 量子密信 | 平台主动连机器人（HTTP回调） | 需要（SSH反向隧道/公网服务器） | zmx_adapter.py（独立进程） | 文字/图片/文件/Markdown |

> 量子密信是回调模式，平台把 @机器人 消息 POST 到指定 URL，必须有公网可达的 HTTPS 端点；企微/QQ 是长连接模式无需公网。这是平台机制决定，代码无法绕过。

## 核心能力

### 1. 消息收发（三通道自动回复）

| 通道 | 接收 | 回复 |
|---|---|---|
| 企微 | 群聊 @机器人（文字/图片/语音/视频） | WebSocket 原路 stream 回复 |
| QQ | 群@消息 + 单聊消息 | QQ 官方 API post_message |
| 量子密信 | 群聊 @机器人（文字） | POST callBackUrl webhook（文本/Markdown/图片/文件） |

会话按用户/群固定（8088 代理按 `session_title` 复用会话），标题格式：`通道 | 场景 | 显示名 | YYYY-MM-DD HH:mm`。

### 2. 主动推送（push.py）

```bash
# 群聊
python push.py group "消息内容"
python push.py group_md "## 标题\n内容"
python push.py group_img /path/to/image.jpg

# 个人1v1
python push.py user <userid> "消息内容"
python push.py user_file <userid> /path/to/file.docx
```

也可通过 Web 面板 http://127.0.0.1:8505 直接发送。

### 3. 企微文档/智能表格/待办（wecom_api.py MCP）

```python
# 创建文档
create_wecom_doc("配餐方案_20260819", "# 配餐方案\n...")

# 创建智能表格 + 追加记录
create_smart_sheet_with_headers("配餐台账_202608", ["时间","处理人","客户号码",...])
append_peican_record({"时间":"...", "处理人":"张三", ...})

# 待办
create_todo(content="跟进客户139xxx签约", follower_userid="<userid>")
get_todo_list(follower_userid="<userid>", limit=10)
change_todo_user_status(todo_id="td-xxx", follower_userid="<userid>", todo_status=0)
search_todo_userid("张三")
```

### 4. 服务管理（launchd）

三个服务常驻、自动重启：

```bash
launchctl list | grep wecom-bot        # 企微（含8505面板）
launchctl list | grep qq-adapter       # QQ
launchctl list | grep zmx-tunnel       # 量子密信反向隧道
```

### 5. 监控面板（8505）

Web 面板嵌入 server.py（端口 8505），展示三通道状态（企微/QQ/量子密信）、消息记录、AI回复日志、推送历史、配餐台账链接、用户姓名映射管理。

## 量子密信通道

量子密信适配器（zmx_adapter.py），独立进程，复用 server.py 的 AI 管线。

**公网入口（SSH 反向隧道）**：
```
量子密信平台 → 公网服务器:1011 → SSH反向隧道 → Mac localhost:1011 → zmx_adapter → localhost:8088 AI
```
- 云服务器需开启 `GatewayPorts clientspecified`，iptables + 安全组双层放行 1011
- Mac 端 autossh 保活：`autossh -M 0 -N -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -R 0.0.0.0:1011:localhost:1011 root@<服务器IP>`

**消息类型**：支持文本、Markdown、图片、文件 4 种消息。图片/文件通过两步式发送（先调 upload-attachment 上传获取 fileId，再调 send 发送）。

## 快速部署

1. 克隆仓库：`git clone https://github.com/lizhun781021/wecom-bot.git`
2. 复制配置：`cp config_example.py config.py`
3. 编辑 `config.py`，填入各通道凭证（企微/QQ/量子密信，按需启用）
4. 安装依赖：`pip install -r requirements.txt`
5. 启动机器人：`python server.py`

## 配置（config.py）

```python
# 企微
BOT_ID = "xxx"
BOT_SECRET = "xxx"
WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
CORP_ID = "xxx"
CORP_SECRET = "xxx"
AGENT_ID = "xxx"

# AI 代理
TELEAGENT_PROXY_URL = "http://127.0.0.1:8088/v1/chat/completions"
TELEAGENT_MODEL = "NewApi/chat-pro"

# QQ（按需启用）
QQ_ENABLED = False
QQ_APPID = "xxx"
QQ_SECRET = "xxx"

# 量子密信（按需启用）
ZMX_ENABLED = False
ZMX_CALLBACK_URL = "https://imtwo.zdxlz.com/im-external/v1/webhook/send?key=xxx"
ZMX_LISTEN_PORT = 1011
ZMX_USER_MAP = {"139xxxxxxxx": "张三"}

# 用户映射
WECOM_USER_MAP = {"wo-xxxxx": "李准"}
```

## 文件说明

| 文件 | 说明 |
|------|------|
| `server.py` | 企微主程序（WebSocket连接、消息处理、图片解密、代理调用、文件上传、配餐后处理、面板嵌入、启动QQ/量子密信子进程） |
| `qq_official_adapter.py` | QQ官方机器人适配器（监听群@/单聊 + TeleAgent双向桥 + 内部推送端点18506） |
| `zmx_adapter.py` | 量子密信适配器（webhook回调收发 + 文本/Markdown/图片/文件推送 + SSH反向隧道公网入口） |
| `push.py` | 主动推送模块（群聊Webhook：文字/Markdown/图片/图文；1v1应用消息：文字/Markdown/卡片/图片/文件） |
| `wecom_api.py` | 企微文档/表格/待办 API 封装（HTTP MCP，含 mcp_config 加密配置自动解密） |
| `dashboard.py` | Web管理面板（端口8505，三通道状态监控+主动推送+消息记录+实时日志） |
| `config.py` | 配置文件（**已加入.gitignore，不上传**） |
| `config_example.py` | 配置模板（脱敏样例） |

## 技术要点

- **企微长连接**：必须用 `stream` 类型回复，mixed 消息结构（text+image），图片 AES-256-CBC 解密
- **文件上传**：三步同步流程（init→chunk→finish），异步线程执行不阻塞消息接收
- **图片自动压缩**：超 2MB 自动压缩（PIL，quality 88→40，再 resize 90%→30%）
- **MCP 配置兜底**：wecom_api.py 优先读 config.py，其次解密 `~/.config/wecom/mcp_config.enc`（AES-256-GCM）
- **智能表格**：用 `doc_create` + `doc_type="smartsheet"` + `fields` + `sheet_title`，不能用整数类型码
- **QQ 群推送限制**：官方 API 禁止群主动推送，只能在最后 @mention 后 5 分钟内被动回复（复用 msg_id）
- **量子密信消息类型**：webhook 模式支持文本/Markdown/图片/文件 4 种，图片/文件为两步式上传发送；DCOOS 模式支持 7 种（含音频/视频/卡片）
- **量子密信群隔离**：回调携带的 callBackUrl 是群专属回复地址，必须用它回复（否则串群）
- **SSH 反向隧道双层防火墙**：需同时放行 iptables + 云平台安全组
- **消息状态持久化**：适配器内存更新状态后必须同步写回 JSON 文件（面板读文件而非内存），所有路径都要更新

## 依赖服务

- **openai-proxy**（端口8088）：本地 OpenAI 兼容代理，转发请求给 TeleAgent
- **TeleAgent**：AI 能力来源（看图、河南标准化赋能技能等）

## 版本管理

本项目使用语义化版本号，通过 git tag 标记每个版本。

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v1.15.0 | 2026-08-27 | 三通道权限确认群内应答 + 流式输出（企微打字机/QQ密信进度消息），对话不再中断 |
| v1.14.0 | 2026-08-26 | 量子密信 webhook 模式解除限制，图片/文件推送全通（upload-attachment 修复） |
| v1.13.0 | 2026-08-22 | 量子密信 DCOOS 平台模式（7 种消息类型 + 加密验签 + 多群推送） |
| v1.12.0 | 2026-08-21 | 量子密信消息状态持久化修复 + 推送格式限制为文本/Markdown + 技能文档同步更新 |
| v1.11.0 | 2026-08-21 | 新增量子密信机器人通道 + 会话标题持久化 + 三通道管理面板 |
| v1.10.0 | 2026-08-21 | 管理面板全面升级，支持企微、QQ、量子密信三通道管理 |
| v1.9.0 | 2026-08-20 | 会话按用户/群固定（8088代理按标题复用会话） |
| v1.8.0 | 2026-08-17 | 待办能力完全脱离 wecom-cli，7个待办函数全部走 HTTP MCP |
| v1.7.0 | 2026-08-17 | 企微侧4类事件处理+Markdown回复+5种模板卡片+文档MCP能力 |
| v1.6.0 | 2026-08-17 | QQ机器人6大新能力：Markdown/视频/语音(TTS)/主动@/关键词指令/事件回调 |
| v1.5.0 | 2026-08-16 | 管理面板双通道：QQ状态卡片+消息来源列+QQ主动推送 |
| v1.4.0 | 2026-08-16 | QQ官方机器人接入+TeleAgent双向桥 |
| v1.3.0 | 2026-08-16 | 面板图片推送+自动压缩+Tab三菜单布局 |
| v1.2.0 | 2026-08-16 | AI配餐后处理：台账表格+跟进待办+企微文档 |
| v1.1.0 | 2026-08-16 | 新增主动推送群聊消息 + Web管理面板（8505） |
| v1.0.0 | 2026-08-09 | 首个正式版，支持群聊收图+AI配餐+文件发送全流程 |

- 当前版本号见 [VERSION](VERSION) 文件
- 完整更新日志见 [CHANGELOG.md](CHANGELOG.md)
- 历史版本可通过 `git checkout v1.0.0` 回退

## Author

李准的星小辰

> AI生成