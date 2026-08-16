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


def add_message_record(msg_type, user, preview, status="处理中"):
    """添加一条消息记录（供 server.py 调用）"""
    with MESSAGE_RECORDS_LOCK:
        MESSAGE_RECORDS.insert(0, {
            "time": time.strftime("%H:%M:%S"),
            "full_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "type": msg_type,
            "user": user,
            "preview": preview[:80] if preview else "",
            "status": status
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


def get_recent_logs(n=50):
    """获取最近N行日志（内存 + QQ 适配器日志文件）"""
    with RECENT_LOGS_LOCK:
        lines = list(reversed(RECENT_LOGS[-n:]))
    # 合并 QQ 适配器日志文件尾部（独立进程，通过文件共享）
    qq_lines = _read_qq_log_tail(n)
    merged = qq_lines + lines
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


def get_message_records(n=50):
    """获取最近N条消息记录（企微内存记录 + QQ 落盘记录合并，按时间倒序）"""
    # QQ 消息来自独立进程（qq_official_adapter.py），落盘到 qq_messages.json
    qq_records = []
    qq_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'qq_messages.json')
    try:
        if os.path.exists(qq_file):
            with open(qq_file, 'r', encoding='utf-8') as f:
                qq_records = json.load(f)
    except Exception:
        qq_records = []
    with MESSAGE_RECORDS_LOCK:
        merged = qq_records + MESSAGE_RECORDS
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
<title>企微+QQ机器人管理面板</title>
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
.tag-qq { background: #1e3a2f; color: #34d399; }
.status-ok { color: #4ade80; }
.status-processing { color: #fbbf24; }
.status-fail { color: #ef4444; }
.log-box { max-height: 500px; overflow-y: auto; padding: 12px 16px; font-family: "SF Mono", "Consolas", monospace; font-size: 12px; line-height: 1.6; }
.log-line { white-space: pre-wrap; word-break: break-all; }
.log-INFO { color: #c0c0c0; }
.log-ERROR { color: #ef4444; }
.log-WARNING { color: #fbbf24; }
.log-DEBUG { color: #60a5fa; }
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
  <h1><span id="dot" class="status-dot status-offline"></span>企微+QQ机器人管理面板</h1>
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

  <!-- Tab 菜单 -->
  <div class="tabs">
    <button class="tab-btn" data-tab="push" onclick="switchTab('push')">主动推送</button>
    <button class="tab-btn" data-tab="messages" onclick="switchTab('messages')">消息记录</button>
    <button class="tab-btn" data-tab="logs" onclick="switchTab('logs')">实时日志</button>
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
        </select>
        <input id="push-userid" type="text" placeholder="userid / QQ openid（个人/私聊模式填）" style="display:none;background:#0d1b2a;color:#e0e0e0;border:1px solid #2a3a4a;border-radius:6px;padding:6px 12px;font-size:13px;margin-left:10px;width:280px;">
        <select id="push-qq-session" style="display:none;background:#0d1b2a;color:#e0e0e0;border:1px solid #2a3a4a;border-radius:6px;padding:6px 12px;font-size:13px;margin-left:10px;max-width:260px;" onchange="document.getElementById('push-userid').value=this.value">
          <option value="">-- 最近会话快捷选择 --</option>
        </select>
      </div>
      <div style="margin-bottom: 12px;">
        <label style="font-size:13px;color:#8a9aaa;margin-right:10px;">消息格式</label>
        <select id="push-format" style="background:#0d1b2a;color:#e0e0e0;border:1px solid #2a3a4a;border-radius:6px;padding:6px 12px;font-size:13px;">
          <option value="text">纯文本</option>
          <option value="markdown">Markdown</option>
          <option value="image">图片</option>
        </select>
      </div>
      <div id="push-content-wrap" style="margin-bottom: 12px;">
        <textarea id="push-content" placeholder="输入消息内容..." style="width:100%;min-height:120px;background:#0d1b2a;color:#e0e0e0;border:1px solid #2a3a4a;border-radius:8px;padding:12px;font-size:14px;font-family:inherit;resize:vertical;"></textarea>
      </div>
      <div id="push-image-wrap" style="margin-bottom: 12px; display:none;">
        <label style="font-size:13px;color:#8a9aaa;margin-right:10px;">选择图片</label>
        <input id="push-image" type="file" accept="image/*" style="background:#0d1b2a;color:#e0e0e0;border:1px solid #2a3a4a;border-radius:6px;padding:6px 12px;font-size:13px;max-width:70%;">
        <div id="push-image-preview" style="margin-top:10px;"></div>
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
        <thead><tr><th>日期</th><th>时间</th><th>来源</th><th>类型</th><th>发送人</th><th>内容预览</th><th>状态</th></tr></thead>
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

  <div class="footer">企微机器人 Web 管理面板 · 端口 8505</div>
</div>

<script>
function tagClass(t) {
  const m = {'text':'tag-text','image':'tag-image','mixed':'tag-mixed','file':'tag-file','voice':'tag-voice','video':'tag-video'};
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
}
async function loadMessages() {
  try {
    const r = await fetch('/api/messages');
    const d = await r.json();
    document.getElementById('msg-count').textContent = d.length + ' 条';
    const tbody = document.getElementById('msg-table');
    if (d.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:#5a6a7a;padding:30px;">暂无消息记录</td></tr>';
      return;
    }
    tbody.innerHTML = d.map(m => {
      const ft = m.full_time || '';
      const date = ft ? ft.split(' ')[0] : (m.time ? '' : '-');
      return `<tr>
      <td>${date || '-'}</td>
      <td>${m.time}</td>
      <td><span class="tag ${m.source === 'qq' ? 'tag-qq' : 'tag-text'}">${m.source === 'qq' ? 'QQ' : '企微'}</span></td>
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
  } else if (!content.trim()) {
    resultEl.innerHTML = '<span style="color:#ef4444;">请输入消息内容</span>';
    return;
  }
  statusEl.textContent = '发送中...';
  resultEl.innerHTML = '';
  try {
    const body = { target, format, content };
    if ((target === 'user' || isQq) && userid) body.userid = userid;
    if (format === 'image') {
      const file = document.getElementById('push-image').files[0];
      body.imageData = await readFileAsBase64(file);
      body.imageName = file.name;
    }
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
    const needsId = v === 'user' || isQq;
    useridInput.style.display = needsId ? 'inline' : 'none';
    qqSessionSel.style.display = isQq ? 'inline' : 'none';
    if (isQq) {
      // QQ 官方机器人支持文本与图片（图片经 base64 上传），不锁定格式
      onFormatChange();
      loadQQSessions();
    }
  }
  targetSel.addEventListener('change', refreshTargetUI);
  refreshTargetUI();
  const formatSel = document.getElementById('push-format');
  const contentWrap = document.getElementById('push-content-wrap');
  const imageWrap = document.getElementById('push-image-wrap');
  const imageInput = document.getElementById('push-image');
  const imagePreview = document.getElementById('push-image-preview');
  function onFormatChange() {
    const isImage = formatSel.value === 'image';
    contentWrap.style.display = isImage ? 'none' : '';
    imageWrap.style.display = isImage ? '' : 'none';
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

            if fmt == 'image':
                if not image_data:
                    self._serve_json({"success": False, "error": "图片数据不能为空"})
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
                ok, err = self._forward_qq_push(target, userid, content, fmt, image_data)
                if ok:
                    self._serve_json({"success": True, "detail": f"QQ推送成功 ({target}/{fmt})"})
                else:
                    self._serve_json({"success": False, "error": f"QQ推送失败: {err}"})
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

    def _forward_qq_push(self, target, openid, content, fmt='text', image_data=''):
        """通过 QQ 适配器内部端点转发推送（跨进程）
        fmt: text / image；image 时附带 base64 图片数据"""
        import urllib.request
        try:
            payload = {
                "target": "group" if target == 'qq_group' else "user",
                "openid": openid,
            }
            if fmt == 'image':
                # 图片推送：透传 base64 + 可选 caption
                payload["image"] = image_data
                if content.strip():
                    payload["caption"] = content
            else:
                payload["content"] = content
            req = urllib.request.Request(
                f"http://127.0.0.1:{QQ_PUSH_PORT}/push",
                data=json.dumps(payload).encode('utf-8'),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
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

                    records.append({
                        "time": time_str,
                        "full_time": full_time,
                        "type": mtype,
                        "user": user,
                        "preview": preview,
                        "status": "已处理"  # 历史消息标记为已处理
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
