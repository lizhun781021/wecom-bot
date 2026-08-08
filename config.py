# 企业微信智能机器人长连接配置

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

# === 企微用户ID → 姓名映射（用于TeleAgent会话标题显示）===
# 新用户加入群聊后，在这里加上映射即可
WECOM_USER_MAP = {
    "wo-nRCBgAA1oVvwfR286z-ksQVxcnGKA": "李准",
    # 下面可继续添加其他群成员
}

# === 图片保存目录 ===
IMAGE_SAVE_DIR = "./images"
