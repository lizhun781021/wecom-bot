#!/usr/bin/env python3
"""
企业微信智能机器人 - 长连接模式
基于 WebSocket 长连接接收消息，无需公网IP和内网穿透
支持：文字/图片/文件/语音/视频消息接收
功能：保存图片 + PushPlus通知 + 转发群聊 + AI图片识别
"""

import hashlib
import time
import json
import os
import re
import base64
import uuid
import threading
import requests
import logging
import glob as glob_module
from pathlib import Path
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

import websocket  # pip install websocket-client

import config

# ========== WebSocket线程安全发送锁 ==========
# websocket-client的send方法虽然有内部锁，但on_message回调和子线程同时send仍可能出问题
# 所有ws.send()调用都通过send_ws_message，受此锁保护
_send_lock = threading.Lock()

# ========== 日志配置 ==========
LOG_DIR = os.path.dirname(os.path.abspath(__file__))
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(LOG_DIR, 'wecom-bot.log'), encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# WebSocket 连接地址
WS_URL = "wss://openws.work.weixin.qq.com"

# 全局 WebSocket 连接
ws_app = None
reconnect_count = 0
MAX_RECONNECT = 100


def gen_req_id():
    """生成唯一请求ID"""
    return str(uuid.uuid4())


def send_ws_message(ws, cmd, body, req_id=None):
    """发送 WebSocket 消息（线程安全）"""
    if req_id is None:
        req_id = gen_req_id()
    msg = {
        "cmd": cmd,
        "headers": {"req_id": req_id},
        "body": body
    }
    try:
        with _send_lock:
            ws.send(json.dumps(msg))
        logger.info(f"发送 [{cmd}] req_id={req_id}")
    except Exception as e:
        logger.error(f"发送消息失败: {e}")


def reply_stream(ws, req_id, content, stream_id=None, finish=True):
    """用流式消息回复（企微长连接不支持text类型回复，必须用stream）"""
    if stream_id is None:
        stream_id = gen_req_id()
    send_ws_message(ws, "aibot_respond_msg", {
        "msgtype": "stream",
        "stream": {
            "id": stream_id,
            "finish": finish,
            "content": content
        }
    }, req_id)
    return stream_id


# ========== 多媒体资源解密 ==========
def decrypt_media(encrypted_data, aeskey):
    """解密企业微信多媒体资源
    算法：AES-256-CBC
    IV：aeskey 前16字节
    aeskey 是 Base64 编码的，需先解码为 32 字节原始密钥
    注意：企微图片加密不使用标准PKCS7 padding，unpad可能失败，
    此时直接返回解密后的原始数据（图像格式自带结束标记，多余padding不影响）
    """
    # 补齐 Base64 padding（企微 aeskey 可能缺少 = 填充）
    padded_key = aeskey + '=' * (4 - len(aeskey) % 4) if len(aeskey) % 4 else aeskey
    key = base64.b64decode(padded_key)
    iv = key[:16]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted_raw = cipher.decrypt(encrypted_data)
    # 尝试PKCS7 unpad，失败则直接用原始解密数据
    try:
        return unpad(decrypted_raw, AES.block_size)
    except Exception:
        logger.warning("PKCS7 unpad失败，使用原始解密数据（企微非标准padding）")
        return decrypted_raw


def download_and_decrypt_media(url, aeskey):
    """下载并解密多媒体资源"""
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code != 200:
            logger.error(f"下载资源失败: HTTP {resp.status_code}")
            return None

        encrypted_data = resp.content
        if aeskey:
            decrypted = decrypt_media(encrypted_data, aeskey)
        else:
            decrypted = encrypted_data

        return decrypted
    except Exception as e:
        logger.error(f"下载/解密资源异常: {e}")
        return None


# ========== TeleAgent 对接：通过8088代理直接调用 ==========

# WebSocket异步响应同步等待机制
_ws_response_events = {}  # req_id -> threading.Event
_ws_response_data = {}    # req_id -> dict (响应数据)
_ws_lock = threading.Lock()


def register_pending_request(req_id):
    """注册一个待等待的请求"""
    with _ws_lock:
        _ws_response_events[req_id] = threading.Event()
        _ws_response_data[req_id] = {}


def wait_for_ws_response(req_id, timeout=30):
    """等待指定req_id的响应，返回响应数据dict"""
    with _ws_lock:
        ev = _ws_response_events.get(req_id)
    if not ev:
        return {}
    ev.wait(timeout=timeout)
    with _ws_lock:
        data = _ws_response_data.get(req_id, {})
        # 清理
        _ws_response_events.pop(req_id, None)
        _ws_response_data.pop(req_id, None)
    return data


def deliver_ws_response(req_id, data):
    """on_message中调用，投递响应数据并唤醒等待线程"""
    with _ws_lock:
        if req_id in _ws_response_events:
            _ws_response_data[req_id] = data
            _ws_response_events[req_id].set()
            return True
    return False


# ========== 企微通讯录API：userid → 姓名自动查询 ==========

# access_token 缓存
_access_token = None
_access_token_expire = 0
_token_lock = threading.Lock()

# 本地姓名缓存文件
NAME_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "name_cache.json")
_name_cache = {}  # {userid: name}


def _load_name_cache():
    """启动时从本地文件加载姓名缓存"""
    global _name_cache
    try:
        if os.path.exists(NAME_CACHE_FILE):
            with open(NAME_CACHE_FILE, 'r', encoding='utf-8') as f:
                _name_cache = json.load(f)
            logger.info(f"已加载姓名缓存: {len(_name_cache)} 条")
    except Exception as e:
        logger.warning(f"加载姓名缓存失败: {e}")
        _name_cache = {}


def _save_name_cache():
    """保存姓名缓存到本地文件"""
    try:
        with open(NAME_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(_name_cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"保存姓名缓存失败: {e}")


def _get_access_token():
    """获取企微access_token，带缓存（有效期2小时）"""
    global _access_token, _access_token_expire
    with _token_lock:
        now = time.time()
        # 提前5分钟刷新，避免临界过期
        if _access_token and now < _access_token_expire - 300:
            return _access_token
        try:
            url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
            params = {
                "corpid": config.CORP_ID,
                "corpsecret": config.CORP_SECRET
            }
            resp = requests.get(url, params=params, timeout=10)
            data = resp.json()
            if data.get("errcode") != 0:
                logger.error(f"获取access_token失败: {data}")
                return None
            _access_token = data["access_token"]
            _access_token_expire = now + data.get("expires_in", 7200)
            logger.info("access_token 获取成功")
            return _access_token
        except Exception as e:
            logger.error(f"获取access_token异常: {e}")
            return None


def _query_userid_name(userid):
    """通过企微通讯录API查询userid对应的姓名"""
    token = _get_access_token()
    if not token:
        return None
    try:
        url = f"https://qyapi.weixin.qq.com/cgi-bin/user/get"
        params = {
            "access_token": token,
            "userid": userid
        }
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        if data.get("errcode") == 0:
            name = data.get("name", "")
            if name:
                logger.info(f"通讯录查询成功: {userid} → {name}")
                return name
        elif data.get("errcode") == 60011:
            logger.warning(f"无权查看用户 {userid}（不在应用可见范围内）")
        else:
            logger.warning(f"通讯录查询失败: {userid}, {data}")
        return None
    except Exception as e:
        logger.error(f"通讯录查询异常: {e}")
        return None


def get_user_name(userid):
    """将企微userid转换为姓名：手动映射 > 本地缓存 > API查询 > 降级显示原始ID"""
    # 1. 手动映射优先
    if userid in config.WECOM_USER_MAP:
        return config.WECOM_USER_MAP[userid]
    # 2. 本地缓存
    if userid in _name_cache:
        return _name_cache[userid]
    # 3. 调API查询
    name = _query_userid_name(userid)
    if name:
        _name_cache[userid] = name
        _save_name_cache()
        return name
    # 4. 查不到，降级显示原始ID
    return userid


def call_teleagent(prompt, timeout=1800, session_title=None):
    """通过 OpenAI 兼容代理调用 TeleAgent AI 能力，返回回复文本"""
    try:
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer any"
        }
        data = {
            "model": config.TELEAGENT_MODEL,
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "stream": False
        }
        # 传会话标题，让TeleAgent界面上显示调用人+技能
        if session_title:
            data["session_title"] = session_title
        resp = requests.post(
            config.TELEAGENT_PROXY_URL,
            headers=headers,
            json=data,
            timeout=timeout
        )
        if resp.status_code != 200:
            logger.error(f"代理调用失败: HTTP {resp.status_code}, {resp.text[:200]}")
            return None
        result = resp.json()
        content = result['choices'][0]['message']['content']
        logger.info(f"TeleAgent回复: {content[:100]}...")
        return content
    except Exception as e:
        logger.error(f"调用TeleAgent代理异常: {e}")
        return None


def build_prompt(file_paths, text_content, from_user):
    """构建prompt：文件给路径、文字原文转发，不加任何多余说明
    file_paths: [(path, type), type为'image'或'file'或'voice'或'video']
    """
    parts = []
    if file_paths:
        image_paths = [p for p, t in file_paths if t == 'image']
        other_paths = [(p, t) for p, t in file_paths if t != 'image']
        if image_paths:
            parts.append(f"{from_user}在群聊中发送了图片，请用 image_understanding 工具查看：")
            for path in image_paths:
                abs_path = os.path.abspath(path)
                parts.append(abs_path)
        for path, ftype in other_paths:
            abs_path = os.path.abspath(path)
            if ftype == 'voice':
                parts.append(f"{from_user}在群聊中发送了语音录音文件，请用 offline_asr 技能转写并分析：")
            elif ftype == 'video':
                parts.append(f"{from_user}在群聊中发送了视频文件：")
            else:
                parts.append(f"{from_user}在群聊中发送了文件：")
            parts.append(abs_path)
        if text_content:
            parts.append(f"\n{from_user}说：{text_content}")
    else:
        parts.append(f"{from_user}说：{text_content}")
    return "\n".join(parts)


def extract_file_paths(text):
    """从回复文本中提取所有FILE_PATH:后面的路径，返回列表"""
    import re
    paths = []
    for match in re.finditer(r'FILE_PATH:(.+?)(?:\n|$)', text):
        path = match.group(1).strip()
        # 去掉可能的前后引号
        path = path.strip('"').strip("'")
        if os.path.exists(path):
            paths.append(path)
        else:
            logger.warning(f"FILE_PATH路径不存在: {path}")
    return paths


def process_and_reply(ws, req_id, stream_id, file_paths, text_content, from_user):
    """异步处理：调用TeleAgent代理 -> 回复群里（含文件发送）
    file_paths: [(path, type), type为'image'/'file'/'voice'/'video']
    """
    user_name = get_user_name(from_user)

    # 构建prompt：文件路径+文字原文
    prompt = build_prompt(file_paths, text_content, user_name)

    # 会话标题：姓名 | 技能 | 时间
    time_str = time.strftime("%H:%M")
    session_title = f"{user_name} | 河南标准化赋能 | {time_str}"

    has_files = bool(file_paths)
    logger.info(f"开始调用TeleAgent, 调用人={user_name}, 有文件={has_files}")

    # 调用TeleAgent代理
    result = call_teleagent(prompt, timeout=1800, session_title=session_title)

    if not result:
        reply_stream(
            ws, req_id,
            "抱歉，处理超时或出错了。请稍后重试，或直接私聊发给星小辰处理。",
            stream_id=stream_id, finish=True
        )
        logger.error("TeleAgent调用失败，已回复错误消息")
        return

    # 尝试提取所有文件路径
    file_paths = extract_file_paths(result)

    if file_paths:
        # 从回复中去掉所有FILE_PATH行，保留摘要
        text_reply = re.sub(r'FILE_PATH:.+?(?:\n|$)', '', result).strip()
        if not text_reply:
            text_reply = "配餐分析文档已生成，请查看下方文件。"

        # 先发文字摘要
        if len(text_reply) > 3000:
            text_reply = text_reply[:3000] + "\n\n(摘要过长已截断，完整内容见下方文档)"
        reply_stream(ws, req_id, text_reply, stream_id=stream_id, finish=True)
        logger.info(f"已回复文字摘要, 长度={len(text_reply)}")

        # 逐个上传并发送文件
        for fp in file_paths:
            logger.info(f"开始上传文件到企微: {fp}")
            media_id = upload_media_sync(ws, fp, media_type="file")
            if media_id:
                send_file_message(ws, req_id, media_id, os.path.basename(fp))
                logger.info(f"已发送文件到群: {os.path.basename(fp)}")
                # 文件间间隔0.5秒，避免企微限流
                if fp != file_paths[-1]:
                    time.sleep(0.5)
            else:
                reply_stream(ws, req_id, "文档已生成但发送失败，路径：" + fp)
                logger.error(f"文件上传失败: {fp}")
        logger.info(f"共发送{len(file_paths)}个文件")
    else:
        # 没有文件，直接回复文本
        if len(result) > 3000:
            result = result[:3000] + "\n\n(回复过长已截断)"
        reply_stream(ws, req_id, result, stream_id=stream_id, finish=True)
        logger.info(f"已回复群消息(纯文本), 长度={len(result)}")


# ========== PushPlus 通知 ==========
def send_pushplus(title, content):
    """发送 PushPlus 通知"""
    if not config.PUSHPLUS_TOKEN:
        return
    try:
        url = "http://pushplus.plus/send"
        data = {
            "token": config.PUSHPLUS_TOKEN,
            "title": title,
            "content": content,
            "template": "html"
        }
        requests.post(url, json=data, timeout=10)
        logger.info("PushPlus 通知已发送")
    except Exception as e:
        logger.error(f"PushPlus 通知失败: {e}")


# ========== 群聊 Webhook 转发 ==========
def forward_to_webhook(image_path):
    """转发图片到群聊 Webhook"""
    if not config.WEBHOOK_URL or not config.WEBHOOK_KEY:
        logger.info("Webhook 未配置，跳过转发")
        return

    webhook_url = f"{config.WEBHOOK_URL}?key={config.WEBHOOK_KEY}"
    try:
        with open(image_path, 'rb') as f:
            img_b64 = base64.b64encode(f.read()).decode('utf-8')

        md5_hash = hashlib.md5(base64.b64decode(img_b64)).hexdigest()
        msg_data = {
            "msgtype": "image",
            "image": {
                "base64": img_b64,
                "md5": md5_hash
            }
        }
        resp = requests.post(webhook_url, json=msg_data, timeout=10)
        logger.info(f"转发到群聊: {resp.json()}")
    except Exception as e:
        logger.error(f"转发群聊异常: {e}")


# ========== AI 图片识别 ==========
def ai_analyze_image(image_path):
    """AI 识别图片内容"""
    if not config.AI_API_URL or not config.AI_API_KEY:
        return None

    try:
        with open(image_path, 'rb') as f:
            img_b64 = base64.b64encode(f.read()).decode('utf-8')

        ext = os.path.splitext(image_path)[1].lower()
        mime_map = {'.jpg': 'jpeg', '.jpeg': 'jpeg', '.png': 'png', '.gif': 'gif', '.webp': 'webp'}
        mime_type = mime_map.get(ext, 'jpeg')

        url = f"{config.AI_API_URL}/chat/completions"
        headers = {
            "Authorization": f"Bearer {config.AI_API_KEY}",
            "Content-Type": "application/json"
        }
        data = {
            "model": config.AI_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请识别并描述这张图片的内容，如果是文档或截图请提取其中的文字信息。"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/{mime_type};base64,{img_b64}"}
                        }
                    ]
                }
            ],
            "max_tokens": 1024
        }
        resp = requests.post(url, headers=headers, json=data, timeout=60)
        result = resp.json()
        content = result['choices'][0]['message']['content']
        logger.info(f"AI识别结果: {content[:100]}...")
        return content
    except Exception as e:
        logger.error(f"AI识别异常: {e}")
        return None


# ========== 上传临时素材（长连接模式，同步版）==========
def upload_media_sync(ws, file_path, media_type="file"):
    """同步上传素材：init → 等upload_id → chunk逐片 → finish → 等media_id
    返回 media_id 或 None"""
    try:
        filename = os.path.basename(file_path)
        total_size = os.path.getsize(file_path)
        chunk_size = 512 * 1024  # 512KB
        total_chunks = (total_size + chunk_size - 1) // chunk_size

        # 计算MD5
        with open(file_path, 'rb') as f:
            md5_hash = hashlib.md5(f.read()).hexdigest()

        # Step 1: init —— 等响应拿 upload_id
        init_req_id = gen_req_id()
        register_pending_request(init_req_id)
        send_ws_message(ws, "aibot_upload_media_init", {
            "type": media_type,
            "filename": filename,
            "total_size": total_size,
            "total_chunks": total_chunks,
            "md5": md5_hash
        }, init_req_id)
        init_data = wait_for_ws_response(init_req_id, timeout=180)
        logger.info(f"上传初始化响应数据: {json.dumps(init_data, ensure_ascii=False)[:500]}")
        upload_id = init_data.get("upload_id", "")
        if not upload_id:
            logger.error(f"上传初始化失败，未获得upload_id: {init_data}")
            return None
        logger.info(f"上传初始化成功: upload_id={upload_id}")

        # Step 2: 逐片上传，等每片响应
        with open(file_path, 'rb') as f:
            chunk_index = 0
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                chunk_req_id = gen_req_id()
                register_pending_request(chunk_req_id)
                send_ws_message(ws, "aibot_upload_media_chunk", {
                    "upload_id": upload_id,
                    "chunk_index": chunk_index,
                    "base64_data": base64.b64encode(chunk).decode('utf-8')
                }, chunk_req_id)
                chunk_data = wait_for_ws_response(chunk_req_id, timeout=180)
                if chunk_data.get("errcode", -1) != 0:
                    logger.error(f"分片{chunk_index}上传失败: {chunk_data}")
                    return None
                chunk_index += 1
        logger.info(f"所有{chunk_index}个分片上传完成")

        # Step 3: finish —— 等响应拿 media_id
        finish_req_id = gen_req_id()
        register_pending_request(finish_req_id)
        send_ws_message(ws, "aibot_upload_media_finish", {
            "upload_id": upload_id
        }, finish_req_id)
        finish_data = wait_for_ws_response(finish_req_id, timeout=180)
        media_id = finish_data.get("media_id", "")
        if media_id:
            logger.info(f"素材上传完成: media_id={media_id}, filename={filename}")
            return media_id
        else:
            logger.error(f"上传完成但未获得media_id: {finish_data}")
            return None

    except Exception as e:
        logger.error(f"上传素材异常: {e}", exc_info=True)
        return None


def send_file_message(ws, req_id, media_id, filename):
    """发送文件消息到群聊"""
    send_ws_message(ws, "aibot_respond_msg", {
        "msgtype": "file",
        "file": {
            "media_id": media_id,
            "filename": filename
        }
    }, req_id)


def upload_media(ws, file_path, media_type="image"):
    """旧接口保留兼容（异步，不等待响应）"""
    logger.warning("upload_media(旧版)已弃用，请使用upload_media_sync")
    return None


# ========== 待发文件队列 ==========
# 当有文件需要发送到群聊但没有活跃的req_id时，先存队列
# 下次收到群消息时自动发送
PENDING_FILES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pending_files.json")
PENDING_FILES = []  # [{"path": "/abs/path", "summary": "描述"}]
PENDING_FILES_LOCK = threading.Lock()

def _load_pending_files():
    """从本地JSON文件加载待发文件（支持跨进程：外部写入文件，bot读取发送）"""
    global PENDING_FILES
    with PENDING_FILES_LOCK:
        try:
            if os.path.exists(PENDING_FILES_FILE):
                with open(PENDING_FILES_FILE, 'r', encoding='utf-8') as f:
                    PENDING_FILES = json.load(f)
                if PENDING_FILES:
                    logger.info(f"已加载待发文件队列: {len(PENDING_FILES)} 个文件")
                    # 不清空文件，等发送成功后再清
                else:
                    PENDING_FILES = []
        except Exception as e:
            logger.error(f"加载待发文件异常: {e}")
            PENDING_FILES = []

def _clear_pending_files_file():
    """清空待发文件JSON"""
    try:
        with open(PENDING_FILES_FILE, 'w', encoding='utf-8') as f:
            json.dump([], f)
        logger.info("已清空待发文件队列文件")
    except Exception as e:
        logger.error(f"清空待发文件异常: {e}")

def add_pending_files(file_paths, summary=""):
    """添加待发文件到队列（同时写入JSON文件供运行中的bot读取）"""
    with PENDING_FILES_LOCK:
        for fp in file_paths:
            if os.path.exists(fp):
                PENDING_FILES.append({"path": fp, "summary": summary})
                logger.info(f"已添加待发文件: {fp}")
            else:
                logger.error(f"待发文件不存在，跳过: {fp}")
        # 写入JSON文件
        try:
            with open(PENDING_FILES_FILE, 'w', encoding='utf-8') as f:
                json.dump(PENDING_FILES, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"写入待发文件JSON异常: {e}")
        logger.info(f"待发文件队列长度={len(PENDING_FILES)}")

def flush_pending_files(ws, req_id):
    """检查待发文件队列，如有则启动异步上传线程发送，不阻塞on_message回调
    返回True表示有待发文件（已启动异步上传），False表示没有"""
    # 先从文件重新加载（可能有外部写入的新文件）
    _load_pending_files()

    files_to_send = []
    with PENDING_FILES_LOCK:
        if not PENDING_FILES:
            return False
        files_to_send = PENDING_FILES[:]
    # 不清空队列！等上传成功后再清

    logger.info(f"发现{len(files_to_send)}个待发文件，启动异步上传线程...")
    # 先回复一条消息告知用户
    reply_stream(ws, req_id, f"检测到{len(files_to_send)}个待发文件，正在上传中，请稍候...", finish=True)
    
    # 启动独立线程执行文件上传（不阻塞on_message回调）
    thread = threading.Thread(
        target=_upload_and_send_files_async,
        args=(ws, req_id, files_to_send),
        daemon=True
    )
    thread.start()
    return True


def _upload_and_send_files_async(ws, req_id, files_to_send):
    """在独立线程中执行文件上传和发送（不阻塞WebSocket接收循环）
    所有 ws.send() 调用通过 send_lock 保护，避免和 on_message 中的 send 冲突"""
    success_count = 0
    for i, item in enumerate(files_to_send):
        fp = item["path"]
        logger.info(f"上传待发文件[{i+1}/{len(files_to_send)}]: {fp}")
        media_id = upload_media_sync(ws, fp, media_type="file")
        if media_id:
            send_file_message(ws, req_id, media_id, os.path.basename(fp))
            logger.info(f"已发送待发文件: {os.path.basename(fp)}")
            success_count += 1
            if i < len(files_to_send) - 1:
                time.sleep(1)
        else:
            logger.error(f"待发文件上传失败: {fp}")
            reply_stream(ws, req_id, f"文档上传失败: {os.path.basename(fp)}")
    
    logger.info(f"文件上传完成: 成功{success_count}/{len(files_to_send)}")
    
    # 发送完成后清空文件队列
    if success_count > 0:
        with PENDING_FILES_LOCK:
            PENDING_FILES.clear()
        _clear_pending_files_file()


# ========== 消息处理 ==========
def handle_text_message(ws, msg, req_id):
    """处理文字消息：统一走TeleAgent代理回复"""
    body = msg.get("body", {})
    from_user = body.get("from", {}).get("userid", "unknown")
    text_content = body.get("text", {}).get("content", "")
    chattype = body.get("chattype", "single")

    logger.info(f"收到文字消息: from={from_user}, chattype={chattype}, content={text_content[:50]}")

    # 先检查待发文件队列
    if flush_pending_files(ws, req_id):
        logger.info("待发文件已发送，跳过当前消息处理")
        return

    # 先回复收到
    stream_id = reply_stream(ws, req_id, "收到，正在处理...", finish=False)
    # 异步调用TeleAgent
    thread = threading.Thread(
        target=process_and_reply,
        args=(ws, req_id, stream_id, [], text_content, from_user),
        daemon=True
    )
    thread.start()


def handle_image_message(ws, msg, req_id):
    """处理图片消息：下载解密 -> 走TeleAgent代理（含看图+配餐）"""
    body = msg.get("body", {})
    from_user = body.get("from", {}).get("userid", "unknown")
    chattype = body.get("chattype", "single")
    image_info = body.get("image", {})
    url = image_info.get("url", "")
    aeskey = image_info.get("aeskey", "")

    logger.info(f"收到图片消息: from={from_user}, chattype={chattype}")

    # 先检查待发文件队列
    if flush_pending_files(ws, req_id):
        logger.info("待发文件已发送，跳过图片消息处理")
        return

    # 1. 下载并解密图片
    decrypted = download_and_decrypt_media(url, aeskey)
    if not decrypted:
        reply_stream(ws, req_id, "图片下载失败，请重新发送")
        return

    # 保存到本地
    save_dir = config.IMAGE_SAVE_DIR
    os.makedirs(save_dir, exist_ok=True)
    timestamp = int(time.time())
    image_filename = f"img_{timestamp}_{from_user}.jpg"
    image_path = os.path.join(save_dir, image_filename)
    with open(image_path, 'wb') as f:
        f.write(decrypted)
    logger.info(f"图片已保存: {image_path} ({len(decrypted)} bytes)")

    # 先回复收到
    stream_id = reply_stream(ws, req_id, "图片已收到，正在分析...", finish=False)

    # 2. PushPlus 通知
    send_pushplus(
        "企业微信收到图片",
        f"<p>发送者: {from_user}</p><p>图片已保存: {image_path}</p><p>大小: {len(decrypted)} bytes</p><p>正在调用TeleAgent分析</p>"
    )

    # 3. 异步调用TeleAgent代理（含看图+配餐分析）
    thread = threading.Thread(
        target=process_and_reply,
        args=(ws, req_id, stream_id, [(image_path, 'image')], "", from_user),
        daemon=True
    )
    thread.start()


def handle_file_message(ws, msg, req_id):
    """处理文件消息：下载解密 -> 走TeleAgent代理"""
    body = msg.get("body", {})
    from_user = body.get("from", {}).get("userid", "unknown")
    file_info = body.get("file", {})
    url = file_info.get("url", "")
    aeskey = file_info.get("aeskey", "")
    filename = file_info.get("filename", "unknown_file")

    logger.info(f"收到文件消息: from={from_user}, filename={filename}")

    # 先回复收到
    stream_id = reply_stream(ws, req_id, f"收到文件: {filename}，正在处理...", finish=False)

    decrypted = download_and_decrypt_media(url, aeskey)
    if not decrypted:
        reply_stream(ws, req_id, "文件下载失败", stream_id=stream_id, finish=True)
        return

    save_dir = config.FILE_SAVE_DIR
    os.makedirs(save_dir, exist_ok=True)
    # 保留原始扩展名
    safe_name = filename.replace('/', '_').replace(' ', '_')
    filepath = os.path.join(save_dir, f"file_{int(time.time())}_{safe_name}")
    with open(filepath, 'wb') as f:
        f.write(decrypted)
    logger.info(f"文件已保存: {filepath} ({len(decrypted)} bytes)")

    # 异步调用TeleAgent
    thread = threading.Thread(
        target=process_and_reply,
        args=(ws, req_id, stream_id, [(filepath, 'file')], "", from_user),
        daemon=True
    )
    thread.start()


def handle_voice_message(ws, msg, req_id):
    """处理语音消息：下载解密 -> 走TeleAgent代理（offline_asr转写分析）"""
    body = msg.get("body", {})
    from_user = body.get("from", {}).get("userid", "unknown")
    voice_info = body.get("voice", {})
    url = voice_info.get("url", "")
    aeskey = voice_info.get("aeskey", "")

    logger.info(f"收到语音消息: from={from_user}")

    if not url:
        reply_stream(ws, req_id, "语音消息无下载URL")
        return

    # 先回复收到
    stream_id = reply_stream(ws, req_id, "收到语音，正在转写分析...", finish=False)

    decrypted = download_and_decrypt_media(url, aeskey)
    if not decrypted:
        reply_stream(ws, req_id, "语音下载失败", stream_id=stream_id, finish=True)
        return

    save_dir = config.VOICE_SAVE_DIR
    os.makedirs(save_dir, exist_ok=True)
    # 语音文件通常为amr格式
    filepath = os.path.join(save_dir, f"voice_{int(time.time())}_{from_user}.amr")
    with open(filepath, 'wb') as f:
        f.write(decrypted)
    logger.info(f"语音已保存: {filepath} ({len(decrypted)} bytes)")

    # 异步调用TeleAgent
    thread = threading.Thread(
        target=process_and_reply,
        args=(ws, req_id, stream_id, [(filepath, 'voice')], "", from_user),
        daemon=True
    )
    thread.start()


def handle_video_message(ws, msg, req_id):
    """处理视频消息：下载解密 -> 走TeleAgent代理"""
    body = msg.get("body", {})
    from_user = body.get("from", {}).get("userid", "unknown")
    video_info = body.get("video", {})
    url = video_info.get("url", "")
    aeskey = video_info.get("aeskey", "")

    logger.info(f"收到视频消息: from={from_user}")

    if not url:
        reply_stream(ws, req_id, "视频消息无下载URL")
        return

    # 先回复收到
    stream_id = reply_stream(ws, req_id, "收到视频，正在处理...", finish=False)

    decrypted = download_and_decrypt_media(url, aeskey)
    if not decrypted:
        reply_stream(ws, req_id, "视频下载失败", stream_id=stream_id, finish=True)
        return

    save_dir = config.VIDEO_SAVE_DIR
    os.makedirs(save_dir, exist_ok=True)
    filepath = os.path.join(save_dir, f"video_{int(time.time())}_{from_user}.mp4")
    with open(filepath, 'wb') as f:
        f.write(decrypted)
    logger.info(f"视频已保存: {filepath} ({len(decrypted)} bytes)")

    # 异步调用TeleAgent
    thread = threading.Thread(
        target=process_and_reply,
        args=(ws, req_id, stream_id, [(filepath, 'video')], "", from_user),
        daemon=True
    )
    thread.start()


def handle_mixed_message(ws, msg, req_id):
    """处理图文混排消息（群聊@机器人发图的主要类型）"""
    body = msg.get("body", {})
    from_user = body.get("from", {}).get("userid", "unknown")
    chattype = body.get("chattype", "single")
    mixed_info = body.get("mixed", {})
    msg_items = mixed_info.get("msg_item", [])

    logger.info(f"收到图文混排消息: from={from_user}, chattype={chattype}, items={len(msg_items)}")
    logger.info(f"mixed消息原文: {json.dumps(body, ensure_ascii=False)[:1000]}")

    # 先检查待发文件队列
    if flush_pending_files(ws, req_id):
        logger.info("待发文件已发送，跳过mixed消息处理")
        return

    # 先回复收到
    # 根据items预判消息类型
    has_image = any(item.get("msgtype") == "image" for item in msg_items)
    has_other = any(item.get("msgtype") in ("file", "voice", "video") for item in msg_items)
    if has_image and has_other:
        reply_text = "收到图文/文件，正在处理..."
    elif has_image:
        reply_text = "收到图片，正在分析..."
    elif has_other:
        reply_text = "收到文件，正在处理..."
    else:
        reply_text = "收到，正在处理..."
    stream_id = reply_stream(ws, req_id, reply_text, finish=False)

    # 解析 msg_item 数组
    text_parts = []
    file_paths = []  # [(path, type)]
    timestamp = int(time.time())
    img_dir = config.IMAGE_SAVE_DIR; os.makedirs(img_dir, exist_ok=True)
    file_dir = config.FILE_SAVE_DIR; os.makedirs(file_dir, exist_ok=True)
    voice_dir = config.VOICE_SAVE_DIR; os.makedirs(voice_dir, exist_ok=True)
    video_dir = config.VIDEO_SAVE_DIR; os.makedirs(video_dir, exist_ok=True)

    for i, item in enumerate(msg_items):
        item_type = item.get("msgtype", "")
        if item_type == "text":
            text_parts.append(item.get("text", {}).get("content", ""))
        elif item_type == "image":
            img_info = item.get("image", {})
            url = img_info.get("url", "")
            aeskey = img_info.get("aeskey", "")
            if url:
                decrypted = download_and_decrypt_media(url, aeskey)
                if decrypted:
                    img_filename = f"mixed_{timestamp}_{from_user}_{i}.jpg"
                    img_path = os.path.join(img_dir, img_filename)
                    with open(img_path, 'wb') as f:
                        f.write(decrypted)
                    file_paths.append((img_path, 'image'))
                    logger.info(f"mixed图片已保存: {img_path} ({len(decrypted)} bytes)")
                else:
                    logger.error(f"mixed图片下载失败: item {i}")
        elif item_type == "file":
            file_info = item.get("file", {})
            url = file_info.get("url", "")
            aeskey = file_info.get("aeskey", "")
            filename = file_info.get("filename", f"unknown_{i}")
            if url:
                decrypted = download_and_decrypt_media(url, aeskey)
                if decrypted:
                    safe_name = filename.replace('/', '_').replace(' ', '_')
                    fpath = os.path.join(file_dir, f"mixed_{timestamp}_{safe_name}")
                    with open(fpath, 'wb') as f:
                        f.write(decrypted)
                    file_paths.append((fpath, 'file'))
                    logger.info(f"mixed文件已保存: {fpath} ({len(decrypted)} bytes)")
                else:
                    logger.error(f"mixed文件下载失败: item {i}")
        elif item_type == "voice":
            voice_info = item.get("voice", {})
            url = voice_info.get("url", "")
            aeskey = voice_info.get("aeskey", "")
            if url:
                decrypted = download_and_decrypt_media(url, aeskey)
                if decrypted:
                    vpath = os.path.join(voice_dir, f"mixed_{timestamp}_voice_{i}.amr")
                    with open(vpath, 'wb') as f:
                        f.write(decrypted)
                    file_paths.append((vpath, 'voice'))
                    logger.info(f"mixed语音已保存: {vpath} ({len(decrypted)} bytes)")
                else:
                    logger.error(f"mixed语音下载失败: item {i}")
        elif item_type == "video":
            video_info = item.get("video", {})
            url = video_info.get("url", "")
            aeskey = video_info.get("aeskey", "")
            if url:
                decrypted = download_and_decrypt_media(url, aeskey)
                if decrypted:
                    vpath = os.path.join(video_dir, f"mixed_{timestamp}_video_{i}.mp4")
                    with open(vpath, 'wb') as f:
                        f.write(decrypted)
                    file_paths.append((vpath, 'video'))
                    logger.info(f"mixed视频已保存: {vpath} ({len(decrypted)} bytes)")
                else:
                    logger.error(f"mixed视频下载失败: item {i}")

    # 异步调用TeleAgent代理 -> 回复群里
    text_content = " ".join(text_parts) if text_parts else ""
    thread = threading.Thread(
        target=process_and_reply,
        args=(ws, req_id, stream_id, file_paths, text_content, from_user),
        daemon=True
    )
    thread.start()


# ========== WebSocket 事件处理 ==========
def on_open(ws):
    """连接建立后发送订阅请求"""
    logger.info("WebSocket 连接已建立，发送订阅请求...")
    req_id = gen_req_id()
    # 把订阅req_id存到ws对象上，避免跨线程全局变量问题
    ws.subscribe_req_id = req_id
    send_ws_message(ws, "aibot_subscribe", {
        "bot_id": config.BOT_ID,
        "secret": config.BOT_SECRET
    }, req_id)


def on_message(ws, message):
    """处理收到的 WebSocket 消息"""
    global reconnect_count
    reconnect_count = 0  # 成功收到消息，重置重连计数

    try:
        msg = json.loads(message)
        cmd = msg.get("cmd", "")
        headers = msg.get("headers", {})
        req_id = headers.get("req_id", "")
        errcode = msg.get("errcode", -1)
        errmsg = msg.get("errmsg", "")
        body = msg.get("body", {})

        logger.info(f"收到消息: cmd={cmd}, errcode={errcode}, req_id={req_id}")
        # 记录完整原始消息用于调试上传响应问题
        if not cmd and req_id:
            logger.info(f"无cmd响应完整原文: {message[:1000]}")

        # 有cmd的消息：按类型处理
        if cmd == "aibot_msg_callback":
            body_dict = msg.get("body", {})
            msgtype = body_dict.get("msgtype", "")
            callback_req_id = req_id

            if msgtype == "text":
                handle_text_message(ws, msg, callback_req_id)
            elif msgtype == "image":
                handle_image_message(ws, msg, callback_req_id)
            elif msgtype == "file":
                handle_file_message(ws, msg, callback_req_id)
            elif msgtype == "voice":
                handle_voice_message(ws, msg, callback_req_id)
            elif msgtype == "video":
                handle_video_message(ws, msg, callback_req_id)
            elif msgtype == "mixed":
                handle_mixed_message(ws, msg, callback_req_id)
            else:
                logger.info(f"暂不处理的消息类型: {msgtype}")
                reply_stream(ws, callback_req_id, f"收到{msgtype}类型消息")
            return

        if cmd == "aibot_event_callback":
            body_dict = msg.get("body", {})
            event = body_dict.get("event", {})
            eventtype = event.get("eventtype", "")

            if eventtype == "enter_chat":
                logger.info(f"用户进入会话: {body_dict.get('from', {}).get('userid')}")
                send_ws_message(ws, "aibot_respond_welcome_msg", {
                    "msgtype": "text",
                    "text": {"content": "你好！我是星小辰机器人，可以接收图片、文件等消息。发张图片试试？"}
                })
            elif eventtype == "disconnected_event":
                logger.warning("收到连接断开事件，停止心跳等待重连")
                stop_heartbeat()
            elif eventtype == "template_card_event":
                logger.info("收到模板卡片事件")
            elif eventtype == "feedback_event":
                logger.info("收到用户反馈事件")
            else:
                logger.info(f"收到事件: {eventtype}")
            return

        if cmd == "aibot_upload_media_init":
            if errcode == 0:
                upload_id = msg.get("body", {}).get("upload_id", "")
                logger.info(f"素材上传初始化成功: upload_id={upload_id}")
            else:
                logger.error(f"素材上传初始化失败: {errmsg}")
            deliver_ws_response(req_id, {"upload_id": upload_id if errcode == 0 else "", "errcode": errcode, "errmsg": errmsg})
            return

        if cmd == "aibot_upload_media_chunk":
            if errcode != 0:
                logger.error(f"素材分片上传失败: {errmsg}")
            deliver_ws_response(req_id, {"errcode": errcode, "errmsg": errmsg})
            return

        if cmd == "aibot_upload_media_finish":
            media_id = ""
            if errcode == 0:
                media_id = msg.get("body", {}).get("media_id", "")
                logger.info(f"素材上传完成: media_id={media_id}")
            else:
                logger.error(f"素材上传完成失败: {errmsg}")
            deliver_ws_response(req_id, {"media_id": media_id, "errcode": errcode, "errmsg": errmsg})
            return

        # 无cmd的响应（订阅/心跳/上传素材的响应都可能是这种格式）
        if cmd == "" and errcode == 0:
            # 先检查是否有 pending request 在等待这个 req_id（上传素材的响应cmd可能为空）
            with _ws_lock:
                is_pending = req_id in _ws_response_events
            if is_pending:
                # 投递完整响应数据，让等待线程能取到 upload_id / media_id 等字段
                body_data = msg.get("body", {})
                body_data["errcode"] = errcode
                logger.info(f"上传响应body内容: {json.dumps(body_data, ensure_ascii=False)[:500]}")
                deliver_ws_response(req_id, body_data)
                logger.info(f"已投递无cmd响应给pending request: req_id={req_id}")
                return

            subscribe_req_id = getattr(ws, 'subscribe_req_id', None)
            if req_id == subscribe_req_id:
                logger.info("✓ 订阅成功！机器人已上线")
                if not heartbeat_running:
                    start_heartbeat(ws)
            else:
                # 所有其他无cmd响应视为心跳响应
                logger.debug("心跳响应 OK")
            return

        logger.info(f"未处理的消息: cmd={cmd}, errcode={errcode}")

    except Exception as e:
        logger.error(f"处理消息异常: {e}", exc_info=True)


def on_error(ws, error):
    """WebSocket 错误"""
    logger.error(f"WebSocket 错误: {error}")


def on_close(ws, close_status_code, close_msg):
    """WebSocket 连接关闭"""
    global reconnect_count
    logger.warning(f"WebSocket 连接关闭: code={close_status_code}, msg={close_msg}")
    # 先停止心跳，防止旧线程干扰
    stop_heartbeat()
    # 自动重连
    schedule_reconnect()


# ========== 心跳保活 ==========
heartbeat_running = False


def start_heartbeat(ws):
    """启动心跳线程"""
    global heartbeat_running
    heartbeat_running = True

    def heartbeat_loop():
        while heartbeat_running:
            try:
                time.sleep(30)
                if ws.sock and ws.sock.connected:
                    ping_req_id = gen_req_id()
                    ping_msg = {
                        "cmd": "ping",
                        "headers": {"req_id": ping_req_id}
                    }
                    with _send_lock:
                        ws.send(json.dumps(ping_msg))
                    logger.debug("发送心跳 ping")
                else:
                    logger.warning("WebSocket 未连接，停止心跳")
                    break
            except Exception as e:
                logger.error(f"心跳异常: {e}")
                break

    t = threading.Thread(target=heartbeat_loop, daemon=True)
    t.start()
    logger.info("心跳线程已启动（每30秒）")


def stop_heartbeat():
    """停止心跳"""
    global heartbeat_running
    heartbeat_running = False


# ========== 自动重连 ==========
def schedule_reconnect():
    """计划重连（在 on_close 中调用，但实际重连在主循环控制）"""
    pass  # 重连逻辑移到 main_loop


# ========== 主连接 ==========
def connect_websocket():
    """建立 WebSocket 长连接"""
    global ws_app

    logger.info(f"正在连接企业微信长连接: {WS_URL}")

    ws_app = websocket.WebSocketApp(
        WS_URL,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )

    ws_app.run_forever(
        ping_interval=0,  # 不使用 websocket-client 内置 ping，我们自己控制
        ping_timeout=None,
        skip_utf8_validation=True
    )


# ========== 启动 ==========
if __name__ == '__main__':
    if not config.BOT_ID or not config.BOT_SECRET:
        logger.error("请先在 config.py 中填入 BOT_ID 和 BOT_SECRET")
        logger.error("获取方式：企业微信客户端 → 工作台 → 智能机器人 → API模式(长连接)")
        exit(1)

    logger.info("=" * 50)
    logger.info("企业微信智能机器人 - 长连接模式")
    logger.info(f"BotID: {config.BOT_ID}")
    logger.info(f"文件保存: 图片={config.IMAGE_SAVE_DIR}")

    # 加载本地姓名缓存
    _load_name_cache()

    # 加载待发文件队列
    _load_pending_files()

    logger.info("=" * 50)

    # 主循环：断线自动重连
    while True:
        try:
            connect_websocket()
        except Exception as e:
            logger.error(f"连接异常: {e}")

        # run_forever 返回后，等待重连
        stop_heartbeat()
        reconnect_count += 1
        if reconnect_count > MAX_RECONNECT:
            logger.error(f"重连次数超过上限({MAX_RECONNECT})，退出")
            break

        delay = min(2 ** min(reconnect_count, 6), 60)
        logger.info(f"将在 {delay} 秒后重连（第 {reconnect_count} 次）")
        time.sleep(delay)
