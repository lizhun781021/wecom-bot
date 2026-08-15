---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '1716aa0f-fb9d-45a3-8219-263646f82ccd'
  PropagateID: '1716aa0f-fb9d-45a3-8219-263646f82ccd'
  ReservedCode1: 'b9daf286-7b3f-41c7-9c28-a746ee7aa4e1'
  ReservedCode2: 'b9daf286-7b3f-41c7-9c28-a746ee7aa4e1'
---

# 更新日志

版本号规则：`主版本.次版本.修订号`（语义化版本）
- 主版本：架构级重构或不兼容改动
- 次版本：新增功能
- 修订号：Bug修复

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