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
    # 同步到文件（供 dashboard 读取）
    try:
        with ZMX_MESSAGE_RECORDS_LOCK:
            records = list(ZMX_MESSAGE_RECORDS)
        with open(ZMX_MESSAGES_FILE, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

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
    """量子密信平台回调入口：POST /webhook 接收 @机器人 消息；POST /push 处理面板主动推送。"""

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            data = json.loads(body.decode("utf-8")) if body else {}
        except Exception as e:
            self._reply(400, {"status": "error", "message": f"bad body: {e}"})
            return

        # 处理面板主动推送请求
        if self.path == '/push':
            self._handle_push(data)
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

        logger.info(f"[量子密信] 收到回调 群={group_id} phone={phone} 内容={content[:50]}")
        self._reply(200, {"status": "success"})
        threading.Thread(
            target=_process_zmx_message,
            args=(content, group_id, phone, callback_url),
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
        """处理面板主动推送请求"""
        try:
            target = data.get("target", "group")
            group_id = data.get("groupid", "")
            content = data.get("content", "")
            fmt = data.get("format", "text")
            image_data = data.get("image", "")
            image_name = data.get("imagename", "image.png")
            caption = data.get("caption", "")
            
            if target != "group":
                self._reply(400, {"success": False, "error": "量子密信仅支持群聊推送"})
                return
            
            if not group_id:
                self._reply(400, {"success": False, "error": "群ID不能为空"})
                return
            
            # 发送文本
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
        # 更新消息记录状态为已回复
        with ZMX_MESSAGE_RECORDS_LOCK:
            if ZMX_MESSAGE_RECORDS:
                ZMX_MESSAGE_RECORDS[0]["status"] = "已回复"
    except Exception as e:
        logger.error(f"量子密信处理异常: {e}")
        _update_zmx_status(total_errors=ZMX_STATUS.get("total_errors", 0) + 1,
                          last_error=str(e)[:200])
        try:
            zmx_send_markdown("⚠️ 抱歉，处理您的请求时出现异常，请稍后重试。", group_id, phone, callback_url)
        except Exception:
            pass


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
    logger.info(f"回调URL: {ZMX_CALLBACK_URL}")
    start_zmx_server()


if __name__ == "__main__":
    main()