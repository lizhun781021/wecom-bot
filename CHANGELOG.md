---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '38048970-d15c-42d8-9a39-8d4bd3b6b2a8'
  PropagateID: '38048970-d15c-42d8-9a39-8d4bd3b6b2a8'
  ReservedCode1: 'c6495138-9124-4b49-b390-9dc69af3ff2e'
  ReservedCode2: 'c6495138-9124-4b49-b390-9dc69af3ff2e'
---

# 更新日志

版本号规则：`主版本.次版本.修订号`（语义化版本）
- 主版本：架构级重构或不兼容改动
- 次版本：新增功能
- 修订号：Bug修复

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