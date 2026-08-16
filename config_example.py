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
