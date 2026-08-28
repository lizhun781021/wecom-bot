#!/usr/bin/env python3
"""
企微机器人 Web 管理面板
轻量级 HTTP 服务，提供状态监控、消息记录、实时日志查看
手机/电脑浏览器均可访问
"""

import os
import sys
import re
import json
import time
import threading
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# 项目目录
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(PROJECT_DIR, 'wecom-bot.log')
QQ_STATUS_FILE = os.path.join(PROJECT_DIR, 'qq_status.json')
QQ_LOG_FILE = os.path.join(PROJECT_DIR, 'qq-adapter-app.log')
QQ_PUSH_PORT = 18506  # QQ 适配器内部推送端点（与 qq_official_adapter.py 保持一致）
ZMX_STATUS_FILE = os.path.join(PROJECT_DIR, 'zmx_status.json')
ZMX_MESSAGES_FILE = os.path.join(PROJECT_DIR, 'zmx_messages.json')
FEISHU_STATUS_FILE = os.path.join(PROJECT_DIR, 'feishu_status.json')
FEISHU_MESSAGES_FILE = os.path.join(PROJECT_DIR, 'feishu_messages.json')
QQ_PENDING_CONFIRM_FILE = os.path.join(PROJECT_DIR, 'qq_pending_confirmations.json')

# ========== 共享状态（由 server.py 写入，dashboard 读取）==========
# 消息记录列表，每条: {"time": "10:30:45", "type": "text/image/mixed", "user": "李准", "preview": "...", "status": "处理中/已回复/失败"}
MESSAGE_RECORDS = []
MESSAGE_RECORDS_LOCK = threading.Lock()
MAX_MESSAGE_RECORDS = 100

# 机器人状态
BOT_STATUS = {
    "online": False,           # WebSocket 是否连接
    "subscribed": False,       # 是否订阅成功
    "connect_time": 0,         # 本次连接时间戳
    "last_heartbeat": 0,       # 最后心跳时间
    "reconnect_count": 0,      # 重连次数
    "total_messages": 0,       # 总收到消息数
    "total_replies": 0,        # 总回复消息数
    "total_errors": 0,         # 总错误数
    "pending_files": 0,        # 待发文件数
}
BOT_STATUS_LOCK = threading.Lock()

# ========== 日志捕获（最近N行）==========
RECENT_LOGS = []
RECENT_LOGS_LOCK = threading.Lock()
MAX_RECENT_LOGS = 200


class DashboardLogHandler(logging.Handler):
    """自定义日志处理器，捕获日志到内存列表"""
    def emit(self, record):
        try:
            msg = self.format(record)
            with RECENT_LOGS_LOCK:
                RECENT_LOGS.append(msg)
                if len(RECENT_LOGS) > MAX_RECENT_LOGS:
                    del RECENT_LOGS[:len(RECENT_LOGS) - MAX_RECENT_LOGS]
        except Exception:
            pass


def setup_log_capture():
    """挂载日志捕获器到主 logger"""
    handler = DashboardLogHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger = logging.getLogger()
    logger.addHandler(handler)


def add_message_record(msg_type, user, preview, status="处理中", scene=""):
    """添加一条消息记录（供 server.py 调用）

    scene: 来源场景，'group'=群聊 / 'single'=私聊 / ''=未知（显示'-'）
    """
    with MESSAGE_RECORDS_LOCK:
        MESSAGE_RECORDS.insert(0, {
            "time": time.strftime("%H:%M:%S"),
            "full_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "type": msg_type,
            "user": user,
            "preview": preview[:80] if preview else "",
            "status": status,
            "scene": scene if scene in ("group", "single") else ""
        })
        if len(MESSAGE_RECORDS) > MAX_MESSAGE_RECORDS:
            del MESSAGE_RECORDS[MAX_MESSAGE_RECORDS:]
    # total_messages 不在这里累加，由回填或 server.py 统计


def update_message_status(index, status):
    """更新消息记录状态"""
    with MESSAGE_RECORDS_LOCK:
        if 0 <= index < len(MESSAGE_RECORDS):
            MESSAGE_RECORDS[index]["status"] = status


def update_bot_status(**kwargs):
    """更新机器人状态（供 server.py 调用）"""
    with BOT_STATUS_LOCK:
        BOT_STATUS.update(kwargs)


def get_bot_status():
    """获取机器人状态（附加计算字段）"""
    with BOT_STATUS_LOCK:
        s = BOT_STATUS.copy()
    now = time.time()
    if s.get("connect_time"):
        uptime = int(now - s["connect_time"])
        h, rem = divmod(uptime, 3600)
        m, sec = divmod(rem, 60)
        s["uptime"] = f"{h}h {m}m {sec}s"
    else:
        s["uptime"] = "-"
    if s.get("last_heartbeat"):
        s["heartbeat_ago"] = f"{int(now - s['last_heartbeat'])}s前"
    else:
        s["heartbeat_ago"] = "-"
    return s


def get_qq_status():
    """读取 QQ 适配器状态（跨进程，从 qq_status.json 读取）"""
    default = {
        "running": False,
        "connected": False,
        "last_message_at": "",
        "last_error": "",
        "total_received": 0,
        "total_replied": 0,
        "updated_at": "",
        "session": {"group": {}, "user": {}},
    }
    try:
        if not os.path.exists(QQ_STATUS_FILE):
            return default
        with open(QQ_STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        result = dict(default)
        result.update(data.get("status", {}))
        result["session"] = data.get("session", {"group": {}, "user": {}})
        result["updated_at"] = data.get("updated_at", "")
        # 附带 QQ openid → 昵称映射，供前端会话下拉显示可读名
        try:
            import config as _cfg
            result["user_map"] = getattr(_cfg, 'QQ_USER_MAP', {})
        except Exception:
            result["user_map"] = {}
        return result
    except Exception:
        return default


def get_zmx_status():
    """读取量子密信适配器状态（跨进程，从 zmx_status.json 读取）"""
    default = {
        "running": False,
        "listening": False,
        "last_message_at": "",
        "last_error": "",
        "total_received": 0,
        "total_replied": 0,
        "total_errors": 0,
        "total_attachments": 0,
        "updated_at": "",
        "session": {"group": {}, "user": {}},
    }
    try:
        if not os.path.exists(ZMX_STATUS_FILE):
            return default
        with open(ZMX_STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        result = dict(default)
        result.update(data.get("status", {}))
        result["session"] = data.get("session", {"group": {}, "user": {}})
        result["updated_at"] = data.get("updated_at", "")
        return result
    except Exception:
        return default


def get_feishu_status():
    """读取飞书适配器状态（跨进程，从 feishu_status.json 读取）"""
    default = {
        "running": False,
        "connected": False,
        "last_message_at": "",
        "last_error": "",
        "total_received": 0,
        "total_replied": 0,
        "updated_at": "",
        "session": {"group": {}, "user": {}},
    }
    try:
        if not os.path.exists(FEISHU_STATUS_FILE):
            return default
        with open(FEISHU_STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        result = dict(default)
        result.update(data.get("status", {}))
        result["session"] = data.get("session", {"group": {}, "user": {}})
        result["updated_at"] = data.get("updated_at", "")
        return result
    except Exception:
        return default


def get_feishu_messages(n=50):
    """获取飞书最近N条消息记录"""
    try:
        if not os.path.exists(FEISHU_MESSAGES_FILE):
            return []
        with open(FEISHU_MESSAGES_FILE, "r", encoding="utf-8") as f:
            records = json.load(f)
        return records[:n]
    except Exception:
        return []


def get_qq_pending_confirmations():
    """读取 QQ 待确认权限请求列表（跨进程，从 qq_pending_confirmations.json 读取）"""
    try:
        if not os.path.exists(QQ_PENDING_CONFIRM_FILE):
            return []
        with open(QQ_PENDING_CONFIRM_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        now = time.time()
        result = []
        for k, v in data.items():
            age = now - v.get("time", 0)
            if age > 1800:
                continue
            result.append({
                "session_title": k,
                "conf_id": v.get("conf_id", ""),
                "type": v.get("type", "permission"),
                "from_user": v.get("from_user", ""),
                "is_group": v.get("is_group", False),
                "notice_text": (v.get("notice_text", "") or "")[:200],
                "notice_sent": v.get("notice_sent", True),
                "retry_count": v.get("retry_count", 0),
                "age_seconds": int(age),
            })
        return result
    except Exception:
        return []


def get_zmx_messages(n=50):
    """获取量子密信最近N条消息记录"""
    try:
        if not os.path.exists(ZMX_MESSAGES_FILE):
            return []
        with open(ZMX_MESSAGES_FILE, "r", encoding="utf-8") as f:
            records = json.load(f)
        return records[:n]
    except Exception:
        return []


def get_recent_logs(n=50):
    """获取最近N行日志（内存 + QQ 适配器日志文件 + 量子密信适配器日志文件）"""
    with RECENT_LOGS_LOCK:
        lines = list(reversed(RECENT_LOGS[-n:]))
    # 合并 QQ 适配器日志文件尾部（独立进程，通过文件共享）
    qq_lines = _read_qq_log_tail(n)
    # 合并量子密信适配器日志文件尾部
    zmx_lines = _read_zmx_log_tail(n)
    merged = zmx_lines + qq_lines + lines
    return merged[-n:]


def _read_qq_log_tail(n=30):
    """读取 QQ 适配器日志文件末尾 N 行（带 [QQ] 前缀便于区分）"""
    try:
        if not os.path.exists(QQ_LOG_FILE):
            return []
        with open(QQ_LOG_FILE, 'r', encoding='utf-8') as f:
            all_lines = f.readlines()
        tail = all_lines[-n:]
        return [ln.rstrip('\n') for ln in tail if ln.strip()]
    except Exception:
        return []


def _read_zmx_log_tail(n=30):
    """读取量子密信适配器日志文件末尾 N 行（带 [ZMX] 前缀便于区分）"""
    zmx_log_file = os.path.join(PROJECT_DIR, 'zmx-adapter.log')
    try:
        if not os.path.exists(zmx_log_file):
            return []
        with open(zmx_log_file, 'r', encoding='utf-8') as f:
            all_lines = f.readlines()
        tail = all_lines[-n:]
        result = []
        for ln in tail:
            if ln.strip():
                clean_line = ln.rstrip('\n')
                result.append(f"[ZMX] {clean_line}")
        return result
    except Exception:
        return []


def get_message_records(n=50):
    """获取最近N条消息记录（企微内存记录 + QQ 落盘记录 + 量子密信落盘记录合并，按时间倒序）"""
    # QQ 消息来自独立进程（qq_official_adapter.py），落盘到 qq_messages.json
    qq_records = []
    qq_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'qq_messages.json')
    try:
        if os.path.exists(qq_file):
            with open(qq_file, 'r', encoding='utf-8') as f:
                qq_records = json.load(f)
    except Exception:
        qq_records = []
    
    # 量子密信消息来自独立进程（zmx_adapter.py），落盘到 zmx_messages.json
    zmx_records = []
    try:
        if os.path.exists(ZMX_MESSAGES_FILE):
            with open(ZMX_MESSAGES_FILE, 'r', encoding='utf-8') as f:
                zmx_records = json.load(f)
            # 给量子密信消息添加来源标记
            for rec in zmx_records:
                rec['source'] = 'zmx'
    except Exception:
        zmx_records = []
    
    with MESSAGE_RECORDS_LOCK:
        merged = qq_records + zmx_records + MESSAGE_RECORDS
    # 排序：优先用完整时间（YYYY-MM-DD HH:MM:SS），旧数据只有 HH:MM:SS 则补当天日期
    today = time.strftime("%Y-%m-%d")
    def _sort_key(r):
        ft = r.get('full_time')
        if ft:
            return ft
        t = r.get('time', '')
        return f"{today} {t}" if t else ""
    merged.sort(key=_sort_key, reverse=True)
    return merged[:n]


# ========== HTML 页面 ==========
HTML_PAGE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>企微+QQ+量子密信机器人管理面板</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; background: #0f1923; color: #e0e0e0; min-height: 100vh; }
.header { background: linear-gradient(135deg, #1a2a3a 0%, #0d1b2a 100%); padding: 20px; border-bottom: 1px solid #2a3a4a; }
.header h1 { font-size: 22px; color: #00b4d8; display: flex; align-items: center; gap: 10px; }
.status-dot { width: 12px; height: 12px; border-radius: 50%; display: inline-block; }
.status-online { background: #4ade80; box-shadow: 0 0 8px #4ade80; }
.status-offline { background: #ef4444; box-shadow: 0 0 8px #ef4444; }
.container { max-width: 1200px; margin: 0 auto; padding: 20px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 24px; }
.card { background: #1a2a3a; border: 1px solid #2a3a4a; border-radius: 12px; padding: 20px; }
.card-label { font-size: 13px; color: #8a9aaa; margin-bottom: 6px; }
.card-value { font-size: 24px; font-weight: 600; color: #e0e0e0; }
.card-value.green { color: #4ade80; }
.card-value.red { color: #ef4444; }
.card-value.blue { color: #00b4d8; }
.card-value.yellow { color: #fbbf24; }
.section { background: #1a2a3a; border: 1px solid #2a3a4a; border-radius: 12px; margin-bottom: 20px; overflow: hidden; }
.section-header { padding: 14px 20px; border-bottom: 1px solid #2a3a4a; display: flex; justify-content: space-between; align-items: center; }
.section-title { font-size: 15px; color: #00b4d8; font-weight: 600; }
.badge { background: #2a3a4a; color: #8a9aaa; padding: 2px 10px; border-radius: 20px; font-size: 12px; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th { background: #0d1b2a; padding: 10px 16px; text-align: left; font-size: 13px; color: #8a9aaa; font-weight: 500; white-space: nowrap; }
td { padding: 10px 16px; border-top: 1px solid #2a3a4a; font-size: 13px; }
tr:hover { background: #162232; }
.tag { padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 500; }
.tag-text { background: #1e3a5f; color: #60a5fa; }
.tag-image { background: #3b1e3f; color: #c084fc; }
.tag-mixed { background: #1e3b1e; color: #4ade80; }
.tag-file { background: #3b3b1e; color: #fbbf24; }
.tag-voice { background: #1e2b3b; color: #67e8f9; }
.tag-video { background: #3b1e1e; color: #f87171; }
.tag-event { background: #3b301e; color: #fbbf24; }
.tag-qq { background: #1e3a2f; color: #34d399; }
.tag-zmx { background: #2a1e3f; color: #a78bfa; }
.tag-group { background: #1e3a5f; color: #60a5fa; }
.tag-single { background: #3b1e3f; color: #c084fc; }
.status-ok { color: #4ade80; }
.status-processing { color: #fbbf24; }
.status-fail { color: #ef4444; }
.log-box { max-height: 500px; overflow-y: auto; padding: 12px 16px; font-family: "SF Mono", "Consolas", monospace; font-size: 12px; line-height: 1.6; }
.log-line { white-space: pre-wrap; word-break: break-all; }
.log-INFO { color: #c0c0c0; }
.log-ERROR { color: #ef4444; }
.log-WARNING { color: #fbbf24; }
.log-DEBUG { color: #60a5fa; }
/* 能力说明 */
.cap-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; margin-bottom: 20px; }
.cap-card { background: #1a2a3a; border: 1px solid #2a3a4a; border-radius: 12px; padding: 18px 20px; }
.cap-card .cap-head { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.cap-card .cap-title { font-size: 15px; font-weight: 600; }
.cap-card .cap-sub { font-size: 12px; color: #8a9aaa; }
.cap-qq .cap-head .cap-title { color: #34d399; }
.cap-qq .cap-head .cap-icon { background: #1e3a2f; color: #34d399; }
.cap-wecom .cap-head .cap-title { color: #60a5fa; }
.cap-wecom .cap-head .cap-icon { background: #1e3a5f; color: #60a5fa; }
.cap-zmx .cap-head .cap-title { color: #a78bfa; }
.cap-zmx .cap-head .cap-icon { background: #2a1e3f; color: #a78bfa; }
.cap-icon { width: 34px; height: 34px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 18px; flex-shrink: 0; }
.cap-item { display: flex; gap: 10px; padding: 8px 0; border-bottom: 1px dashed #263646; font-size: 13px; }
.cap-item:last-child { border-bottom: none; }
.cap-item .cap-label { color: #00b4d8; min-width: 72px; flex-shrink: 0; font-weight: 500; }
.cap-item .cap-desc { color: #c0c8d0; line-height: 1.5; }
.cap-item .cap-desc b { color: #e0e0e0; }
.cap-note { background: #162232; border-left: 3px solid #f59e0b; padding: 10px 14px; border-radius: 0 8px 8px 0; font-size: 12px; color: #c0c8d0; margin: 12px 0; line-height: 1.6; }
.footer { text-align: center; padding: 20px; color: #4a5a6a; font-size: 12px; }
.refresh-btn { background: #00b4d8; color: #fff; border: none; padding: 6px 16px; border-radius: 6px; cursor: pointer; font-size: 13px; }
.refresh-btn:hover { background: #0096c7; }
.auto-badge { font-size: 12px; color: #4ade80; }
.tabs { display: flex; gap: 8px; margin-bottom: 20px; flex-wrap: wrap; }
.tab-btn { background: #162232; color: #8a9aaa; border: 1px solid #2a3a4a; padding: 10px 24px; border-radius: 10px; cursor: pointer; font-size: 14px; font-weight: 500; transition: all .2s; }
.tab-btn:hover { color: #e0e0e0; border-color: #00b4d8; }
.tab-btn.active { background: #00b4d8; color: #fff; border-color: #00b4d8; box-shadow: 0 2px 12px rgba(0,180,216,.25); }
.tab-panel { display: none; }
.tab-panel.active { display: block; }
@media (max-width: 640px) {
  .header h1 { font-size: 18px; }
  .card-value { font-size: 18px; }
  .container { padding: 12px; }
  .grid { grid-template-columns: repeat(2, 1fr); gap: 10px; }
  .card { padding: 14px; }
  th, td { padding: 8px 10px; font-size: 12px; }
}
</style>
</head>
<body>
<div class="header">
  <h1><span id="dot" class="status-dot status-offline"></span>企微+QQ+量子密信+飞书机器人管理面板</h1>
</div>
<div class="container">
  <!-- 状态卡片：企微通道 -->
  <div class="section-header" style="border:none;padding:0 0 8px 0;"><span class="section-title">企微通道</span></div>
  <div class="grid" id="statusGrid">
    <div class="card"><div class="card-label">连接状态</div><div class="card-value" id="v-conn">离线</div></div>
    <div class="card"><div class="card-label">运行时长</div><div class="card-value blue" id="v-uptime">-</div></div>
    <div class="card"><div class="card-label">心跳</div><div class="card-value" id="v-heartbeat">-</div></div>
    <div class="card"><div class="card-label">重连次数</div><div class="card-value yellow" id="v-reconnect">0</div></div>
    <div class="card"><div class="card-label">收到消息</div><div class="card-value green" id="v-msgs">0</div></div>
    <div class="card"><div class="card-label">待发文件</div><div class="card-value" id="v-pending">0</div></div>
  </div>

  <!-- 状态卡片：QQ通道 -->
  <div class="section-title" style="margin-top:4px;padding:0 0 8px 0;"><span class="section-title">QQ通道</span></div>
  <div class="grid" id="qqStatusGrid">
    <div class="card"><div class="card-label">连接状态</div><div class="card-value" id="q-conn">离线</div></div>
    <div class="card"><div class="card-label">收到消息</div><div class="card-value green" id="q-msgs">0</div></div>
    <div class="card"><div class="card-label">已回复</div><div class="card-value green" id="q-replies">0</div></div>
    <div class="card"><div class="card-label">最后消息</div><div class="card-value blue" id="q-last">-</div></div>
    <div class="card"><div class="card-label">最近群会话</div><div class="card-value yellow" id="q-groups">0</div></div>
    <div class="card"><div class="card-label">最近单聊会话</div><div class="card-value yellow" id="q-users">0</div></div>
  </div>

  <!-- 待确认权限请求看板 -->
  <div id="qqPendingSection" style="display:none;margin-top:4px;">
    <div class="section-title" style="padding:0 0 8px 0;"><span class="section-title">待确认权限请求</span> <span class="badge yellow" id="qp-count">0</span></div>
    <div id="qqPendingList" style="display:flex;flex-direction:column;gap:8px;"></div>
  </div>

  <!-- 状态卡片：量子密信通道 -->
  <div class="section-title" style="margin-top:4px;padding:0 0 8px 0;"><span class="section-title">量子密信通道</span></div>
  <div class="grid" id="zmxStatusGrid">
    <div class="card"><div class="card-label">监听状态</div><div class="card-value" id="z-conn">离线</div></div>
    <div class="card"><div class="card-label">收到消息</div><div class="card-value green" id="z-msgs">0</div></div>
    <div class="card"><div class="card-label">已回复</div><div class="card-value green" id="z-replies">0</div></div>
    <div class="card"><div class="card-label">附件发送</div><div class="card-value yellow" id="z-attachments">0</div></div>
    <div class="card"><div class="card-label">最后消息</div><div class="card-value blue" id="z-last">-</div></div>
    <div class="card"><div class="card-label">错误数</div><div class="card-value red" id="z-errors">0</div></div>
  </div>

  <!-- 状态卡片：飞书通道 -->
  <div class="section-title" style="margin-top:4px;padding:0 0 8px 0;"><span class="section-title">飞书通道</span></div>
  <div class="grid" id="feishuStatusGrid">
    <div class="card"><div class="card-label">连接状态</div><div class="card-value" id="f-conn">离线</div></div>
    <div class="card"><div class="card-label">收到消息</div><div class="card-value green" id="f-msgs">0</div></div>
    <div class="card"><div class="card-label">已回复</div><div class="card-value green" id="f-replies">0</div></div>
    <div class="card"><div class="card-label">最后消息</div><div class="card-value blue" id="f-last">-</div></div>
    <div class="card"><div class="card-label">最近群会话</div><div class="card-value yellow" id="f-groups">0</div></div>
    <div class="card"><div class="card-label">最近单聊会话</div><div class="card-value yellow" id="f-users">0</div></div>
  </div>

  <!-- Tab 菜单 -->
  <div class="tabs">
    <button class="tab-btn" data-tab="push" onclick="switchTab('push')">主动推送</button>
    <button class="tab-btn" data-tab="messages" onclick="switchTab('messages')">消息记录</button>
    <button class="tab-btn" data-tab="logs" onclick="switchTab('logs')">实时日志</button>
    <button class="tab-btn" data-tab="caps" onclick="switchTab('caps')">能力说明</button>
  </div>

  <!-- 主动推送 -->
  <div id="tab-push" class="tab-panel active">
  <div class="section">
    <div class="section-header">
      <span class="section-title">主动推送消息</span>
      <span class="badge" id="push-status">就绪</span>
    </div>
    <div style="padding: 20px;">
      <div style="margin-bottom: 12px;">
        <label style="font-size:13px;color:#8a9aaa;margin-right:10px;">推送目标</label>
        <select id="push-target" style="background:#0d1b2a;color:#e0e0e0;border:1px solid #2a3a4a;border-radius:6px;padding:6px 12px;font-size:13px;">
          <option value="group">企微群聊 (Webhook)</option>
          <option value="user">企微个人 (应用消息)</option>
          <option value="qq_group">QQ群 (官方机器人)</option>
          <option value="qq_user">QQ私聊 (官方机器人)</option>
          <option value="zmx_group">量子密信群聊 (Webhook)</option>
        </select>
        <input id="push-userid" type="text" placeholder="userid / QQ openid / 量子密信群ID（个人/私聊/量子密信模式填）" style="display:none;background:#0d1b2a;color:#e0e0e0;border:1px solid #2a3a4a;border-radius:6px;padding:6px 12px;font-size:13px;margin-left:10px;width:280px;">
        <select id="push-qq-session" style="display:none;background:#0d1b2a;color:#e0e0e0;border:1px solid #2a3a4a;border-radius:6px;padding:6px 12px;font-size:13px;margin-left:10px;max-width:260px;" onchange="document.getElementById('push-userid').value=this.value">
          <option value="">-- 最近会话快捷选择 --</option>
        </select>
      </div>
      <div id="push-qq-tip" style="margin-bottom: 12px; display:none; font-size:12px; color:#f59e0b;">
        QQ 群聊已不支持主动推送，需群内最近 5 分钟内有 @ 机器人才能下发（文本/图片/文件均可）。若提示无有效 @，请先在群内 @ 一下机器人再重试。
      </div>
      <div id="push-zmx-tip" style="margin-bottom: 12px; display:none; font-size:12px; color:#a78bfa;">
        量子密信群聊支持文本/Markdown/图片/文件推送，需填写群ID (groupId)。先上传附件获取fileId再发送（两步式）。
      </div>
      <div style="margin-bottom: 12px;">
        <label style="font-size:13px;color:#8a9aaa;margin-right:10px;">消息格式</label>
        <select id="push-format" style="background:#0d1b2a;color:#e0e0e0;border:1px solid #2a3a4a;border-radius:6px;padding:6px 12px;font-size:13px;">
          <option value="text">纯文本</option>
          <option value="markdown">Markdown</option>
          <option value="image">图片</option>
          <option value="video">视频</option>
          <option value="voice">语音 (TTS)</option>
          <option value="file">文件</option>
        </select>
      </div>
      <div id="push-at-wrap" style="margin-bottom: 12px; display:none;">
        <label style="font-size:13px;color:#8a9aaa;margin-right:10px;">@用户 (群聊富文本)</label>
        <input id="push-at" type="text" placeholder="目标成员 openid（可选，留空则普通文本）" style="background:#0d1b2a;color:#e0e0e0;border:1px solid #2a3a4a;border-radius:6px;padding:6px 12px;font-size:13px;width:280px;">
        <div style="font-size:12px;color:#8a9aaa;margin-top:6px;">在文本中写 <code>@用户</code> 占位，将替换为 QQ 富文本 @ 语法（仅群聊有效）</div>
      </div>
      <div id="push-content-wrap" style="margin-bottom: 12px;">
        <textarea id="push-content" placeholder="输入消息内容..." style="width:100%;min-height:120px;background:#0d1b2a;color:#e0e0e0;border:1px solid #2a3a4a;border-radius:8px;padding:12px;font-size:14px;font-family:inherit;resize:vertical;"></textarea>
      </div>
      <div id="push-image-wrap" style="margin-bottom: 12px; display:none;">
        <label style="font-size:13px;color:#8a9aaa;margin-right:10px;">选择图片</label>
        <input id="push-image" type="file" accept="image/*" style="background:#0d1b2a;color:#e0e0e0;border:1px solid #2a3a4a;border-radius:6px;padding:6px 12px;font-size:13px;max-width:70%;">
        <div id="push-image-preview" style="margin-top:10px;"></div>
      </div>
      <div id="push-file-wrap" style="margin-bottom: 12px; display:none;">
        <label style="font-size:13px;color:#8a9aaa;margin-right:10px;">选择文件</label>
        <input id="push-file" type="file" style="background:#0d1b2a;color:#e0e0e0;border:1px solid #2a3a4a;border-radius:6px;padding:6px 12px;font-size:13px;max-width:70%;">
        <div id="push-file-preview" style="margin-top:10px;font-size:12px;color:#8a9aaa;"></div>
      </div>
      <div id="push-video-wrap" style="margin-bottom: 12px; display:none;">
        <label style="font-size:13px;color:#8a9aaa;margin-right:10px;">选择视频 (mp4 ≤30MB)</label>
        <input id="push-video" type="file" accept="video/mp4" style="background:#0d1b2a;color:#e0e0e0;border:1px solid #2a3a4a;border-radius:6px;padding:6px 12px;font-size:13px;max-width:70%;">
        <div id="push-video-preview" style="margin-top:10px;font-size:12px;color:#8a9aaa;"></div>
      </div>
      <div id="push-voice-wrap" style="margin-bottom: 12px; display:none;">
        <label style="font-size:13px;color:#8a9aaa;margin-right:10px;">语音文本（本地TTS合成）</label>
        <textarea id="push-voice-text" placeholder="输入要合成的语音内容..." style="width:100%;min-height:60px;background:#0d1b2a;color:#e0e0e0;border:1px solid #2a3a4a;border-radius:8px;padding:12px;font-size:14px;font-family:inherit;resize:vertical;"></textarea>
      </div>
      <button class="refresh-btn" style="padding:8px 24px;font-size:14px;" onclick="sendPush()">发送</button>
      <div id="push-result" style="margin-top:10px;font-size:13px;"></div>
    </div>
  </div>
  </div>

  <!-- 消息记录 -->
  <div id="tab-messages" class="tab-panel">
  <div class="section">
    <div class="section-header">
      <span class="section-title">消息记录</span>
      <span class="badge" id="msg-count">0 条</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>日期</th><th>时间</th><th>来源</th><th>场景</th><th>类型</th><th>发送人</th><th>内容预览</th><th>状态</th></tr></thead>
        <tbody id="msg-table"></tbody>
      </table>
    </div>
  </div>
  </div>

  <!-- 实时日志 -->
  <div id="tab-logs" class="tab-panel">
  <div class="section">
    <div class="section-header">
      <span class="section-title">实时日志</span>
      <span><span class="auto-badge">5s 自动刷新</span> <button class="refresh-btn" onclick="loadLogs()">刷新</button></span>
    </div>
    <div class="log-box" id="log-box"></div>
  </div>
  </div>

  <!-- 能力说明 -->
  <div id="tab-caps" class="tab-panel">
  <div class="cap-grid">

    <!-- QQ 官方机器人 -->
    <div class="cap-card cap-qq">
      <div class="cap-head">
        <div class="cap-icon">💬</div>
        <div>
          <div class="cap-title">QQ 官方机器人</div>
          <div class="cap-sub">独立进程 qq_official_adapter.py · 端口 18506</div>
        </div>
      </div>
      <div class="cap-item"><span class="cap-label">群聊 @</span><span class="cap-desc">群里 <b>@机器人</b> 即可对话、提问、查配餐方案、质检录音、日报等，AI 智能回复</span></div>
      <div class="cap-item"><span class="cap-label">私聊</span><span class="cap-desc">单聊机器人生成内容，支持<b>文本 / 图片 / 文件（≤200MB）</b>，Markdown 排版</span></div>
      <div class="cap-item"><span class="cap-label">主动推送</span><span class="cap-desc">面板可主动推送文本/图片/文件/视频/语音到 QQ；<b>群聊需最近 5 分钟内有 @</b> 才能被动下发</span></div>
      <div class="cap-item"><span class="cap-label">配餐台账</span><span class="cap-desc">AI 回复识别配餐数据自动写入台账（MCP 智能表格）</span></div>
      <div class="cap-item"><span class="cap-label">指令</span><span class="cap-desc"><b>/帮助</b> 查看全部指令（/配餐 /质检 /日报 等关键词指令直接回复）</span></div>
      <div class="cap-note">⚠️ 群成员昵称显示依赖 QQ 开放平台「获取群成员信息」接口权限；未开通时显示截断 ID（如 E3AC7D1A...F07A347），可通过手动映射在 config.QQ_USER_MAP 中补全。</div>
    </div>

    <!-- 企微机器人 -->
    <div class="cap-card cap-wecom">
      <div class="cap-head">
        <div class="cap-icon">🏢</div>
        <div>
          <div class="cap-title">企微机器人</div>
          <div class="cap-sub">主服务 server.py · 端口 8505 面板</div>
        </div>
      </div>
      <div class="cap-item"><span class="cap-label">群聊</span><span class="cap-desc">企微群 <b>@机器人</b> 对话，Webhook 回复，AI 智能应答</span></div>
      <div class="cap-item"><span class="cap-label">个人</span><span class="cap-desc">应用消息<b>主动推送</b>到指定员工（userid），可发文本/图片</span></div>
      <div class="cap-item"><span class="cap-label">群发</span><span class="cap-desc">Webhook 群发：文本 / <b>Markdown 卡片</b> / 图文 / 模板卡片 / 投票 / 按钮 / 语音</span></div>
      <div class="cap-item"><span class="cap-label">事件</span><span class="cap-desc">进群 / 退群 / 好友增删 / 权限开关等管理事件实时记录到面板</span></div>
      <div class="cap-item"><span class="cap-label">待办</span><span class="cap-desc"><b>创建 / 查询 / 更新 / 删除 / 改状态 / 搜索用户</b>，走 MCP 服务（已脱离 wecom-cli）</span></div>
      <div class="cap-item"><span class="cap-label">文档</span><span class="cap-desc">MCP <b>智能表格 / 文档</b>创建，配餐台账自动写入</span></div>
      <div class="cap-note">⚠ 成员姓名显示依赖企微通讯录 API（需在企微管理后台将当前出口 IP 加入「企业可信 IP」白名单）；未开通时显示截断 ID，可手动在 config.WECOM_USER_MAP 补全。</div>
    </div>

    <!-- 量子密信机器人 -->
    <div class="cap-card cap-zmx">
      <div class="cap-head">
        <div class="cap-icon">🔐</div>
        <div>
          <div class="cap-title">量子密信机器人</div>
          <div class="cap-sub">独立进程 zmx_adapter.py · 端口 1011</div>
        </div>
      </div>
      <div class="cap-item"><span class="cap-label">群聊 @</span><span class="cap-desc">量子密信群 <b>@机器人</b> 对话，AI 智能回复，支持文本/图片/文件消息</span></div>
      <div class="cap-item"><span class="cap-label">主动推送</span><span class="cap-desc">面板可主动推送文本/Markdown/图片/文件到量子密信群；需填写群ID (groupId)</span></div>
      <div class="cap-item"><span class="cap-label">会话隔离</span><span class="cap-desc">每个群独立会话，<b>群与群互不干扰</b>；回调携带专属回复地址</span></div>
      <div class="cap-item"><span class="cap-label">用户名映射</span><span class="cap-desc">手机号自动映射为可读用户名（config.ZMX_USER_MAP），不查企微通讯录</span></div>
      <div class="cap-item"><span class="cap-label">公网入口</span><span class="cap-desc">SSH反向隧道方案：Mac → 公网服务器:1011 → 量子密信平台回调</span></div>
      <div class="cap-note">量子密信支持文本/Markdown/图片/文件四种消息（两步式上传+发送）。入站回调目前仅文本。</div>
    </div>
  </div>

  <!-- 通用能力 -->
  <div class="section">
    <div class="section-header"><span class="section-title">通用能力</span><span class="badge">三通道共用</span></div>
    <div style="padding: 16px 20px;">
      <div class="cap-item"><span class="cap-label">AI 对话</span><span class="cap-desc">复用 TeleAgent 管线（8088 代理），支持<b>文字 / 图片理解 / 文件</b>，上下文会话记忆</span></div>
      <div class="cap-item"><span class="cap-label">会话分组</span><span class="cap-desc">同一用户私聊 = 一个会话，同一群 @ = 一个会话，<b>群与私聊互不干扰</b>；对话上下文自动延续，一句话不再开新会话</span></div>
      <div class="cap-item"><span class="cap-label">场景技能</span><span class="cap-desc">电信业务咨询、套餐比算、<b>配餐方案生成</b>、质检录音分析、收入数据看板、日报/周报生成</span></div>
      <div class="cap-item"><span class="cap-label">富媒体</span><span class="cap-desc">QQ 支持图片/视频/语音/文件；企微支持图片/文件/语音合成；量子密信支持图片</span></div>
      <div class="cap-item"><span class="cap-label">消息记录</span><span class="cap-desc">企微 + QQ + 量子密信三通道消息合并展示，实时状态（处理中/已回复/失败）</span></div>
      <div class="cap-item"><span class="cap-label">定时任务</span><span class="cap-desc">Token 日报 / AI 新闻 / 邮件日报 / 短信日报 / 工作日志每日自动推送</span></div>
    </div>
  </div>
  </div>

  <div class="footer">企微+QQ+量子密信机器人 Web 管理面板 · 端口 8505</div>
</div>

<script>
function tagClass(t) {
  const m = {'text':'tag-text','image':'tag-image','mixed':'tag-mixed','file':'tag-file','voice':'tag-voice','video':'tag-video','event':'tag-event','system':'tag-event'};
  return m[t] || 'tag-text';
}
function statusClass(s) {
  if (s.includes('已回复') || s.includes('成功')) return 'status-ok';
  if (s.includes('处理中') || s.includes('上传')) return 'status-processing';
  if (s.includes('失败') || s.includes('错误')) return 'status-fail';
  return '';
}
async function loadStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    const dot = document.getElementById('dot');
    const conn = document.getElementById('v-conn');
    if (d.online && d.subscribed) {
      dot.className = 'status-dot status-online';
      conn.textContent = '在线'; conn.className = 'card-value green';
    } else if (d.online) {
      dot.className = 'status-dot status-online';
      conn.textContent = '连接中'; conn.className = 'card-value yellow';
    } else {
      dot.className = 'status-dot status-offline';
      conn.textContent = '离线'; conn.className = 'card-value red';
    }
    document.getElementById('v-uptime').textContent = d.uptime;
    document.getElementById('v-heartbeat').textContent = d.heartbeat_ago;
    document.getElementById('v-reconnect').textContent = d.reconnect_count;
    document.getElementById('v-msgs').textContent = d.total_messages;
    document.getElementById('v-pending').textContent = d.pending_files;
  } catch(e) { console.error(e); }
  try {
    const r = await fetch('/api/qqstatus');
    const d = await r.json();
    const qconn = document.getElementById('q-conn');
    if (d.connected) {
      qconn.textContent = '在线'; qconn.className = 'card-value green';
    } else if (d.running) {
      qconn.textContent = '连接中'; qconn.className = 'card-value yellow';
    } else {
      qconn.textContent = '离线'; qconn.className = 'card-value red';
    }
    document.getElementById('q-msgs').textContent = d.total_received;
    document.getElementById('q-replies').textContent = d.total_replied;
    document.getElementById('q-last').textContent = d.last_message_at || '-';
    document.getElementById('q-groups').textContent = Object.keys(d.session.group || {}).length;
    document.getElementById('q-users').textContent = Object.keys(d.session.user || {}).length;
    if (d.last_error) console.warn('QQ:', d.last_error);
  } catch(e) { console.error(e); }
  try {
    const r = await fetch('/api/zmxstatus');
    const d = await r.json();
    const zconn = document.getElementById('z-conn');
    if (d.listening) {
      zconn.textContent = '监听中'; zconn.className = 'card-value green';
    } else if (d.running) {
      zconn.textContent = '运行中'; zconn.className = 'card-value yellow';
    } else {
      zconn.textContent = '离线'; zconn.className = 'card-value red';
    }
    document.getElementById('z-msgs').textContent = d.total_received;
    document.getElementById('z-replies').textContent = d.total_replied;
    document.getElementById('z-attachments').textContent = d.total_attachments;
    document.getElementById('z-last').textContent = d.last_message_at || '-';
    document.getElementById('z-errors').textContent = d.total_errors;
    if (d.last_error) console.warn('ZMX:', d.last_error);
  } catch(e) { console.error(e); }
  try {
    const r = await fetch('/api/feishustatus');
    const d = await r.json();
    const fconn = document.getElementById('f-conn');
    if (d.connected) {
      fconn.textContent = '在线'; fconn.className = 'card-value green';
    } else if (d.running) {
      fconn.textContent = '连接中'; fconn.className = 'card-value yellow';
    } else {
      fconn.textContent = '离线'; fconn.className = 'card-value red';
    }
    document.getElementById('f-msgs').textContent = d.total_received;
    document.getElementById('f-replies').textContent = d.total_replied;
    document.getElementById('f-last').textContent = d.last_message_at || '-';
    document.getElementById('f-groups').textContent = Object.keys(d.session.group || {}).length;
    document.getElementById('f-users').textContent = Object.keys(d.session.user || {}).length;
    if (d.last_error) console.warn('FEISHU:', d.last_error);
  } catch(e) { console.error(e); }
  // QQ 待确认权限请求
  try {
    const r = await fetch('/api/qqpending');
    const d = await r.json();
    const section = document.getElementById('qqPendingSection');
    const list = document.getElementById('qqPendingList');
    const countBadge = document.getElementById('qp-count');
    if (!d || d.length === 0) {
      section.style.display = 'none';
      countBadge.textContent = '0';
      return;
    }
    section.style.display = '';
    countBadge.textContent = d.length;
    list.innerHTML = d.map(item => {
      const ageMin = Math.floor(item.age_seconds / 60);
      const ageSec = item.age_seconds % 60;
      const ageStr = ageMin > 0 ? `${ageMin}分${ageSec}秒前` : `${ageSec}秒前`;
      const typeLabel = item.type === 'permission' ? '权限确认' : '问题选择';
      const sentLabel = item.notice_sent ? '已送达' : `<span style="color:#f59e0b;">待补发(${item.retry_count}/5)</span>`;
      const sceneLabel = item.is_group ? '群聊' : '私聊';
      const textPreview = (item.notice_text || '').substring(0, 120);
      return `<div style="background:#0d1b2a;border:1px solid #2a3a4a;border-radius:8px;padding:12px 14px;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
          <span style="font-size:13px;font-weight:600;color:#e0e0e0;">${typeLabel} · ${sceneLabel}</span>
          <span style="font-size:11px;color:#5a6a7a;">${ageStr} · ${sentLabel}</span>
        </div>
        <div style="font-size:12px;color:#8a9aaa;margin-bottom:4px;">会话: ${item.session_title}</div>
        <div style="font-size:12px;color:#a0b0c0;background:#111f30;border-radius:4px;padding:6px 8px;">${textPreview}</div>
      </div>`;
    }).join('');
  } catch(e) { console.error(e); }
}
async function loadMessages() {
  try {
    const r = await fetch('/api/messages');
    const d = await r.json();
    document.getElementById('msg-count').textContent = d.length + ' 条';
    const tbody = document.getElementById('msg-table');
    if (d.length === 0) {
      tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#5a6a7a;padding:30px;">暂无消息记录</td></tr>';
      return;
    }
    tbody.innerHTML = d.map(m => {
      const ft = m.full_time || '';
      const date = ft ? ft.split(' ')[0] : (m.time ? '' : '-');
      const scene = m.scene === 'group' ? '群聊' : (m.scene === 'single' ? '私聊' : '-');
      const sourceTag = m.source === 'qq' ? 'QQ' : (m.source === 'zmx' ? '密信' : '企微');
      const sourceClass = m.source === 'qq' ? 'tag-qq' : (m.source === 'zmx' ? 'tag-zmx' : 'tag-text');
      return `<tr>
      <td>${date || '-'}</td>
      <td>${m.time}</td>
      <td><span class="tag ${sourceClass}">${sourceTag}</span></td>
      <td><span class="tag ${scene === '群聊' ? 'tag-group' : (scene === '私聊' ? 'tag-single' : '')}">${scene}</span></td>
      <td><span class="tag ${tagClass(m.type)}">${m.type}</span></td>
      <td>${m.user}</td>
      <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${m.preview}</td>
      <td class="${statusClass(m.status)}">${m.status}</td>
    </tr>`;
    }).join('');
  } catch(e) { console.error(e); }
}
async function loadLogs() {
  try {
    const r = await fetch('/api/logs?n=80');
    const d = await r.json();
    const box = document.getElementById('log-box');
    box.innerHTML = d.map(l => {
      let cls = 'log-INFO';
      if (l.includes('[ERROR]')) cls = 'log-ERROR';
      else if (l.includes('[WARNING]')) cls = 'log-WARNING';
      else if (l.includes('[DEBUG]')) cls = 'log-DEBUG';
      return `<div class="log-line ${cls}">${l}</div>`;
    }).join('');
  } catch(e) { console.error(e); }
}
function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
  if (name === 'messages') loadMessages();
  if (name === 'logs') loadLogs();
}
function loadAll() { loadStatus(); loadMessages(); }
async function sendPush() {
  const target = document.getElementById('push-target').value;
  const format = document.getElementById('push-format').value;
  const content = document.getElementById('push-content').value;
  const userid = document.getElementById('push-userid').value;
  const statusEl = document.getElementById('push-status');
  const resultEl = document.getElementById('push-result');
  const isQq = target === 'qq_group' || target === 'qq_user';
    if (isQq) {
      if (!userid.trim()) {
        resultEl.innerHTML = '<span style="color:#ef4444;">请填写 QQ 群的 group_openid / 用户 openid（或从最近会话选择）</span>';
        return;
      }
      if (format === 'image') {
        const fileInput = document.getElementById('push-image');
        if (!fileInput.files || fileInput.files.length === 0) {
          resultEl.innerHTML = '<span style="color:#ef4444;">请先选择图片</span>';
          return;
        }
      } else if (format === 'file') {
        const fileInput = document.getElementById('push-file');
        if (!fileInput.files || fileInput.files.length === 0) {
          resultEl.innerHTML = '<span style="color:#ef4444;">请先选择文件</span>';
          return;
        }
      } else if (format === 'video') {
        const videoInput = document.getElementById('push-video');
        if (!videoInput.files || videoInput.files.length === 0) {
          resultEl.innerHTML = '<span style="color:#ef4444;">请先选择视频</span>';
          return;
        }
      } else if (format === 'voice') {
        if (!document.getElementById('push-voice-text').value.trim()) {
          resultEl.innerHTML = '<span style="color:#ef4444;">请输入要合成的语音内容</span>';
          return;
        }
      } else if (!content.trim()) {
        resultEl.innerHTML = '<span style="color:#ef4444;">请输入消息内容</span>';
        return;
      }
    } else if (target === 'zmx_group') {
      // 量子密信推送：需要群ID（groupId），格式支持文本和图片
      if (!userid.trim()) {
        resultEl.innerHTML = '<span style="color:#ef4444;">请填写量子密信群ID（groupId）</span>';
        return;
      }
      if (format === 'image') {
        const fileInput = document.getElementById('push-image');
        if (!fileInput.files || fileInput.files.length === 0) {
          resultEl.innerHTML = '<span style="color:#ef4444;">请先选择图片</span>';
          return;
        }
      } else if (!content.trim()) {
        resultEl.innerHTML = '<span style="color:#ef4444;">请输入消息内容</span>';
        return;
      }
    } else if (format === 'image') {
    const fileInput = document.getElementById('push-image');
    if (!fileInput.files || fileInput.files.length === 0) {
      resultEl.innerHTML = '<span style="color:#ef4444;">请先选择图片</span>';
      return;
    }
  } else if (format === 'file') {
    resultEl.innerHTML = '<span style="color:#ef4444;">文件推送仅支持 QQ 官方机器人</span>';
    return;
  } else if (!content.trim()) {
    resultEl.innerHTML = '<span style="color:#ef4444;">请输入消息内容</span>';
    return;
  }
  statusEl.textContent = '发送中...';
  resultEl.innerHTML = '';
  try {
    const body = { target, format, content };
    if ((target === 'user' || isQq || target === 'zmx_group') && userid) body.userid = userid;
    if (format === 'image') {
      const file = document.getElementById('push-image').files[0];
      body.imageData = await readFileAsBase64(file);
      body.imageName = file.name;
    } else if (format === 'file') {
      const file = document.getElementById('push-file').files[0];
      if (file.size > 200 * 1024 * 1024) {
        statusEl.textContent = '失败';
        resultEl.innerHTML = '<span style="color:#ef4444;">文件超过 200MB，超出 QQ 官方硬限制</span>';
        return;
      }
      body.fileData = await readFileAsBase64(file);
      body.fileName = file.name;
    } else if (format === 'video') {
      const vf = document.getElementById('push-video').files[0];
      if (vf.size > 200 * 1024 * 1024) {
        statusEl.textContent = '失败';
        resultEl.innerHTML = '<span style="color:#ef4444;">视频超过 200MB，超出 QQ 官方硬限制</span>';
        return;
      }
      body.videoData = await readFileAsBase64(vf);
      body.videoName = vf.name;
    } else if (format === 'voice') {
      body.voiceText = document.getElementById('push-voice-text').value;
    }
    const atVal = document.getElementById('push-at');
    if (atVal && atVal.value.trim()) body.at = atVal.value.trim();
    const r = await fetch('/api/push', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const d = await r.json();
    if (d.success) {
      statusEl.textContent = '成功';
      resultEl.innerHTML = '<span style="color:#4ade80;">' + (d.detail || '推送成功') + '</span>';
    } else {
      statusEl.textContent = '失败';
      resultEl.innerHTML = '<span style="color:#ef4444;">' + (d.error || '推送失败') + '</span>';
    }
  } catch(e) {
    statusEl.textContent = '异常';
    resultEl.innerHTML = '<span style="color:#ef4444;">' + e.message + '</span>';
  }
  setTimeout(() => statusEl.textContent = '就绪', 3000);
}
function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.split(',')[1]);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}
document.addEventListener('DOMContentLoaded', function() {
  const targetSel = document.getElementById('push-target');
  const useridInput = document.getElementById('push-userid');
  const qqSessionSel = document.getElementById('push-qq-session');
  function refreshTargetUI() {
    const v = targetSel.value;
    const isQq = v === 'qq_group' || v === 'qq_user';
    const isZmx = v === 'zmx_group';
    const needsId = v === 'user' || isQq || isZmx;
    useridInput.style.display = needsId ? 'inline' : 'none';
    qqSessionSel.style.display = isQq ? 'inline' : 'none';
    const qqTip = document.getElementById('push-qq-tip');
    const zmxTip = document.getElementById('push-zmx-tip');
    if (qqTip) qqTip.style.display = v === 'qq_group' ? '' : 'none';
    if (zmxTip) zmxTip.style.display = isZmx ? '' : 'none';
    if (isQq) {
      // QQ 官方机器人支持文本与图片（图片经 base64 上传），不锁定格式
      setFormatOptions(v);
      onFormatChange();
      loadQQSessions();
    } else if (isZmx) {
      // 量子密信支持文本/Markdown/图片/文件
      setFormatOptions(v);
      onFormatChange();
    } else {
      setFormatOptions(v);
      onFormatChange();
    }
  }
  targetSel.addEventListener('change', refreshTargetUI);
    const formatSel = document.getElementById('push-format');
    const allFormatOpts = Array.from(formatSel.options);
    function setFormatOptions(target) {
      // 各目标支持的格式列表
      const supportedByTarget = {
        'group':     ['text', 'markdown', 'image', 'video', 'voice', 'file'],
        'user':      ['text', 'markdown', 'image', 'video', 'voice', 'file'],
        'qq_group':  ['text', 'markdown', 'image', 'video', 'voice', 'file'],
        'qq_user':   ['text', 'markdown', 'image', 'video', 'voice', 'file'],
        'zmx_group': ['text', 'markdown', 'image', 'file'],
      };
      const allowed = supportedByTarget[target] || ['text', 'markdown', 'image', 'video', 'voice', 'file'];
      const currentVal = formatSel.value;
      // 隐藏不支持的选项
      allFormatOpts.forEach(opt => {
        if (allowed.includes(opt.value)) {
          opt.style.display = '';
        } else {
          opt.style.display = 'none';
        }
      });
      // 如果当前选中的格式被隐藏了，自动切到第一个可用格式
      if (!allowed.includes(currentVal)) {
        formatSel.value = allowed[0];
      }
      onFormatChange();
    }
  const contentWrap = document.getElementById('push-content-wrap');
  const imageWrap = document.getElementById('push-image-wrap');
  const imageInput = document.getElementById('push-image');
  const imagePreview = document.getElementById('push-image-preview');
  const fileWrap = document.getElementById('push-file-wrap');
  const fileInput = document.getElementById('push-file');
  const filePreview = document.getElementById('push-file-preview');
  const videoWrap = document.getElementById('push-video-wrap');
  const videoInput = document.getElementById('push-video');
  const videoPreview = document.getElementById('push-video-preview');
  const voiceWrap = document.getElementById('push-voice-wrap');
  const voiceText = document.getElementById('push-voice-text');
  const atWrap = document.getElementById('push-at-wrap');
  function onFormatChange() {
    const f = formatSel.value;
    const isImage = f === 'image';
    const isFile = f === 'file';
    const isVideo = f === 'video';
    const isVoice = f === 'voice';
    contentWrap.style.display = (isImage || isFile || isVideo || isVoice) ? 'none' : '';
    imageWrap.style.display = isImage ? '' : 'none';
    fileWrap.style.display = isFile ? '' : 'none';
    videoWrap.style.display = isVideo ? '' : 'none';
    voiceWrap.style.display = isVoice ? '' : 'none';
    // @ 输入框：文本/Markdown 且目标为 QQ 群时显示
    const t = document.getElementById('push-target').value;
    atWrap.style.display = (t === 'qq_group' && (f === 'text' || f === 'markdown')) ? '' : 'none';
  }
  formatSel.addEventListener('change', onFormatChange);
  onFormatChange();
  imageInput.addEventListener('change', function() {
    if (this.files && this.files[0]) {
      const reader = new FileReader();
      reader.onload = (e) => {
        imagePreview.innerHTML = '<img src="' + e.target.result + '" style="max-width:200px;max-height:200px;border-radius:8px;border:1px solid #2a3a4a;">';
      };
      reader.readAsDataURL(this.files[0]);
    } else {
      imagePreview.innerHTML = '';
    }
  });
  fileInput.addEventListener('change', function() {
    if (this.files && this.files[0]) {
      const f = this.files[0];
      const sizeMB = (f.size / 1024 / 1024).toFixed(2);
      filePreview.textContent = f.name + ' (' + sizeMB + ' MB)' + (f.size > 5*1024*1024 ? ' - 超过5MB将走分片上传' : '');
      filePreview.style.color = f.size > 200*1024*1024 ? '#ef4444' : '#8a9aaa';
    } else {
      filePreview.textContent = '';
    }
  });
  videoInput.addEventListener('change', function() {
    if (this.files && this.files[0]) {
      const f = this.files[0];
      const sizeMB = (f.size / 1024 / 1024).toFixed(2);
      videoPreview.textContent = f.name + ' (' + sizeMB + ' MB)' + (f.size > 30*1024*1024 ? ' - 超过30MB将降级为文件' : '');
      videoPreview.style.color = f.size > 200*1024*1024 ? '#ef4444' : '#8a9aaa';
    } else {
      videoPreview.textContent = '';
    }
  });
  // 目标切换时也联动 @ 输入显示
  const targetSel2 = document.getElementById('push-target');
  targetSel2.addEventListener('change', function() { onFormatChange(); });
  // 初始化：根据默认目标刷新 UI
  refreshTargetUI();
});
async function loadQQSessions() {
  try {
    const r = await fetch('/api/qqstatus');
    const d = await r.json();
    const sel = document.getElementById('push-qq-session');
    const target = document.getElementById('push-target').value;
    const isGroup = target === 'qq_group';
    const list = isGroup ? (d.session.group || {}) : (d.session.user || {});
    const opts = Object.entries(list)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 20)
      .map(([k, ts]) => {
        const t = new Date(ts * 1000);
        const hh = String(t.getHours()).padStart(2, '0');
        const mm = String(t.getMinutes()).padStart(2, '0');
        const nick = (d.user_map || {})[k] || k.slice(0, 12);
        return '<option value="' + k + '">' + nick + ' (' + hh + ':' + mm + ')</option>';
      });
    sel.innerHTML = '<option value="">-- 最近会话快捷选择 --</option>' + opts.join('');
  } catch(e) { console.error(e); }
}
loadAll();
loadLogs();
setInterval(loadAll, 5000);
setInterval(loadLogs, 5000);
</script>
</body>
</html>'''


# ========== HTTP 请求处理 ==========
class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/' or path == '/dashboard':
            self._serve_html()
        elif path == '/api/status':
            self._serve_json(get_bot_status())
        elif path == '/api/qqstatus':
            self._serve_json(get_qq_status())
        elif path == '/api/zmxstatus':
            self._serve_json(get_zmx_status())
        elif path == '/api/feishustatus':
            self._serve_json(get_feishu_status())
        elif path == '/api/feishumessages':
            self._serve_json(get_feishu_messages(50))
        elif path == '/api/qqpending':
            self._serve_json(get_qq_pending_confirmations())
        elif path == '/api/messages':
            self._serve_json(get_message_records(50))
        elif path == '/api/logs':
            n = 80
            query = parsed.query
            if query.startswith('n='):
                try: n = int(query[2:])
                except: pass
            self._serve_json(get_recent_logs(n))
        elif path == '/api/push/config':
            self._serve_json(self._get_push_config())
        else:
            self._serve_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/api/push':
            self._handle_push()
        else:
            self._serve_json({"error": "not found"}, 404)

    def _get_push_config(self):
        """返回推送配置状态"""
        try:
            import config as _cfg
            return {
                "webhook_configured": bool(getattr(_cfg, 'WEBHOOK_URL', '')),
                "agent_id": getattr(_cfg, 'AGENT_ID', 0),
                "corp_id": getattr(_cfg, 'CORP_ID', ''),
                "known_users": getattr(_cfg, 'WECOM_USER_MAP', {}),
                "qq_enabled": bool(getattr(_cfg, 'QQ_ENABLED', False)),
            }
        except Exception:
            return {"webhook_configured": False, "agent_id": 0, "qq_enabled": False}

    def _handle_push(self):
        """处理推送请求"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            req = json.loads(body)

            target = req.get('target', 'group')
            fmt = req.get('format', 'text')
            content = req.get('content', '')
            userid = req.get('userid', '')
            image_data = req.get('imageData', '')
            image_name = req.get('imageName', 'image.png')
            file_data = req.get('fileData', '')
            file_name = req.get('fileName', '')
            video_data = req.get('videoData', '')
            video_name = req.get('videoName', '')
            voice_text = req.get('voiceText', '')
            at_user = req.get('at', '')

            if fmt == 'image':
                if not image_data:
                    self._serve_json({"success": False, "error": "图片数据不能为空"})
                    return
            elif fmt == 'file':
                if not file_data:
                    self._serve_json({"success": False, "error": "文件数据不能为空"})
                    return
            elif fmt == 'video':
                if not video_data:
                    self._serve_json({"success": False, "error": "视频数据不能为空"})
                    return
            elif fmt == 'voice':
                if not voice_text.strip():
                    self._serve_json({"success": False, "error": "语音文本不能为空"})
                    return
            elif not content.strip():
                self._serve_json({"success": False, "error": "消息内容不能为空"})
                return

            # 延迟导入push模块，避免循环依赖
            import push as _push

            # QQ 推送：通过本机内部端点转发给 QQ 适配器进程（跨进程调用）
            if target in ('qq_group', 'qq_user'):
                if not userid.strip():
                    self._serve_json({"success": False, "error": "QQ推送需要填写 group_openid / openid"})
                    return
                if fmt == 'image' and not image_data:
                    self._serve_json({"success": False, "error": "QQ图片推送需要图片数据"})
                    return
                if fmt == 'file' and not file_data:
                    self._serve_json({"success": False, "error": "QQ文件推送需要文件数据"})
                    return
                ok, err = self._forward_qq_push(
                    target, userid, content, fmt,
                    image_data, file_data, file_name,
                    video_data, video_name, voice_text, at_user,
                )
                if ok:
                    self._serve_json({"success": True, "detail": f"QQ推送成功 ({target}/{fmt})"})
                else:
                    self._serve_json({"success": False, "error": f"QQ推送失败: {err}"})
                return
            
            # 量子密信推送：通过量子密信适配器发送
            if target == 'zmx_group':
                if not userid.strip():
                    self._serve_json({"success": False, "error": "量子密信推送需要填写群ID (groupId)"})
                    return
                if fmt == 'image' and not image_data:
                    self._serve_json({"success": False, "error": "量子密信图片推送需要图片数据"})
                    return
                ok, err = self._forward_zmx_push(
                    userid, content, fmt, image_data, image_name,
                )
                if ok:
                    self._serve_json({"success": True, "detail": f"量子密信推送成功 ({fmt})"})
                else:
                    # 提供更详细的错误信息
                    error_msg = f"量子密信推送失败: {err}"
                    if "机器人不存在" in err:
                        error_msg += "\n\n可能原因：\n1. key没有上传权限\n2. 需要不同的机器人key\n3. 量子密信平台不支持通过webhook上传附件\n\n建议：请检查量子密信平台配置，或暂时使用文本推送。"
                    self._serve_json({"success": False, "error": error_msg})
                return

            # 企微通道不支持文件/视频/语音推送
            if fmt in ('file', 'video', 'voice'):
                self._serve_json({"success": False, "error": "文件/视频/语音推送仅支持 QQ 官方机器人"})
                return

            result = None
            if target == 'group':
                if fmt == 'markdown':
                    result = _push.push_to_group_markdown(content)
                elif fmt == 'image':
                    image_path = self._save_temp_image(image_data, image_name)
                    if not image_path:
                        self._serve_json({"success": False, "error": "图片保存失败"})
                        return
                    try:
                        result = _push.push_to_group_image(image_path)
                    finally:
                        self._remove_temp_image(image_path)
                else:
                    result = _push.push_to_group(content)
            elif target == 'user':
                if not userid.strip():
                    self._serve_json({"success": False, "error": "个人推送需要填写userid"})
                    return
                if fmt == 'markdown':
                    result = _push.push_markdown_to_user(userid, content)
                elif fmt == 'image':
                    image_path = self._save_temp_image(image_data, image_name)
                    if not image_path:
                        self._serve_json({"success": False, "error": "图片保存失败"})
                        return
                    try:
                        result = _push.push_image_to_user(userid, image_path)
                    finally:
                        self._remove_temp_image(image_path)
                else:
                    result = _push.push_to_user(userid, content)
            else:
                self._serve_json({"success": False, "error": f"未知推送目标: {target}"})
                return

            if result and result.get('errcode') == 0:
                self._serve_json({"success": True, "detail": f"推送成功 ({target}/{fmt})"})
            else:
                err = result.get('errmsg', '未知错误') if result else '无返回'
                self._serve_json({"success": False, "error": f"推送失败: {err}"})
        except Exception as e:
            self._serve_json({"success": False, "error": f"异常: {str(e)}"})

    def _save_temp_image(self, image_data, image_name):
        """把 base64 图片数据保存为临时文件"""
        import base64
        try:
            ext = os.path.splitext(image_name)[1].lower() or '.png'
            if ext not in ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'):
                ext = '.png'
            temp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'temp_uploads')
            os.makedirs(temp_dir, exist_ok=True)
            path = os.path.join(temp_dir, f"push_{int(time.time()*1000)}{ext}")
            with open(path, 'wb') as f:
                f.write(base64.b64decode(image_data))
            return path
        except Exception as e:
            logger.error(f"[dashboard] 保存临时图片失败: {e}")
            return None

    def _remove_temp_image(self, path):
        """删除临时图片文件"""
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    def _forward_qq_push(self, target, openid, content, fmt='text', image_data='', file_data='', file_name='', video_data='', video_name='', voice_text='', at_user=''):
        """通过 QQ 适配器内部接口转发推送（跨进程）
        fmt: text / markdown / image / file / video / voice；
        image/video/file 时附带 base64 数据+文件名，voice 时附带 voice_text（本地 TTS），markdown 映射 msg_type=2"""
        import urllib.request
        try:
            payload = {
                "target": "group" if target == 'qq_group' else "user",
                "openid": openid,
            }
            if at_user.strip():
                # 富文本 @ 目标 openid（仅群聊有效，由适配器替换 @用户 占位）
                payload["at"] = at_user
            if fmt == 'image':
                # 图片推送：透传 base64 + 可选 caption
                payload["image"] = image_data
                if content.strip():
                    payload["caption"] = content
            elif fmt == 'file':
                # 文件推送：透传 base64 + 文件名 + 可选 caption
                payload["file"] = file_data
                payload["filename"] = file_name or "文件"
                if content.strip():
                    payload["caption"] = content
            elif fmt == 'video':
                # 视频推送：透传 base64 + 文件名（适配器按扩展名推断 file_type=2）
                payload["file"] = video_data
                payload["filename"] = video_name or "video.mp4"
                if content.strip():
                    payload["caption"] = content
            elif fmt == 'voice':
                # 语音推送：适配器本地 TTS 合成 mp3 后发送
                payload["voice_text"] = voice_text
            elif fmt == 'markdown':
                payload["msg_type"] = 2
                payload["content"] = content
            else:
                payload["content"] = content
            req = urllib.request.Request(
                f"http://127.0.0.1:{QQ_PUSH_PORT}/push",
                data=json.dumps(payload).encode('utf-8'),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            # 大文件分片上传耗时较长，放宽超时（默认20s不够，200MB 分片可能数分钟）
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                return bool(data.get('success')), data.get('error', '') or ''
        except Exception as e:
            return False, str(e)

    def _forward_zmx_push(self, group_id, content, fmt='text', image_data='', image_name='image.png'):
        """通过量子密信适配器发送推送（跨进程）
        fmt: text / image；量子密信仅支持文本和图片"""
        import urllib.request
        import base64
        try:
            payload = {
                "target": "group",
                "groupid": group_id,
                "format": fmt,
            }
            if fmt == 'image':
                # 图片推送：传递base64数据
                payload["image"] = image_data
                payload["imagename"] = image_name
                if content.strip():
                    payload["caption"] = content
            else:
                payload["content"] = content
            
            # 通过量子密信适配器内部接口转发
            req = urllib.request.Request(
                f"http://127.0.0.1:1011/push",
                data=json.dumps(payload).encode('utf-8'),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                return bool(data.get('success')), data.get('error', '') or ''
        except Exception as e:
            return False, str(e)

    def _serve_html(self):
        data = HTML_PAGE.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass  # 不打印 HTTP 访问日志


def run_dashboard(port=8505):
    """启动管理面板 HTTP 服务"""
    setup_log_capture()
    _backfill_messages_from_log()
    server = HTTPServer(('0.0.0.0', port), DashboardHandler)
    print(f"[Dashboard] 管理面板已启动: http://127.0.0.1:{port}")
    server.serve_forever()


def _backfill_messages_from_log():
    """启动时从日志文件回填历史消息记录"""
    try:
        if not os.path.exists(LOG_FILE):
            return
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        # 加载姓名映射：config手动映射 + name_cache.json
        name_map = {}
        try:
            import config as _cfg
            name_map.update(getattr(_cfg, 'WECOM_USER_MAP', {}))
            # QQ openid 映射（历史消息兜底：日志回填时把 openid 换成昵称）
            name_map.update(getattr(_cfg, 'QQ_USER_MAP', {}))
        except Exception:
            pass
        cache_file = os.path.join(PROJECT_DIR, 'name_cache.json')
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    name_map.update(json.load(f))
            except Exception:
                pass

        # 日志中的消息记录格式：
        # 2026-08-10 08:26:33,962 [INFO] 收到文字消息: from=xxx, chattype=group, content=...
        # 2026-08-10 08:26:33 [INFO] 收到图片消息: from=xxx, chattype=group
        # 2026-08-10 08:26:33 [INFO] 收到文件消息: from=xxx, filename=xxx
        # 2026-08-10 08:26:33 [INFO] 收到语音消息: from=xxx
        # 2026-08-10 08:26:33 [INFO] 收到视频消息: from=xxx
        # 2026-08-10 08:26:33 [INFO] 收到图文混排消息: from=xxx, chattype=group, items=3
        patterns = [
            (r'收到文字消息: from=(\S+?)(?:,\s|\s).*?content=(.*)', 'text'),
            (r'收到图片消息: from=(\S+?)(?:,\s|\s|$)', 'image'),
            (r'收到文件消息: from=(\S+?).*?filename=(\S+)', 'file'),
            (r'收到语音消息: from=(\S+?)(?:,\s|\s|$)', 'voice'),
            (r'收到视频消息: from=(\S+?)(?:,\s|\s|$)', 'video'),
            (r'收到图文混排消息: from=(\S+?).*?items=(\d+)', 'mixed'),
        ]

        records = []
        for line in lines:
            for pat, mtype in patterns:
                m = re.search(pat, line.strip())
                if m:
                    # 提取时间（完整 + HH:MM:SS）
                    time_match = re.match(r'\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2})', line)
                    full_time = time_match.group(0) if time_match else ""
                    time_str = time_match.group(1) if time_match else "--:--:--"

                    userid = m.group(1).rstrip(',')
                    user = name_map.get(userid, userid[:8] + '...')

                    if mtype == 'text':
                        preview = m.group(2)[:80]
                    elif mtype == 'file':
                        preview = f"文件: {m.group(2)}"
                    elif mtype == 'mixed':
                        preview = f"{m.group(2)}个附件"
                    else:
                        preview = f"{mtype}消息"

                    # 解析来源场景（chattype=group/single，日志里可能没有该字段）
                    ct = re.search(r'chattype=(\S+)', line)
                    scene = ct.group(1).rstrip(',') if ct else ""
                    scene = scene if scene in ("group", "single") else ""

                    records.append({
                        "time": time_str,
                        "full_time": full_time,
                        "type": mtype,
                        "user": user,
                        "preview": preview,
                        "status": "已处理",  # 历史消息标记为已处理
                        "scene": scene,
                    })
                    break

        # 倒序填充（最近的消息在最前面）
        with MESSAGE_RECORDS_LOCK:
            for r in reversed(records):
                MESSAGE_RECORDS.append(r)
                if len(MESSAGE_RECORDS) > MAX_MESSAGE_RECORDS:
                    break

        with BOT_STATUS_LOCK:
            BOT_STATUS["total_messages"] = len(records)

        print(f"[Dashboard] 从日志回填 {len(records)} 条历史消息记录")
    except Exception as e:
        print(f"[Dashboard] 回填历史消息失败: {e}")


if __name__ == '__main__':
    # 独立运行模式（仅查看日志，无实时状态）
    run_dashboard()
