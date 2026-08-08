# 企微Python机器人（长连接模式）

## 简介
企业微信群聊智能机器人，基于 WebSocket 长连接接收消息，无需域名/备案/回调服务器。
群聊 @机器人 发图 → 自动下载解密 → 调用 TeleAgent AI（看图+河南标准化配餐）→ 结果回复群里。

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
```

## 文件说明
| 文件 | 说明 |
|------|------|
| `server.py` | 机器人主程序（WebSocket连接、消息处理、图片解密、代理调用、文件上传） |
| `config.py` | 配置文件（机器人凭证、代理地址、用户ID→姓名映射、PushPlus） |
| `requirements.txt` | Python 依赖 |
| `venv/` | Python 虚拟环境 |
| `images/` | 接收的图片保存目录 |

## 关键配置（config.py）
```python
BOT_ID = "xxx"           # 企微智能机器人ID
BOT_SECRET = "xxx"       # 长连接密钥
TELEAGENT_PROXY_URL = "http://127.0.0.1:8088/v1/chat/completions"
TELEAGENT_MODEL = "NewApi/chat-pro"
WECOM_USER_MAP = {       # 企微用户ID → 姓名映射（会话标题显示用）
    "wo-xxxxx": "李准",
}
```

## 技术要点
- **企微长连接不支持 text 类型回复**，必须用 `stream` 类型（`aibot_respond_msg` + `msgtype=stream`）
- **mixed 消息结构**：`body.mixed.msg_item[]`，每个 item 有 `msgtype`（text/image）
- **图片 AES 解密**：aeskey 是 Base64 编码（43字节需补齐padding），AES-256-CBC，IV 为 key 前16字节
- **文件上传**：三步同步流程（init→chunk→finish），用 Event 机制等待 WebSocket 异步响应
- **超时设置**：看图+生成文档需较长时间，HTTP 超时设为 600 秒

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

## 依赖服务
- **openai-proxy**（端口8088）：本地 OpenAI 兼容代理，将请求转发给 TeleAgent super-agent
- **TeleAgent**：AI 能力来源，含 image_understanding 看图工具和河南标准化赋能技能

## 使用方式
1. 企微群聊中 @机器人
2. 发送客户截图 + 文字说明（如"帮我配餐"）
3. 机器人自动看图分析，回复结果到群里
