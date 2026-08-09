---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: 'ae827575-8ae9-4cd3-9f1b-c827c3a9425d'
  PropagateID: 'ae827575-8ae9-4cd3-9f1b-c827c3a9425d'
  ReservedCode1: '350aa678-ee37-441a-9d6c-27cf2471df77'
  ReservedCode2: '350aa678-ee37-441a-9d6c-27cf2471df77'
---

# 更新日志

版本号规则：`主版本.次版本.修订号`（语义化版本）
- 主版本：架构级重构或不兼容改动
- 次版本：新增功能
- 修订号：Bug修复

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