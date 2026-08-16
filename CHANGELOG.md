---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: 'bb893743-0181-454c-b670-e6e9986fbd6c'
  PropagateID: 'bb893743-0181-454c-b670-e6e9986fbd6c'
  ReservedCode1: '98768f74-0809-446d-81e9-b17aaaa788df'
  ReservedCode2: '98768f74-0809-446d-81e9-b17aaaa788df'
---

# 更新日志

版本号规则：`主版本.次版本.修订号`（语义化版本）
- 主版本：架构级重构或不兼容改动
- 次版本：新增功能
- 修订号：Bug修复

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