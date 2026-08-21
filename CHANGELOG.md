---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '8c3b963f-e3d7-47bb-a7f3-4810e66ac6da'
  PropagateID: '8c3b963f-e3d7-47bb-a7f3-4810e66ac6da'
  ReservedCode1: 'a368cf41-2629-4699-9c70-b28fbbf9c994'
  ReservedCode2: 'a368cf41-2629-4699-9c70-b28fbbf9c994'
---

# 更新日志

版本号规则：`主版本.次版本.修订号`（语义化版本）
- 主版本：架构级重构或不兼容改动
- 次版本：新增功能
- 修订号：Bug修复

---

## v1.11.1 (2026-08-21)

**修复：量子密信图片推送错误处理优化**。

### 修复
- 修复量子密信图片推送失败时错误信息不明确的问题
- 添加详细的错误诊断信息，帮助用户理解问题原因
- 更新面板错误提示，提供更友好的用户指引

### 变更文件
- `zmx_adapter.py`（添加详细的错误处理和日志）
- `dashboard.py`（优化错误提示信息）

### 测试
- 量子密信图片推送测试：错误信息清晰，用户可理解问题原因
- 文本推送测试：功能正常

---

## v1.11.0 (2026-08-21)

**面板全面升级：支持企微、QQ、量子密信三通道管理**。

### 新增功能
- **管理面板三通道支持**（dashboard.py）：
  - **主动推送**：下拉菜单新增「量子密信群聊 (Webhook)」选项，支持向量子密信群聊推送文本和图片
  - **消息记录**：合并量子密信消息记录（从 `zmx_messages.json` 读取），表格中显示紫色"密信"来源标签
  - **实时日志**：读取量子密信适配器日志（带 `[ZMX]` 前缀），合并到日志流中
  - **能力说明**：新增量子密信专属卡片，说明群聊@、主动推送、会话隔离、用户名映射、公网入口等功能
  - **页面更新**：标题、头部和 footer 更新为"企微+QQ+量子密信机器人管理面板"，通用能力描述改为"三通道共用"
- **量子密信适配器推送接口**（zmx_adapter.py）：
  - 新增 `_handle_push` 方法，处理面板推送请求（支持文本和图片）
  - 支持通过 HTTP POST `/push` 端点接收面板推送指令
- **会话标题修复**（zmx_adapter.py）：
  - 会话标题显示名从 `group_id or phone` 改为 `user_name`（通过 `ZMX_USER_MAP` 映射）
  - 确保量子密信会话显示可读用户名而非纯ID

### 修复
- 修复 `dashboard.py` 中 f-string 反斜杠语法错误（Python 3.11 不支持）
- 修复量子密信会话标题显示问题（显示用户名而非 group_id）

### 变更文件
- `dashboard.py`（面板前端与后端逻辑全面升级，支持三通道）
- `zmx_adapter.py`（新增推送接口，修复会话标题显示）
- `server.py`（新增 `_forward_zmx_push` 方法）

### 测试
- 三通道主动推送测试：企微、QQ、量子密信推送功能正常
- 消息记录合并测试：三个通道的消息记录正确合并显示
- 实时日志测试：三个通道的日志正确合并显示
- 会话标题测试：量子密信会话显示用户名而非纯ID
- 服务重启测试：wecom-bot 服务重启后功能正常

### 待办
- [ ] 更新 wecom-bot 技能文档（SKILL.md）以反映三通道支持
- [ ] 提交 GitHub 并打 tag（v1.11.0）
- [ ] 更新工作知识库索引

---

## v1.10.0 (2026-08-21)

**新增：量子密信（中国电信）机器人通道接入 + 会话标题持久化优化**。

### 新增功能
- **量子密信适配器**（zmx_adapter.py，新文件）：
  - 将量子密信接入 wecom-bot 统一 AI 管线，作为继企微、QQ 之后的第三个通讯通道
  - 复用 server.py 的 `call_teleagent` / `build_prompt` / `extract_file_paths` / `get_session_title`
  - 出站发送：`zmx_send_text`（自动分片 5000 字）、`zmx_send_markdown`（带 title 卡片，行首语法标记+行内符号清洗，24 字符截断）、`zmx_upload_and_send`（附件+图片，30MB 限制）
  - 入站回调：HTTP 服务（端口 1011，路径 /webhook），校验必填字段（type/textMsg/phone/groupId/callBackUrl）
  - 群隔离：回调携带的 `callBackUrl` 作为群回复地址，多群不串
  - 出站限流：每 callback key 60s/20 条滑动窗口
  - 协议移植自 mixin-chatbot（已验证的量子密信 webhook 协议）
  - **注意**：量子密信是回调模式（平台主动 POST 到我们的公网地址），与企微/QQ 的 WebSocket 长连接不同，入站需公网入口（Cloudflare 隧道/内网穿透）
- **配置项**（config.py / config_example.py）：新增 `ZMX_ENABLED` / `ZMX_CALLBACK_URL` / `ZMX_LISTEN_PORT` / `ZMX_LISTEN_HOST` / `ZMX_WEBHOOK_SECRET`

### 优化
- **会话标题持久化**（server.py）：
  - 新增 `get_session_title(channel, scene, display_name, session_key)` 函数
  - 会话标题第四部分改为"首次建立时间"并持久化到 `session_time_cache.json`，同一会话始终复用同一时间戳（之前每次消息都变 → 8088 代理无法复用会话）
  - 企微侧（server.py）：私聊显示姓名、群聊显示群 ID，会话分别按 userid/chatid 复用
  - QQ 侧（qq_official_adapter.py）：私聊显示昵称、群聊显示群 ID，统一复用 `get_session_title()`

### 变更文件
- `zmx_adapter.py`（新增，量子密信适配器，366 行）
- `server.py`（新增 `get_session_title()` + 会话时间缓存 + `process_and_reply` 改用新函数）
- `qq_official_adapter.py`（`_handle_qq_message` 改用 `server.get_session_title()`）
- `config_example.py`（新增 ZMX 配置模板）
- `.gitignore`（新增 `session_time_cache.json` 运行时缓存）

### 测试
- markdown title 清洗逻辑通过 10 个单元用例（标题/列表/引用语法剥离、行内 `*_\``#` 符号清除、空行兜底、24 字符截断）
- 本地 mock 服务器端到端双向闭环测试通过（回调 ACK → AI 调用 → markdown 构造 → 出站发送）

### 待办
- [ ] 打通 Cloudflare 隧道（安装 cloudflared，映射本机 1011 端口）
- [ ] 量子密信平台创建会话机器人，获取 webhook key 填入 config.py
- [ ] 真实环境双向验证
- [ ] launchd 托管

---

## v1.9.3 (2026-08-19)

**调整：技能脱敏处理，准备上架技能广场**。

### 变更内容
- SKILL.md 脱敏：个人路径→`<your_project_dir>`、userid→`<userid>`、launchd服务名→`com.<your_name>`、人名→通用示例
- 重新通过 quick_validate 验证 + teleai_claw_scan 安全扫描（0 高风险）
- 重新注册到 super-agent，同步技能文件到项目目录备份

---

## v1.9.2 (2026-08-19)

**新增：制作 TeleAgent 技能（wecom-bot skill）**。

### 变更内容
- 按 skill-creator 规范创建 TeleAgent 技能 `wecom-bot`，注册到 super-agent
- SKILL.md 采用 Capabilities-Based 结构，覆盖 6 大能力：消息推送、文档/表格/待办创建、服务管理、会话管理、配置管理、8505面板
- references/api_reference.md 包含 push.py 和 wecom_api.py 全部函数签名速查表
- 技能目录：`~/.config/TeleAgent/skills/wecom-bot/`（SKILL.md + references/）

---

## v1.9.1 (2026-08-19)

**调整：面板「能力说明」新增会话分组说明**。

### 变更内容
- 8505 管理面板「能力说明 → 通用能力」新增「会话分组」条目：同一用户私聊 = 一个会话、同一群 @ = 一个会话、群与私聊互不干扰，对话上下文自动延续

---

## v1.9.0 (2026-08-19)

**重大调整：会话按用户/群固定，对话上下文可延续**。

### 背景
原会话标题带时间戳（如 `企微 | 群聊 | 李准 | 14:02`），每次消息都生成新标题 → TeleAgent 每次打开新会话，交互式对话被拆散到不同会话。

### 变更内容
- **企微侧**（server.py）：
  - `process_and_reply` 新增 `chat_id` 参数；6 个消息处理函数（文字/图片/文件/语音/视频/图文混排）从企微消息体读取 `chatid` 传入
  - 会话标题改为稳定标识：私聊 `企微|私聊|userid`、群聊 `企微|群聊|chatid`（同一用户/同一群固定一个会话，群与私聊互不干扰）
- **QQ 侧**（qq_official_adapter.py）：`_handle_qq_message` 会话标题改为：私聊 `QQ|私聊|openid`、群聊 `QQ|群聊|group_openid`
- **8088 代理端**（openai-proxy v1.6.0）：`/v1/chat/completions` 支持按 `session_title` 复用已存在会话（同名同目录），不再无条件新建

### 效果
- 私聊同一个用户 → 固定一个会话，多轮对话上下文连续
- 群聊同一个群 → 固定一个会话（同群成员共享群聊上下文），与私聊完全隔离
- 一句话不再开一个新会话

---

## v1.8.4 (2026-08-18)

**调整：TeleAgent 会话标题去掉"机器人"三个字**。

### 变更内容
- 会话标题格式由 `企微机器人 | 群聊/私聊 | 用户 | 时间`、`QQ机器人 | 群聊/私聊 | 用户 | 时间` 缩短为 `企微 | 群聊/私聊 | 用户 | 时间`、`QQ | 群聊/私聊 | 用户 | 时间`（避免标题过长显示不全）

---

## v1.8.3 (2026-08-18)

**调整：TeleAgent 会话标题格式改为「机器人 | 群聊/私聊 | 用户 | 时间」**。

### 变更内容
- **企微侧**（server.py）：`process_and_reply` 新增 `chat_type` 参数（'group'/'single'），6 个消息处理函数（文字/图片/文件/语音/视频/图文混排）均把企微消息的 `chattype` 传入；会话标题由 `姓名 | 企微机器人 | 时间` 改为 `企微机器人 | 群聊/私聊 | 姓名 | 时间`
- **QQ 侧**（qq_official_adapter.py）：`_handle_qq_message` 会话标题由 `姓名 | QQ机器人 | 时间` 改为 `QQ机器人 | 群聊/私聊 | 姓名 | 时间`（复用现有 `is_group` 判断）

### 效果
TeleAgent 会话列表标题现在一眼能看出：哪个机器人、群里还是私聊、谁发的、几点发的。

---

## v1.8.2 (2026-08-18)

**新增：面板「能力说明」Tab，展示 QQ 与企微双通道能力全景**。

### 变更内容
- dashboard.py 新增「能力说明」Tab（纯静态 HTML，无需数据加载）：
  - **QQ 官方机器人卡片**：群聊 @ / 私聊 / 主动推送 / 配餐台账 / 指令，附群成员昵称权限提示（依赖 QQ 开放平台「获取群成员信息」接口，未开通显示截断 ID）
  - **企微机器人卡片**：群聊 / 个人 / 群发（Markdown 卡片等）/ 事件 / 待办 / 文档，附通讯录姓名查询权限提示（需企微后台企业可信 IP 白名单）
  - **通用能力**：AI 对话 / 场景技能 / 富媒体 / 消息记录 / 定时任务
- 新增能力说明相关 CSS（cap-grid / cap-card / cap-item / cap-note 等），与现有暗色主题一致

---

## v1.8.1 (2026-08-18)

**修复：QQ 面板消息记录显示 openid 而非昵称**。

### 背景
v1.8.0 新增的 QQ 昵称解析代码存在两个缺陷，导致同事 @ 机器人后面板仍显示截断的 openid（如 `E3AC7D1AD6...F07A347`）：
1. **事件循环死锁**：`_resolve_qq_nickname` 内部用 `run_coroutine_threadsafe` + `future.result(timeout=5)` 同步等待 API 结果，而它在事件循环线程内被调用（`on_group_at_message_create` → `_display_name` → `_record_message`），提交的协程要等事件循环空闲才能执行，但线程正阻塞等待结果 → 永远超时 → 走截断降级。
2. **二次解析**：调用方已把解析好的展示名传给 `_record_message`，但 `_record_message` 内部又对 `user` 调 `_display_name()`，把昵称当 openid 再次解析。

### 变更内容
- **拆分解析函数为三层**，按调用线程选择：
  - `_resolve_qq_nickname()`：快速同步版（手动映射 > 缓存 > 截断展示），任何线程可安全调用，永不阻塞
  - `_resolve_qq_nickname_async()` / `_display_name_async()`：异步完整版（含 API 查询），事件循环内 `await` 调用，不阻塞
  - `_display_name_sync()`：worker 线程版（含 API 查询，`run_coroutine_threadsafe` + 5s 超时等待）
- **修复二次解析**：`_record_message` 不再对 `user` 调 `_display_name`，直接存调用方传入的展示名
- **事件回调统一改造**：8 个 botpy 事件回调（进群/退群/好友增删/主动消息开关）和 2 个消息回调统一改用 `await _display_name_async`
- **`_handle_qq_message`**：worker 线程改用 `_display_name_sync`（可查 API 并回填缓存）
- **server.py**：企微侧 `get_user_name` 降级改为截断展示（`userid[:8] + "..." + userid[-6:]`），避免面板显示过长的原始 ID

### 验证
- 本地逻辑测试 7 项全部通过：异步解析、缓存回填、手动映射、worker 同步解析、记录不二次解析、未知降级截断、事件循环快速版
- 语法检查通过

---

## v1.8.0 (2026-08-17)

**重大改进：待办能力完全脱离 wecom-cli，统一走 MCP 服务**。

### 背景
v1.7.1 已让配餐台账和文档脱离 wecom-cli，但待办功能仍通过 wecom-cli 二进制调用。wecom-cli 的 MCP 配置加密保存在 `~/.config/wecom/mcp_config.enc`（AES-256-GCM），通过解密发现待办有独立的 MCP 服务地址（`/mcp/robot-todo`），使彻底脱离 wecom-cli 成为可能。

### 变更内容
- **新增 `_load_mcp_config()`**：自动解密 wecom-cli 的 `mcp_config.enc`（AES-256-GCM，密钥 `.encryption_key`），获取 doc/todo 两类业务的 MCP 服务 URL，作为 config.py 配置的兜底
- **新增 `_get_mcp_url(biz_type)`**：按业务类型获取 MCP URL（config 优先，解密配置兜底）
- **泛化 `_mcp_call()`**：新增 `service` 参数（"doc"/"todo"），支持调用不同 MCP 服务
- **重写 `create_todo()`**：改用 MCP `create_todo` 工具（HTTP 调用），彻底移除 `subprocess.run` + wecom-cli 调用
- **新增 6 个待办函数**：`get_todo_list`、`get_todo_detail`、`update_todo`、`delete_todo`、`change_todo_user_status`、`search_todo_userid`，全部走 HTTP MCP
- **更新 `config.py`**：新增 `WECOM_TODO_MCP_URL` 配置项（留空则自动解密 wecom-cli 配置）

### 待办 MCP 工具列表（7 个）
| 工具名 | 功能 | 参数 |
|--------|------|------|
| `create_todo` | 创建待办 | content, follower_list, end_time, remind_type_list |
| `get_todo_list` | 查询待办列表 | follower_id, todo_status, limit, cursor |
| `get_todo_detail` | 批量查询详情 | todo_id_list (最多20) |
| `update_todo` | 更新待办 | todo_id, content/end_time/remind_type_list |
| `delete_todo` | 删除待办 | todo_id |
| `change_todo_user_status` | 改参与人状态 | todo_id, follower_userid, todo_status |
| `search_todo_userid` | 搜索用户 | keyword |

### 测试结果
- create_todo ✅ 创建成功（返回 todo_id）
- get_todo_list ✅ 列表查询成功（含 next_cursor 分页）
- get_todo_detail ✅ 详情查询成功（含完整 follower/end_time/remind 等）
- 原有 server.py 调用 `create_todo(content, follower_userid)` 接口不变，完全兼容

---

## v1.7.1 (2026-08-17)

**修复：配餐台账/待办/文档全部脱离 wecom-cli 依赖，统一走 MCP**。

### 背景
launchd 托管环境下 `wecom-cli` 不在 PATH（实际安装在 TeleAgent node runtime），导致配餐后处理三步全部失败（写台账/建待办/生成文档均报 `No such file or directory: 'wecom-cli'`）。

### 变更内容
- **配餐台账（append_peican_record）**：改用 MCP 智能表格（`doc_create` 一步建表带表头 + `smartsheet_records_add` 写记录），不再走 wecom-cli `sheet_append_data`；旧缓存为普通表格时自动重建为智能表格（实测重建+追加成功）
- **企微文档（create_wecom_doc）**：改用 MCP `doc_create`（doc_type=doc + content 直接传 Markdown），不再走 wecom-cli
- **待办（create_todo）**：保留 wecom-cli（todo 无 MCP 工具），新增 `_find_wecom_cli()` 自动探测绝对路径（环境变量 `WECOM_CLI` → PATH → TeleAgent node runtime → /opt/homebrew/bin），解决 launchd PATH 受限问题
- `create_smart_sheet_with_headers` 新增 `field_types` 参数（支持指定字段类型，配餐台账全用 text 避免数字列小数问题）

### 实测结果
- `append_peican_record`：旧缓存普通表格 → 自动重建智能表格 → 追加记录成功
- `create_wecom_doc`：建文档 + 写入 Markdown 成功
- `create_todo`：路径探测成功，待办创建成功（todo_id 返回）

---

## v1.7.0 (2026-08-17)

**企微文档能力接入 + 4 类事件处理 + Markdown/模板卡片全套**。

### 新增功能
- **企微文档 MCP 能力**（wecom_api.py）：
  - `doc_create` 一步创建智能表格（传 `fields` + `sheet_title`），自动生成指定表头，无需"建默认表→改字段"弯路
  - `smartsheet_sheets_list` / `fields_list` / `fields_add` / `records_add` 全链路封装
  - `create_smart_sheet_with_headers()` 一键建"带表头智能表格"，`add_smart_sheet_records()` 写记录（自动做 `values` 格式归一）
  - `_mcp_call` 修正：补 `Accept: application/json, text/event-stream` 请求头（否则 HTTP 406）；工具名对齐实际暴露名（实测确认 62 个工具，`create_doc`→`doc_create` 等）
- **4 类事件处理**（server.py）：`enter_chat`（欢迎语）、`template_card_event`（5 秒内更新卡片）、`feedback_event`（点赞/点踩解析入库）、`disconnected_event`
- **Markdown 回复** `reply_markdown()` + 流式消息 `feedback_id` 参数（消息下方可点赞/点踩）
- **模板卡片 5 种类型**：文本通知 / 新闻通知 / 按钮交互 / 投票 / 多项选择，`reply_template_card` / `reply_welcome` / `update_template_card` / `send_push_message` 全套发送
- **内置测试指令**：`/md` `/card` `/btn` `/vote` `/multi` `/push` `/fd` `/table`

### 实测记录
- 企微单聊实测：Markdown / 文本卡片 / 按钮卡片 / 投票卡片 / 多项选择 / 主动推送 / 反馈事件（点赞触发 `type=1`）/ 智能表格建表+写记录全部通过
- 智能表格 MCP 实测：`doc_create` 一步建表（含表头）→ `records_add` 写入 → `records_list` 读回，全链路成功

---

## v1.6.0 (2026-08-17)

**QQ 机器人 6 大新能力**：Markdown 消息、视频消息、语音消息（本地 TTS）、主动@成员、关键词指令系统、富文本消息、管理事件回调（机器人进群/退群、好友增删、权限变更）。

### 新增功能
- **Markdown 消息**（msg_type=2）：AI 回复自动检测 Markdown 特征（换行/加粗/标题）并排版发送，失败自动降级纯文本；`/push` 支持 `msg_type=2` 主动推送 Markdown
- **视频消息**（msg_type=7 + file_type=2）：mp4 ≤30MB 软限制，超限自动降级为文件类型（file_type=4）；面板新增「视频」格式（≤200MB 硬限制）
- **语音消息**（msg_type=7 + file_type=3）：本地 TTS 合成（macOS say + ffmpeg 转 mp3），支持中文音色；面板新增「语音 (TTS)」格式，直接输入文本即可合成发送
- **主动@成员**：面板文本/Markdown 新增 @用户输入框（QQ 群聊），文本内 `@用户` 占位自动替换为 `<@openid>` 富文本语法
- **关键词指令系统**：`/配餐` `/质检` `/日报` `/话术` `/帮助`（含短别名 `/pc` `/zj` `/rb` `/hs` `/bz`，兼容去掉斜杠写法），触发后直接回复预设文案、不走 AI
- **富文本消息**：文本内嵌 @ 语法替换（`_rich_text_at`），群聊有效
- **管理事件回调**（public_messages intent 内 8 种）：`on_group_add_robot`（进群欢迎语）、`on_group_del_robot`（清理会话与被动回复记录）、`on_friend_add/del`、`on_group_msg_receive/reject`、`on_c2c_msg_receive/reject`
- **富媒体自动类型识别**：`_file_type_by_ext` 按扩展名推断（jpg→1 图片 / mp4→2 视频 / mp3·wav·ogg→3 语音 / 其他→4 文件），文件被动回复自动识别类型
- **dashboard.py**：消息格式新增「视频」「语音 (TTS)」，新增 @用户输入框；后端 `/api/push` 支持 videoData/videoName/voiceText/at 字段并转发至适配器

### 修复
- `_tts_say`：`finally` 中变量名错误（`aiff` 未定义）导致 TTS 合成异常 → 改为 `aiff_path`
- `/push` 端点：`qq_push_to_group/user` 返回布尔时 `detail` 变量未初始化 → 补充默认错误信息
- `_match_command` / `_handle_command`：引用未定义变量 `QQ_CMD` → 改为 `QQ_COMMANDS`
- `_rich_text_at`：@ 占位替换逻辑修正——替换为 `<@openid>` 富文本语法（原实现错误地替换为 `<@名字>`）

### 实测记录（2026-08-17）
- 单聊文本/Markdown/图片/视频/语音(TTS)/mp3 文件推送全部成功
- 面板全链路（8505 → 18506）文本/Markdown/视频/语音/@ 均成功
- 指令匹配 8 组用例全部通过（含别名与去斜杠）
- 富文本 @ 替换、文件类型推断、TTS 合成（mp3 61KB）均验证通过
- 群聊被动回复（需群内 @ 机器人后 5 分钟内下发）待真实群实测

### 备注
- 事件回调仅支持官方 API 提供的 8 种管理事件；群成员进出、群创建/解散、消息撤回官方不支持（需频道 Intent，当前机器人是群场景）
- 语音软限制 20MB（mp3），视频软限制 30MB（mp4），超限自动降级为文件类型；文件硬限制 200MB

---

**面板→QQ群聊下发打通（被动回复通道）**：腾讯已下线 QQ 机器人群聊主动消息推送（40034105），本次实现面板向 QQ 群下发文本/图片/文件均走**被动回复通道**——自动复用群内最近一次 @ 机器人的 msg_id，无需手动复制 msg_id。

### 新增功能
- **qq_official_adapter.py**：
  - 新增 `QQ_LAST_GROUP_MSG` 记录每个群最近一次 @ 机器人的 msg_id + 时间戳（`_remember_group_msg`），并随 `qq_status.json` 持久化（重启不丢失，5 分钟内有效）
  - `/push` 端点群聊分支（target=group）改为：查最近有效 @ → 构造被动回复（带 msg_id + 递增 msg_seq）→ 文件走 `_reply_file_passive`（base64 → 临时落盘 → 上传 → 富媒体消息）、图片走新增 `qq_push_image_passive()`、文本走 `_reply_text()`；无有效 @ 时返回明确提示「该群最近5分钟内没有 @ 机器人，请先在群内 @ 机器人一下」
  - `_reply_text()` 重构：不再依赖 `message.reply()`（伪造消息对象无 `_api` 属性会崩），改直接构造 Route 调 `client.api._http.request`，群聊带 `msg_id`+`msg_seq`、单聊带 `msg_id`
- **dashboard.py**：前端选择「QQ群」时显示黄色提示条「QQ 群聊已不支持主动推送，需群内最近 5 分钟内有 @ 机器人才能下发」

### 实测记录
- 面板链路 → `127.0.0.1:18506/push`（target=group, file）→ 被动回复文件成功：`测试群发文件.txt` 已发到测试群（日志确认 `[QQ] 被动回复文件成功`）
- 无有效 @ 时正确返回「该群最近5分钟内没有 @ 机器人，请先在群内 @ 机器人一下，再重新发送」

### 备注
- 被动回复有效期 5 分钟（官方限制），超过需群内重新 @ 机器人
- 群聊文本被动回复也走 `msg_id` 通道（`msg_type=0` + `msg_seq` 递增），不再调用已被官方下线的主动推送接口

---

## v1.6.2 (2026-08-17)

**修复：面板消息记录状态一直停留「处理中」**。

QQ 消息记录落盘后，回复流程中缺少状态回写机制，导致面板里所有消息永远显示「处理中」。本次修复：

- `_record_message` 新增 `msg_id` 字段（消息唯一 id，用于回写定位）
- 新增 `_mark_message_status(msg_id, status)`：按 msg_id 匹配更新落盘记录状态（`处理中` → `已回复`/`失败`）
- 群聊/单聊消息回调写入记录时携带 `msg_id`
- `_handle_qq_message` 各回复出口统一回写状态：
  - 关键词指令回复完成 → `已回复`
  - AI 回复成功（含超时兜底回复）→ `已回复`
  - 处理异常 → `失败`
- 事件类记录（`status=事件`）不参与状态流转，保持原样

### 备注
- 历史记录（本次修复前写入、无 `msg_id`）无法回写，仍显示「处理中」，不影响新消息

---

## v1.6.1 (2026-08-17)

**事件回调落地到面板**：8 个管理事件（机器人进群/退群、好友增删、群聊/单聊主动消息权限开关）现在会实时记录到面板「消息记录」，以「事件」标签（橙色）展示。

### 变更内容
- **qq_official_adapter.py**：8 个事件回调（`on_group_add`/`on_group_del`/`on_friend_add`/`on_friend_del`/`on_group_msg_receive`/`on_group_msg_reject`/`on_c2c_msg_receive`/`on_c2c_msg_reject`）全部接入 `_record_message("event", ...)`，status 为「事件」，scene 区分 group/single
- 新增 `_short_openid()`：openid 过长时截取首尾展示（如 `38F8A2AB...6DA278`），避免表格撑爆
- **dashboard.py**：新增 `.tag-event` 橙色样式，`tagClass` 增加 `event`/`system` 映射

### 备注
- 事件记录与消息记录同存 `qq_messages.json`，面板自动合并展示，无需额外 IPC

---

## v1.5.9 (2026-08-16)

**QQ 文件推送支持 >5MB 大文件（官方分片上传，上限 200MB）**：此前仅支持 ≤5MB base64 直传；本次实现官方分片上传全流程，实测 8MB（1 片）与 15MB（2 片）私聊推送成功。

### 新增功能
- **qq_official_adapter.py**：新增 `_qq_chunked_upload()` 分片上传全流程——`upload_prepare`（取 upload_id/block_size/分片预签名 URL）→ 逐片 HTTP PUT → `upload_part_finish` 上报分片 → `files` 合并成最终文件；`qq_push_file()` 自动分流：≤5MB base64 直传（保留 file_name），>5MB 走分片上传，>200MB 拒绝
- **dashboard.py**：前端文件大小限制从 5MB 放宽到 200MB（>5MB 提示「将走分片上传」）；`_forward_qq_push` 转发超时从 20s 放宽到 600s（大文件上传耗时）

### 实测记录
- 8MB 文件 `大文件测试.bin`：1 片（8388608B）上传成功
- 15MB 文件 `多分片测试.bin`：2 片（10485760B + 5242880B）上传成功，日志确认「分片 1/2」「分片 2/2」「分片上传完成」「文件发送成功」

### 备注
- 官方分片大小由服务端下发（默认 5MB，实测 10MB）；预上传必填 `file_type/file_size/file_name/md5/sha1/md5_10m`（文件前 10002432 字节的 MD5）
- 群聊通道仍受官方「主动消息推送下线」限制（错误码 40034105），私聊（c2c）不受影响

## v1.5.8 (2026-08-16)

**修复 QQ 文件推送文件名丢失**：QQ 端收到文件显示「未命名」，实测确认根因——官方 `/v2/users|groups/{openid}/files` 上传接口支持 `file_name` 字段（botpy SDK 未封装），此前上传 payload 未携带该字段，导致接收端无文件名。

### Bug 修复
- **qq_official_adapter.py**：`qq_push_file()` 上传 payload 增加 `file_name` 字段（文件名从面板 → 18506 内部端点 → 适配器全程透传），文件名正常显示
- 已实测：私聊推送 `文件名修复测试.txt` 成功，日志确认 `文件发送成功 (文件名修复测试.txt)`

### 备注
- QQ 群聊发送（含 @ 被动回复与文件推送）被官方限制：错误码 `40034105 主动消息失败,无权限`（2025 年 4 月腾讯下线 QQ 机器人主动消息推送能力，群通道收敛）；私聊（c2c）通道不受影响

## v1.5.7 (2026-08-16)

**新增 QQ 主动发送文件能力**：管理面板可向 QQ 群/私聊主动推送文件（base64 → 官方 v2 文件接口 → 富媒体消息），与图片推送同路。

### 新增功能
- **QQ 适配器**（qq_official_adapter.py）：新增 `qq_push_file()`，`file_type=4`(文件) + `file_data`(base64) 直传官方 `/v2/groups|users/{openid}/files` 接口，然后发 `msg_type=7` 富媒体消息；支持 ≤5MB 直传（5MB-100MB 分片后续扩展）、data: 前缀剥离、文件名透传、caption 附带
- **内部端点**（18506）：`/push` 支持 `file` 字段路由到 `qq_push_file`，与图片/文本推送并列
- **面板前端**（dashboard.py）：消息格式下拉新增「文件」选项；新增文件上传区（选择+大小预览，>5MB 标红）；企微目标选择文件时提示仅支持 QQ；后端 `_handle_push` / `_forward_qq_push` 支持 `fileData`/`fileName` 透传
- 已验证完整链路：面板→18506→qq_push_file→官方API（无效 openid 正确返回错误，请求真实到达 `api.sgroup.qq.com/v2/users/{openid}/files`）

---

## v1.5.6 (2026-08-16)

**消息记录增加「场景」列**：面板「消息记录」表格新增「场景」列，每条消息可区分来源是群聊还是私聊。

### 新增功能
- **面板渲染**（dashboard.py）：`add_message_record()` 增加 `scene` 参数；表头增加「场景」列（第 4 列）；数据行按 `scene` 渲染「群聊」蓝色标签 /「私聊」紫色标签（`.tag-group` / `.tag-single`），未知显示 `-`
- **企微侧**（server.py）：六类消息处理（text/image/file/voice/video/mixed）均从请求体读取 `chattype` 并传入 `scene`，其中 file/voice/video 三处此前未读取 chattype，本次补全
- **QQ 侧**（qq_official_adapter.py）：`_record_message()` 增加 `scene` 参数；群 @ 回调传 `group`、单聊回调传 `single`
- **历史回填**（dashboard.py）：`_backfill_messages_from_log()` 解析日志中的 `chattype=` 字段回填场景，无该字段的旧记录显示 `-`
- 存量 `qq_messages.json` 7 条私聊测试记录已补 `scene: "single"`

---

## v1.5.5 (2026-08-16)

**修复 QQ 消息记录重启丢失**：QQ 适配器重启后内存列表被清空，新消息落盘时整体覆盖 `qq_messages.json`，导致历史记录只剩最新一条。

### 修复
- **启动时加载历史**（qq_official_adapter.py）：新增 `_load_qq_messages()`，启动时从 `qq_messages.json` 读回最多 100 条历史到内存，重启不再丢记录
- **落盘前防御合并**：`_record_message()` 写盘前先读取磁盘历史，与内存去重合并后再写回（按 `full_time+preview` 判重），杜绝任何路径下覆盖丢失
- 已从适配器日志找回 15:48「测试」记录并回填 `qq_messages.json`，面板恢复两条历史

---

## v1.5.4 (2026-08-16)

**消息记录增加日期列**：面板「消息记录」表格新增「日期」列（从 full_time 提取），跨天消息一眼可辨。

### 新增功能
- 消息记录表头与数据行增加「日期」列（dashboard.py），从 `full_time` 提取 `YYYY-MM-DD`，缺省显示 `-` 

---
## v1.5.3 (2026-08-16)

**面板 QQ 消息显示可读昵称**：QQ 发送者 openid 编码可通过 `QQ_USER_MAP` 手动映射为昵称，面板消息记录、会话下拉、会话标题、日志全部显示昵称。

### 新增功能
- **`QQ_USER_MAP` 手动映射**（config.py）：`{openid: "昵称"}`，沿用企微 `WECOM_USER_MAP` 的机制，`config_example.py` 同步示例
- **`_display_name()`**（qq_official_adapter.py）：openid → 昵称统一转换，映射不到保留原值（仍截断防超长）；消息记录、会话标题、日志均使用该函数
- **面板会话下拉显示昵称**（dashboard.py）：`/api/qqstatus` 附带 `user_map`，QQ 私聊/群聊「最近会话快捷选择」下拉显示「李准 (15:48)」而非 openid 前 12 位

### 修复
- **单聊不再误触发企微通讯录查询**：已映射的 QQ openid 直接用昵称，避免 openid 被当企微 userid 查姓名产生 60020 报错日志
- **历史消息兜底映射**：日志回填时同步套用 `QQ_USER_MAP`，旧记录里的 openid 也显示为昵称

### 说明
- 新增 QQ 用户时，在 `config.py` 的 `QQ_USER_MAP` 加一行 `"openid": "昵称"` 即可
- QQ 官方消息不含昵称字段，也无查询昵称的开放接口，只能手动映射（与企微通讯录自动查姓名不同）

---

## v1.5.2 (2026-08-16)

**面板消息记录接入 QQ 通道 + 修复排序混乱**：QQ 适配器收发的消息现在会出现在面板「消息记录」Tab，带绿色 `QQ` 来源标签；修复历史回填记录缺日期导致的时间排序错乱。

### 新增功能
- **QQ 消息落盘记录**（qq_official_adapter.py）：新增 `_record_message()`，群@/单聊收到消息时写入 `qq_messages.json`（含 `source: qq`、完整时间戳），供 dashboard 跨进程合并展示
- **面板合并 QQ 消息记录**（dashboard.py）：`get_message_records()` 读取 `qq_messages.json` 与企微内存记录合并，按时间倒序返回（前端已有 `source` 字段渲染 QQ 标签）

### 修复
- **修复消息记录时间排序错乱**：历史回填记录只存了 `HH:MM:SS` 丢失日期，导致与当天 QQ 消息比较时字符串排序颠倒（如昨天 23:26 排在今天 15:48 前）；回填时保留完整时间 `full_time`，排序统一按完整时间戳
- 修复 QQ 图片推送富媒体消息 `Route` 缺少 `"POST"` 方法参数导致私聊图片发送报错（`Route.__init__() missing ... 'path'`）

### 说明
- `qq_messages.json` 首次收到 QQ 消息时自动创建，与 `qq_status.json` 同目录
- 面板「消息记录」Tab 现在同时展示企微 + QQ 消息，QQ 记录带 `QQ` 绿色标签

---

## v1.5.1 (2026-08-16)

**管理面板支持 QQ 图片推送**：QQ 官方机器人主动推送由「仅文本」升级为「文本+图片」，图片经 base64 直传官方 API，无需公网 URL。

### 新增功能
- **`qq_push_image(kind, openid, image_b64, caption)`**（qq_official_adapter.py）：base64 → 官方 v2 `/files` 接口上传 media → `/messages` 发富媒体消息（msg_type=7），群/私聊均支持
  - 自动剥离 `data:image/xxx;base64,` 前缀、校验解码后 ≤5MB
  - botpy SDK 未封装 base64 上传，直接走 `client.api._http.request(route, json=payload)` 底层接口（不改 site-packages）
- **内部端点 18506 支持图片**：`/push` 请求体增加 `image`（base64）与 `caption` 字段，有 `image` 走图片推送，否则回退文本
- **dashboard 前端放开 QQ 图片选项**：QQ 目标不再锁定纯文本，可自由选择「图片」格式并上传文件（原 v1.5.0 强制锁定 text 并隐藏图片上传）

### 修复
- 修复 QQ 目标下格式被锁死、图片选项不可用的问题（v1.5.0 限制逻辑移除）

### 说明
- 图片大小上限 5MB（QQ 官方限制），超限会返回明确错误提示

---

## v1.5.0 (2026-08-16)

**管理面板接入 QQ 通道**：dashboard 升级为「企微+QQ」双通道管理，支持查看 QQ 状态、按来源查看消息记录、向 QQ 群/私聊主动推送。

### 新增功能
- **面板 QQ 状态卡片**（dashboard.py）：页面顶部新增「QQ通道」独立卡片区，实时显示连接状态、收到/回复消息数、最后消息时间、最近群会话/单聊会话数
- **消息记录来源列**：表格新增「来源」列，QQ 消息绿色 `QQ` 标签，企微消息 `企微` 标签
- **主动推送支持 QQ 目标**：推送目标新增「QQ群 (官方机器人)」「QQ私聊 (官方机器人)」
  - 自动加载最近 QQ 会话下拉（group_openid / user_openid 快捷选择）
  - QQ 官方机器人仅支持纯文本，切换目标自动锁定文本格式并隐藏图片上传
- **跨进程推送链路**：dashboard（8505）与 QQ 适配器独立进程，通过本机内部端点 `127.0.0.1:18506` 转发推送（`qq_official_adapter.py` 内置轻量 HTTP 服务，仅绑定 127.0.0.1）
- **实时日志合并**：日志页签同时展示企微日志与 `qq-adapter-app.log`（QQ 日志带 `[QQ]` 前缀）
- **`/api/qqstatus` 端点**：面板前端轮询读取 QQ 状态（跨进程读取 `qq_status.json`）

### Bug 修复
- 修复 QQ 适配器多实例进程并存问题（旧进程无内部端点，新代码需干净重启）
- `_get_push_config` 补充 `qq_enabled` 配置项

### 说明
- QQ 适配器运行方式不变：`venv/bin/python qq_official_adapter.py`（建议加入 launchd 开机自启）
- 内部推送端点 `QQ_PUSH_PORT=18506` 仅监听本机回环地址，不对外暴露

---

## v1.4.0 (2026-08-16)

新增 **QQ 官方机器人适配器**（qq_official_adapter.py），将 QQ 群聊（@触发）与单聊消息接入 wecom-bot 统一 AI 处理管线。

### 新增功能
- **QQ 官方机器人接入**（基于腾讯官方 qq-botpy SDK）：
  - `qq_official_adapter.py`：监听群@消息（`on_group_at_message_create`）与单聊消息（`on_c2c_message_create`）
  - 复用 `server.py` 现有管线（`call_teleagent` / `build_prompt` / `extract_file_paths` / `post_process_actions`），同一 8088 代理、同一套河南标准化技能
  - 图片/附件自动下载到本地并交给 AI 分析（复用 IMAGE_SAVE_DIR / FILE_SAVE_DIR）
  - 回复走 QQ 官方 API（`post_group_message` / `post_c2c_message`），超长自动截断
  - 与企微长连接互不干扰，独立进程运行：`python qq_official_adapter.py`
- **配置项**（config.py）：新增 `QQ_ENABLED` / `QQ_APPID` / `QQ_SECRET` / `QQ_USER_MAP`，默认关闭，申请审核通过后填入即可启用
- **运行状态**：`QQ_STATUS` 可被 dashboard 或外部查询
- **TeleAgent 双向桥（主动推送）**：
  - `qq_push_to_group(group_openid, text)` / `qq_push_to_user(openid, text)` / `qq_push_reply("group:xxx"|"user:xxx", text)`
  - 自动记录最近活跃会话（QQ_SESSION），支持 TeleAgent 侧主动向 QQ 回消息/提问
  - 与企微 push.py 对称，可被外部脚本/定时任务复用

### Bug 修复
- **on_ready 疑似不触发问题**：实为 botpy 在 import 时执行 `logging.basicConfig()`（root 默认 WARNING）导致 `server.logger` 的 INFO 日志被过滤，造成"回调未执行"假象
  - 适配器内显式 `logger.setLevel(INFO)` + 独立 StreamHandler/FileHandler（qq-adapter-app.log），日志不再被吞
  - 新增 **READY 事件探针**（包装 `client.ws_dispatch`）：在事件分发层确认连接状态，比 on_ready 更可靠，双重保障
  - 新增 30 秒连接看门狗：未收到 READY 自动记录错误状态
- **清理重复的 `_reply_text` 定义**（原来定义两次，后者覆盖前者，存在隐患）
- 新增 `on_error` 兜底：事件回调异常不再静默吞掉

### 说明
- QQ 官方机器人需先在 q.qq.com 开放平台申请（AppID/AppSecret），审核通过后才能使用
- QQ 官方当前不支持回复内直接下发文件，AI 生成的文件会提示到企微端查看

### 依赖
- 新增 qq-botpy（腾讯官方 QQ 机器人 SDK）：`venv/bin/pip install qq-botpy`

---

## v1.3.0 (2026-08-16)

新增 Web 面板**主动推送图片**能力（群聊 + 个人 1v1），并重构面板为 **Tab 三菜单布局**。

### 新增功能
- **面板支持图片推送**（dashboard.py）：消息格式新增「图片」选项，支持本地选图、base64 上传、前端预览
  - 推送目标支持群聊（Webhook）和个人（应用消息，自动 upload_media 拿 media_id）
  - 临时文件自动清理（temp_uploads/）
- **图片自动压缩**（push.py）：企微群机器人限制图片 base64 后 ≤2MB（超出报 40009 invalid image size）
  - `_auto_compress_image()`：超限自动降 JPEG 质量（88→40）→ 不足再等比缩分辨率（0.9→0.3）
  - 群推送和个人推送两条链路均生效，压缩临时文件自动清理
- **面板 Tab 布局**：主动推送 / 消息记录 / 实时日志 三个菜单页签，**默认选中「主动推送」**
  - 状态卡片保持常驻顶部

### Bug 修复
- 修复 dashboard 后端清理方法名不一致（`_cleanup_temp_image` vs `_remove_temp_image`）导致的 AttributeError

### 依赖
- 新增 Pillow（图片压缩）：`venv/bin/pip install Pillow`

---

## v1.2.0 (2026-08-16)

新增 AI 配餐后处理三件套：配餐结果自动写入企微在线表格台账、自动创建跟进待办、复杂方案自动生成企微文档。机器人从「只回复文字」升级为「自动操作企微文档/表格/待办」。

### 新增功能
- **wecom_api.py**（新模块）：封装企微文档/表格/待办 API，通过 wecom-cli 调用（绕过 >10 人企业 API 限制）
  - `append_peican_record()`：向配餐台账在线表格追加一行（自动建表+表头+缓存 docid）
  - `create_todo()`：创建企微待办（含截止时间、提醒方式）
  - `create_wecom_doc()`：创建企微文档并写入 Markdown 内容（edit_doc_content 优先，batch_update 降级）
- **server.py 集成 post_process_actions()**：AI 回复后自动执行后处理
  - `extract_peican_data()`：正则提取 AI 回复中的客户号码、套餐、金额等 7 个字段
  - 智能判断：非配餐内容自动跳过（至少 2 个字段匹配才算配餐）
  - 自动写台账 → 创建待办 → 生成文档 → 群里发通知（含表格链接、待办提示、文档链接）
  - 文档生成条件：AI 回复 >800 字或含多级标题
- **config.py 新增 `DEFAULT_TODO_USERID`**：待办默认创建人 userid
- **wecom-cli JSON-RPC 解包**：`_wecom_cli()` 统一封装，自动解包 wecom-cli 的 JSON-RPC 响应格式

### Bug 修复
- 修复 `create_wecom_doc()` 的 `UnboundLocalError`（edit_doc_content 成功时 requests_list 未定义）
- 修复 `append_peican_record()` 重建表格时 `_sheet_docid` 未重置的拼写错误

### 依赖
- 需安装 wecom-cli（`npm install -g @anthropic/wecom-cli` 或类似）并完成扫码配置
- wecom-cli 需有文档（doc）和待办（todo）权限

---

## v1.1.0 (2026-08-16)

新增主动推送群聊消息能力和 Web 管理面板，机器人从「单向接收回复」升级为「双向收发」。

### 新增功能
- **群机器人 Webhook 主动推送**（push.py）：支持向群聊推送文字、Markdown、图片、图文消息
  - 命令行入口：`python push.py group "内容"` / `python push.py group_md "## 标题"` / `python push.py group_img /path/to/image.jpg`
  - 应用消息 1v1 推送：`python push.py user <userid> "内容"`（需配置可信 IP，企业 >10 人受腾讯限制可能不可用）
- **Web 管理面板**（dashboard.py，端口 8505）：
  - 机器人运行状态监控（连接状态、最近消息、日志查看）
  - 主动推送消息区域（浏览器直接发消息，支持群聊/个人、文字/Markdown/图片）
  - REST API 接口 `POST /api/push`（target=group/user，msgtype=text/markdown/image）
- **配置模板**（config_example.py）：脱敏样例，不含真实凭证，方便他人部署

### 安全改进
- config.py 移出 git 跟踪，加入 .gitignore（防止 Bot Secret / Webhook Key / Corp Secret 泄露）
- 运行时日志文件（stdout/stderr）加入 .gitignore

### 使用说明
- 群机器人 Webhook 获取方式：企微群聊 → 右上角「...」→「消息推送」→「自定义消息推送」→ 复制 Webhook 地址
- 管理面板地址：http://127.0.0.1:8505

---

## v1.0.0 (2026-08-09)

企微Python机器人首个正式版本，支持群聊@机器人收图+AI配餐+文件发送全流程。

### 功能
- WebSocket长连接模式接收消息（wss://openws.work.weixin.qq.com），无需域名/备案/回调服务器
- 群聊@机器人发图 → mixed消息解析 → AES-256-CBC解密图片
- 图片经8088代理转发给TeleAgent（image_understanding看图+河南标准化配餐技能）
- stream格式回复企微群聊（企微长连接不支持text类型回复）
- 文件上传到群聊：init→chunk→finish三步同步流程，异步执行不阻塞消息接收
- 待发文件队列（pending_files.json）：跨进程持久化，机器人重启不丢失
- 企微通讯录API自动查询userid→姓名，本地缓存（name_cache.json）
- 支持文字/图片/文件/语音/视频/mixed全消息类型
- launchd常驻服务（KeepAlive=true，崩溃自动重启，开机自启）

### 技术要点
- 文件上传异步化：独立线程执行，_send_lock互斥锁保护ws.send()线程安全
- 上传响应无cmd字段，通过req_id匹配pending request唤醒等待线程
- 8088代理超时1800秒（30分钟），适配配餐等长任务
- 图片AES解密兼容企微非标准PKCS7 padding，unpad失败返回原始数据