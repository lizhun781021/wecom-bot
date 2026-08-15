# 企业微信智能机器人长连接配置

import os

# === 长连接凭证（管理后台 → 智能机器人 → API模式 → 长连接）===
# BotID：智能机器人唯一标识
BOT_ID = "aibH-Ec1vk6qf-z0ZVjVLS5Ov-uOvnQXpXl"
# Secret：长连接专用密钥
BOT_SECRET = "oGU112WMVsFTciZsazO5TiYXDiTscwq4YW4WWnQaNXz"

# === 群聊Webhook（可选，用于转发图片到群聊）===
WEBHOOK_URL = ""
WEBHOOK_KEY = ""

# === PushPlus 通知 ===
PUSHPLUS_TOKEN = "d6dc0bf7c0d748f4a5fb43fb24078303"

# === TeleAgent 代理配置 ===
# 通过本地 OpenAI 兼容代理调用 TeleAgent AI 能力（含看图+河南标准化技能）
TELEAGENT_PROXY_URL = "http://127.0.0.1:8088/v1/chat/completions"
TELEAGENT_MODEL = "NewApi/chat-pro"

# === 企业微信通讯录API（自动查姓名）+ 应用消息推送 ===
CORP_ID = "wx5ec6562b2e1ea8de"
CORP_SECRET = "6L5KlRlEdgvudhKC2af9r675gSTtAS5cMhQ8EliQDr4"
# 应用ID（管理后台 → 应用管理 → 自建应用 → AgentId）
# 用于主动推送应用消息（1v1推送给指定同事）
AGENT_ID = 1000002  # TODO: 准哥在企微管理后台确认实际的AgentId

# === 群机器人 Webhook（主动推送群聊消息）===
# 在群聊中添加"群机器人" → 复制 Webhook 地址 → 填入下方
# 格式：https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxx
WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=3dc1266c-e228-401e-8c21-763cdead31e9"
WEBHOOK_KEY = "3dc1266c-e228-401e-8c21-763cdead31e9"

# === 企微用户ID → 姓名映射 ===
# 手动映射优先级最高（可覆盖自动查询结果）
# 新用户会通过通讯录API自动查询姓名并缓存到本地，无需手动加
WECOM_USER_MAP = {
    "wo-nRCBgAA1oVvwfR286z-ksQVxcnGKA": "李准",
}

# === 接收文件保存目录（按类型分目录存到项目目录下）===
_PROJECT_DIR = os.path.expanduser("~/Desktop/星小辰工作空间/henan-standardized-empowerment/上传文件")
IMAGE_SAVE_DIR = os.path.join(_PROJECT_DIR, "图片")
FILE_SAVE_DIR = os.path.join(_PROJECT_DIR, "文件")
VOICE_SAVE_DIR = os.path.join(_PROJECT_DIR, "语音")
VIDEO_SAVE_DIR = os.path.join(_PROJECT_DIR, "视频")
