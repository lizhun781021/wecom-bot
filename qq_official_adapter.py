#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQ 官方机器人适配器（基于腾讯官方 qq-botpy SDK）
====================================================
将 QQ 群聊（@触发）与单聊消息接入 wecom-bot 统一 AI 处理管线。

设计原则：本模块只做"协议适配"，不做"业务大脑"——
  - 收到 QQ 消息 → 统一转成 (from_user, text_content, file_paths) 三元组
  - 复用 server.py 的 call_teleagent / build_prompt / extract_file_paths / post_process_actions
  - 回复与文件回传：优先走 QQ 官方 API（post_group_message / post_c2c_message）
  - 与企微长连接互不影响，可同时运行（同一 8088 代理、同一套技能）

前置条件（config.py）：
    QQ_ENABLED = True
    QQ_APPID  = "平台审核通过后分配的 AppID"
    QQ_SECRET = "平台审核通过后分配的 AppSecret"

运行方式（单独进程，与 wecom-bot server.py 互不干扰）：
    python qq_official_adapter.py
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import sys
import time
import threading
import traceback
from pathlib import Path

# 允许从项目根目录导入 config / server
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402

# 仅当启用时导入 botpy，避免未配置时拖累主进程
if config.QQ_ENABLED:
    import botpy
    from botpy.message import GroupMessage, C2CMessage
    from botpy.ext.cog_yaml import read
    from botpy import logging as botpy_logging
else:
    # 未启用时提供占位，保证模块可导入（dashboard 查询状态用）
    class _PlaceholderClient:
        pass
    botpy = type("botpy", (), {"Client": _PlaceholderClient})

# 复用 server.py 的核心管线（不启动它的 WebSocket 主循环）
import server  # noqa: E402

logger = server.logger
# 关键：botpy 在 import 时执行 logging.basicConfig（不设 level，root 保持默认 WARNING），
# 会把我们 logger 的 INFO 日志过滤掉。这里显式把 level 提到 INFO，并保证 stdout 可输出。
import logging as _logging

logger.setLevel(_logging.INFO)
logger.propagate = False  # 独立控制，不受 botpy 的 root 配置影响
if not logger.handlers:
    _h = _logging.StreamHandler()
    _h.setFormatter(_logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_h)
# 文件输出：与企微主服务日志分离，便于单独排查 QQ 链路
try:
    _fh = _logging.FileHandler(os.path.join(PROJECT_ROOT, "qq-adapter-app.log"), encoding="utf-8")
    _fh.setFormatter(_logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_fh)
except Exception:
    pass

# ========== 运行状态 ==========
QQ_STATUS = {
    "running": False,
    "connected": False,
    "last_message_at": "",
    "last_error": "",
    "total_received": 0,
    "total_replied": 0,
}

# 最近活跃会话（用于主动推送/双向桥）：{"group": {openid: ts}, "user": {openid: ts}}
QQ_SESSION = {"group": {}, "user": {}}
_QQ_CLIENT = None  # 运行时挂载的 botpy Client（供主动推送）
_MSG_SEQ_MAP = {}  # 群聊被动回复 msg_seq 递增计数器 {message_id: seq}

# 每个群最近一次 @ 机器人的消息 {group_openid: {"msg_id": str, "time": float}}
# 用于面板群聊下发：复用最近 @ 消息的 msg_id 走被动回复通道（官方已下线群主动推送）
QQ_LAST_GROUP_MSG = {}
QQ_LAST_GROUP_MSG_LOCK = threading.Lock()
QQ_PASSIVE_TTL = 5 * 60  # 被动回复有效期（官方 5 分钟）

# 状态落地文件（dashboard 等跨进程读取用）
QQ_STATUS_FILE = os.path.join(PROJECT_ROOT, "qq_status.json")
QQ_MESSAGES_FILE = os.path.join(PROJECT_ROOT, "qq_messages.json")
QQ_MESSAGES = []  # 内存缓存最近100条
QQ_MESSAGES_LOCK = threading.Lock()


def _load_qq_messages():
    """启动时加载已落盘的历史消息，避免重启覆盖丢失历史"""
    global QQ_MESSAGES
    try:
        if os.path.exists(QQ_MESSAGES_FILE):
            with open(QQ_MESSAGES_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, list):
                QQ_MESSAGES = loaded[:100]
                logger.info(f"[QQ] 启动加载历史消息 {len(QQ_MESSAGES)} 条")
    except Exception as e:
        logger.warning(f"[QQ] 加载历史消息失败（忽略，继续空列表）: {e}")


_load_qq_messages()


def _persist_status():
    """把状态+会话写入 qq_status.json，供 dashboard（独立进程）读取"""
    try:
        payload = {
            "status": dict(QQ_STATUS),
            "session": {
                "group": {k: v for k, v in QQ_SESSION.get("group", {}).items()},
                "user": {k: v for k, v in QQ_SESSION.get("user", {}).items()},
            },
            "last_group_msg": {k: dict(v) for k, v in QQ_LAST_GROUP_MSG.items()},
            "msg_seq_map": dict(_MSG_SEQ_MAP),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(QQ_STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[QQ] 状态落盘失败: {e}")


def _load_last_group_msg():
    """启动时从 qq_status.json 恢复「群最近 @ 消息」记录（避免重启丢失被动回复通道）"""
    global _MSG_SEQ_MAP
    try:
        if os.path.exists(QQ_STATUS_FILE):
            with open(QQ_STATUS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            recs = data.get("last_group_msg") or {}
            now = time.time()
            for gid, rec in recs.items():
                if isinstance(rec, dict) and rec.get("msg_id") and now - rec.get("time", 0) <= QQ_PASSIVE_TTL:
                    QQ_LAST_GROUP_MSG[gid] = {"msg_id": rec["msg_id"], "time": rec["time"]}
            # 恢复 msg_seq 计数器（防止同 msg_id 重启后 seq 重置被 QQ 判定「消息去重」）
            seq_map = data.get("msg_seq_map") or {}
            if isinstance(seq_map, dict):
                _MSG_SEQ_MAP = {k: int(v) for k, v in seq_map.items() if isinstance(v, (int, float, str))}
    except Exception as e:
        logger.warning(f"[QQ] 加载群最近 @ 消息失败（忽略）: {e}")


_load_last_group_msg()


def _remember_session(kind: str, openid: str):
    """记录最近活跃会话（内存，最多保留 100 个），并同步落盘"""
    s = QQ_SESSION.setdefault(kind, {})
    s[openid] = time.time()
    if len(s) > 100:
        for k in sorted(s, key=s.get)[: len(s) - 100]:
            s.pop(k, None)
    _persist_status()


def _remember_group_msg(group_openid: str, msg_id: str):
    """记录某个群最近一次 @ 机器人的 msg_id（被动回复复用用），并同步落盘"""
    if not group_openid or not msg_id:
        return
    with QQ_LAST_GROUP_MSG_LOCK:
        QQ_LAST_GROUP_MSG[group_openid] = {"msg_id": msg_id, "time": time.time()}
        # 清理过期条目（防止内存膨胀）
        now = time.time()
        expired = [k for k, v in QQ_LAST_GROUP_MSG.items() if now - v.get("time", 0) > QQ_PASSIVE_TTL * 2]
        for k in expired:
            QQ_LAST_GROUP_MSG.pop(k, None)
    _persist_status()


def _get_last_group_msg(group_openid: str):
    """取某群最近一次 @ 消息；5 分钟外视为过期不可复用，返回 None"""
    if not group_openid:
        return None
    with QQ_LAST_GROUP_MSG_LOCK:
        rec = QQ_LAST_GROUP_MSG.get(group_openid)
        if not rec:
            return None
        if time.time() - rec.get("time", 0) > QQ_PASSIVE_TTL:
            return None
        return rec


def _display_name(openid: str, max_len: int = 40) -> str:
    """openid → 可读昵称（QQ_USER_MAP 手动映射），映射不到保留原值"""
    if not openid:
        return ""
    name = config.QQ_USER_MAP.get(openid) if hasattr(config, "QQ_USER_MAP") else None
    if not name:
        name = openid
    return str(name)[:max_len]


def _record_message(msg_type: str, user: str, preview: str, status: str = "处理中", scene: str = ""):
    """记录一条 QQ 消息（内存+落盘），供 dashboard（独立进程）合并展示

    scene: 来源场景，'group'=群聊 / 'single'=私聊 / ''=未知（面板显示'-'）
    """
    global QQ_MESSAGES
    rec = {
        "time": time.strftime("%H:%M:%S"),
        "full_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "qq",
        "type": msg_type,
        "user": _display_name(user),
        "preview": (preview or "")[:80],
        "status": status,
        "scene": scene if scene in ("group", "single") else "",
    }
    with QQ_MESSAGES_LOCK:
        QQ_MESSAGES.insert(0, rec)
        if len(QQ_MESSAGES) > 100:
            del QQ_MESSAGES[100:]
        # 落盘前防御合并：若当前进程内存为空而磁盘已有历史，说明是旧版本残留，
        # 保留磁盘历史（避免重启覆盖丢失）；正常情况磁盘与内存一致，直接写回
        try:
            if os.path.exists(QQ_MESSAGES_FILE):
                with open(QQ_MESSAGES_FILE, "r", encoding="utf-8") as f:
                    disk = json.load(f)
                if isinstance(disk, list) and disk:
                    disk_ids = {(r.get("full_time"), r.get("preview")) for r in disk[:100]}
                    mem_ids = {(r.get("full_time"), r.get("preview")) for r in QQ_MESSAGES}
                    extra = [r for r in disk if (r.get("full_time"), r.get("preview")) not in mem_ids]
                    if extra:
                        QQ_MESSAGES = (QQ_MESSAGES + extra)[:100]
        except Exception:
            pass  # 磁盘读取失败不影响本次写入
        try:
            with open(QQ_MESSAGES_FILE, "w", encoding="utf-8") as f:
                json.dump(QQ_MESSAGES, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[QQ] 消息落盘失败: {e}")


def qq_push_to_group(group_openid: str, content: str):
    """主动向 QQ 群推送文本（双向桥）。返回 True/False"""
    if not group_openid or not content:
        return False
    client = _QQ_CLIENT
    if client is None or _QQ_LOOP is None:
        logger.warning("[QQ] 主动推送失败：QQ 客户端未运行")
        return False
    try:
        future = asyncio.run_coroutine_threadsafe(
            client.api.post_group_message(group_openid=group_openid, msg_type=0, content=content[:3000]),
            _QQ_LOOP,
        )
        future.result(timeout=15)
        logger.info(f"[QQ] 主动推送群消息成功: {group_openid}")
        return True
    except Exception as e:
        logger.error(f"[QQ] 主动推送群消息失败: {group_openid}, {e}")
        return False


def qq_push_to_user(openid: str, content: str):
    """主动推送单聊消息（双向通道返回 True 或 False）"""
    if not openid or not content:
        return False
    client = _QQ_CLIENT
    if client is None or _QQ_LOOP is None:
        logger.warning("[QQ] 未推送失败（bot 客户端未运行）")
        return False
    try:
        future = asyncio.run_coroutine_threadsafe(
            client.api.post_c2c_message(openid=openid, msg_type=0, content=content[:3000]),
            _QQ_LOOP,
        )
        future.result(timeout=10)
        logger.info(f"[QQ] 主动推送单聊成功: {openid}")
        return True
    except Exception as e:
        logger.error(f"[QQ] 主动推送单聊失败: {openid}, {e}")
        return False


def qq_push_reply(target: str, content: str):
    """双向桥：TeleAgent 侧向 QQ 会话主动回消息（target 形如 group:xxx 或 user:xxx）"""
    if not target or not content:
        return False
    kind, _, openid = target.partition(":")
    if kind == "group":
        return qq_push_to_group(openid, content)
    return qq_push_to_user(openid, content)


def qq_push_image(kind: str, openid: str, image_b64: str, caption: str = ""):
    """向 QQ 群/单聊发送图片（base64 → 上传 media → 富媒体消息）。

    官方 v2 接口：POST /v2/groups/{group_openid}/files 支持 file_data(base64) 字段，
    botpy SDK 未封装，这里直接走 client.api._http.request。
    返回 (success, detail)
    """
    if not openid or not image_b64:
        return False, "缺少 openid 或图片数据"
    client = _QQ_CLIENT
    if client is None or _QQ_LOOP is None:
        return False, "QQ 客户端未运行"

    # 去掉可能的 data:image/xxx;base64, 前缀
    if "," in image_b64 and image_b64.strip().startswith("data:"):
        image_b64 = image_b64.split(",", 1)[1]

    # 图片大小校验（QQ 官方限制 base64 后 ≤ ~5MB）
    import base64 as _b64
    try:
        raw_len = len(_b64.b64decode(image_b64))
    except Exception:
        return False, "图片 base64 解码失败"
    if raw_len > 5 * 1024 * 1024:
        return False, "图片超过 5MB，请压缩后重试"

    try:
        # 1) 上传图片拿 media（file_info）
        payload = {
            "file_type": 1,  # 1=图片
            "file_data": image_b64,
            "srv_send_msg": False,
        }
        from botpy.http import Route
        if kind == "group":
            route = Route("POST", "/v2/groups/{group_openid}/files", group_openid=openid)
        else:
            route = Route("POST", "/v2/users/{openid}/files", openid=openid)

        future = asyncio.run_coroutine_threadsafe(
            client.api._http.request(route, json=payload), _QQ_LOOP
        )
        media = future.result(timeout=20)
        if not media or not media.get("file_info"):
            return False, f"图片上传失败: {media}"

        # 2) 发富媒体消息（msg_type=7）
        msg_payload = {
            "msg_type": 7,
            "media": media,
        }
        if caption:
            msg_payload["content"] = caption[:300]
        if kind == "group":
            msg_route = Route("POST", "/v2/groups/{group_openid}/messages", group_openid=openid)
        else:
            msg_route = Route("POST", "/v2/users/{openid}/messages", openid=openid)
        future3 = asyncio.run_coroutine_threadsafe(
            client.api._http.request(msg_route, json=msg_payload), _QQ_LOOP
        )
        result = future3.result(timeout=20)
        logger.info(f"[QQ] 图片发送成功: {kind}/{openid}")
        return True, "图片发送成功"
    except Exception as e:
        logger.error(f"[QQ] 图片发送失败: {kind}/{openid}, {e}")
        return False, str(e)


def _qq_chunked_upload(kind: str, openid: str, raw: bytes, filename: str = ""):
    """QQ 官方分片上传（>5MB 大文件）。

    官方协议（api-v2 / 富媒体分片上传，推荐）：
      1) POST /v2/users|groups/{openid}/upload_prepare  → upload_id + block_size + parts[]预签名URL
      2) 按 block_size 逐片 HTTP PUT 到 presigned_url
      3) 每片 PUT 成功后 POST /v2/users|groups/{openid}/upload_part_finish 通知完成
      4) 全部分片完成后 POST /v2/users|groups/{openid}/files 携带 upload_id 合并 → file_info

    支持任意格式文件（file_type=4），软/硬限制均为 200MB。
    返回 (success, file_info_or_detail)
    """
    import base64 as _b64
    import hashlib as _hl
    import urllib.request as _ur

    client = _QQ_CLIENT
    if client is None or _QQ_LOOP is None:
        return False, "QQ 客户端未运行"
    from botpy.http import Route

    size = len(raw)
    fname = filename or "file"
    md5 = _hl.md5(raw).hexdigest()
    sha1 = _hl.sha1(raw).hexdigest()
    md5_10m = _hl.md5(raw[:10002432]).hexdigest()  # 前 ~9.54MB 的 MD5（秒传判断）

    def _request(route, payload=None, timeout=60):
        fut = asyncio.run_coroutine_threadsafe(
            client.api._http.request(route, json=payload) if payload is not None
            else client.api._http.request(route),
            _QQ_LOOP,
        )
        return fut.result(timeout=timeout)

    try:
        # 1) 预上传
        prep_payload = {
            "file_type": 4,
            "file_size": str(size),
            "file_name": fname,
            "md5": md5,
            "sha1": sha1,
            "md5_10m": md5_10m,
        }
        if kind == "group":
            prep_route = Route("POST", "/v2/groups/{group_openid}/upload_prepare", group_openid=openid)
        else:
            prep_route = Route("POST", "/v2/users/{openid}/upload_prepare", openid=openid)
        prep = _request(prep_route, prep_payload, timeout=60)
        if not prep or not prep.get("upload_id"):
            return False, f"预上传失败: {prep}"
        upload_id = prep["upload_id"]
        parts = prep.get("parts") or []
        if not parts:
            return False, f"预上传未返回分片列表: {prep}"
        # 服务端下发分块大小（默认 5MB）；最后一片按剩余字节
        block_sizes = [int(p.get("block_size") or 0) for p in parts]
        default_block = block_sizes[0] if block_sizes and block_sizes[0] > 0 else 5 * 1024 * 1024
        logger.info(f"[QQ] 分片上传开始: {fname} {size}B, {len(parts)}片, upload_id={upload_id}")

        # 2)+3) 逐片 PUT + part_finish
        offset = 0
        for idx, part in enumerate(parts):
            p_index = part.get("index", idx)
            p_url = part.get("presigned_url", "")
            p_block = int(part.get("block_size") or default_block)
            if not p_url:
                return False, f"分片{idx}缺少预签名URL"
            chunk = raw[offset:offset + p_block]
            if not chunk:
                break
            # 3.1) PUT 分片到预签名 URL（直连 COS，不走 botpy session）
            req = _ur.Request(p_url, data=chunk, method="PUT")
            req.add_header("Content-Type", "application/octet-stream")
            with _ur.urlopen(req, timeout=120) as resp:
                if resp.status not in (200, 201, 204):
                    return False, f"分片{idx} PUT 失败: HTTP {resp.status}"
            # 3.2) 通知服务端该分片完成
            fin_payload = {
                "upload_id": upload_id,
                "part_index": p_index,
                "block_size": str(p_block),
                "md5": _hl.md5(chunk).hexdigest(),
            }
            if kind == "group":
                fin_route = Route("POST", "/v2/groups/{openid}/upload_part_finish", openid=openid)
            else:
                fin_route = Route("POST", "/v2/users/{openid}/upload_part_finish", openid=openid)
            fin = _request(fin_route, fin_payload, timeout=60)
            if fin is not None and isinstance(fin, dict) and fin.get("err_code"):
                return False, f"分片{idx} finish 失败: {fin}"
            offset += len(chunk)
            logger.info(f"[QQ] 分片 {idx + 1}/{len(parts)} 上传完成 ({len(chunk)}B)")

        # 4) 合并：携带 upload_id 调上传接口 → file_info
        merge_payload = {
            "file_type": 4,
            "srv_send_msg": False,
            "file_name": fname,
            "upload_id": upload_id,
        }
        if kind == "group":
            merge_route = Route("POST", "/v2/groups/{group_openid}/files", group_openid=openid)
        else:
            merge_route = Route("POST", "/v2/users/{openid}/files", openid=openid)
        media = _request(merge_route, merge_payload, timeout=120)
        if not media or not media.get("file_info"):
            return False, f"分片合并失败: {media}"
        logger.info(f"[QQ] 分片上传完成: {fname} → file_info 已获取")
        return True, media
    except Exception as e:
        logger.error(f"[QQ] 分片上传失败: {kind}/{openid}, {e}")
        return False, str(e)


def qq_push_file(kind: str, openid: str, file_b64: str, filename: str = "", caption: str = ""):
    """向 QQ 群/单聊发送文件（base64 → 上传 media → 富媒体消息）。

    官方 v2 接口：POST /v2/groups|users/{openid}/files 支持 file_type=4(文件)。
      - ≤5MB：file_data(base64) 直传（botpy SDK 注释"暂不开放"已过时，实测可用）
      - >5MB：官方分片上传（upload_prepare → PUT分片 → part_finish → 合并），上限 200MB
    返回 (success, detail)
    """
    if not openid or not file_b64:
        return False, "缺少 openid 或文件数据"
    client = _QQ_CLIENT
    if client is None or _QQ_LOOP is None:
        return False, "QQ 客户端未运行"

    # 去掉可能的数据前缀
    if "," in file_b64 and file_b64.strip().startswith("data:"):
        file_b64 = file_b64.split(",", 1)[1]

    import base64 as _b64
    try:
        raw = _b64.b64decode(file_b64)
    except Exception:
        return False, "文件 base64 解码失败"
    raw_len = len(raw)
    if raw_len > 200 * 1024 * 1024:
        return False, "文件超过 200MB，超出 QQ 官方硬限制"

    try:
        from botpy.http import Route
        if raw_len > 5 * 1024 * 1024:
            # 大文件 → 分片上传
            ok, media = _qq_chunked_upload(kind, openid, raw, filename)
            if not ok:
                return False, media
        else:
            # 小文件 → base64 直传
            payload = {
                "file_type": 4,  # 4=文件
                "file_data": file_b64,
                "srv_send_msg": False,
            }
            # 关键：必须带 file_name，否则 QQ 端显示「未命名」（官方字段，botpy 未封装）
            if filename:
                payload["file_name"] = filename
            if kind == "group":
                route = Route("POST", "/v2/groups/{group_openid}/files", group_openid=openid)
            else:
                route = Route("POST", "/v2/users/{openid}/files", openid=openid)

            future = asyncio.run_coroutine_threadsafe(
                client.api._http.request(route, json=payload), _QQ_LOOP
            )
            media = future.result(timeout=30)
            if not media or not media.get("file_info"):
                return False, f"文件上传失败: {media}"

        # 2) 发富媒体消息（msg_type=7）
        msg_payload = {
            "msg_type": 7,
            "media": media,
        }
        if caption:
            msg_payload["content"] = caption[:300]
        if kind == "group":
            msg_route = Route("POST", "/v2/groups/{group_openid}/messages", group_openid=openid)
        else:
            msg_route = Route("POST", "/v2/users/{openid}/messages", openid=openid)
        future3 = asyncio.run_coroutine_threadsafe(
            client.api._http.request(msg_route, json=msg_payload), _QQ_LOOP
        )
        result = future3.result(timeout=30)
        logger.info(f"[QQ] 文件发送成功: {kind}/{openid} ({filename or 'unnamed'})")
        return True, "文件发送成功"
    except Exception as e:
        logger.error(f"[QQ] 文件发送失败: {kind}/{openid}, {e}")
        return False, str(e)


def get_qq_status():
    """供 dashboard 或外部查询 QQ 运行状态"""
    return dict(QQ_STATUS)


def _update_status(**kwargs):
    for k, v in kwargs.items():
        if k in QQ_STATUS:
            QQ_STATUS[k] = v
    _persist_status()


def _download_attachment(url: str, save_dir: str, prefix: str) -> str | None:
    """下载 QQ 附件到本地，返回本地路径（失败返回 None）"""
    try:
        import requests
        resp = requests.get(url, timeout=30)
        if resp.status_code != 200:
            logger.error(f"QQ附件下载失败: HTTP {resp.status_code}")
            return None
        os.makedirs(save_dir, exist_ok=True)
        # 从 Content-Type 推断扩展名
        ctype = resp.headers.get("Content-Type", "")
        ext = ".bin"
        if "image" in ctype:
            ext = ".jpg"
        elif "pdf" in ctype:
            ext = ".pdf"
        elif "text" in ctype:
            ext = ".txt"
        path = os.path.join(save_dir, f"qq_{int(time.time())}_{prefix}{ext}")
        with open(path, "wb") as f:
            f.write(resp.content)
        logger.info(f"QQ附件已保存: {path} ({len(resp.content)} bytes)")
        return path
    except Exception as e:
        logger.error(f"QQ附件下载异常: {e}")
        return None


def _next_msg_seq(message) -> int:
    """被动回复 msg_seq 递增（群聊专用）。

    QQ 官方规则：同一 msg_id + msg_seq 不可重复；msg_seq 随每条回复递增。
    单聊默认 msg_seq=1 固定不变；群聊每次回复需递增 msg_seq，否则报重复。
    """
    if isinstance(message, GroupMessage):
        # 按消息 id 隔离计数（不同 @ 消息互不干扰）
        global _MSG_SEQ_MAP
        mid = getattr(message, "id", None) or "_default"
        n = _MSG_SEQ_MAP.get(mid, 0) + 1
        _MSG_SEQ_MAP[mid] = n
        # 清理老旧条目（防内存泄漏）
        if len(_MSG_SEQ_MAP) > 200:
            _MSG_SEQ_MAP = {k: v for k, v in _MSG_SEQ_MAP.items() if k == mid}
        # 同步落盘（重启后继续递增，避免同 msg_id 重复 msg_seq 被官方去重）
        _persist_status()
        return n
    return 1


def _reply_text(message, content: str, max_len: int = 3000):
    """统一回复文本（群聊 / 单聊），超长自动截断。

    群聊为被动回复（带 msg_id），需带递增 msg_seq。
    被动回复有效期 5 分钟，超时降级提示。（msg_id 有时间戳，但 SDK 未暴露，这里统一兜底）
    直接走 client.api._http.request（不依赖 message.reply()，兼容伪造消息对象）
    """
    if not content:
        return
    if len(content) > max_len:
        content = content[:max_len] + "\n\n(回复过长已截断)"
    client = _QQ_CLIENT
    if client is None or _QQ_LOOP is None:
        logger.warning("[QQ] 回复文本失败：QQ 客户端未运行")
        return None
    try:
        from botpy.http import Route
        if isinstance(message, GroupMessage):
            seq = _next_msg_seq(message)
            route = Route(
                "POST", "/v2/groups/{group_openid}/messages",
                group_openid=getattr(message, "group_openid", None),
            )
            payload = {
                "msg_type": 0,
                "content": content,
                "msg_id": getattr(message, "id", None),
                "msg_seq": seq,
            }
        elif isinstance(message, C2CMessage):
            route = Route(
                "POST", "/v2/users/{openid}/messages",
                openid=getattr(getattr(message, "author", None), "user_openid", None),
            )
            payload = {"msg_type": 0, "content": content, "msg_id": getattr(message, "id", None)}
        else:
            return None
        fut = asyncio.run_coroutine_threadsafe(
            client.api._http.request(route, json=payload), _QQ_LOOP
        )
        return fut
    except Exception as e:
        logger.error(f"QQ回复失败: {e}")
        return None


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


# ========== 消息处理核心（复用 server.py 管线） ==========

def _handle_qq_message(message, from_user, text_content, file_paths, is_group: bool):
    """QQ 消息统一入口：走 server.process_and_reply 的同款逻辑（但回复走 QQ 官方 API）"""
    try:
        # 显示名：优先 QQ_USER_MAP 手动映射，映射不到保留 openid
        display = _display_name(from_user, max_len=20)
        if is_group:
            user_name = display
        else:
            # 单聊：已映射直接用昵称（避免触发企微通讯录查询报错）；未映射再走企微姓名管线
            user_name = display if display != from_user else server.get_user_name(from_user)

        # 构建 prompt（复用 server.build_prompt）
        prompt = server.build_prompt(file_paths, text_content, user_name)
        time_str = time.strftime("%H:%M")
        session_title = f"{user_name} | QQ机器人 | {time_str}"

        logger.info(f"[QQ] 调用TeleAgent: 来源={from_user} (显示={user_name}), 有文件={bool(file_paths)}")

        # 调用 AI（阻塞等待，复用 8088 代理）
        result = server.call_teleagent(prompt, timeout=1800, session_title=session_title)
        _update_status(total_replied=QQ_STATUS["total_replied"] + 1)

        if not result:
            _reply_text(message, "抱歉，处理超时或出错了。请稍后重试。")
            return

        # 分离文本与文件
        text_reply, paths = _extract_text_and_files(result)
        _reply_text_and_files(message, text_reply, paths)

        # 配餐等后处理动作（复用企微的台账/待办逻辑，但跳过企微 WS 依赖的部分）
        # 说明：QQ 场景下只做轻量后处理（台账+待办），不生成企微文档、不依赖企微 WS
        try:
            peican_data = server.extract_peican_data(result, user_name, from_user)
            if peican_data:
                try:
                    import wecom_api
                    sheet_result = wecom_api.append_peican_record(peican_data)
                    if sheet_result.get("success"):
                        logger.info(f"[QQ] 配餐台账写入成功")
                    else:
                        logger.warning(f"[QQ] 配餐台账写入失败: {sheet_result.get('error')}")
                except Exception as e:
                    logger.warning(f"[QQ] 配餐台账写入异常: {e}")
        except Exception as e:
            logger.error(f"[QQ] 配餐后处理异常: {e}")
    except Exception as e:
        logger.error(f"[QQ] 处理消息异常: {e}")
        traceback.print_exc()
        _reply_text_and_files(message, f"处理出错: {e}", [])


def _reply_file_passive(message, file_path: str) -> bool:
    """群聊/私聊被动回复发文件（走 msg_id 被动通道，不受主动消息限制）。

    流程：读取本地文件 → base64 编码 → post files 上传拿 file_info →
          post messages(msg_type=7, media=file_info, msg_id=原消息id, msg_seq=递增)
    群聊被动回复有效期 5 分钟，超时则失败。
    返回 True/False
    """
    if not file_path or not os.path.isfile(file_path):
        logger.warning(f"[QQ] 被动回复发文件：文件不存在 {file_path}")
        return False
    client = _QQ_CLIENT
    if client is None or _QQ_LOOP is None:
        return False

    import base64 as _b64
    from botpy.http import Route

    try:
        with open(file_path, "rb") as f:
            raw = f.read()
        file_b64 = _b64.b64encode(raw).decode()
        fname = os.path.basename(file_path)
        size = len(raw)

        is_group = isinstance(message, GroupMessage)
        openid = getattr(message, "group_openid", None) or getattr(getattr(message, "author", None), "user_openid", None)
        if not openid:
            return False

        # 1) 上传文件拿 file_info（≤5MB 直传 / >5MB 分片）
        if size > 5 * 1024 * 1024:
            ok, media = _qq_chunked_upload("group" if is_group else "user", openid, raw, fname)
            if not ok:
                logger.error(f"[QQ] 被动回复上传文件失败: {media}")
                return False
        else:
            upload_payload = {
                "file_type": 4,
                "file_data": file_b64,
                "srv_send_msg": False,
                "file_name": fname,
            }
            if is_group:
                up_route = Route("POST", "/v2/groups/{group_openid}/files", group_openid=openid)
            else:
                up_route = Route("POST", "/v2/users/{openid}/files", openid=openid)
            fut = asyncio.run_coroutine_threadsafe(
                client.api._http.request(up_route, json=upload_payload), _QQ_LOOP
            )
            media = fut.result(timeout=30)
            if not media or not media.get("file_info"):
                logger.error(f"[QQ] 被动回复上传文件失败: {media}")
                return False

        # 2) 被动回复发富媒体消息（带 msg_id，不受主动消息限制）
        seq = _next_msg_seq(message)
        # 直接走 client.api（不依赖 message.reply()，兼容伪造消息对象测试）
        msg_route = Route(
            "POST",
            "/v2/groups/{group_openid}/messages" if is_group else "/v2/users/{openid}/messages",
            **({"group_openid": openid} if is_group else {"openid": openid}),
        )
        msg_payload = {"msg_type": 7, "media": media, "msg_id": getattr(message, "id", None), "msg_seq": seq}
        fut2 = asyncio.run_coroutine_threadsafe(
            client.api._http.request(msg_route, json=msg_payload), _QQ_LOOP
        )
        fut2.result(timeout=30)
        logger.info(f"[QQ] 被动回复文件成功: {fname} ({'群聊' if is_group else '私聊'})")
        return True
    except Exception as e:
        err_str = str(e)
        # 被动回复超时（5 分钟过期）
        if "40034105" in err_str or "主动消息" in err_str or "过期" in err_str or "无效" in err_str:
            logger.warning(f"[QQ] 被动回复发文件失败（可能已超5分钟有效期）: {fname}, {e}")
        else:
            logger.error(f"[QQ] 被动回复发文件异常: {fname}, {e}")
        return False


def _reply_text_and_files(message, text, file_paths):
    """先回复文字，再尝试被动回复发文件。

    群聊：走被动回复通道（msg_id），文件类型 msg_type=7+media，不受主动消息限制；
    私聊：同样走 message.reply() 被动通道；
    若被动回复超时（5分钟有效期满）或失败，降级提示文件路径。
    """
    _reply_text(message, text)
    if not file_paths:
        return

    sent = []
    failed = []
    for fp in file_paths:
        if _reply_file_passive(message, fp):
            sent.append(fp)
        else:
            failed.append(fp)

    if failed:
        # 降级提示：文件可通过其他途径查看
        names = "\n".join(os.path.basename(f) for f in failed)
        _reply_text(message, f"以下文件未能发送（可能回复超时或格式限制），请通过其他方式查看：\n{names}")
    if sent and not failed:
        logger.info(f"[QQ] 被动回复 {len(sent)} 个文件全部发送成功")


def qq_push_image_passive(message, image_b64: str, caption: str = "") -> tuple:
    """面板群聊下发图片：走被动回复通道（复用最近 @ 的 msg_id）。

    返回 (success, detail)
    """
    if not image_b64:
        return False, "缺少图片数据"
    client = _QQ_CLIENT
    if client is None or _QQ_LOOP is None:
        return False, "QQ 客户端未运行"

    # 去掉可能的 data:image/xxx;base64, 前缀
    if "," in image_b64 and image_b64.strip().startswith("data:"):
        image_b64 = image_b64.split(",", 1)[1]

    import base64 as _b64
    try:
        raw_len = len(_b64.b64decode(image_b64))
    except Exception:
        return False, "图片 base64 解码失败"
    if raw_len > 5 * 1024 * 1024:
        return False, "图片超过 5MB，请压缩后重试"

    try:
        from botpy.http import Route
        is_group = isinstance(message, GroupMessage)
        openid = getattr(message, "group_openid", None) or getattr(getattr(message, "author", None), "user_openid", None)
        if not openid:
            return False, "缺少 openid"
        # 1) 上传图片拿 media（file_type=1）
        payload = {"file_type": 1, "file_data": image_b64, "srv_send_msg": False}
        if is_group:
            up_route = Route("POST", "/v2/groups/{group_openid}/files", group_openid=openid)
        else:
            up_route = Route("POST", "/v2/users/{openid}/files", openid=openid)
        fut = asyncio.run_coroutine_threadsafe(
            client.api._http.request(up_route, json=payload), _QQ_LOOP
        )
        media = fut.result(timeout=30)
        if not media or not media.get("file_info"):
            return False, f"图片上传失败: {media}"
        # 2) 被动回复富媒体消息（带 msg_id + msg_seq）
        seq = _next_msg_seq(message)
        msg_payload = {"msg_type": 7, "media": media, "msg_id": getattr(message, "id", None), "msg_seq": seq}
        if caption:
            msg_payload["content"] = caption[:300]
        if is_group:
            msg_route = Route("POST", "/v2/groups/{group_openid}/messages", group_openid=openid)
        else:
            msg_route = Route("POST", "/v2/users/{openid}/messages", openid=openid)
        fut2 = asyncio.run_coroutine_threadsafe(
            client.api._http.request(msg_route, json=msg_payload), _QQ_LOOP
        )
        fut2.result(timeout=30)
        logger.info(f"[QQ] 被动回复图片成功: {openid}")
        return True, "图片发送成功"
    except Exception as e:
        err_str = str(e)
        if "40034105" in err_str or "主动消息" in err_str or "过期" in err_str or "无效" in err_str:
            logger.warning(f"[QQ] 被动回复发图片失败（可能已超5分钟有效期）: {e}")
        else:
            logger.error(f"[QQ] 被动回复发图片异常: {e}")
        return False, str(e)


# ========== botpy Client ==========

class QQOfficialClient(botpy.Client):
    """官方机器人客户端：监听群@消息 + 单聊消息"""

    async def on_ready(self):
        try:
            robot_name = getattr(self.robot, "name", "未知")
        except Exception:
            robot_name = "未知"
        logger.info(f"[QQ] 机器人「{robot_name}」已就绪 (on_ready)")
        _update_status(connected=True, last_error="")

    async def on_error(self, event_method: str, *args, **kwargs):
        """事件回调异常兜底：打印，不让异常静默吞掉"""
        logger.error(f"[QQ] 事件回调异常: {event_method}")
        traceback.print_exc()

    async def on_group_at_message_create(self, message: GroupMessage):
        """群聊内 @ 机器人 触发"""
        try:
            _update_status(total_received=QQ_STATUS["total_received"] + 1)
            content = (message.content or "").strip()
            # 去掉 @机器人 前缀（官方消息 content 含 <@!openid> 或 @机器人）
            content = re.sub(r"<@[^>]+>", "", content).strip()
            from_user = getattr(message.author, "member_openid", None) or "qq_group_user"

            # 调试日志：记录 msg_id + group_openid（用于被动回复文件测试）
            logger.info(
                f"[QQ] 群消息: group_openid={getattr(message, 'group_openid', None)} "
                f"msg_id={getattr(message, 'id', None)} content={content[:50]!r}"
            )

            # 统计信息
            _update_status(last_message_at=time.strftime("%H:%M:%S"))

            # 记录会话（供主动推送/双向桥）
            group_openid = getattr(message, "group_openid", None)
            if group_openid:
                _remember_session("group", group_openid)
                # 记录最近 @ 消息 msg_id（面板群聊下发走被动回复复用）
                _remember_group_msg(group_openid, getattr(message, "id", None))

            # 图片附件处理
            file_paths = []
            for att in (message.attachments or []):
                if att.url and att.content_type and "image" in att.content_type:
                    p = _download_attachment(att.url, config.IMAGE_SAVE_DIR, "group_img")
                    if p:
                        file_paths.append((p, "image"))

            if not content and not file_paths:
                return

            # 记录到消息列表（供面板消息记录展示）
            _record_message("text" if content else "image",
                            from_user,
                            content or ("图片" if any(p[1] == "image" for p in file_paths) else "消息"),
                            scene="group")

            # 异步处理（不阻塞事件循环）
            loop = asyncio.get_event_loop()
            loop.run_in_executor(None, lambda: _handle_qq_message(
                message, from_user, content, file_paths, is_group=True))
        except Exception as e:
            logger.error(f"[QQ] on_group_at_message_create 异常: {e}")

    async def on_c2c_message_create(self, message: C2CMessage):
        """处理单聊（私聊）消息"""
        try:
            _update_status(total_received=QQ_STATUS["total_received"] + 1)
            content = (message.content or "").strip()
            from_user = getattr(message.author, "user_openid", None) or "qq_c2c_user"
            _update_status(last_message_at=time.strftime("%H:%M:%S"))

            # 记录会话（供主动推送/双向桥）
            if from_user and from_user != "qq_c2c_user":
                _remember_session("user", from_user)

            file_paths = []
            for att in (message.attachments or []):
                if att.url:
                    ctype = att.content_type or ""
                    if "image" in ctype:
                        p = _download_attachment(att.url, config.IMAGE_SAVE_DIR, "c2c_img")
                        if p:
                            file_paths.append((p, "image"))
                    elif "pdf" in ctype or "text" in ctype:
                        p = _download_attachment(att.url, config.FILE_SAVE_DIR, "c2c_file")
                        if p:
                            file_paths.append((p, "file"))

            if not content and not file_paths:
                return

            # 记录到消息列表（供面板消息记录展示）
            _record_message("text" if content else "image",
                            from_user,
                            content or ("图片" if any(p[1] == "image" for p in file_paths) else "消息"),
                            scene="single")

            loop = asyncio.get_event_loop()
            loop.run_in_executor(None, lambda: _handle_qq_message(
                message, from_user, content, file_paths, is_group=False))
        except Exception as e:
            logger.error(f"[QQ] on_c2c_message_create 异常: {e}")


# ========== 本机内部 HTTP 推送端点（供 dashboard 跨进程调用） ==========
# dashboard（8505）与 QQ 适配器是独立进程，无法直接调用内存中的 _QQ_CLIENT。
# 这里在本机 127.0.0.1:18506 提供内部推送接口，dashboard 收到 QQ 推送请求后转发到这里。
QQ_PUSH_PORT = 18506


def _start_internal_http():
    """启动内部 HTTP 服务（仅绑定 127.0.0.1，不对外）"""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class _QQPushHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                # 特殊测试路由：/replyfile —— 用真实 msg_id 走被动回复发文件（绕过 AI）
                if self.path == "/replyfile":
                    self._handle_reply_file(body)
                    return
                target = body.get("target", "")
                content = body.get("content", "")
                openid = body.get("openid", "")
                image_b64 = body.get("image", "")  # 可选：图片 base64
                caption = body.get("caption", "")
                file_b64 = body.get("file", "")  # 可选：文件 base64
                filename = body.get("filename", "")  # 可选：文件名
                # 群聊下发：官方已下线群主动推送，统一走被动回复通道（复用最近 @ 的 msg_id）
                if target == "group":
                    rec = _get_last_group_msg(openid)
                    if not rec:
                        resp = json.dumps({
                            "success": False,
                            "error": "该群最近5分钟内没有 @ 机器人，请先在群内 @ 机器人一下，再重新发送",
                        }).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Content-Length", str(len(resp)))
                        self.end_headers()
                        self.wfile.write(resp)
                        return
                    # 构造带 msg_id 的被动回复消息（复用 _reply_* 逻辑）
                    from botpy.message import GroupMessage
                    class _FakeAuthor:
                        member_openid = "panel"
                    fake = GroupMessage.__new__(GroupMessage)
                    fake.group_openid = openid
                    fake.id = rec["msg_id"]
                    fake.author = _FakeAuthor()
                    if file_b64:
                        # 文件：base64 → 临时落盘 → 被动回复发文件
                        import base64 as _b64
                        import tempfile
                        tmp_path = ""
                        try:
                            raw = _b64.b64decode(file_b64)
                            tmp_path = os.path.join(tempfile.gettempdir(), f"qq_panel_{int(time.time()*1000)}_{filename or 'file'}")
                            with open(tmp_path, "wb") as f:
                                f.write(raw)
                            ok = _reply_file_passive(fake, tmp_path)
                            detail = "" if ok else "被动回复发文件失败（可能已超过5分钟有效期），见日志"
                        finally:
                            if tmp_path and os.path.exists(tmp_path):
                                try: os.remove(tmp_path)
                                except Exception: pass
                        resp = json.dumps({"success": ok, "error": detail if not ok else ""}).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Content-Length", str(len(resp)))
                        self.end_headers()
                        self.wfile.write(resp)
                        return
                    elif image_b64:
                        ok, detail = qq_push_image_passive(fake, image_b64, caption)
                    else:
                        fut = _reply_text(fake, content)
                        # _reply_text 返回 Future（异步）或 None（失败），统一解析为布尔
                        if fut is not None and hasattr(fut, "result"):
                            try:
                                fut.result(timeout=30)
                                ok, detail = True, ""
                            except Exception as e:
                                ok, detail = False, f"被动回复文本失败: {e}"
                        else:
                            ok, detail = False, "被动回复文本失败"
                    resp = json.dumps({"success": ok, "error": detail if not ok else ""}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(resp)))
                    self.end_headers()
                    self.wfile.write(resp)
                    return
                if file_b64:
                    # 文件推送
                    ok, detail = qq_push_file(target, openid, file_b64, filename, caption)
                elif image_b64:
                    # 图片推送
                    ok, detail = qq_push_image(target, openid, image_b64, caption)
                elif target == "group":
                    ok = qq_push_to_group(openid, content)
                elif target == "user":
                    ok = qq_push_to_user(openid, content)
                else:
                    ok, detail = False, "未知目标"
                if isinstance(ok, tuple):
                    ok, detail = ok
                resp = json.dumps({"success": ok, "error": detail if not ok else ""}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
            except Exception as e:
                resp = json.dumps({"success": False, "error": str(e)}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

        def log_message(self, format, *args):
            pass  # 静默，不打印内部接口日志

        def _handle_reply_file(self, body):
            """测试路由：用真实 msg_id 走被动回复发文件（绕过 AI 环节）。
            入参：group_openid, msg_id, file_path（本机绝对路径）
            """
            try:
                group_openid = body.get("group_openid", "")
                msg_id = body.get("msg_id", "")
                file_path = body.get("file_path", "")
                if not group_openid or not msg_id or not file_path:
                    self._send_json({"success": False, "error": "缺少 group_openid/msg_id/file_path"})
                    return
                from botpy.message import GroupMessage
                # 构造最小 GroupMessage（仅测试被动回复通道）
                class _FakeAuthor:
                    member_openid = "FAB0F1DD3BF78BA136E426153F06EAAA"
                fake = GroupMessage.__new__(GroupMessage)
                fake.group_openid = group_openid
                fake.id = msg_id
                fake.author = _FakeAuthor()
                ok = _reply_file_passive(fake, file_path)
                self._send_json({"success": ok, "error": "" if ok else "被动回复发文件失败，见日志"})
            except Exception as e:
                self._send_json({"success": False, "error": str(e)})

        def _send_json(self, obj):
            resp = json.dumps(obj).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

    try:
        httpd = HTTPServer(("127.0.0.1", QQ_PUSH_PORT), _QQPushHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        logger.info(f"[QQ] 内部推送端点已启动: http://127.0.0.1:{QQ_PUSH_PORT}")
    except Exception as e:
        logger.error(f"[QQ] 内部推送端点启动失败: {e}")


# ========== 启动入口 ==========

# READY 事件探针：包装 client.ws_dispatch，捕获 botpy 所有下行事件分发。
# 说明：botpy 的 on_ready 在某些版本/场景下不会被触发（SDK 内部 READY 分支只打日志），
# 这里在事件分发层挂钩，收到 ready 事件即确认连接成功——比 on_ready 更可靠。
def _install_ready_probe(client, loop):
    orig_dispatch = client.ws_dispatch

    def _probe(event, *args, **kwargs):
        try:
            if event == "ready":
                _update_status(connected=True, last_error="")
                logger.info("[QQ] READY 事件确认：QQ 机器人连接成功")
        except Exception as e:
            logger.error(f"[QQ] READY 探针异常: {e}")
        return orig_dispatch(event, *args, **kwargs)

    client.ws_dispatch = _probe
    # 看门狗：若 30 秒内未收到 READY，视为连接失败（便于排查）
    def _watchdog():
        time.sleep(30)
        if not QQ_STATUS.get("connected"):
            _update_status(last_error="30秒内未收到 READY 事件，连接可能异常")
            logger.error(QQ_STATUS["last_error"])
    threading.Thread(target=_watchdog, daemon=True).start()


def start_qq_bot():
    """启动 QQ 机器人（独立进程）"""
    if not config.QQ_ENABLED:
        logger.warning("[QQ] QQ_ENABLED=False，跳过启动")
        return
    if not config.QQ_APPID or not config.QQ_SECRET:
        logger.error("[QQ] 未配置 QQ_APPID / QQ_SECRET，请在 config.py 填写")
        return

    # 先启动内部推送端点（不依赖 botpy 连接，无连接时优雅返回 False）
    _start_internal_http()

    global _QQ_LOOP, _QQ_CLIENT
    _QQ_LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(_QQ_LOOP)

    intents = botpy.Intents(public_messages=True)
    client = QQOfficialClient(intents=intents)
    _QQ_CLIENT = client
    _install_ready_probe(client, _QQ_LOOP)

    try:
        _update_status(running=True)
        client.run(appid=config.QQ_APPID, secret=config.QQ_SECRET)
    except Exception as e:
        _update_status(connected=False, last_error=str(e))
        logger.error(f"[QQ] 启动失败: {e}")
        raise


if __name__ == "__main__":
    # 与企微主服务相互独立：QQ 适配器单独进程运行
    start_qq_bot()