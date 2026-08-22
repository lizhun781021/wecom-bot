# 企业微信智能机器人配置模板
# 使用方法：复制本文件为 config.py，填入你自己的凭证

import os

# === 长连接凭证（管理后台 → 智能机器人 → API模式 → 长连接）===
# BotID：智能机器人唯一标识
BOT_ID = "your_bot_id_here"
# Secret：长连接专用密钥
BOT_SECRET = "your_bot_secret_here"

# === 群机器人 Webhook（主动推送群聊消息）===
# 在群聊中添加"消息推送" → "自定义消息推送" → 复制 Webhook 地址
# 格式：https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxx
WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=your_key_here"
WEBHOOK_KEY = "your_key_here"

# === PushPlus 通知 ===
PUSHPLUS_TOKEN = "your_pushplus_token_here"

# === TeleAgent 代理配置 ===
# 通过本地 OpenAI 兼容代理调用 TeleAgent AI 能力
TELEAGENT_PROXY_URL = "http://127.0.0.1:8088/v1/chat/completions"
TELEAGENT_MODEL = "NewApi/chat-pro"

# === 企业微信通讯录API（自动查姓名）+ 应用消息推送 ===
CORP_ID = "your_corp_id_here"
CORP_SECRET = "your_corp_secret_here"
# 应用ID（管理后台 → 应用管理 → 自建应用 → AgentId）
AGENT_ID = 1000002

# === 待办功能配置 ===
# 默认待办创建人userid（企微通讯录中的userid，非机器人ID）
DEFAULT_TODO_USERID = "your_userid_here"

# === 企微用户ID → 姓名映射 ===
# 手动映射优先级最高（可覆盖自动查询结果）
# 新用户会通过通讯录API自动查询姓名并缓存到本地，无需手动加
WECOM_USER_MAP = {
    # "wo-xxxxxxxx": "张三",
}

# === 接收文件保存目录 ===
_PROJECT_DIR = os.path.expanduser("~/Desktop/星小辰工作空间/接收文件")
IMAGE_SAVE_DIR = os.path.join(_PROJECT_DIR, "图片")
FILE_SAVE_DIR = os.path.join(_PROJECT_DIR, "文件")
VOICE_SAVE_DIR = os.path.join(_PROJECT_DIR, "语音")
VIDEO_SAVE_DIR = os.path.join(_PROJECT_DIR, "视频")

# === QQ 官方机器人配置（v1.4.0）===
# 先到 q.qq.com 开放平台申请官方机器人（需审核），拿到 AppID/AppSecret 后填入
# QQ_ENABLED=True 后，独立进程运行：python qq_official_adapter.py
QQ_ENABLED = False
QQ_APPID = "your_qq_appid_here"
QQ_SECRET = "your_qq_appsecret_here"
# QQ 用户 openid → 名称映射（可选，面板消息记录/会话/日志显示用）
QQ_USER_MAP = {
    # "user_openid": "昵称",
    # 例：给发过消息的 openid 起个可读名字，面板就会显示"李准"而不是一长串编码
}

# === 量子密信（中国电信）机器人配置（v1.10.0）===
# 在量子密信群聊 → 群设置 → 机器人管理 → 添加机器人，复制 webhook URL 填到下方
# 格式：https://imtwo.zdxlz.com/im-external/v1/webhook/send?key=<KEY>
# 说明：量子密信是"回调模式"（平台把 @机器人 消息回调到公网地址），
#       与企微/QQ 的 WebSocket 长连接不同，入站需公网入口（Cloudflare 隧道/内网穿透）
ZMX_ENABLED = False  # 改为 True 启用量子密信机器人（独立进程 zmx_adapter.py）
ZMX_CALLBACK_URL = ""  # 量子密信出站发送 webhook URL（含 key）
# 入站回调监听端口/地址（需公网可访问，配合隧道把平台回调转发到本端口）
ZMX_LISTEN_PORT = 1011
ZMX_LISTEN_HOST = "0.0.0.0"
# 入站回调密钥（可选，若平台支持自定义请求头校验收紧；留空则不校验）
ZMX_WEBHOOK_SECRET = ""

# === DCOOS 平台模式配置（v1.0.0 接口文档新增能力）===
# 量子密信 DCOOS 平台提供更丰富的消息类型（音频/视频/卡片）、
# 加密验签回调、多群推送等能力，与 webhook 模式并存，通过 ZMX_MODE 切换。
#
# 模式切换：
#   "webhook" — 原有模式，URL 带 key 鉴权（text/markdown 可用，附件受限）
#   "dcoos"   — DCOOS 平台模式，Headers 三字段鉴权（全消息类型 + 加密回调）
ZMX_MODE = "webhook"  # 切到 "dcoos" 启用新能力

# --- DCOOS 环境地址 ---
# 测试环境
ZMX_DCOOS_TEST_SEND_URL = "https://jt-eop-test.dcoos.189.cn:19443/serviceAgent/rest/forcustomers/robots/message/send"
ZMX_DCOOS_TEST_UPLOAD_URL = "https://jt-eop-test.dcoos.189.cn:19443/serviceAgent/rest/im-external/v1/webhook/upload-attachment"
# 生产环境
ZMX_DCOOS_PROD_SEND_URL = "https://10.141.243.200:8443/serviceAgent/rest/forcustomers/robots/message/send"
ZMX_DCOOS_PROD_UPLOAD_URL = "https://10.141.243.200:8443/serviceAgent/rest/zdxlz/im-external/v1/webhook/upload-attachment"
# 环境选择："test" 或 "prod"
ZMX_DCOOS_ENV = "test"

# --- DCOOS 鉴权凭证 ---
# 在 DCOOS 开发者后台新建应用 → 获取 AppID / AppKey
# 在应用中新建应用机器人 → 获取 clientId
ZMX_DCOOS_APP_ID = ""
ZMX_DCOOS_APP_KEY = ""
ZMX_DCOOS_CLIENT_ID = ""

# --- DCOOS 回调加密配置 ---
# 在开发者平台事件订阅中获取：
#   encryptedKey      — 加密密钥（Base64），用于派生 AES 会话密钥
#   verificationToken — 校验 Token（Base64），用于 HMAC-SHA256 验签
ZMX_DCOOS_ENCRYPTED_KEY = ""
ZMX_DCOOS_VERIFY_TOKEN = ""

# --- DCOOS Dcoos Sku ApiId（订阅后获取，记录用，代码不直接使用）---
ZMX_DCOOS_TEST_API_ID = "1963507091651870808"      # 测试环境消息推送 Sku
ZMX_DCOOS_PROD_API_ID = "1964986933788733440"      # 生产环境消息推送 Sku
ZMX_DCOOS_TEST_UPLOAD_API_ID = "1944653606550421602"  # 测试环境文件上传 Sku
ZMX_DCOOS_PROD_UPLOAD_API_ID = "1912378263823056896"  # 生产环境文件上传 Sku
