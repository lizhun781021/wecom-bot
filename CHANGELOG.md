---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '52ac8d01-a97f-432b-a18c-a5347b07135b'
  PropagateID: '52ac8d01-a97f-432b-a18c-a5347b07135b'
  ReservedCode1: '9e32fffb-c5a4-443b-b5e8-3614c0aabda5'
  ReservedCode2: '9e32fffb-c5a4-443b-b5e8-3614c0aabda5'
---

# 更新日志

版本号规则：`主版本.次版本.修订号`（语义化版本）
- 主版本：架构级重构或不兼容改动
- 次版本：新增功能
- 修订号：Bug修复

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