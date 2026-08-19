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
import dashboard
import wecom_api

# ========== 禁用环境代理 ==========
# 本机进程常从 shell/launchd 继承 http_proxy/HTTPS_PROXY（如 127.0.0.1:7892），
# requests 默认尊重环境代理，会把访问本机 127.0.0.1:8088 的 AI 请求也转发给代理，
# 导致请求挂起永不返回（QQ/企微消息均受影响）。这里在 import 早期统一清除。
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)
os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("all_proxy", None)
os.environ.pop("ALL_PROXY", None)
# 注意：requests 会缓存环境代理，必须在其他模块 import requests 前清理；
# 若后续 import 顺序变化，可在 call_teleagent 内再强制 session.trust_env=False。

# ========== Dashboard 管理面板端口 ==========
DASHBOARD_PORT = 8505

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

# 全局 WebSocket 连接（线程安全引用：process_and_reply等子线程通过get_ws()获取最新连接）
ws_app = None
_ws_lock = threading.Lock()
reconnect_count = 0
MAX_RECONNECT = 100


def get_ws():
    """获取当前最新的WebSocket连接对象（线程安全）"""
    with _ws_lock:
        return ws_app


def set_ws(ws):
    """更新全局WebSocket连接引用（线程安全）"""
    global ws_app
    with _ws_lock:
        ws_app = ws


def gen_req_id():
    """生成唯一请求ID"""
    return str(uuid.uuid4())


def send_ws_message(ws, cmd, body, req_id=None):
    """发送 WebSocket 消息（线程安全，自动使用最新连接）"""
    # 如果传入的ws已失效，尝试用最新的全局连接
    current_ws = get_ws()
    if current_ws and (not ws or not hasattr(ws, 'sock') or ws.sock is None or not ws.sock.connected):
        ws = current_ws
    if req_id is None:
        req_id = gen_req_id()
    msg = {
        "cmd": cmd,
        "headers": {"req_id": req_id},
        "body": body
    }
    try:
        with _send_lock:
            if ws and ws.sock and ws.sock.connected:
                ws.send(json.dumps(msg))
            else:
                logger.error(f"WebSocket未连接，无法发送 [{cmd}]")
                return None
        logger.info(f"发送 [{cmd}] req_id={req_id}")
    except Exception as e:
        logger.error(f"发送消息失败: {e}")
        return None
    return req_id


def reply_stream(ws, req_id, content, stream_id=None, finish=True, feedback_id=None):
    """用流式消息回复（企微长连接不支持text类型回复，必须用stream）
    feedback_id: 可选，设置后用户可对消息点赞/点踩，反馈事件会回调给机器人
    """
    if stream_id is None:
        stream_id = gen_req_id()
    stream_body = {
        "id": stream_id,
        "finish": finish,
        "content": content
    }
    if feedback_id:
        stream_body["feedback"] = {"id": feedback_id[:256]}
    send_ws_message(ws, "aibot_respond_msg", {
        "msgtype": "stream",
        "stream": stream_body
    }, req_id)
    return stream_id


def reply_markdown(ws, req_id, content, feedback_id=None):
    """用 Markdown 消息回复（支持标题/加粗/列表/表格/代码等格式）

    Args:
        content: Markdown 内容，最长 20480 字节（utf-8）
        feedback_id: 可选，设置后用户可对消息点赞/点踩，反馈事件会回调给机器人
    """
    markdown_body = {"content": content[:20480]}
    if feedback_id:
        markdown_body["feedback"] = {"id": feedback_id[:256]}
    send_ws_message(ws, "aibot_respond_msg", {
        "msgtype": "markdown",
        "markdown": markdown_body
    }, req_id)


def reply_template_card(ws, req_id, card):
    """回复模板卡片消息（aibot_respond_msg 的 template_card 类型）"""
    send_ws_message(ws, "aibot_respond_msg", {
        "msgtype": "template_card",
        "template_card": card
    }, req_id)


def reply_welcome(ws, msgtype="text", content="", card=None):
    """回复进入会话欢迎语（enter_chat 事件后 5 秒内）
    支持文本消息或模板卡片消息两种形式。
    """
    if msgtype == "template_card" and card:
        send_ws_message(ws, "aibot_respond_welcome_msg", {
            "msgtype": "template_card",
            "template_card": card
        })
    else:
        send_ws_message(ws, "aibot_respond_welcome_msg", {
            "msgtype": "text",
            "text": {"content": content}
        })


def update_template_card(ws, req_id, card):
    """更新模板卡片（template_card_event 事件后 5 秒内）
    仅适用于模板卡片点击事件，其他事件类型不支持。
    """
    send_ws_message(ws, "aibot_respond_update_msg", {
        "response_type": "update_template_card",
        "template_card": card
    }, req_id)


def send_push_message(ws, chatid, msgtype, payload, chat_type=0):
    """主动推送消息（aibot_send_msg）
    前置条件：用户必须先给机器人发过消息（会话已建立）。

    Args:
        chatid: 单聊填 userid，群聊填 chatid
        chat_type: 1=单聊 2=群聊 0=自动兼容（优先按群聊）
        msgtype: template_card / markdown / file / image / voice / video
        payload: 对应类型的消息体，如 {"content": "..."} 或 {"media_id": "..."}
    """
    body = {
        "chatid": chatid,
        "chat_type": chat_type,
        "msgtype": msgtype,
        msgtype: payload
    }
    send_ws_message(ws, "aibot_send_msg", body)


# ========== 模板卡片构造 ==========

def build_text_notice_card(main_title, sub_title_text="", card_action=None,
                           source=None, emphasis_content=None, horizontal_content_list=None,
                           jump_list=None, task_id="", action_menu=None):
    """文本通知模板卡片（text_notice）
    卡片点击跳转事件 card_action 为必填项。
    """
    card = {"card_type": "text_notice"}
    if source:
        card["source"] = source
    if action_menu:
        card["action_menu"] = action_menu
    if main_title:
        card["main_title"] = main_title
    if emphasis_content:
        card["emphasis_content"] = emphasis_content
    if sub_title_text:
        card["sub_title_text"] = sub_title_text
    if horizontal_content_list:
        card["horizontal_content_list"] = horizontal_content_list
    if jump_list:
        card["jump_list"] = jump_list
    if card_action:
        card["card_action"] = card_action
    if task_id:
        card["task_id"] = task_id
    return card


def build_news_notice_card(main_title, sub_title_text="", source_id=None,
                           horizontal_content_list=None, jump_list=None,
                           card_action=None, task_id=""):
    """图文展示模板卡片（news_notice）"""
    card = {"card_type": "news_notice"}
    if source_id:
        card["source"] = source_id
    if main_title:
        card["main_title"] = main_title
    if sub_title_text:
        card["sub_title_text"] = sub_title_text
    if horizontal_content_list:
        card["horizontal_content_list"] = horizontal_content_list
    if jump_list:
        card["jump_list"] = jump_list
    if card_action:
        card["card_action"] = card_action
    if task_id:
        card["task_id"] = task_id
    return card


def build_button_interaction_card(main_title, button_list, task_id, sub_title_text="",
                                  horizontal_content_list=None, card_action=None,
                                  button_selection=None):
    """按钮交互模板卡片（button_interaction）
    注意：机器人设置了回调URL时才能下发，长连接模式可能不支持，需实测。
    """
    card = {
        "card_type": "button_interaction",
        "main_title": main_title,
        "button_list": button_list,
        "task_id": task_id
    }
    if sub_title_text:
        card["sub_title_text"] = sub_title_text
    if horizontal_content_list:
        card["horizontal_content_list"] = horizontal_content_list
    if card_action:
        card["card_action"] = card_action
    if button_selection:
        card["button_selection"] = button_selection
    return card


def build_vote_interaction_card(main_title, checkbox, submit_button, task_id):
    """投票选择模板卡片（vote_interaction）
    注意：机器人设置了回调URL时才能下发，长连接模式可能不支持，需实测。
    """
    return {
        "card_type": "vote_interaction",
        "main_title": main_title,
        "checkbox": checkbox,
        "submit_button": submit_button,
        "task_id": task_id
    }


def build_multiple_interaction_card(main_title, select_list, submit_button, task_id):
    """多项选择模板卡片（multiple_interaction）
    注意：机器人设置了回调URL时才能下发，长连接模式可能不支持，需实测。
    """
    return {
        "card_type": "multiple_interaction",
        "main_title": main_title,
        "select_list": select_list,
        "submit_button": submit_button,
        "task_id": task_id
    }


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
    """将企微userid转换为姓名：手动映射 > 本地缓存 > API查询 > 降级截断展示"""
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
    # 4. 查不到，降级截断展示（避免显示过长的原始ID）
    if len(userid) > 16:
        return userid[:8] + "..." + userid[-6:]
    return userid


def call_teleagent(prompt, timeout=1800, session_title=None):
    """调用 TeleAgent AI 能力，返回回复文本"""
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
        # 关键：必须绕过环境代理。本机 AI 请求只能直连 127.0.0.1:8088，
        # 若被 http_proxy 劫持会转发到 7892 等外网代理导致挂起永不返回。
        _session = requests.Session()
        _session.trust_env = False
        resp = _session.post(
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
    """从回复文本中提取所有FILE_PATH:后面的路径，返回列表。
    正则匹配到合法文件扩展名(.pdf/.docx/.xlsx等)为止，避免尾随中文污染路径。
    """
    import re
    paths = []
    # 先尝试匹配带已知扩展名的路径（贪婪回溯会停在扩展名处，尾随中文不被捕获）
    ext_pattern = r'FILE_PATH:\s*(\S+\.(?:pdf|docx|doc|xlsx|xls|pptx|ppt|txt|csv|png|jpg|jpeg|gif|bmp|zip|7z|mp4|mp3|wav|m4a|aac|flac|html|htm|json|md))'
    for match in re.finditer(ext_pattern, text, re.IGNORECASE):
        path = match.group(1).strip().strip('"').strip("'")
        if os.path.exists(path):
            paths.append(path)
        else:
            logger.warning(f"FILE_PATH路径不存在(扩展名匹配): {path}")
    # 兜底：旧方式（针对没有标准扩展名的路径）
    if not paths:
        for match in re.finditer(r'FILE_PATH:\s*([^\s\n]+)', text):
            path = match.group(1).strip().rstrip('。，,：:').strip('"').strip("'")
            if os.path.exists(path):
                paths.append(path)
            else:
                logger.warning(f"FILE_PATH路径不存在(兜底): {path}")
    return paths


# ========== 配餐后处理：表格台账 + 待办 + 文档 ==========

def extract_peican_data(ai_reply, user_name, from_user):
    """从AI回复文本中提取结构化配餐数据，用于写入台账和创建待办
    
    尝试用正则匹配AI回复中的关键配餐信息：
    客户号码、当前套餐、出账金额、推荐套餐、套餐月费、配餐路径、提值空间
    
    Returns:
        dict or None: struct_field -> value，至少包含部分字段；None表示没有配餐相关内容
    """
    data = {}
    data["时间"] = time.strftime("%Y-%m-%d %H:%M")
    data["处理人"] = user_name
    
    # 清理Markdown格式标记，便于正则匹配
    text = ai_reply.replace('**', '').replace('*', '').replace('###', '').replace('##', '').replace('#', '')
    
    # 客户号码：匹配手机号/固话模式
    phone_match = re.search(r'(客户号码|客户手机|号码|手机号)[：:\s]*([0-9\-]{7,13})', text)
    if phone_match:
        data["客户号码"] = phone_match.group(2)
    else:
        phone_match2 = re.search(r'(1[3-9]\d{9})', text)
        if phone_match2:
            data["客户号码"] = phone_match2.group(1)
        else:
            data["客户号码"] = "未提取"
    
    # 当前套餐
    cur_match = re.search(r'(当前套餐|原套餐|现有套餐)[：:\s]*([^\n，。]+)', text)
    if cur_match:
        data["当前套餐"] = cur_match.group(2).strip()[:30]
    else:
        data["当前套餐"] = "未提取"
    
    # 出账金额
    bill_match = re.search(r'(出账金额|月出账|出账|账单金额|账单|消费)[：:\s]*([0-9.]+)\s*元?', text)
    if bill_match:
        data["出账金额"] = bill_match.group(2) + "元"
    else:
        data["出账金额"] = "未提取"
    
    # 推荐套餐
    rec_match = re.search(r'(推荐套餐|建议套餐|配餐结果)[：:\s]*([^\n，。]+)', text)
    if rec_match:
        data["推荐套餐"] = rec_match.group(2).strip()[:30]
    else:
        data["推荐套餐"] = "未提取"
    
    # 套餐月费
    fee_match = re.search(r'套餐月费[：:\s]*([0-9.]+)\s*元?', text)
    if fee_match:
        data["套餐月费"] = fee_match.group(1) + "元"
    else:
        data["套餐月费"] = "未提取"
    
    # 配餐路径
    path_match = re.search(r'(配餐路径|路径)[：:\s]*([^\n，。]+)', text)
    if path_match:
        data["配餐路径"] = path_match.group(2).strip()[:30]
    else:
        data["配餐路径"] = "未提取"
    
    # 提值空间
    uplift_match = re.search(r'(提值空间|提值)[：:\s]*([0-9.+\-～到]+)\s*元?', text)
    if uplift_match:
        data["提值空间"] = uplift_match.group(2) + "元/月"
    else:
        uplift_match2 = re.search(r'(提值空间|提值|增收)[：:\s]*([^\n，。]+)', text)
        if uplift_match2:
            data["提值空间"] = uplift_match2.group(2).strip()[:20]
        else:
            data["提值空间"] = "未提取"
    
    data["备注"] = ""
    
    # 判断是否为配餐相关回复：至少有2个字段成功提取
    extracted_count = sum(1 for v in data.values() if v and v != "未提取" and v != "" and v not in [user_name, time.strftime("%Y-%m-%d %H:%M"), ""])
    if extracted_count < 2:
        logger.info(f"回复未匹配到足够配餐数据（{extracted_count}个字段），跳过后处理")
        return None
    
    logger.info(f"已提取配餐数据: {data}")
    return data


def post_process_actions(ws, req_id, stream_id, ai_reply, user_name, from_user):
    """AI回复后的自动后处理：写台账 + 建待办 + 生成文档
    
    在process_and_reply发送文字回复后调用，异步执行不阻塞主流程。
    任何一步失败不影响其他步骤。
    """
    # 1. 提取配餐数据
    peican_data = extract_peican_data(ai_reply, user_name, from_user)
    if not peican_data:
        return
    
    action_messages = []  # 收集每步结果，最后统一发一条stream消息
    
    # 2. 写入配餐台账表格
    try:
        sheet_result = wecom_api.append_peican_record(peican_data)
        if sheet_result.get("success"):
            action_messages.append(f"📋 配餐台账已记录：{sheet_result['url']}")
            logger.info(f"配餐台账写入成功: {sheet_result['url']}")
        else:
            logger.warning(f"配餐台账写入失败: {sheet_result.get('error')}")
    except Exception as e:
        logger.error(f"配餐台账写入异常: {e}")
    
    # 3. 创建跟进待办
    try:
        todo_content = f"【配餐跟进】客户{peican_data.get('客户号码', '')}，推荐{peican_data.get('推荐套餐', '')}"
        todo_userid = getattr(config, 'DEFAULT_TODO_USERID', 'sscblizhun')
        todo_result = wecom_api.create_todo(
            content=todo_content,
            follower_userid=todo_userid
        )
        if todo_result.get("success"):
            action_messages.append("✅ 已创建跟进待办")
            logger.info(f"待办创建成功: {todo_result.get('todo_id')}")
        else:
            logger.warning(f"待办创建失败: {todo_result.get('error')}")
    except Exception as e:
        logger.error(f"待办创建异常: {e}")
    
    # 4. 复杂配餐方案生成企微文档
    # 判断是否需要生成文档：AI回复较长（>800字）或包含多级标题
    needs_doc = len(ai_reply) > 800 or ai_reply.count('##') >= 2 or ai_reply.count('#') >= 3
    if needs_doc:
        try:
            # 清理FILE_PATH行
            clean_reply = re.sub(r'FILE_PATH:.+?(?:\n|$)', '', ai_reply).strip()
            doc_name = f"配餐方案_{peican_data.get('客户号码', '客户')}_{time.strftime('%m%d%H%M')}"
            doc_result = wecom_api.create_wecom_doc(doc_name, clean_reply)
            if doc_result.get("success"):
                action_messages.append(f"📄 详细方案已生成文档：{doc_result['url']}")
                logger.info(f"配餐文档生成成功: {doc_result['url']}")
            else:
                logger.warning(f"配餐文档生成失败: {doc_result.get('error')}")
        except Exception as e:
            logger.error(f"配餐文档生成异常: {e}")
    
    # 5. 发送后处理结果通知
    if action_messages:
        try:
            notice = "\n".join(action_messages)
            # 用新的stream消息发送（原stream已finish）
            reply_stream(ws, req_id, notice, stream_id=None, finish=True)
            logger.info(f"后处理通知已发送: {len(action_messages)}条")
        except Exception as e:
            logger.error(f"发送后处理通知失败: {e}")


def process_and_reply(ws, req_id, stream_id, file_paths, text_content, from_user, chat_type="group", chat_id=""):
    """异步处理：调用TeleAgent代理 -> 回复群里（含文件发送）
    file_paths: [(path, type), type为'image'/'file'/'voice'/'video']
    chat_type: 'group'=群聊 / 'single'=私聊
    chat_id: 群聊时的群ID（chatid），用于会话标题分组
    企微stream消息有10分钟超时限制，处理过程中每8分钟发一次心跳保活
    """
    user_name = get_user_name(from_user)

    # 构建prompt：文件路径+文字原文
    prompt = build_prompt(file_paths, text_content, user_name)

    # 会话标题：稳定标识，同一用户/同一群固定一个会话（不再带时间戳，避免一句话开一个会话）
    # 私聊：按 userid 区分；群聊：按 chatid 区分（同群共享一个会话，群与私聊互不干扰）
    if chat_type == "group":
        session_key = chat_id or from_user
        session_title = f"企微|群聊|{session_key}"
    else:
        session_title = f"企微|私聊|{from_user}"

    has_files = bool(file_paths)
    logger.info(f"开始调用TeleAgent, 调用人={user_name}, 有文件={has_files}")

    # 心跳保活线程：每8分钟更新stream消息，防止企微10分钟超时
    heartbeat_stop = threading.Event()
    heartbeat_count = [0]
    def heartbeat():
        while not heartbeat_stop.wait(480):  # 8分钟
            if heartbeat_stop.is_set():
                break
            heartbeat_count[0] += 1
            try:
                reply_stream(ws, req_id, f"正在处理中，请稍候...({heartbeat_count[0]})",
                             stream_id=stream_id, finish=False)
                logger.info(f"心跳保活第{heartbeat_count[0]}次, stream_id={stream_id}")
            except Exception as e:
                logger.error(f"心跳保活发送失败: {e}")
                break
    hb_thread = threading.Thread(target=heartbeat, daemon=True)
    hb_thread.start()

    # 调用TeleAgent代理
    result = call_teleagent(prompt, timeout=1800, session_title=session_title)

    # 停止心跳
    heartbeat_stop.set()

    # 刷新ws引用：重连后旧ws失效，必须用最新的全局连接
    ws = get_ws() or ws
    if not ws or not hasattr(ws, 'sock') or ws.sock is None or not ws.sock.connected:
        logger.error("process_and_reply: WebSocket已断开，无法回复和发送文件")
        # 文件已生成但无法发送，写入待发队列
        file_paths_from_result = extract_file_paths(result) if result else []
        if file_paths_from_result:
            add_pending_files(file_paths_from_result, summary="WebSocket断连期间生成")
            logger.info(f"已将{len(file_paths_from_result)}个文件加入待发队列")
        return

    if not result:
        reply_stream(
            ws, req_id,
            "抱歉，处理超时或出错了。请稍后重试，或直接私聊发给星小辰处理。",
            stream_id=stream_id, finish=True
        )
        logger.error("TeleAgent调用失败，已回复错误消息")
        dashboard.update_message_status(0, "失败")
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
        dashboard.update_message_status(0, "已回复+发文件")
        with dashboard.BOT_STATUS_LOCK:
            dashboard.BOT_STATUS["total_replies"] += 1

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
        dashboard.update_message_status(0, "已回复")
        with dashboard.BOT_STATUS_LOCK:
            dashboard.BOT_STATUS["total_replies"] += 1

    # ========== 配餐后处理：写台账 + 建待办 + 生成文档 ==========
    # 在主回复发送完成后，异步执行后处理动作
    try:
        post_process_actions(ws, req_id, stream_id, result, user_name, from_user)
    except Exception as e:
        logger.error(f"配餐后处理异常: {e}")


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
        dashboard.update_bot_status(pending_files=len(PENDING_FILES))

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
        dashboard.update_bot_status(pending_files=0)


# ========== 消息处理 ==========
# 内置测试指令：用于实机验证 markdown/模板卡片/主动推送等新能力
# 只在单聊生效，避免群聊刷屏
BUILTIN_CMD_MAP = {
    "/md": "markdown 消息测试",
    "/card": "模板卡片测试",
    "/btn": "按钮交互卡片测试",
    "/vote": "投票卡片测试",
    "/multi": "多项选择卡片测试",
    "/push": "主动推送测试",
    "/fd": "反馈事件测试",
    "/table": "智能表格建表测试",
}


def handle_builtin_cmd(ws, req_id, text_content, from_user, chattype):
    """处理内置测试指令，返回 True 表示已处理"""
    cmd = text_content.strip().split()[0] if text_content.strip() else ""
    if cmd not in BUILTIN_CMD_MAP:
        return False
    # 只在单聊或测试群生效，避免打扰
    if chattype == "group" and from_user not in getattr(config, "WECOM_ADMIN_USERIDS", []):
        return False

    if cmd == "/md":
        reply_markdown(ws, req_id, (
            "# 星小辰 Markdown 测试\n"
            "## 标题\n"
            "**加粗** *斜体* `行内代码`\n\n"
            "### 列表\n"
            "- 图片分析\n"
            "- 文件处理\n"
            "- 配餐推荐\n\n"
            "### 表格\n"
            "| 功能 | 状态 |\n"
            "| :--- | :---: |\n"
            "| Markdown | ✅ 可用 |\n"
            "| 模板卡片 | ✅ 可用 |\n"
            "| 主动推送 | ✅ 可用 |\n\n"
            "> 本消息由 **星小辰** 生成"
        ), feedback_id=f"md_{int(time.time())}")
    elif cmd == "/card":
        card = build_text_notice_card(
            main_title={"title": "配餐方案已生成", "desc": "客户 139****0000"},
            sub_title_text="推荐套餐：129元5G融合，月省30元，建议尽快联系客户办理。",
            emphasis_content={"title": "129", "desc": "推荐月费(元)"},
            horizontal_content_list=[
                {"keyname": "当前套餐", "value": "99元不限量"},
                {"keyname": "提值空间", "value": "30元/月"},
                {"keyname": "配餐路径", "value": "平替升级"}
            ],
            card_action={"type": 1, "url": "https://work.weixin.qq.com/"},
            task_id=f"card_{int(time.time())}"
        )
        reply_template_card(ws, req_id, card)
    elif cmd == "/btn":
        card = build_button_interaction_card(
            main_title={"title": "请确认配餐方案", "desc": "客户 139****0000 是否同意升级？"},
            button_list=[
                {"text": "同意办理", "style": 1, "key": "agree"},
                {"text": "再考虑", "style": 2, "key": "consider"},
                {"text": "拒绝", "style": 3, "key": "reject"}
            ],
            task_id=f"btn_{int(time.time())}"
        )
        reply_template_card(ws, req_id, card)
    elif cmd == "/vote":
        card = build_vote_interaction_card(
            main_title={"title": "服务满意度调查", "desc": "您对本次服务是否满意？"},
            checkbox={
                "question_key": "satisfaction",
                "option_list": [
                    {"id": "sat", "text": "满意", "is_checked": False},
                    {"id": "unsat", "text": "不满意", "is_checked": False}
                ],
                "disable": False,
                "mode": 1
            },
            submit_button={"text": "提交", "key": "submit_key"},
            task_id=f"vote_{int(time.time())}"
        )
        reply_template_card(ws, req_id, card)
    elif cmd == "/multi":
        card = build_multiple_interaction_card(
            main_title={"title": "需求调查", "desc": "您最关心哪些功能？"},
            select_list=[{
                "question_key": "features",
                "title": "请选择",
                "disable": False,
                "selected_id": "doc",
                "option_list": [
                    {"id": "doc", "text": "报告生成"},
                    {"id": "sheet", "text": "表格处理"},
                    {"id": "ai", "text": "AI分析"}
                ]
            }],
            submit_button={"text": "提交", "key": "submit_key"},
            task_id=f"multi_{int(time.time())}"
        )
        reply_template_card(ws, req_id, card)
    elif cmd == "/push":
        send_push_message(ws, from_user, "markdown", {
            "content": "这是一条**主动推送**消息，由定时任务或后台触发。"
        }, chat_type=1 if chattype == "single" else 2)
    elif cmd == "/fd":
        # 反馈测试：发一条带 feedback_id 的流式消息，用户可在气泡下方点赞/点踩
        reply_stream(ws, req_id,
                     "这是一条**带反馈**的消息。\n如果你觉得这条消息有帮助，请在下方点【准确】；\n如果觉得不准确，点【不准确】并选择原因。\n\n这条消息的反馈ID会触发 feedback_event 事件。",
                     finish=True, feedback_id=f"fd_{int(time.time())}")
    elif cmd == "/table":
        # 智能表格建表测试：MCP 方式一键创建带表头的智能表格
        try:
            reply_stream(ws, req_id, "正在创建智能表格，请稍候...", finish=False)
            result = wecom_api.create_smart_sheet_with_headers(
                f"星小辰配餐台账_{time.strftime('%m%d%H%M')}",
                ["时间", "处理人", "客户号码", "金额", "备注"]
            )
            if result.get("success"):
                reply_stream(ws, req_id,
                             f"智能表格创建成功！\n表格地址：{result.get('url', '')}\n\n"
                             f"表格名称：配餐台账\n表头：时间 / 处理人 / 客户号码 / 金额 / 备注\n\n"
                             f"发「写入一行测试数据」可继续验证写入。",
                             finish=True)
                logger.info(f"智能表格建表成功: docid={result.get('docid')} sheet_id={result.get('sheet_id')}")
            else:
                reply_stream(ws, req_id, f"智能表格创建失败：{result.get('error', '未知错误')}", finish=True)
                logger.error(f"智能表格建表失败: {result.get('error')}")
        except Exception as e:
            logger.error(f"/table 指令异常: {e}")
            reply_stream(ws, req_id, f"智能表格创建异常：{e}", finish=True)
    return True


def handle_text_message(ws, msg, req_id):
    """处理文字消息：统一走TeleAgent代理回复"""
    body = msg.get("body", {})
    from_user = body.get("from", {}).get("userid", "unknown")
    text_content = body.get("text", {}).get("content", "")
    chattype = body.get("chattype", "single")
    chat_id = body.get("chatid", "")

    logger.info(f"收到文字消息: from={from_user}, chattype={chattype}, content={text_content[:50]}")
    user_name = get_user_name(from_user)
    dashboard.add_message_record("text", user_name, text_content[:80], "处理中", scene=chattype)

    # 先检查内置测试指令
    if handle_builtin_cmd(ws, req_id, text_content, from_user, chattype):
        dashboard.update_message_status(0, "已回复(指令)")
        return

    # 先检查待发文件队列
    if flush_pending_files(ws, req_id):
        logger.info("待发文件已发送，跳过当前消息处理")
        return

    # 先回复收到
    stream_id = reply_stream(ws, req_id, "收到，正在处理...", finish=False)
    # 异步调用TeleAgent
    thread = threading.Thread(
        target=process_and_reply,
        args=(ws, req_id, stream_id, [], text_content, from_user, chattype, chat_id),
        daemon=True
    )
    thread.start()


def handle_image_message(ws, msg, req_id):
    """处理图片消息：下载解密 -> 走TeleAgent代理（含看图+配餐）"""
    body = msg.get("body", {})
    from_user = body.get("from", {}).get("userid", "unknown")
    chattype = body.get("chattype", "single")
    chat_id = body.get("chatid", "")
    image_info = body.get("image", {})
    url = image_info.get("url", "")
    aeskey = image_info.get("aeskey", "")

    logger.info(f"收到图片消息: from={from_user}, chattype={chattype}")
    user_name = get_user_name(from_user)
    dashboard.add_message_record("image", user_name, "图片消息", "处理中", scene=chattype)

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
        args=(ws, req_id, stream_id, [(image_path, 'image')], "", from_user, chattype, chat_id),
        daemon=True
    )
    thread.start()


def handle_file_message(ws, msg, req_id):
    """处理文件消息：下载解密 -> 走TeleAgent代理"""
    body = msg.get("body", {})
    from_user = body.get("from", {}).get("userid", "unknown")
    chattype = body.get("chattype", "single")
    chat_id = body.get("chatid", "")
    file_info = body.get("file", {})
    url = file_info.get("url", "")
    aeskey = file_info.get("aeskey", "")
    filename = file_info.get("filename", "unknown_file")

    logger.info(f"收到文件消息: from={from_user}, chattype={chattype}, filename={filename}")
    user_name = get_user_name(from_user)
    dashboard.add_message_record("file", user_name, f"文件: {filename}", "处理中", scene=chattype)

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
        args=(ws, req_id, stream_id, [(filepath, 'file')], "", from_user, chattype, chat_id),
        daemon=True
    )
    thread.start()


def handle_voice_message(ws, msg, req_id):
    """处理语音消息：下载解密 -> 走TeleAgent代理（offline_asr转写分析）"""
    body = msg.get("body", {})
    from_user = body.get("from", {}).get("userid", "unknown")
    chattype = body.get("chattype", "single")
    chat_id = body.get("chatid", "")
    voice_info = body.get("voice", {})
    url = voice_info.get("url", "")
    aeskey = voice_info.get("aeskey", "")

    logger.info(f"收到语音消息: from={from_user}, chattype={chattype}")
    user_name = get_user_name(from_user)
    dashboard.add_message_record("voice", user_name, "语音消息", "处理中", scene=chattype)

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
        args=(ws, req_id, stream_id, [(filepath, 'voice')], "", from_user, chattype, chat_id),
        daemon=True
    )
    thread.start()


def handle_video_message(ws, msg, req_id):
    """处理视频消息：下载解密 -> 走TeleAgent代理"""
    body = msg.get("body", {})
    from_user = body.get("from", {}).get("userid", "unknown")
    chattype = body.get("chattype", "single")
    chat_id = body.get("chatid", "")
    video_info = body.get("video", {})
    url = video_info.get("url", "")
    aeskey = video_info.get("aeskey", "")

    logger.info(f"收到视频消息: from={from_user}, chattype={chattype}")
    user_name = get_user_name(from_user)
    dashboard.add_message_record("video", user_name, "视频消息", "处理中", scene=chattype)

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
        args=(ws, req_id, stream_id, [(filepath, 'video')], "", from_user, chattype, chat_id),
        daemon=True
    )
    thread.start()


def handle_mixed_message(ws, msg, req_id):
    """处理图文混排消息（群聊@机器人发图的主要类型）"""
    body = msg.get("body", {})
    from_user = body.get("from", {}).get("userid", "unknown")
    chattype = body.get("chattype", "single")
    chat_id = body.get("chatid", "")
    mixed_info = body.get("mixed", {})
    msg_items = mixed_info.get("msg_item", [])

    logger.info(f"收到图文混排消息: from={from_user}, chattype={chattype}, items={len(msg_items)}")
    logger.info(f"mixed消息原文: {json.dumps(body, ensure_ascii=False)[:1000]}")
    user_name = get_user_name(from_user)
    # 预览内容
    text_parts_preview = [item.get("text", {}).get("content", "") for item in msg_items if item.get("msgtype") == "text"]
    preview = " ".join(text_parts_preview)[:80] if text_parts_preview else f"{len(msg_items)}个附件"
    dashboard.add_message_record("mixed", user_name, preview, "处理中", scene=chattype)

    # 先检查待发文件队列
    if flush_pending_files(ws, req_id):
        logger.info("待发文件已发送，跳过mixed消息处理")
        return

    # 先回复收到
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
        args=(ws, req_id, stream_id, file_paths, text_content, from_user, chattype, chat_id),
        daemon=True
    )
    thread.start()


# ========== WebSocket 事件处理 ==========
def on_open(ws):
    """连接建立后发送订阅请求"""
    logger.info("WebSocket 连接已建立，发送订阅请求...")
    dashboard.update_bot_status(online=True, subscribed=False, connect_time=time.time())
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
            from_user = body_dict.get("from", {}).get("userid", "")
            user_name = get_user_name(from_user) if from_user else "未知"
            chattype = body_dict.get("chattype", "")

            if eventtype == "enter_chat":
                logger.info(f"用户进入会话: {from_user}")
                dashboard.add_message_record("event", user_name, "进入会话", "已回复欢迎语", scene=chattype)
                # 回复欢迎语（模板卡片形式，带操作按钮）
                welcome_card = build_text_notice_card(
                    main_title={"title": "欢迎使用星小辰", "desc": "您的AI办公助手已上线"},
                    sub_title_text="我可以帮你分析配餐方案、处理图片文件、生成报告文档，发消息即可开始体验。",
                    card_action={"type": 1, "url": "https://work.weixin.qq.com/"},
                    horizontal_content_list=[
                        {"keyname": "图片分析", "value": "发图片试试"},
                        {"keyname": "文件处理", "value": "发文档试试"},
                        {"keyname": "配餐推荐", "value": "说出需求"}
                    ],
                    task_id=f"welcome_{int(time.time())}"
                )
                reply_welcome(ws, msgtype="template_card", card=welcome_card)
            elif eventtype == "disconnected_event":
                logger.warning("收到连接断开事件，停止心跳等待重连")
                stop_heartbeat()
            elif eventtype == "template_card_event":
                # 用户点击了模板卡片按钮/选择项，需在5秒内回复更新卡片
                card_event = event.get("template_card_event", {})
                card_type = card_event.get("card_type", "")
                event_key = card_event.get("event_key", "")
                task_id = card_event.get("task_id", "")
                selected_items = card_event.get("selected_items", {})
                logger.info(f"收到模板卡片事件: card_type={card_type}, event_key={event_key}, task_id={task_id}")
                dashboard.add_message_record("event", user_name, f"卡片点击: {event_key}", "已处理", scene=chattype)
                try:
                    # 构造更新后的卡片（把按钮置灰/标记已选，防止重复点击）
                    updated_card = {
                        "card_type": card_type,
                        "task_id": task_id,
                        "main_title": {"title": "已收到您的选择", "desc": f"您点击了: {event_key}"},
                        "sub_title_text": "感谢反馈，如需继续操作请重新发送消息。"
                    }
                    update_template_card(ws, req_id, updated_card)
                except Exception as e:
                    logger.error(f"更新模板卡片失败: {e}")
            elif eventtype == "feedback_event":
                # 用户对AI回复点赞/点踩
                feedback = event.get("feedback_event", {})
                fb_id = feedback.get("id", "")
                fb_type = feedback.get("type", 0)
                content = feedback.get("content", "")
                reason_list = feedback.get("inaccurate_reason_list", [])
                type_map = {1: "准确", 2: "不准确", 3: "取消准确/不准确"}
                reason_map = {1: "与问题无关", 2: "内容不完整", 3: "内容有错误", 4: "数据分析错误"}
                reasons = "、".join([reason_map.get(r, str(r)) for r in reason_list])
                logger.info(
                    f"收到用户反馈: id={fb_id}, type={type_map.get(fb_type, fb_type)}, "
                    f"content={content}, reasons={reasons}"
                )
                fb_preview = f"反馈[{type_map.get(fb_type, fb_type)}]"
                if content:
                    fb_preview += f": {content[:30]}"
                if reasons:
                    fb_preview += f" ({reasons})"
                dashboard.add_message_record("event", user_name, fb_preview, "已记录", scene=chattype)
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
                dashboard.update_bot_status(online=True, subscribed=True, connect_time=time.time())
                if not heartbeat_running:
                    start_heartbeat(ws)
            else:
                # 所有其他无cmd响应视为心跳响应
                dashboard.update_bot_status(last_heartbeat=time.time())
                logger.debug("心跳响应 OK")
            return

        logger.info(f"未处理的消息: cmd={cmd}, errcode={errcode}")

    except Exception as e:
        logger.error(f"处理消息异常: {e}", exc_info=True)


def on_error(ws, error):
    """WebSocket 错误"""
    logger.error(f"WebSocket 错误: {error}")
    with dashboard.BOT_STATUS_LOCK:
        dashboard.BOT_STATUS["total_errors"] += 1


def on_close(ws, close_status_code, close_msg):
    """WebSocket 连接关闭"""
    global reconnect_count
    logger.warning(f"WebSocket 连接关闭: code={close_status_code}, msg={close_msg}")
    dashboard.update_bot_status(online=False, subscribed=False)
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
    logger.info(f"正在连接企业微信长连接: {WS_URL}")

    new_ws = websocket.WebSocketApp(
        WS_URL,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    # 更新全局ws引用（线程安全）
    set_ws(new_ws)

    new_ws.run_forever(
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
    logger.info(f"管理面板: http://127.0.0.1:{DASHBOARD_PORT}")

    # 启动 Dashboard 管理面板（后台线程）
    dashboard_thread = threading.Thread(
        target=dashboard.run_dashboard,
        args=(DASHBOARD_PORT,),
        daemon=True
    )
    dashboard_thread.start()
    logger.info(f"Dashboard 管理面板已启动（端口 {DASHBOARD_PORT}）")

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
        dashboard.update_bot_status(reconnect_count=reconnect_count)
        if reconnect_count > MAX_RECONNECT:
            logger.error(f"重连次数超过上限({MAX_RECONNECT})，退出")
            break

        delay = min(2 ** min(reconnect_count, 6), 60)
        logger.info(f"将在 {delay} 秒后重连（第 {reconnect_count} 次）")
        time.sleep(delay)
