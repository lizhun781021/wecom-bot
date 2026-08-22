#!/usr/bin/env python3
"""
量子密信（中国电信）机器人适配器
================================
将量子密信接入 wecom-bot 统一 AI 管线，作为继企微、QQ 之后的第三个通道。

架构（与 qq_official_adapter.py 同款独立进程模式）：
  - 复用 server.py 的 call_teleagent / build_prompt / extract_file_paths / get_session_title
  - AI 大脑统一走本地 8088 代理（config.TELEAGENT_PROXY_URL）
  - 收发协议移植自 mixin-chatbot（已验证的量子密信 webhook 协议）：
      * 发送：POST im-external/v1/webhook/send?key=xxx（text / markdown / image / file）
      * 附件：POST im-api/v1/webhook/upload-attachment?key=xxx&type=1|2 -> fileId
  - 入站回调：量子密信平台把 @机器人 消息 POST 到本模块起的 HTTP 服务
    （需公网入口：Cloudflare 隧道 / 内网穿透 / 公网服务器反代）

运行方式（单独进程，常驻）：
    python3 zmx_adapter.py
    （默认监听 0.0.0.0:1011，入站回调路径 /webhook）
"""

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import hashlib
import hmac
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 复用 server.py 核心管线（不启动它的 WebSocket 主循环）
import server  # noqa: E402
import config

logger = server.logger

# ========== 配置（可在 config.py 里覆盖）==========
ZMX_ENABLED = getattr(config, "ZMX_ENABLED", True)
# 量子密信出站发送 webhook URL（机器人 key），格式：
#   https://imtwo.zdxlz.com/im-external/v1/webhook/send?key=<KEY>
ZMX_CALLBACK_URL = getattr(config, "ZMX_CALLBACK_URL", "")
# 入站回调监听端口（量子密信平台把群消息回调到这里，需公网可访问）
ZMX_LISTEN_PORT = getattr(config, "ZMX_LISTEN_PORT", 1011)
ZMX_LISTEN_HOST = getattr(config, "ZMX_LISTEN_HOST", "0.0.0.0")
# 入站回调密钥（可选，校验收紧用）
ZMX_WEBHOOK_SECRET = getattr(config, "ZMX_WEBHOOK_SECRET", "")
# 手机号 → 用户名映射（与企微 WECOM_USER_MAP / QQ_USER_MAP 同模式）
ZMX_USER_MAP = getattr(config, "ZMX_USER_MAP", {})
# 附件大小上限（30MB，与量子密信平台一致）
ZMX_MAX_ATTACHMENT = 30 * 1024 * 1024
# 单条文本长度上限
ZMX_TEXT_MAX = 5000
# 出站限流：每个机器人 key 60 秒窗口最多 20 条
ZMX_RATE_WINDOW = 60.0
ZMX_RATE_MAX = 20

# ========== DCOOS 平台模式配置 ==========
ZMX_MODE = getattr(config, "ZMX_MODE", "webhook")  # "webhook" 或 "dcoos"
ZMX_DCOOS_ENV = getattr(config, "ZMX_DCOOS_ENV", "test")  # "test" 或 "prod"
ZMX_DCOOS_APP_ID = getattr(config, "ZMX_DCOOS_APP_ID", "")
ZMX_DCOOS_APP_KEY = getattr(config, "ZMX_DCOOS_APP_KEY", "")
ZMX_DCOOS_CLIENT_ID = getattr(config, "ZMX_DCOOS_CLIENT_ID", "")
ZMX_DCOOS_ENCRYPTED_KEY = getattr(config, "ZMX_DCOOS_ENCRYPTED_KEY", "")
ZMX_DCOOS_VERIFY_TOKEN = getattr(config, "ZMX_DCOOS_VERIFY_TOKEN", "")

# DCOOS 环境 URL 映射
_DCOOS_URLS = {
    "test": {
        "send": getattr(config, "ZMX_DCOOS_TEST_SEND_URL", ""),
        "upload": getattr(config, "ZMX_DCOOS_TEST_UPLOAD_URL", ""),
    },
    "prod": {
        "send": getattr(config, "ZMX_DCOOS_PROD_SEND_URL", ""),
        "upload": getattr(config, "ZMX_DCOOS_PROD_UPLOAD_URL", ""),
    },
}


def _dcoos_send_url():
    return _DCOOS_URLS.get(ZMX_DCOOS_ENV, {}).get("send", "")


def _dcoos_upload_url():
    return _DCOOS_URLS.get(ZMX_DCOOS_ENV, {}).get("upload", "")

# ========== 状态监控 ==========
ZMX_STATUS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'zmx_status.json')
ZMX_MESSAGES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'zmx_messages.json')

# 状态字典（供 dashboard 读取）
ZMX_STATUS = {
    "running": False,
    "listening": False,
    "last_message_at": "",
    "last_error": "",
    "total_received": 0,
    "total_replied": 0,
    "total_errors": 0,
    "total_attachments": 0,
}
ZMX_STATUS_LOCK = threading.Lock()

# 会话记录（内存，最多 100 个）
ZMX_SESSION = {"group": {}, "user": {}}
ZMX_SESSION_LOCK = threading.Lock()

# 消息记录（内存，最多 100 条）
ZMX_MESSAGE_RECORDS = []
ZMX_MESSAGE_RECORDS_LOCK = threading.Lock()
MAX_ZMX_MESSAGE_RECORDS = 100


def _persist_zmx_status():
    """把状态+会话写入 zmx_status.json，供 dashboard（独立进程）读取"""
    try:
        with ZMX_STATUS_LOCK:
            status = dict(ZMX_STATUS)
        with ZMX_SESSION_LOCK:
            session = {
                "group": dict(ZMX_SESSION.get("group", {})),
                "user": dict(ZMX_SESSION.get("user", {})),
            }
        payload = {
            "status": status,
            "session": session,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(ZMX_STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"量子密信状态落盘失败: {e}")


def _update_zmx_status(**kwargs):
    """更新量子密信状态"""
    with ZMX_STATUS_LOCK:
        ZMX_STATUS.update(kwargs)
    _persist_zmx_status()


def _remember_zmx_session(kind: str, identifier: str):
    """记录最近活跃会话（内存，最多保留 100 个），并同步落盘"""
    with ZMX_SESSION_LOCK:
        s = ZMX_SESSION.setdefault(kind, {})
        s[identifier] = time.time()
        if len(s) > 100:
            for k in sorted(s, key=s.get)[: len(s) - 100]:
                s.pop(k, None)
    _persist_zmx_status()


def _add_zmx_message_record(msg_type, user, preview, status="处理中", scene="group"):
    """添加一条消息记录"""
    with ZMX_MESSAGE_RECORDS_LOCK:
        ZMX_MESSAGE_RECORDS.insert(0, {
            "time": time.strftime("%H:%M:%S"),
            "full_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "type": msg_type,
            "user": user,
            "preview": preview[:80] if preview else "",
            "status": status,
            "scene": scene
        })
        if len(ZMX_MESSAGE_RECORDS) > MAX_ZMX_MESSAGE_RECORDS:
            del ZMX_MESSAGE_RECORDS[MAX_ZMX_MESSAGE_RECORDS:]
    _persist_zmx_messages()


def _update_zmx_message_status(new_status: str):
    """更新最新一条消息记录的状态并持久化到文件"""
    with ZMX_MESSAGE_RECORDS_LOCK:
        if ZMX_MESSAGE_RECORDS:
            ZMX_MESSAGE_RECORDS[0]["status"] = new_status
    _persist_zmx_messages()


def _persist_zmx_messages():
    """把消息记录写入 zmx_messages.json，供 dashboard（独立进程）读取"""
    try:
        with ZMX_MESSAGE_RECORDS_LOCK:
            records = list(ZMX_MESSAGE_RECORDS)
        with open(ZMX_MESSAGES_FILE, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"量子密信消息记录落盘失败: {e}")

# ========== 出站限流状态 ==========
_rate_lock = threading.Lock()
_rate_state = {}  # key -> {"timestamps": [...], "blocked_until": 0.0}


def _rate_allowed(callback_url: str):
    """基于 sliding window 的令牌判断（每个机器人 key 独立窗口）。返回 (bool, str)。"""
    key = callback_url
    now = time.time()
    with _rate_lock:
        st = _rate_state.setdefault(key, {"timestamps": [], "blocked_until": 0.0})
        if now < st["blocked_until"]:
            return False, f"平台限流冷却中(剩余{int(st['blocked_until'] - now)}s)"
        st["timestamps"] = [t for t in st["timestamps"] if t > now - ZMX_RATE_WINDOW]
        if len(st["timestamps"]) >= ZMX_RATE_MAX:
            wait = st["timestamps"][0] + ZMX_RATE_WINDOW - now
            st["blocked_until"] = now + max(wait, 1)
            return False, f"出站限流(窗口已满{ZMX_RATE_MAX}/{ZMX_RATE_WINDOW}s)"
        st["timestamps"].append(now)
        return True, ""


def _extract_key(callback_url: str) -> str:
    """从 webhook 发送 URL 提取 key 参数。"""
    try:
        qs = urllib.parse.urlparse(callback_url).query
        return urllib.parse.parse_qs(qs).get("key", [""])[0]
    except Exception:
        return ""


def _zmx_http(method, url, payload=None, headers=None, timeout=30, raw=None):
    """统一 HTTP 封装，绕过环境代理。返回 (status, parsed_body)。"""
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", "wecom-bot/zmx-adapter")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    if raw is not None:
        data = raw
    elif payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if not headers or "Content-Type" not in headers:
            req.add_header("Content-Type", "application/json")
    else:
        data = None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        resp = opener.open(req, data=data, timeout=timeout)
        body = resp.read().decode("utf-8", errors="replace")
        try:
            return resp.status, json.loads(body)
        except Exception:
            return resp.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body
    except Exception as e:
        logger.error(f"量子密信 HTTP 请求异常: {e}")
        return -1, None


def _ok(body) -> bool:
    return isinstance(body, dict) and body.get("ok") is True and body.get("code") == 200


# ========== DCOOS 加密验签工具 ==========

def _dcoos_generate_signature(timestamp: str, nonce: str, verify_token: str, data: str) -> str:
    """HMAC-SHA256(key=verify_token, msg=timestamp+nonce+data) → 小写十六进制。"""
    content = timestamp + nonce + data
    mac = hmac.new(verify_token.encode("utf-8"), content.encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()


def _dcoos_derive_session_key(encrypted_key: str, timestamp: str, nonce: str) -> bytes:
    """SHA-256(encryptedKey + ":" + timestamp + ":" + nonce) → 32 字节会话密钥。"""
    material = f"{encrypted_key}:{timestamp}:{nonce}"
    return hashlib.sha256(material.encode("utf-8")).digest()


def _dcoos_verify_signature(timestamp: str, nonce: str, verify_token: str, data: str, signature: str) -> bool:
    """验签：比对 HMAC-SHA256 签名。"""
    expected = _dcoos_generate_signature(timestamp, nonce, verify_token, data)
    return hmac.compare_digest(expected, signature)


def _dcoos_decrypt(encrypted_key: str, verify_token: str, timestamp: str, nonce: str,
                   signature: str, data_b64: str) -> str:
    """DCOOS 回调解密：验签 → base64 解码 → 派生会话密钥 → AES-256-CBC + PKCS7 去填充。
    返回解密后的明文 JSON 字符串；验签失败或解密失败抛 ValueError。"""
    # 1) 验签
    if not _dcoos_verify_signature(timestamp, nonce, verify_token, data_b64, signature):
        raise ValueError("signature verification failed")
    # 2) base64 解码
    buf = base64.b64decode(data_b64)
    if len(buf) < 16:
        raise ValueError("cipher data too short")
    # 3) 派生会话密钥
    key = _dcoos_derive_session_key(encrypted_key, timestamp, nonce)
    # 4) 提取 IV 与密文
    iv = buf[:16]
    ciphertext = buf[16:]
    if len(ciphertext) % 16 != 0:
        raise ValueError("ciphertext length not multiple of block size")
    # 5) AES-256-CBC 解密
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
    except ImportError:
        # 降级：用 pyaes 纯 Python 实现
        return _dcoos_decrypt_fallback(key, iv, ciphertext)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    plain = unpadder.update(padded) + unpadder.finalize()
    return plain.decode("utf-8")


def _dcoos_decrypt_fallback(key: bytes, iv: bytes, ciphertext: bytes) -> str:
    """纯 Python AES-256-CBC 降级解密（无 cryptography 库时）。"""
    try:
        import pyaes
    except ImportError:
        raise ImportError("需要 cryptography 或 pyaes 库来解密 DCOOS 回调")
    decrypter = pyaes.Decrypter(pyaes.AESModeOfOperationCBC(key, iv=iv))
    padded = decrypter.feed(ciphertext) + decrypter.feed()
    # PKCS7 去填充
    pad_len = padded[-1]
    if pad_len < 1 or pad_len > 16:
        raise ValueError("invalid PKCS7 padding")
    plain = padded[:-pad_len]
    return plain.decode("utf-8")


# ========== DCOOS 鉴权与发送层 ==========

def _dcoos_auth_headers() -> dict:
    """构造 DCOOS 鉴权 Headers。"""
    return {
        "X-APP-ID": ZMX_DCOOS_APP_ID,
        "X-APP-KEY": ZMX_DCOOS_APP_KEY,
        "clientId": ZMX_DCOOS_CLIENT_ID,
        "Content-Type": "application/json",
    }


def _dcoos_ok(body) -> bool:
    """DCOOS 返回成功判断：success=true, code=200。"""
    return isinstance(body, dict) and body.get("success") is True and body.get("code") == 200


def _dcoos_send(payload: dict) -> bool:
    """DCOOS 模式统一发送函数。返回 bool。"""
    url = _dcoos_send_url()
    if not url:
        logger.error("DCOOS 发送 URL 未配置")
        return False
    if not (ZMX_DCOOS_APP_ID and ZMX_DCOOS_APP_KEY and ZMX_DCOOS_CLIENT_ID):
        logger.error("DCOOS 鉴权凭证不完整（需 AppID/AppKey/clientId）")
        return False
    status, body = _zmx_http("POST", url, payload, headers=_dcoos_auth_headers())
    if status != 200 or not _dcoos_ok(body):
        logger.error(f"DCOOS 发送失败 status={status} resp={body}")
        return False
    logger.info(f"DCOOS 消息已发送 type={payload.get('type')} 群={payload.get('groupIds')}")
    return True


def dcoos_send_text(content: str, group_ids: list, can_forward: bool = True) -> bool:
    """DCOOS 模式：发送文本消息（支持多群推送）。"""
    chunks = [content[i:i + ZMX_TEXT_MAX] for i in range(0, len(content), ZMX_TEXT_MAX)] or [content]
    for chunk in chunks:
        payload = {
            "type": "text",
            "content": {"content": chunk},
            "groupIds": group_ids,
            "canForward": can_forward,
        }
        if not _dcoos_send(payload):
            return False
    return True


def dcoos_send_markdown(title: str, content: str, group_ids: list, can_forward: bool = True) -> bool:
    """DCOOS 模式：发送 Markdown 消息。"""
    payload = {
        "type": "markdown",
        "content": {"title": title, "content": content},
        "groupIds": group_ids,
        "canForward": can_forward,
    }
    return _dcoos_send(payload)


def dcoos_send_image(file_id: str, width: int, height: int, mime_type: str,
                     group_ids: list, alt_text: str = "", can_forward: bool = True) -> bool:
    """DCOOS 模式：发送图片消息。"""
    content = {"fileId": file_id, "width": width, "height": height, "mimeType": mime_type}
    if alt_text:
        content["altText"] = alt_text
    payload = {"type": "image", "content": content, "groupIds": group_ids, "canForward": can_forward}
    return _dcoos_send(payload)


def dcoos_send_file(file_id: str, file_name: str, size: int, mime_type: str,
                    group_ids: list, can_forward: bool = True) -> bool:
    """DCOOS 模式：发送文件消息。"""
    payload = {
        "type": "file",
        "content": {"fileId": file_id, "fileName": file_name, "size": size, "mimeType": mime_type},
        "groupIds": group_ids,
        "canForward": can_forward,
    }
    return _dcoos_send(payload)


def dcoos_send_voice(file_id: str, duration: float, mime_type: str,
                     group_ids: list, can_forward: bool = True) -> bool:
    """DCOOS 模式：发送音频消息。duration 单位毫秒。"""
    payload = {
        "type": "voice",
        "content": {"fileId": file_id, "duration": duration, "mimeType": mime_type},
        "groupIds": group_ids,
        "canForward": can_forward,
    }
    return _dcoos_send(payload)


def dcoos_send_video(file_id: str, duration: float, width: int, height: int, mime_type: str,
                     group_ids: list, thumbnail: str = "", can_forward: bool = True) -> bool:
    """DCOOS 模式：发送视频消息。duration 单位毫秒，thumbnail 为封面图 fileId。"""
    content = {"fileId": file_id, "duration": duration, "width": width, "height": height, "mimeType": mime_type}
    if thumbnail:
        content["thumbnail"] = thumbnail
    payload = {"type": "video", "content": content, "groupIds": group_ids, "canForward": can_forward}
    return _dcoos_send(payload)


def dcoos_send_card(title: str, content: str, group_ids: list,
                    image_file_id: str = "", url: str = "", msg_uid: str = "",
                    pc_layout: list = None, tail_fields: list = None,
                    can_forward: bool = True) -> bool:
    """DCOOS 模式：发送卡片消息（含按钮控件）。
    - pc_layout: PC 端按钮布局二维数组，如 [[1,2],[3,4]]
    - tail_fields: 按钮控件数组，每个元素含 name/type/value/index/show
    """
    content_obj = {"title": title, "content": content}
    if image_file_id:
        content_obj["imageFileId"] = image_file_id
    if url:
        content_obj["url"] = url
    if msg_uid:
        content_obj["msgUid"] = msg_uid
    content_obj["pcLayout"] = pc_layout or [[1]]
    content_obj["tailFields"] = tail_fields or []
    payload = {"type": "card", "content": content_obj, "groupIds": group_ids, "canForward": can_forward}
    return _dcoos_send(payload)


# ========== DCOOS 文件上传 ==========

def dcoos_upload_file(file_path: str, upload_type: str = "2") -> dict | None:
    """DCOOS 模式：上传文件，返回 {"id": fileId, "name": ..., "type": ..., "size": ...} 或 None。
    upload_type: "1"=图片, "2"=文件。
    """
    url = _dcoos_upload_url()
    if not url:
        logger.error("DCOOS 上传 URL 未配置")
        return None
    if not (ZMX_DCOOS_APP_ID and ZMX_DCOOS_APP_KEY and ZMX_DCOOS_CLIENT_ID):
        logger.error("DCOOS 鉴权凭证不完整")
        return None
    path = Path(file_path)
    if not path.is_file():
        logger.error(f"文件不存在: {file_path}")
        return None
    if path.stat().st_size > ZMX_MAX_ATTACHMENT:
        logger.error(f"文件超过30MB: {file_path}")
        return None

    boundary = "----WebKitFormBoundary" + uuid.uuid4().hex
    parts = []
    # type 字段
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(f'Content-Disposition: form-data; name="type"\r\n\r\n{upload_type}\r\n'.encode())
    # file 字段
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode())
    parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
    parts.append(path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body_bytes = b"".join(parts)

    headers = _dcoos_auth_headers()
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"

    status, body = _zmx_http("POST", url, headers=headers, raw=body_bytes, timeout=60)
    if isinstance(body, dict) and body.get("ok") is True and body.get("code") == 200:
        data = body.get("data") or {}
        if data.get("id"):
            logger.info(f"DCOOS 文件上传成功 id={data['id']} name={data.get('name')} size={data.get('size')}")
            return data
    logger.error(f"DCOOS 文件上传失败 status={status} resp={body}")
    return None


def dcoos_upload_and_send_image(file_path: str, group_ids: list, alt_text: str = "") -> bool:
    """DCOOS 模式：上传图片并发送到群。"""
    data = dcoos_upload_file(file_path, upload_type="1")
    if not data:
        return False
    # 读取图片宽高
    width, height = _get_image_dimensions(file_path)
    mime_type = Path(file_path).suffix.lstrip(".").lower()
    return dcoos_send_image(data["id"], width, height, mime_type, group_ids, alt_text)


def dcoos_upload_and_send_file(file_path: str, group_ids: list) -> bool:
    """DCOOS 模式：上传文件并发送到群。"""
    data = dcoos_upload_file(file_path, upload_type="2")
    if not data:
        return False
    size = Path(file_path).stat().st_size
    mime_type = Path(file_path).suffix.lstrip(".").lower()
    return dcoos_send_file(data["id"], data.get("name", Path(file_path).name), size, mime_type, group_ids)


def dcoos_upload_and_send_voice(file_path: str, duration_ms: float, group_ids: list) -> bool:
    """DCOOS 模式：上传音频并发送到群。duration_ms 单位毫秒。"""
    data = dcoos_upload_file(file_path, upload_type="2")
    if not data:
        return False
    mime_type = Path(file_path).suffix.lstrip(".").lower()
    return dcoos_send_voice(data["id"], duration_ms, mime_type, group_ids)


def dcoos_upload_and_send_video(file_path: str, duration_ms: float, group_ids: list,
                                thumbnail_file_id: str = "") -> bool:
    """DCOOS 模式：上传视频并发送到群。duration_ms 单位毫秒。"""
    data = dcoos_upload_file(file_path, upload_type="2")
    if not data:
        return False
    width, height = _get_video_dimensions(file_path)
    mime_type = Path(file_path).suffix.lstrip(".").lower()
    return dcoos_send_video(data["id"], duration_ms, width, height, mime_type, group_ids, thumbnail_file_id)


def _get_image_dimensions(file_path: str) -> tuple:
    """读取图片宽高，失败返回 (0, 0)。"""
    try:
        from PIL import Image
        with Image.open(file_path) as img:
            return img.size
    except Exception:
        return (0, 0)


def _get_video_dimensions(file_path: str) -> tuple:
    """读取视频宽高，失败返回 (0, 0)。"""
    try:
        import cv2
        cap = cv2.VideoCapture(file_path)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
        return (width, height)
    except Exception:
        return (0, 0)


# ========== 发送层 ==========

def _resolve_callback(callback_url: str) -> str:
    """优先用回调携带的 callBackUrl（多群隔离），否则退回全局 ZMX_CALLBACK_URL。"""
    return callback_url or ZMX_CALLBACK_URL


def zmx_send_text(content: str, group_id: str = "", phone: str = "", callback_url: str = ""):
    """发送纯文本到量子密信群（自动分片）。返回 bool。"""
    url = _resolve_callback(callback_url)
    if not url:
        logger.error("回调 URL 未配置，无法发送")
        return False
    allowed, info = _rate_allowed(url)
    if not allowed:
        logger.warning(f"量子文本发送被限流: {info}")
        return False
    chunks = [content[i:i + ZMX_TEXT_MAX] for i in range(0, len(content), ZMX_TEXT_MAX)] or [content]
    for chunk in chunks:
        payload = {
            "type": "text",
            "textMsg": {"content": chunk},
            "phone": phone,
            "groupId": group_id,
        }
        status, body = _zmx_http("POST", url, payload)
        if status != 200 or not _ok(body):
            logger.error(f"量子文本发送失败 status={status} resp={body}")
            return False
    logger.info(f"量子文本已发送 群={group_id} phone={phone} 分片={len(chunks)}")
    return True


def zmx_send_markdown(content: str, group_id: str = "", phone: str = "", callback_url: str = ""):
    """发送 Markdown 消息。markdown 必须带唯一 title 字段，否则服务端 500 且不送达。"""
    url = _resolve_callback(callback_url)
    if not url:
        logger.error("回调 URL 未配置")
        return False
    allowed, info = _rate_allowed(url)
    if not allowed:
        logger.warning(f"markdown发送限流: {info}")
        return False
    # 提取首行作为卡片标题，清理行首语法标记与行内 markdown 符号
    first_line = (content.strip().split("\n")[0] or "AI 回复")
    title = re.sub(r"^(?:#{1,6}|>|[-*+]|\d+[.)])\s+", "", first_line)
    title = re.sub(r"[*_`#]", "", title).strip()[:24] or "AI 回复"
    payload = {
        "type": "markdown",
        "markdown": {"title": title, "content": content},
        "phone": phone,
        "groupId": group_id,
    }
    status, body = _zmx_http("POST", url, payload)
    if status != 200 or not _ok(body):
        logger.error(f"markdown发送失败 status={status} resp={body}")
        return False
    logger.info(f"markdown已发送 群={group_id}")
    return True


def zmx_upload_and_send(file_path: str, group_id: str = "", phone: str = "", as_image=False, callback_url: str = ""):
    """上传附件并发送到量子密信。返回 fileId（成功）或 None（失败）。"""
    url = _resolve_callback(callback_url)
    if not url:
        logger.error("回调 URL 未配置")
        return None
    key = _extract_key(url)
    if not key:
        logger.error("无法从 ZMX_CALLBACK_URL 提取 key")
        return None
    path = Path(file_path)
    if not path.is_file():
        logger.error(f"附件不存在: {file_path}")
        return None
    if path.stat().st_size > ZMX_MAX_ATTACHMENT:
        logger.error(f"附件超过30MB: {file_path}")
        return None

    # 1. 上传 -> fileId
    upload_url = url.replace(
        "/im-external/v1/webhook/send", "/im-external/v1/webhook/upload-attachment"
    )
    file_type = "1" if as_image else "2"
    boundary = "----WebKitFormBoundary" + uuid.uuid4().hex
    parts = []
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(f'Content-Disposition: form-data; name="key"\r\n\r\n{key}\r\n'.encode())
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(f'Content-Disposition: form-data; name="type"\r\n\r\n{file_type}\r\n'.encode())
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode())
    parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
    parts.append(path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body_bytes = b"".join(parts)
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}

    status, body = _zmx_http("POST", upload_url, headers=headers, raw=body_bytes, timeout=60)
    file_id = None
    if isinstance(body, dict):
        file_id = (body.get("data") or {}).get("id") or (body.get("content") or {}).get("id")
        # 检查是否返回"机器人不存在"错误
        if body.get("code") == 7001 and "机器人不存在" in body.get("message", ""):
            logger.error(f"量子附件上传失败: 机器人不存在 (code=7001)。可能原因：1) key没有上传权限；2) 需要不同的机器人key；3) 量子密信平台不支持通过webhook上传附件")
            return None
    if status != 200 or not file_id:
        logger.error(f"量子附件上传失败 status={status} resp={body}")
        return None

    # 2. 通过 webhook 发送 fileId
    allowed, _ = _rate_allowed(url)
    if not allowed:
        logger.warning("量子附件发送被限流")
        return file_id
    if as_image:
        payload = {"type": "image", "imageMsg": {"fileId": file_id}, "phone": phone, "groupId": group_id}
    else:
        payload = {"type": "file", "fileMsg": {"fileId": file_id}, "phone": phone, "groupId": group_id}
    status, body = _zmx_http("POST", url, payload)
    if status != 200 or not _ok(body):
        logger.error(f"量子附件发送失败 status={status} resp={body}")
        return file_id
    logger.info(f"量子附件已发送 群={group_id} fileId={file_id} image={as_image}")
    return file_id


# ========== 入站回调 HTTP 服务 ==========

class ZMXWebhookHandler(BaseHTTPRequestHandler):
    """量子密信平台回调入口：
    - POST /webhook  → webhook 模式 @机器人消息（明文）
    - POST /callback → DCOOS 模式加密回调（加密验签 + AES 解密）
    - POST /push     → 面板主动推送
    """

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length else b""

        # 面板主动推送
        if self.path == '/push':
            try:
                data = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except Exception as e:
                self._reply(400, {"success": False, "error": f"bad body: {e}"})
                return
            self._handle_push(data)
            return

        # DCOOS 加密回调
        if self.path == '/callback':
            self._handle_dcoos_callback(raw_body)
            return

        # webhook 模式明文回调
        if self.path == '/webhook':
            self._handle_webhook(raw_body)
            return

        self._reply(404, {"status": "error", "message": f"unknown path: {self.path}"})

    # ----- webhook 模式（原有逻辑，保持兼容）-----
    def _handle_webhook(self, raw_body):
        try:
            data = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception as e:
            self._reply(400, {"status": "error", "message": f"bad body: {e}"})
            return

        # 可选密钥校验
        if ZMX_WEBHOOK_SECRET:
            if self.headers.get("X-Zmx-Secret", "") != ZMX_WEBHOOK_SECRET:
                self._reply(403, {"status": "error", "message": "forbidden"})
                return

        # 必填字段校验（对齐 mixin-chatbot）
        required = ("type", "textMsg", "phone", "groupId", "callBackUrl")
        if not all(k in data for k in required):
            self._reply(400, {"status": "error", "message": f"缺少字段: {[k for k in required if k not in data]}"})
            return
        if data.get("type") != "text":
            self._reply(400, {"status": "error", "message": "仅支持 text"})
            return
        phone = str(data.get("phone", "")).strip()
        group_id = str(data.get("groupId", "")).strip()
        content = str((data.get("textMsg") or {}).get("content", "")).strip()
        callback_url = str(data.get("callBackUrl", "")).strip()
        if not (phone and group_id and content and callback_url):
            self._reply(400, {"status": "error", "message": "phone/groupId/content/callBackUrl 不能为空"})
            return

        logger.info(f"[量子密信-webhook] 收到回调 群={group_id} phone={phone} 内容={content[:50]}")
        self._reply(200, {"status": "success"})
        threading.Thread(
            target=_process_zmx_message,
            args=(content, group_id, phone, callback_url),
            daemon=True,
        ).start()

    # ----- DCOOS 加密回调模式（新增）-----
    def _handle_dcoos_callback(self, raw_body):
        """处理 DCOOS 平台加密回调：验签 → 解密 → 分发。"""
        # 读取加密 Headers
        timestamp = self.headers.get("X-CTQ-Timestamp", "")
        nonce = self.headers.get("X-CTQ-Nonce", "")
        signature = self.headers.get("X-CTQ-Signature", "")

        if not (timestamp and nonce and signature):
            # 无加密头，尝试明文模式（兼容未加密的回调）
            self._handle_dcoos_plain_callback(raw_body)
            return

        if not (ZMX_DCOOS_ENCRYPTED_KEY and ZMX_DCOOS_VERIFY_TOKEN):
            self._reply(500, {"success": False, "message": "DCOOS 加密密钥未配置"})
            return

        # 解析加密请求体
        try:
            encrypted_data = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception as e:
            self._reply(400, {"success": False, "message": f"bad body: {e}"})
            return

        # 兼容两种格式：扁平 Headers 或嵌套 JSON
        data_field = encrypted_data.get("data", "")
        sig_field = encrypted_data.get("signature", signature)
        ts_field = encrypted_data.get("timestamp", timestamp)
        nonce_field = encrypted_data.get("nonce", nonce)

        if not data_field:
            # 可能整个 raw_body 就是 base64 密文字符串
            data_field = raw_body.decode("utf-8").strip().strip('"')

        # 验签 + 解密
        try:
            plaintext = _dcoos_decrypt(
                ZMX_DCOOS_ENCRYPTED_KEY, ZMX_DCOOS_VERIFY_TOKEN,
                ts_field, nonce_field, sig_field, data_field
            )
            msg = json.loads(plaintext)
        except ValueError as e:
            logger.error(f"DCOOS 回调解密失败: {e}")
            self._reply(401, {"success": False, "message": str(e)})
            return
        except Exception as e:
            logger.error(f"DCOOS 回调处理异常: {e}")
            self._reply(500, {"success": False, "message": str(e)})
            return

        logger.info(f"[量子密信-DCOOS] 收到回调 userId={msg.get('userId')} group={msg.get('groupId')} "
                     f"mention={msg.get('mentionType')} type={msg.get('type')}")
        self._reply(200, {"success": True, "code": 200})
        threading.Thread(
            target=_process_dcoos_message,
            args=(msg,),
            daemon=True,
        ).start()

    def _handle_dcoos_plain_callback(self, raw_body):
        """DCOOS 未加密回调（兼容模式）。"""
        try:
            msg = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception as e:
            self._reply(400, {"success": False, "message": f"bad body: {e}"})
            return
        if not msg.get("userId"):
            self._reply(400, {"success": False, "message": "缺少 userId"})
            return
        logger.info(f"[量子密信-DCOOS-明文] 收到回调 userId={msg.get('userId')} group={msg.get('groupId')}")
        self._reply(200, {"success": True, "code": 200})
        threading.Thread(
            target=_process_dcoos_message,
            args=(msg,),
            daemon=True,
        ).start()

    def _reply(self, code, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_push(self, data):
        """处理面板主动推送请求（webhook + DCOOS 双模式）"""
        try:
            target = data.get("target", "group")
            group_id = data.get("groupid", "")
            content = data.get("content", "")
            fmt = data.get("format", "text")
            image_data = data.get("image", "")
            image_name = data.get("imagename", "image.png")
            caption = data.get("caption", "")
            # DCOOS 模式新增参数
            group_ids = data.get("groupids", [group_id] if group_id else [])

            if target != "group":
                self._reply(400, {"success": False, "error": "量子密信仅支持群聊推送"})
                return

            if not group_id and not group_ids:
                self._reply(400, {"success": False, "error": "群ID不能为空"})
                return

            # === DCOOS 模式 ===
            if ZMX_MODE == "dcoos":
                if fmt == "text":
                    if not content:
                        self._reply(400, {"success": False, "error": "消息内容不能为空"})
                        return
                    success = dcoos_send_text(content, group_ids or [group_id])
                    self._reply(200 if success else 500,
                                {"success": success, "detail" if success else "error":
                                 f"DCOOS 文本推送{'成功' if success else '失败'}"})
                elif fmt == "markdown":
                    if not content:
                        self._reply(400, {"success": False, "error": "消息内容不能为空"})
                        return
                    title = data.get("title", "AI 推送")
                    success = dcoos_send_markdown(title, content, group_ids or [group_id])
                    self._reply(200 if success else 500,
                                {"success": success, "detail" if success else "error":
                                 f"DCOOS Markdown 推送{'成功' if success else '失败'}"})
                elif fmt == "image":
                    if not image_data:
                        self._reply(400, {"success": False, "error": "图片数据不能为空"})
                        return
                    temp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'temp_uploads')
                    os.makedirs(temp_dir, exist_ok=True)
                    ext = os.path.splitext(image_name)[1].lower() or '.png'
                    temp_path = os.path.join(temp_dir, f"zmx_push_{int(time.time()*1000)}{ext}")
                    try:
                        import base64 as b64
                        with open(temp_path, 'wb') as f:
                            f.write(b64.b64decode(image_data))
                        success = dcoos_upload_and_send_image(temp_path, group_ids or [group_id], image_name)
                        self._reply(200 if success else 500,
                                    {"success": success, "detail" if success else "error":
                                     f"DCOOS 图片推送{'成功' if success else '失败'}"})
                    except Exception as e:
                        self._reply(500, {"success": False, "error": f"图片处理失败: {str(e)}"})
                    finally:
                        try:
                            if os.path.exists(temp_path):
                                os.remove(temp_path)
                        except Exception:
                            pass
                elif fmt == "file":
                    file_path = data.get("filepath", "")
                    if not file_path or not os.path.isfile(file_path):
                        self._reply(400, {"success": False, "error": "文件路径无效"})
                        return
                    success = dcoos_upload_and_send_file(file_path, group_ids or [group_id])
                    self._reply(200 if success else 500,
                                {"success": success, "detail" if success else "error":
                                 f"DCOOS 文件推送{'成功' if success else '失败'}"})
                elif fmt == "card":
                    title = data.get("title", "AI 推送")
                    body_text = content or ""
                    card_url = data.get("url", "")
                    buttons = data.get("buttons", [])
                    tail_fields = []
                    for btn in buttons:
                        tail_fields.append({
                            "name": btn.get("name", "按钮"),
                            "type": "button",
                            "value": {
                                "placeholder": btn.get("key", "k"),
                                "value": btn.get("value", "v"),
                                "style": btn.get("style", 1),
                                "url": btn.get("callback_url", ""),
                                "method": "POST",
                                "data": btn.get("data", {}),
                            },
                            "index": btn.get("index", 1),
                            "show": True,
                        })
                    success = dcoos_send_card(title, body_text, group_ids or [group_id],
                                              url=card_url, tail_fields=tail_fields)
                    self._reply(200 if success else 500,
                                {"success": success, "detail" if success else "error":
                                 f"DCOOS 卡片推送{'成功' if success else '失败'}"})
                else:
                    self._reply(400, {"success": False, "error": f"DCOOS 不支持的格式: {fmt}"})
                return

            # === webhook 模式（原有逻辑）===
            if fmt == "text":
                if not content:
                    self._reply(400, {"success": False, "error": "消息内容不能为空"})
                    return
                success = zmx_send_text(content, group_id)
                if success:
                    self._reply(200, {"success": True, "detail": "量子密信文本推送成功"})
                else:
                    self._reply(500, {"success": False, "error": "量子密信文本发送失败"})

            # 发送图片
            elif fmt == "image":
                if not image_data:
                    self._reply(400, {"success": False, "error": "图片数据不能为空"})
                    return

                # 保存临时图片文件
                import base64
                temp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'temp_uploads')
                os.makedirs(temp_dir, exist_ok=True)
                ext = os.path.splitext(image_name)[1].lower() or '.png'
                temp_path = os.path.join(temp_dir, f"zmx_push_{int(time.time()*1000)}{ext}")

                try:
                    with open(temp_path, 'wb') as f:
                        f.write(base64.b64decode(image_data))

                    # 上传并发送图片
                    file_id = zmx_upload_and_send(temp_path, group_id, as_image=True)
                    if file_id:
                        self._reply(200, {"success": True, "detail": f"量子密信图片推送成功 (fileId={file_id})"})
                    else:
                        self._reply(500, {"success": False, "error": "量子密信图片上传/发送失败。可能原因：1) key没有上传权限；2) 需要不同的机器人key；3) 量子密信平台不支持通过webhook上传附件。请检查量子密信平台配置。"})
                except Exception as e:
                    self._reply(500, {"success": False, "error": f"图片处理失败: {str(e)}"})
                finally:
                    # 清理临时文件
                    try:
                        if os.path.exists(temp_path):
                            os.remove(temp_path)
                    except Exception:
                        pass

            else:
                self._reply(400, {"success": False, "error": f"不支持的格式: {fmt}"})

        except Exception as e:
            self._reply(500, {"success": False, "error": f"推送异常: {str(e)}"})

    def log_message(self, fmt, *args):
        logger.info(f"[zmx-webhook] {fmt % args}")


def _process_zmx_message(content: str, group_id: str, phone: str, callback_url: str):
    """量子密信消息处理：调用 8088 AI 管线 -> 回复群里（复用 server 管线）。
    callback_url 是平台回调携带的群回复地址（多群隔离，必须用它回复）。"""
    try:
        # 手机号映射为用户名（映射不到则用手机号）
        user_name = ZMX_USER_MAP.get(phone, phone)
        
        # 记录收到消息
        _update_zmx_status(total_received=ZMX_STATUS.get("total_received", 0) + 1,
                          last_message_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        _remember_zmx_session("group", group_id)
        _add_zmx_message_record("text", user_name, content, "处理中", "group")
        
        prompt = server.build_prompt([], content, user_name)
        session_title = server.get_session_title("密信", "群聊", user_name, group_id or phone)
        result = server.call_teleagent(prompt, timeout=1800, session_title=session_title)
        if not result:
            zmx_send_text("抱歉，处理超时或出错了，请稍后重试。", group_id, phone, callback_url)
            _update_zmx_status(total_errors=ZMX_STATUS.get("total_errors", 0) + 1)
            _update_zmx_message_status("失败")
            return

        file_paths = server.extract_file_paths(result)
        if file_paths:
            text_reply = re.sub(r"FILE_PATH:.+?(?:\n|$)", "", result).strip()
            if text_reply:
                zmx_send_markdown(text_reply, group_id, phone, callback_url)
            for fp in file_paths:
                is_img = Path(fp).suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
                try:
                    zmx_upload_and_send(fp, group_id, phone, as_image=is_img, callback_url=callback_url)
                    _update_zmx_status(total_attachments=ZMX_STATUS.get("total_attachments", 0) + 1)
                except Exception as e:
                    logger.error(f"量子附件发送失败 {fp}: {e}")
        else:
            zmx_send_markdown(result, group_id, phone, callback_url)
        
        _update_zmx_status(total_replied=ZMX_STATUS.get("total_replied", 0) + 1)
        _update_zmx_message_status("已回复")
    except Exception as e:
        logger.error(f"量子密信处理异常: {e}")
        _update_zmx_status(total_errors=ZMX_STATUS.get("total_errors", 0) + 1,
                          last_error=str(e)[:200])
        _update_zmx_message_status("失败")
        try:
            zmx_send_markdown("⚠️ 抱歉，处理您的请求时出现异常，请稍后重试。", group_id, phone, callback_url)
        except Exception:
            pass


def _process_dcoos_message(msg: dict):
    """DCOOS 回调消息处理：根据 mentionType 分发，调用 AI 管线后用 DCOOS 发送回复。
    msg 字段：userId, groupId, mentionType(1=单聊/2=@所有人/3=@部分人), msgId, type, content
    """
    try:
        user_id = str(msg.get("userId", "")).strip()
        group_id = str(msg.get("groupId", "")).strip()
        mention_type = msg.get("mentionType", 0)
        msg_type = str(msg.get("type", "text")).strip()
        content_obj = msg.get("content") or {}
        mention_desc = {1: "单聊", 2: "@所有人", 3: "@部分人"}.get(mention_type, "未知")

        # 提取文本内容
        if msg_type == "text":
            text_content = str(content_obj.get("content", "")).strip()
        elif msg_type == "image":
            text_content = "[收到图片消息]"
        elif msg_type == "voice":
            text_content = "[收到语音消息]"
        elif msg_type == "video":
            text_content = "[收到视频消息]"
        elif msg_type == "file":
            text_content = "[收到文件消息]"
        else:
            text_content = str(content_obj.get("content", "")) or f"[收到{msg_type}消息]"

        if not text_content:
            text_content = f"[收到{msg_type}消息，暂不支持解析]"

        # 用户名映射（DCOOS 用 userId 而非 phone）
        user_name = ZMX_USER_MAP.get(user_id, user_id)

        # 记录收到消息
        _update_zmx_status(total_received=ZMX_STATUS.get("total_received", 0) + 1,
                           last_message_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        scene = "单聊" if mention_type == 1 else "群聊"
        if group_id:
            _remember_zmx_session("group", group_id)
        _add_zmx_message_record(msg_type, user_name, text_content, "处理中", scene)

        logger.info(f"[DCOOS] 处理消息 {mention_desc} user={user_name} group={group_id} 内容={text_content[:50]}")

        # 调用 AI 管线
        prompt = server.build_prompt([], text_content, user_name)
        session_title = server.get_session_title("密信", scene, user_name, group_id or user_id)
        result = server.call_teleagent(prompt, timeout=1800, session_title=session_title)
        if not result:
            if group_id:
                dcoos_send_text("抱歉，处理超时或出错了，请稍后重试。", [group_id])
            _update_zmx_status(total_errors=ZMX_STATUS.get("total_errors", 0) + 1)
            _update_zmx_message_status("失败")
            return

        # 提取文件路径并发送
        file_paths = server.extract_file_paths(result)
        group_ids = [group_id] if group_id else []

        if file_paths:
            text_reply = re.sub(r"FILE_PATH:.+?(?:\n|$)", "", result).strip()
            if text_reply and group_ids:
                dcoos_send_markdown("AI 回复", text_reply, group_ids)
            for fp in file_paths:
                ext = Path(fp).suffix.lower()
                is_img = ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
                is_voice = ext in (".mp3", ".aac", ".wav", ".m4a", ".ogg")
                is_video = ext in (".mp4", ".avi", ".mov", ".mkv", ".flv")
                try:
                    if is_img and group_ids:
                        dcoos_upload_and_send_image(fp, group_ids)
                    elif is_voice and group_ids:
                        import mutagen
                        audio = mutagen.File(fp)
                        duration_ms = (audio.info.length * 1000) if audio else 0
                        dcoos_upload_and_send_voice(fp, duration_ms, group_ids)
                    elif is_video and group_ids:
                        # 视频时长需 ffprobe，暂用 0
                        dcoos_upload_and_send_video(fp, 0, group_ids)
                    elif group_ids:
                        dcoos_upload_and_send_file(fp, group_ids)
                    _update_zmx_status(total_attachments=ZMX_STATUS.get("total_attachments", 0) + 1)
                except Exception as e:
                    logger.error(f"DCOOS 附件发送失败 {fp}: {e}")
        else:
            if group_ids:
                dcoos_send_markdown("AI 回复", result, group_ids)

        _update_zmx_status(total_replied=ZMX_STATUS.get("total_replied", 0) + 1)
        _update_zmx_message_status("已回复")
    except Exception as e:
        logger.error(f"DCOOS 消息处理异常: {e}")
        _update_zmx_status(total_errors=ZMX_STATUS.get("total_errors", 0) + 1,
                           last_error=str(e)[:200])
        _update_zmx_message_status("失败")


def start_zmx_server():
    """启动量子密信入站回调 HTTP 服务（阻塞）。"""
    if not ZMX_ENABLED:
        logger.warning("ZMX_ENABLED 未开启，量子密信适配器不启动")
        return
    try:
        srv = ThreadingHTTPServer((ZMX_LISTEN_HOST, ZMX_LISTEN_PORT), ZMXWebhookHandler)
        _update_zmx_status(running=True, listening=True, last_error="")
    except OSError as e:
        logger.error(f"量子密信监听端口 {ZMX_LISTEN_PORT} 失败: {e}")
        _update_zmx_status(running=False, listening=False, last_error=str(e)[:200])
        return
    logger.info(f"量子密信入站回调已启动: http://{ZMX_LISTEN_HOST}:{ZMX_LISTEN_PORT}/webhook")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        logger.info("量子密信回调服务已停止")
    finally:
        srv.server_close()
        _update_zmx_status(running=False, listening=False)


def main():
    logger.info("量子密信适配器启动")
    logger.info(f"运行模式: {ZMX_MODE}")
    if ZMX_MODE == "dcoos":
        logger.info(f"DCOOS 环境: {ZMX_DCOOS_ENV}")
        logger.info(f"DCOOS 发送URL: {_dcoos_send_url()}")
        logger.info(f"DCOOS 上传URL: {_dcoos_upload_url()}")
        logger.info(f"DCOOS AppID: {ZMX_DCOOS_APP_ID[:8]}***" if ZMX_DCOOS_APP_ID else "DCOOS AppID: 未配置")
        logger.info(f"DCOOS clientId: {ZMX_DCOOS_CLIENT_ID[:8]}***" if ZMX_DCOOS_CLIENT_ID else "DCOOS clientId: 未配置")
        if ZMX_DCOOS_ENCRYPTED_KEY and ZMX_DCOOS_VERIFY_TOKEN:
            logger.info("DCOOS 回调加密: 已配置（验签+AES解密）")
        else:
            logger.info("DCOOS 回调加密: 未配置（明文模式）")
    else:
        logger.info(f"回调URL: {ZMX_CALLBACK_URL}")
    logger.info(f"回调监听: http://{ZMX_LISTEN_HOST}:{ZMX_LISTEN_PORT}")
    logger.info(f"  /webhook  — webhook 模式回调入口")
    logger.info(f"  /callback — DCOOS 加密回调入口")
    logger.info(f"  /push     — 面板主动推送入口")
    start_zmx_server()


if __name__ == "__main__":
    main()