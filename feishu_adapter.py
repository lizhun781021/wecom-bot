#!/usr/bin/env python3
"""飞书机器人适配器（长连接模式，lark-oapi WebSocket SDK）

通道：飞书（Feishu/Lark）→ lark-oapi WSClient（长连接）→ server.call_teleagent → 8088 代理 → TeleAgent AI
复用 server.py 的 call_teleagent / build_prompt / get_session_title / extract_file_paths

能力：
- AI 对话：单聊 + 群聊 @机器人
- 关键词指令：/配餐 /质检 /日报 /话术 /帮助（复用 QQ 的指令表模式）
- 消息推送：飞书消息 API（文本/富文本/图片/文件）
- 会话忙碌保护（v1.15.2 模式）：同一会话并发超过 1 个请求直接提示等待
- 权限确认：AI 需要确认时提醒用户，用户回复"确认/拒绝"自动投递
- 状态持久化：feishu_status.json / feishu_messages.json（供 8505 面板读取）

运行：独立进程，由 launchd 托管（com.<用户名>.feishu-adapter）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import threading
import time
import traceback

# 项目根目录（与 server.py / config.py 同目录）
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config  # noqa: E402
import server  # noqa: E402

logger = logging.getLogger("feishu")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_h)
try:
    _fh = logging.FileHandler(os.path.join(PROJECT_ROOT, "feishu-adapter-app.log"), encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_fh)
except Exception:
    pass

# ========== 运行状态 ==========
FEISHU_STATUS = {
    "running": False,
    "connected": False,
    "last_message_at": "",
    "last_error": "",
    "total_received": 0,
    "total_replied": 0,
}

# 最近活跃会话（用于主动推送/双向桥）：{"group": {chat_id: ts}, "user": {open_id: ts}}
FEISHU_SESSION = {"group": {}, "user": {}}
_FEISHU_CLIENT = None  # 运行时挂载的 lark Client（供主动推送）
_FEISHU_LOOP = None

# ========== 会话忙碌保护 ==========
_feishu_session_active = {}          # key -> 活跃请求数
_feishu_session_active_lock = threading.Lock()
_FEISHU_SESSION_MAX_CONCURRENCY = 1  # 同一会话同时最多 1 个请求

# ========== 权限确认待处理注册表 ==========
_feishu_pending_confirmations = {}
_feishu_pending_confirm_lock = threading.Lock()
_FEISHU_PENDING_CONFIRM_TTL = 1800  # 30分钟未回复自动清理

# ========== 富文本 / 指令 / 富媒体辅助 ==========
FEISHU_CMD_HELP = (
    "我是星小辰，可用指令：\n"
    "/配餐 帮我生成配餐方案\n"
    "/质检 对录音进行质检评分\n"
    "/日报 生成今日工作日报\n"
    "/话术 获取五步法营销话术\n"
    "/帮助 查看指令说明"
)

# 指令表（触发指令 → (回复内容, 是否走AI)）——复用 QQ 的指令体系
FEISHU_COMMANDS = {
    "/配餐": ("好的，我来为您生成配餐方案。请把用户的账单/套餐情况发给我，我来给出比算推荐。", False),
    "/质检": ("好的，我来对录音进行质检评分。请上传录音文件，我将按照质检标准评分并给出报告。", False),
    "/日报": ("好的，我来生成今日工作日报。请把今天的工作内容发给我，我来整理成结构化日报。", False),
    "/话术": ("好的，这是五步法营销话术：\n1. 外呼邀约\n2. 服务获信任\n3. 优化给方案\n4. 找坑给动力\n5. 比算促成交\n需要哪个场景的详细话术？", False),
    "/帮助": (FEISHU_CMD_HELP, False),
}

# 指令别名（短指令）
FEISHU_CMD_ALIASES = {
    "/pc": "/配餐",
    "/zj": "/质检",
    "/rb": "/日报",
    "/hs": "/话术",
    "/bz": "/帮助",
}

# 状态落地文件（dashboard 等跨进程读取用）
FEISHU_STATUS_FILE = os.path.join(PROJECT_ROOT, "feishu_status.json")
FEISHU_MESSAGES_FILE = os.path.join(PROJECT_ROOT, "feishu_messages.json")
FEISHU_NAME_CACHE_FILE = os.path.join(PROJECT_ROOT, "feishu_name_cache.json")
FEISHU_MESSAGES = []  # 内存缓存最近100条
FEISHU_MESSAGES_LOCK = threading.Lock()
_feishu_name_cache = {}  # {open_id: name} 内存缓存


# ========== 状态持久化 ==========
def _persist_feishu_status():
    try:
        with open(FEISHU_STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "status": FEISHU_STATUS,
                "session": FEISHU_SESSION,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[飞书] 状态落盘失败: {e}")


def _update_feishu_status(**kwargs):
    FEISHU_STATUS.update(kwargs)
    _persist_feishu_status()


def _load_feishu_messages():
    global FEISHU_MESSAGES
    try:
        if os.path.exists(FEISHU_MESSAGES_FILE):
            with open(FEISHU_MESSAGES_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, list):
                FEISHU_MESSAGES = loaded[:100]
                logger.info(f"[飞书] 启动加载历史消息 {len(FEISHU_MESSAGES)} 条")
    except Exception as e:
        logger.warning(f"[飞书] 加载历史消息失败（忽略）: {e}")


_load_feishu_messages()


def _persist_feishu_messages():
    try:
        with FEISHU_MESSAGES_LOCK:
            with open(FEISHU_MESSAGES_FILE, "w", encoding="utf-8") as f:
                json.dump(FEISHU_MESSAGES[:100], f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[飞书] 消息落盘失败: {e}")


def _record_message(msg_type, user, preview, status="处理中", scene="single"):
    """记录消息到内存 + 落盘（供 8505 面板展示）"""
    entry = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "type": msg_type,
        "user": user,
        "preview": preview[:200],
        "status": status,
        "scene": scene,
        "channel": "feishu",
    }
    with FEISHU_MESSAGES_LOCK:
        FEISHU_MESSAGES.insert(0, entry)
        FEISHU_MESSAGES = FEISHU_MESSAGES[:100]
    _persist_feishu_messages()


def _mark_feishu_message_status(new_status: str, idx: int = 0):
    """更新最近一条消息的状态"""
    with FEISHU_MESSAGES_LOCK:
        if FEISHU_MESSAGES:
            FEISHU_MESSAGES[idx]["status"] = new_status
    _persist_feishu_messages()


def _load_feishu_name_cache():
    global _feishu_name_cache
    try:
        if os.path.exists(FEISHU_NAME_CACHE_FILE):
            with open(FEISHU_NAME_CACHE_FILE, "r", encoding="utf-8") as f:
                _feishu_name_cache = json.load(f)
    except Exception:
        pass


def _save_feishu_name_cache():
    try:
        with open(FEISHU_NAME_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_feishu_name_cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


_load_feishu_name_cache()


# ========== 会话工具 ==========
def _remember_feishu_session(kind: str, identifier: str):
    FEISHU_SESSION.setdefault(kind, {})[identifier] = time.time()


def _display_name_sync(open_id: str, max_len: int = 20) -> str:
    """根据 open_id 解析用户显示名（飞书开放平台 contact API，需 scope）"""
    cached = _feishu_name_cache.get(open_id)
    if cached:
        return cached
    try:
        if not _FEISHU_CLIENT:
            return open_id[:max_len]
        from lark_oapi.api.contact.v3 import GetUserRequest
        req = GetUserRequest.builder().user_id(open_id).user_id_type("open_id").build()
        resp = _FEISHU_CLIENT.contact.v3.user.get(req)
        if resp.success():
            name = (resp.data.user.name or "")[:max_len]
            if name:
                _feishu_name_cache[open_id] = name
                _save_feishu_name_cache()
                return name
    except Exception as e:
        logger.warning(f"[飞书] 获取用户信息失败 {open_id}: {e}")
    return open_id[:max_len]


# ========== 会话忙碌保护 ==========
def _feishu_acquire(key: str) -> bool:
    """尝试获取会话占用，返回 True=允许处理 / False=忙碌拒绝"""
    with _feishu_session_active_lock:
        _feishu_session_active[key] = _feishu_session_active.get(key, 0) + 1
        if _feishu_session_active[key] > _FEISHU_SESSION_MAX_CONCURRENCY:
            _feishu_session_active[key] -= 1
            return False
        return True


def _feishu_release(key: str):
    with _feishu_session_active_lock:
        _feishu_session_active[key] = max(0, _feishu_session_active.get(key, 1) - 1)


# ========== 权限确认 ==========
def _check_feishu_confirmation(content: str) -> bool:
    """检查用户消息是否为对"确认/拒绝"的回复"""
    if not content:
        return False
    c = content.strip().lower()
    if c in ("确认", "允许", "同意", "ok", "好的", "是", "yes", "y", "确认执行"):
        return True
    if c in ("拒绝", "取消", "不要", "no", "n", "不同意"):
        return True
    return False


def _prune_feishu_pending_confirmations():
    now = time.time()
    with _feishu_pending_confirm_lock:
        expired = [k for k, v in _feishu_pending_confirmations.items()
                   if now - v.get("time", 0) > _FEISHU_PENDING_CONFIRM_TTL]
        for k in expired:
            _feishu_pending_confirmations.pop(k, None)


# ========== 消息发送 ==========
def _send_text(open_id_or_chat_id: str, text: str, is_chat: bool = False) -> bool:
    """发送文本消息。is_chat=False 表示单聊（open_id），True 表示群聊（chat_id）"""
    try:
        if not _FEISHU_CLIENT:
            logger.error("[飞书] 客户端未就绪，无法发送消息")
            return False
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        body = CreateMessageRequestBody.builder() \
            .receive_id(open_id_or_chat_id) \
            .msg_type("text") \
            .content(json.dumps({"text": text}, ensure_ascii=False)) \
            .build()
        req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id" if is_chat else "open_id") \
            .request_body(body) \
            .build()
        resp = _FEISHU_CLIENT.im.v1.message.create(req)
        if resp.success():
            return True
        logger.error(f"[飞书] 发送文本失败: code={resp.code} msg={resp.msg}")
        return False
    except Exception as e:
        logger.error(f"[飞书] 发送文本异常: {e}")
        return False


def _send_markdown(open_id_or_chat_id: str, text: str, is_chat: bool = False) -> bool:
    """发送 Markdown 消息（interactive 卡片，支持标题/列表/粗体）"""
    try:
        if not _FEISHU_CLIENT:
            return False
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "智能助手"}, "template": "blue"},
            "elements": [{"tag": "markdown", "content": text}]
        }
        body = CreateMessageRequestBody.builder() \
            .receive_id(open_id_or_chat_id) \
            .msg_type("interactive") \
            .content(json.dumps(card, ensure_ascii=False)) \
            .build()
        req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id" if is_chat else "open_id") \
            .request_body(body) \
            .build()
        resp = _FEISHU_CLIENT.im.v1.message.create(req)
        if resp.success():
            return True
        logger.error(f"[飞书] 发送卡片失败: code={resp.code} msg={resp.msg}")
        return False
    except Exception as e:
        logger.error(f"[飞书] 发送卡片异常: {e}")
        return False


def _send_image(open_id_or_chat_id: str, image_path: str, is_chat: bool = False) -> bool:
    """上传并发送图片"""
    try:
        if not _FEISHU_CLIENT:
            return False
        if not os.path.exists(image_path):
            logger.error(f"[飞书] 图片不存在: {image_path}")
            return False
        from lark_oapi.im.v1 import CreateImageRequest, CreateImageRequestBody
        with open(image_path, "rb") as f:
            body = CreateImageRequestBody.builder().image_type("message").image(f).build()
            req = CreateImageRequest.builder().request_body(body).build()
            resp = _FEISHU_CLIENT.im.v1.image.create(req)
            if resp.code != 0:
                logger.error(f"[飞书] 上传图片失败: code={resp.code} msg={resp.msg}")
                return False
            image_key = resp.data.image_key
        from lark_oapi.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        body = CreateMessageRequestBody.builder() \
            .receive_id(open_id_or_chat_id) \
            .msg_type("image") \
            .content(json.dumps({"image_key": image_key})) \
            .build()
        req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id" if is_chat else "open_id") \
            .request_body(body) \
            .build()
        resp = _FEISHU_CLIENT.im.v1.message.create(req)
        return resp.success()
    except Exception as e:
        logger.error(f"[飞书] 发送图片异常: {e}")
        return False


def _send_file(open_id_or_chat_id: str, file_path: str, is_chat: bool = False) -> bool:
    """上传并发送文件"""
    try:
        if not _FEISHU_CLIENT:
            return False
        if not os.path.exists(file_path):
            logger.error(f"[飞书] 文件不存在: {file_path}")
            return False
        from lark_oapi.im.v1 import CreateFileRequest, CreateFileRequestBody
        with open(file_path, "rb") as f:
            body = CreateFileRequestBody.builder() \
                .file_type("stream") \
                .file_name(os.path.basename(file_path)) \
                .file(f) \
                .build()
            req = CreateFileRequest.builder().request_body(body).build()
            resp = _FEISHU_CLIENT.im.v1.file.create(req)
            if resp.code != 0:
                logger.error(f"[飞书] 上传文件失败: code={resp.code} msg={resp.msg}")
                return False
            file_key = resp.data.file_key
        from lark_oapi.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        body = CreateMessageRequestBody.builder() \
            .receive_id(open_id_or_chat_id) \
            .msg_type("file") \
            .content(json.dumps({"file_key": file_key})) \
            .build()
        req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id" if is_chat else "open_id") \
            .request_body(body) \
            .build()
        resp = _FEISHU_CLIENT.im.v1.message.create(req)
        return resp.success()
    except Exception as e:
        logger.error(f"[飞书] 发送文件异常: {e}")
        return False


def _send_reply(open_id_or_chat_id: str, text: str, is_chat: bool = False) -> bool:
    """发送最终回复（优先 Markdown 卡片，失败降级纯文本）"""
    if not text:
        return False
    if len(text) > 12 and ("\n" in text or "**" in text or text.startswith("#")):
        ok = _send_markdown(open_id_or_chat_id, text, is_chat)
        if ok:
            return True
    return _send_text(open_id_or_chat_id, text, is_chat)


# ========== 关键词指令 ==========
def _match_command(content: str):
    """匹配关键词指令。返回 (指令名, 剩余参数) 或 (None, None)"""
    if not content:
        return None, None
    content = content.strip().lower()
    for alias, real in FEISHU_CMD_ALIASES.items():
        if content == alias or content.startswith(alias + " "):
            content = real + content[len(alias):]
            break
    for cmd in FEISHU_COMMANDS:
        if content == cmd or content.startswith(cmd + " "):
            return cmd, content[len(cmd):].strip()
    # 兼容去掉斜杠的写法
    for cmd in FEISHU_COMMANDS:
        plain = cmd[1:]
        if content == plain or content.startswith(plain + " "):
            return cmd, content[len(plain):].strip()
    return None, None


def _handle_command(message_id: str, cmd: str, args: str, is_chat: bool, target_id: str):
    """处理指令：回复预设文案"""
    if cmd not in FEISHU_COMMANDS:
        return False
    reply, _ = FEISHU_COMMANDS[cmd]
    if args:
        reply = reply + f"\n（附加参数：{args}）"
    _send_text(target_id, reply, is_chat)
    return True


# ========== 消息处理核心 ==========
def _extract_text_and_files(result: str):
    """从 AI 回复中分离出 文字摘要 + 文件路径列表（复用 server 的逻辑）"""
    file_paths = server.extract_file_paths(result)
    if file_paths:
        text_reply = re.sub(r"FILE_PATH:.+?(?:\n|$)", "", result).strip()
        if not text_reply:
            text_reply = "处理完成，文档已生成。"
    else:
        text_reply = result
    return text_reply, file_paths


def _handle_feishu_message(message_event, is_chat: bool):
    """飞书消息统一入口：处理单聊/群聊 @机器人"""
    try:
        event = message_event.event
        message = event.message
        message_id = message.message_id or ""
        sender = event.sender
        open_id = ""
        if sender and sender.sender_id:
            open_id = sender.sender_id.open_id or ""
        sender_type = sender.sender_type if sender else ""
        chat_id = message.chat_id or ""
        chat_mode = message.chat_mode or ("group" if is_chat else "p2p")
        # 是否群聊：事件层的 chat_mode 优先，其次参数
        is_chat = (chat_mode == "group") or is_chat

        # 忽略机器人自身消息
        if sender_type == "app":
            return

        # 显示名
        user_name = _display_name_sync(open_id, max_len=20)
        _remember_feishu_session("group" if is_chat else "user", chat_id if is_chat else open_id)
        _update_feishu_status(last_message_at=time.strftime("%H:%M:%S"),
                              total_received=FEISHU_STATUS["total_received"] + 1)

        # 提取文本内容（支持 text / post）
        content_type = message.message_type or ""
        text_content = ""
        if content_type == "text":
            try:
                data = json.loads(message.content or "{}")
                text_content = data.get("text", "")
            except Exception:
                text_content = message.content or ""
        elif content_type == "post":
            try:
                data = json.loads(message.content or "{}")
                for line in data.get("content", []):
                    for seg in line:
                        if seg.get("tag") == "text":
                            text_content += seg.get("text", "")
            except Exception:
                text_content = message.content or ""
        elif content_type == "image":
            text_content = "收到一张图片（飞书通道图片分析待接入）"
        elif content_type == "file":
            text_content = "收到一个文件（飞书通道文件分析待接入）"

        if not text_content.strip():
            return

        _record_message(content_type, user_name, text_content[:200], status="处理中",
                        scene="group" if is_chat else "single")

        # 权限确认优先：用户回复"确认/拒绝"必须直接放行，不受忙碌保护限制
        if _check_feishu_confirmation(text_content):
            _handle_feishu_confirmation_reply(message, text_content, is_chat)
            return

        # 关键词指令
        cmd, args = _match_command(text_content)
        if cmd:
            _handle_command(message_id, cmd, args, is_chat, chat_id if is_chat else open_id)
            _mark_feishu_message_status("已回复")
            return

        # 会话忙碌保护
        busy_key = f"chat:{chat_id}" if is_chat else f"user:{open_id}"
        if not _feishu_acquire(busy_key):
            _send_text(chat_id if is_chat else open_id,
                       "⏳ 您上一条消息还在处理中（可能正在执行较复杂的任务），请稍等片刻再发消息。",
                       is_chat)
            _mark_feishu_message_status("已回复")
            return

        try:
            # 构建 prompt
            prompt = server.build_prompt([], text_content, user_name)
            session_title = server.get_session_title("飞书", "群聊" if is_chat else "私聊",
                                                     user_name, chat_id if is_chat else open_id)

            logger.info(f"[飞书] 调用TeleAgent: 来源={user_name}, 有文件=False")

            # 流式进度推送（飞书不支持消息更新，用定时新消息模拟）
            _feishu_stream_state = {"last_send": 0.0, "last_len": 0, "stop": False}

            def _feishu_on_delta(full_text):
                if _feishu_stream_state["stop"]:
                    return
                now = time.time()
                # 节流：距上次发送不足 8s 且增量不足 100 字则跳过
                if now - _feishu_stream_state["last_send"] < 8 and len(full_text) - _feishu_stream_state["last_len"] < 100:
                    return
                _feishu_stream_state["last_send"] = now
                _feishu_stream_state["last_len"] = len(full_text)
                try:
                    _send_text(chat_id if is_chat else open_id,
                               f"正在生成中…\n\n{full_text[-150:]}", is_chat)
                except Exception:
                    pass

            result = server.call_teleagent(prompt, timeout=1800, session_title=session_title,
                                           on_delta=_feishu_on_delta)
            _feishu_stream_state["stop"] = True
            _update_feishu_status(total_replied=FEISHU_STATUS["total_replied"] + 1)

            # 权限/问题确认
            if isinstance(result, dict) and result.get("confirmation"):
                conf = result["confirmation"]
                conf_id = conf.get("id", "")
                conf_type = conf.get("type", "permission")
                conf_desc = conf.get("description", "")
                partial = result.get("partial_text", "")
                notice = f"⚠️ 需要您确认操作\n\n{conf_desc}\n\n请回复「确认」允许执行，或「拒绝」取消。"
                if conf_type != "permission":
                    notice = f"❓ 需要您选择\n\n{conf_desc}\n\n请直接回复您的选择。"
                if partial:
                    notice = partial + "\n\n---\n" + notice
                _send_markdown(chat_id if is_chat else open_id, notice, is_chat)
                with _feishu_pending_confirm_lock:
                    _feishu_pending_confirmations[session_title] = {
                        "conf_id": conf_id,
                        "type": conf_type,
                        "message_id": message_id,
                        "open_id": open_id,
                        "chat_id": chat_id,
                        "is_chat": is_chat,
                        "session_title": session_title,
                        "time": time.time(),
                    }
                _mark_feishu_message_status("等待确认")
                return

            if not result:
                _send_text(chat_id if is_chat else open_id, "抱歉，处理超时或出错了。请稍后重试。", is_chat)
                _mark_feishu_message_status("已回复")
                return

            # 分离文本与文件
            text_reply, paths = _extract_text_and_files(result)
            # 发送回复
            if text_reply:
                _send_reply(chat_id if is_chat else open_id, text_reply, is_chat)
            for p in paths:
                if os.path.exists(p):
                    _send_file(chat_id if is_chat else open_id, p, is_chat)

            _mark_feishu_message_status("已回复")
        finally:
            _feishu_release(busy_key)

    except Exception as e:
        logger.error(f"[飞书] 处理消息异常: {e}")
        traceback.print_exc()
        _mark_feishu_message_status("失败")
        try:
            _send_text(chat_id if is_chat else open_id, f"处理出错: {e}", is_chat)
        except Exception:
            pass


def _handle_feishu_confirmation_reply(message_event, text_content, is_chat):
    """处理"确认/拒绝"回复：匹配待确认请求并投递"""
    try:
        # 从 pending 注册表里找匹配项
        for session_title, info in list(_feishu_pending_confirmations.items()):
            if info.get("is_chat") == is_chat:
                reply = "once" if text_content.strip().lower() in ("确认", "允许", "同意", "好的", "是", "yes", "y") else "reject"
                conf_id = info.get("conf_id", "")
                ok = server.reply_teleagent_confirmation(conf_id, reply=reply)
                target = info.get("chat_id", "") if is_chat else info.get("open_id", "")
                if ok:
                    _send_text(target, "已收到您的确认，AI 正在继续处理...", is_chat)
                else:
                    _send_text(target, "确认回复失败，请稍后重试", is_chat)
                with _feishu_pending_confirm_lock:
                    _feishu_pending_confirmations.pop(session_title, None)
                return
    except Exception as e:
        logger.error(f"[飞书] 确认回复处理异常: {e}")


# ========== 飞书 SDK 长连接 ==========
def start_feishu_bot():
    """启动飞书机器人（长连接模式）"""
    if not config.FEISHU_ENABLED:
        logger.warning("[飞书] FEISHU_ENABLED=False，跳过启动")
        return
    if not config.FEISHU_APP_ID or not config.FEISHU_APP_SECRET:
        logger.error("[飞书] 未配置 FEISHU_APP_ID / FEISHU_APP_SECRET，请在 config.py 填写")
        return

    import lark_oapi as lark
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

    global _FEISHU_CLIENT, _FEISHU_LOOP

    # 客户端构建
    client = lark.Client.builder() \
        .app_id(config.FEISHU_APP_ID) \
        .app_secret(config.FEISHU_APP_SECRET) \
        .log_level(lark.LogLevel.INFO) \
        .build()

    # 事件处理器：接收消息
    def _on_message(events):
        try:
            loop = asyncio.get_event_loop()
            loop.run_in_executor(None, lambda: _handle_feishu_message(events, False))
        except Exception as e:
            logger.error(f"[飞书] 事件处理异常: {e}")

    client.event.dispatcher.register(P2ImMessageReceiveV1, _on_message)

    _FEISHU_CLIENT = client
    _update_feishu_status(running=True, connected=True, last_error="")
    logger.info("[飞书] 机器人客户端已构建，开始长连接...")

    # 长连接启动（SDK WS 模式）
    client.ws.start()


# ========== 启动入口 ==========
if __name__ == "__main__":
    start_feishu_bot()