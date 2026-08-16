---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '24890d9f-dfd5-4c57-8470-76ed5c5ef819'
  PropagateID: '24890d9f-dfd5-4c57-8470-76ed5c5ef819'
  ReservedCode1: '3b7d6d81-9bb4-41e8-9cdd-425bbc29ff9d'
  ReservedCode2: '3b7d6d81-9bb4-41e8-9cdd-425bbc29ff9d'
---

# 企微Python机器人（长连接模式）

![version](https://img.shields.io/badge/version-1.5.8-blue)

## 简介
企业微信群聊智能机器人，基于 WebSocket 长连接接收消息，无需域名/备案/回调服务器。
群聊 @机器人 发图 → 自动下载解密 → 调用 TeleAgent AI（看图+河南标准化配餐）→ 结果回复群里。

v1.1.0 新增**主动推送**能力：通过群机器人 Webhook，可从终端或 Web 面板主动向群聊推送文字/Markdown/图片消息。

v1.2.0 新增**AI 配餐后处理三件套**：配餐结果自动写入企微在线表格台账、自动创建跟进待办、复杂方案自动生成企微文档。

v1.3.0 新增**面板图片推送 + 自动压缩**：Web 面板可直接推送图片到群聊/个人，超 2MB 自动压缩；面板改为**主动推送 / 消息记录 / 实时日志**三个 Tab 菜单，默认打开即推送页。

## 双向能力
```
【接收】企微群聊 @机器人 → WebSocket长连接 → AI处理 → 自动回复
【接收】QQ群聊 @机器人 / 单聊 → qq-botpy WebSocket → AI处理 → 自动回复（v1.4.0）
【推送】终端/脚本/Web面板 → Webhook API → 主动发消息到群聊
【推送】TeleAgent → qq_push_* → 主动回消息到 QQ（v1.4.0 双向桥）
【管理】Web面板（8505）→ 企微/QQ 双通道状态 + 主动推送（v1.5.0 支持 QQ）
【后处理】AI配餐回复 → 自动写台账+建待办+生成文档 → 群里发通知
```

## QQ 官方机器人（v1.4.0）
```
独立进程运行（与企微主服务互不干扰）：
    python qq_official_adapter.py

接入：腾讯官方 qq-botpy SDK（WebSocket 长连接，无需公网 IP）
监听：群@消息（on_group_at_message_create）+ 单聊消息（on_c2c_message_create）
处理：复用 server.py 管线 → 同一 8088 代理 → 同一套河南标准化技能
回复：QQ 官方 API（post_group_message / post_c2c_message）

配置（config.py）：
    QQ_ENABLED = True
    QQ_APPID  = "开放平台审核通过后的 AppID"
    QQ_SECRET = "AppSecret"
    QQ_USER_MAP = { "openid": "昵称" }   # 可选：面板消息/会话显示昵称（v1.5.3）

先到 q.qq.com 申请官方机器人（需审核），拿到 AppID/Secret 后填入即可启用
QQ 官方消息不含昵称字段、也没有查询昵称的开放接口，
在 QQ_USER_MAP 里给 openid 配昵称，面板就显示「李准」而不是一长串编码（v1.5.3）
```

## 管理面板（v1.5.2 双通道）
```
Web 面板 http://127.0.0.1:8505 支持「企微通道 + QQ通道」双通道管理：
  · 状态卡片：企微（连接/心跳/重连/消息数）+ QQ（连接/收/回/最近会话）
  · 消息记录：区分来源（企微 / QQ 标签），QQ 消息落盘合并展示
  · 主动推送：企微群/个人 + QQ群/私聊（QQ 自动加载最近会话快捷选择，支持文本与图片）
  · 实时日志：企微日志 + QQ 适配器日志（[QQ] 前缀）合并展示

跨进程说明：dashboard（8505）与 QQ 适配器为独立进程，
QQ 主动推送经本机内部端点 127.0.0.1:18506 转发（仅回环，不对外暴露）。
QQ 图片推送：面板选「图片」格式 → base64 直传官方 v2 /files 接口 → 富媒体消息（≤5MB）
QQ 消息记录：qq_official_adapter.py 落盘到 qq_messages.json，面板合并展示（QQ 绿色标签）

## QQ 官方机器人（v1.4.0）
```
独立进程运行（与企微主服务互不干扰）：
    python qq_official_adapter.py

接入：腾讯官方 qq-botpy SDK（WebSocket 长连接，无需公网 IP）
监听：群@消息（on_group_at_message_create）+ 单聊消息（on_c2c_message_create）
处理：复用 server.py 管线 → 同一 8088 代理 → 同一套河南标准化技能
回复：QQ 官方 API（post_group_message / post_c2c_message）

配置（config.py）：
    QQ_ENABLED = True
    QQ_APPID  = "开放平台审核通过后的 AppID"
    QQ_SECRET = "AppSecret"

先到 q.qq.com 申请官方机器人（需审核），拿到 AppID/Secret 后填入即可启用
```

## 架构
```
企微群聊 @机器人发图
    ↓ WebSocket 长连接（wss://openws.work.weixin.qq.com）
    ↓ 收到 mixed 消息（text + image）
    ↓ 下载图片 + AES-256-CBC 解密
    ↓ HTTP POST 调用本地代理（127.0.0.1:8088/v1/chat/completions）
    ↓ 代理转发给 TeleAgent super-agent（看图 + 加载河南标准化技能）
    ↓ 收到 AI 回复
    ↓ stream 格式回复企微群聊
    ↓ （如有文件）上传文件 → file 类型消息发送到群
    ↓ 【v1.2.0 后处理】提取配餐数据 → 写台账 + 建待办 + 生成文档 → 群里发通知
```

## 版本管理

本项目使用语义化版本号（`主版本.次版本.修订号`），通过 git tag 标记每个版本：

| 版本 | 日期 | 说明 |
|------|------|------|
| v1.5.8 | 2026-08-16 | 修复QQ文件推送文件名丢失（上传带 file_name，不再显示"未命名"） |
| v1.5.7 | 2026-08-16 | 新增QQ主动发送文件（base64→官方v2文件接口→富媒体消息，≤5MB） |
| v1.5.6 | 2026-08-16 | 消息记录增加「场景」列（群聊/私聊，企微+QQ 双通道+历史回填） |
| v1.5.5 | 2026-08-16 | 修复QQ消息记录重启丢失（启动加载历史+落盘防御合并） |
| v1.5.4 | 2026-08-16 | 消息记录增加日期列（跨天消息一眼可辨） |
| v1.5.3 | 2026-08-16 | QQ消息显示昵称：QQ_USER_NAME_MAP映射+面板会话/记录昵称化 |
| v1.5.2 | 2026-08-16 | 面板消息记录合并QQ消息+修复时间排序+修复QQ图片Route参数 |
| v1.5.1 | 2026-08-16 | 面板QQ图片推送（base64直传官方接口，≤5MB） |
| v1.5.0 | 2026-08-16 | 管理面板双通道：QQ状态卡片+消息来源列+QQ主动推送+日志合并 |
| v1.4.0 | 2026-08-16 | QQ官方机器人接入+TeleAgent双向桥（主动推送）+READY探针修复 |
| v1.3.0 | 2026-08-16 | 面板图片推送+自动压缩（≤2MB）+Tab三菜单布局 |
| v1.2.0 | 2026-08-16 | AI配餐后处理：台账表格+跟进待办+企微文档 |
| v1.1.0 | 2026-08-16 | 新增主动推送群聊消息 + Web管理面板（8505） |
| v1.0.0 | 2026-08-09 | 首个正式版，支持群聊收图+AI配餐+文件发送全流程 |

- 当前版本号见 [VERSION](VERSION) 文件
- 完整更新日志见 [CHANGELOG.md](CHANGELOG.md)
- 历史版本可通过 `git checkout v1.0.0` 回退

## 文件说明
| 文件 | 说明 |
|------|------|
| `VERSION` | 当前版本号 |
| `CHANGELOG.md` | 更新日志 |
| `server.py` | 机器人主程序（WebSocket连接、消息处理、图片解密、代理调用、文件上传、配餐后处理） |
| `wecom_api.py` | 企微文档/表格/待办 API 封装（通过 wecom-cli 调用） |
| `push.py` | 主动推送模块（群聊Webhook推送：文字/Markdown/图片/图文，1v1应用消息推送，图片超2MB自动压缩） |
| `dashboard.py` | Web管理面板（端口8505，企微+QQ双通道状态监控+主动推送+消息记录+实时日志，Tab菜单，v1.5.0 支持 QQ） |
| `qq_official_adapter.py` | QQ 官方机器人适配器（监听群@/单聊消息 + TeleAgent 双向桥主动推送 + 内部推送端点18506，v1.4.0 新增；v1.5.3 增加 openid→昵称显示映射） |
| `config.py` | 配置文件（**已加入.gitignore，不上传GitHub**） |
| `config_example.py` | 配置模板（脱敏样例，复制为config.py后填入真实凭证） |
| `peican_sheet_cache.json` | 配餐台账表格 docid/sheet_id 缓存 |
| `requirements.txt` | Python 依赖 |
| `venv/` | Python 虚拟环境 |

## 快速部署
1. 克隆仓库：`git clone https://github.com/lizhun781021/wecom-bot.git`
2. 复制配置：`cp config_example.py config.py`
3. 编辑 `config.py`，填入你的 Bot ID、Secret、Webhook 地址等
4. 安装依赖：`pip install -r requirements.txt`
5. 启动机器人：`python server.py`

## 关键配置（config.py）
> 配置模板见 `config_example.py`，复制为 `config.py` 后填入真实凭证。
```python
BOT_ID = "xxx"           # 企微智能机器人ID
BOT_SECRET = "xxx"       # 长连接密钥
WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"  # 群机器人Webhook
WEBHOOK_KEY = "xxx"      # Webhook的key部分
TELEAGENT_PROXY_URL = "http://127.0.0.1:8088/v1/chat/completions"
TELEAGENT_MODEL = "NewApi/chat-pro"
WECOM_USER_MAP = {       # 企微用户ID → 姓名映射（会话标题显示用）
    "wo-xxxxx": "李准",
}
DEFAULT_TODO_USERID = "your_userid"  # 待办默认创建人userid（企微通讯录中的userid）
```

## 群机器人 Webhook 获取方式
1. 企微管理后台 → 应用管理 → 消息推送 → 设置「谁可以创建消息推送」
2. 企微群聊 → 右上角「...」→「消息推送」→「自定义消息推送」→ 添加
3. 创建后复制 Webhook 地址，填入 `config.py` 的 `WEBHOOK_URL` 和 `WEBHOOK_KEY`

## 技术要点
- **企微长连接不支持 text 类型回复**，必须用 `stream` 类型（`aibot_respond_msg` + `msgtype=stream`）
- **mixed 消息结构**：`body.mixed.msg_item[]`，每个 item 有 `msgtype`（text/image）
- **图片 AES 解密**：aeskey 是 Base64 编码（43字节需补齐padding），AES-256-CBC，IV 为 key 前16字节
- **文件上传**：三步同步流程（init→chunk→finish），异步线程执行不阻塞消息接收，_send_lock保护线程安全
- **超时设置**：看图+生成文档需较长时间，HTTP 超时设为 1800 秒（30分钟）

## launchd 服务
- 服务名：`com.lizhun.wecom-bot`
- plist 位置：`~/Library/LaunchAgents/com.lizhun.wecom-bot.plist`
- 配置：KeepAlive=true（崩溃自动重启）、RunAtLoad=true（开机自启）
- 运行目录：`~/.local/share/TeleAgent/TeleAgent的工作空间/wecom-bot/`

## 管理命令
```bash
# 启动/停止
launchctl load ~/Library/LaunchAgents/com.lizhun.wecom-bot.plist
launchctl unload ~/Library/LaunchAgents/com.lizhun.wecom-bot.plist

# 查看状态
launchctl list | grep wecom-bot

# 查看日志
tail -f ~/.local/share/TeleAgent/TeleAgent的工作空间/wecom-bot/wecom-bot.log
```

## 主动推送消息
```bash
# 群聊文字
python push.py group "消息内容"

# 群聊Markdown（支持标题/列表/加粗等）
python push.py group_md "## 标题
内容"

# 群聊图片
python push.py group_img /path/to/image.jpg

# 个人1v1（需配置可信IP，企业>10人可能受限）
python push.py user <userid> "消息内容"
```

或通过 Web 管理面板 http://127.0.0.1:8505 直接发送（支持文字/Markdown/图片，图片超2MB自动压缩）。

## 依赖服务
- **openai-proxy**（端口8088）：本地 OpenAI 兼容代理，将请求转发给 TeleAgent super-agent
- **TeleAgent**：AI 能力来源，含 image_understanding 看图工具和河南标准化赋能技能

## 使用方式
### 自动回复（被动接收）
1. 企微群聊中 @机器人
2. 发送客户截图 + 文字说明（如"帮我配餐"）
3. 机器人自动看图分析，回复结果到群里

### 主动推送
1. 确认 `config.py` 中 `WEBHOOK_URL` 已填写
2. 命令行 `python push.py group "消息"` 或通过 Web 面板发送
3. 消息将出现在群聊中（以机器人身份发送）

### AI 配餐后处理（自动执行）
1. 群聊 @机器人 发图（账单截图等）或文字描述客户情况
2. AI 回复配餐方案后，自动执行后处理：
   - 提取配餐数据（客户号码、套餐、金额等）写入企微在线表格台账
   - 创建跟进待办（默认给 `DEFAULT_TODO_USERID`）
   - 复杂方案（>800字或多级标题）自动生成企微文档
3. 群里收到通知消息（含台账链接、待办提示、文档链接）

> **前提**：需安装 wecom-cli 并完成扫码配置，确保有文档（doc）和待办（todo）权限