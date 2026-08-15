#!/usr/bin/env python3
"""
企业微信主动推送模块

支持两种推送方式：
1. 群机器人 Webhook —— 主动推送消息到群聊（text / markdown / image / news）
2. 应用消息 API —— 主动推送消息到指定同事的企微应用（text / textcard / markdown / image / file）

使用示例：
    from push import push_to_group, push_to_user

    # 推送到群聊
    push_to_group("今天收入数据已更新，请查收")

    # 推送 markdown 到群聊
    push_to_group_markdown("## 收入日报\n- 总收入：1.2亿\n- 同比：+5.2%")

    # 推送到指定同事
    push_to_user("wo-nRCBgAA1oVvwfR286z-ksQVxcnGKA", "你有一份配餐方案待查看")

    # 推送文件到指定同事
    push_file_to_user("wo-nRCBgAA1oVvwfR286z-ksQVxcnGKA", "/path/to/report.docx")
"""

import os
import json
import time
import base64
import hashlib
import logging
import requests
import threading

import config

logger = logging.getLogger(__name__)

# ========== access_token 缓存（与 server.py 独立，避免循环导入问题）==========
_access_token = None
_access_token_expire = 0
_token_lock = threading.Lock()


def _get_access_token():
    """获取企微 access_token，带缓存（有效期2小时，提前5分钟刷新）"""
    global _access_token, _access_token_expire
    with _token_lock:
        now = time.time()
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
                logger.error(f"[push] 获取access_token失败: {data}")
                return None
            _access_token = data["access_token"]
            _access_token_expire = now + data.get("expires_in", 7200)
            logger.info("[push] access_token 获取成功")
            return _access_token
        except Exception as e:
            logger.error(f"[push] 获取access_token异常: {e}")
            return None


# ========== 素材上传 ==========

def upload_media(file_path, media_type="file"):
    """
    上传临时素材到企微，返回 media_id
    :param file_path: 本地文件路径
    :param media_type: 素材类型（image / voice / video / file）
    :return: media_id 或 None
    """
    token = _get_access_token()
    if not token:
        logger.error("[push] 上传素材失败：无access_token")
        return None
    if not os.path.exists(file_path):
        logger.error(f"[push] 文件不存在: {file_path}")
        return None
    try:
        url = f"https://qyapi.weixin.qq.com/cgi-bin/media/upload?access_token={token}&type={media_type}"
        filename = os.path.basename(file_path)
        with open(file_path, "rb") as f:
            files = {"media": (filename, f)}
            resp = requests.post(url, files=files, timeout=60)
        data = resp.json()
        if data.get("errcode", 0) != 0:
            logger.error(f"[push] 上传素材失败: {data}")
            return None
        media_id = data.get("media_id")
        logger.info(f"[push] 素材上传成功: {filename} → {media_id}")
        return media_id
    except Exception as e:
        logger.error(f"[push] 上传素材异常: {e}")
        return None


# ========== 群机器人 Webhook 推送 ==========

def _webhook_send(payload):
    """通过群机器人 Webhook 发送消息（内部方法）"""
    webhook_url = config.WEBHOOK_URL
    if not webhook_url:
        logger.warning("[push] WEBHOOK_URL 未配置，无法推送群聊消息")
        return {"errcode": -1, "errmsg": "WEBHOOK_URL未配置"}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        data = resp.json()
        if data.get("errcode") == 0:
            logger.info(f"[push] Webhook推送成功: {payload.get('msgtype', '?')}")
        else:
            logger.error(f"[push] Webhook推送失败: {data}")
        return data
    except Exception as e:
        logger.error(f"[push] Webhook推送异常: {e}")
        return {"errcode": -1, "errmsg": str(e)}


def push_to_group(content, mentioned_list=None, mentioned_mobile_list=None):
    """
    推送文本消息到群聊（通过 Webhook）
    :param content: 文本内容
    :param mentioned_list: @的成员userid列表，["@all"] 表示@所有人
    :param mentioned_mobile_list: @的手机号列表
    :return: API返回结果
    """
    payload = {
        "msgtype": "text",
        "text": {
            "content": content,
            "mentioned_list": mentioned_list or [],
            "mentioned_mobile_list": mentioned_mobile_list or []
        }
    }
    return _webhook_send(payload)


def push_to_group_markdown(content):
    """
    推送 Markdown 消息到群聊（通过 Webhook）
    :param content: Markdown 文本
    :return: API返回结果
    """
    payload = {
        "msgtype": "markdown",
        "markdown": {"content": content}
    }
    return _webhook_send(payload)


def push_to_group_image(image_path):
    """
    推送图片到群聊（通过 Webhook）
    :param image_path: 本地图片路径（jpg/png，不超过2MB，base64后不超过2MB）
    :return: API返回结果
    """
    if not os.path.exists(image_path):
        logger.error(f"[push] 图片不存在: {image_path}")
        return {"errcode": -1, "errmsg": "图片不存在"}
    try:
        with open(image_path, "rb") as f:
            img_data = f.read()
        img_b64 = base64.b64encode(img_data).decode()
        img_md5 = hashlib.md5(img_data).hexdigest()
        payload = {
            "msgtype": "image",
            "image": {
                "base64": img_b64,
                "md5": img_md5
            }
        }
        return _webhook_send(payload)
    except Exception as e:
        logger.error(f"[push] 推送图片异常: {e}")
        return {"errcode": -1, "errmsg": str(e)}


def push_to_group_news(title, description, url, pic_url=""):
    """
    推送图文链接到群聊（通过 Webhook）
    :param title: 标题
    :param description: 描述
    :param url: 点击跳转URL
    :param pic_url: 缩略图URL（可选）
    :return: API返回结果
    """
    payload = {
        "msgtype": "news",
        "news": {
            "articles": [{
                "title": title,
                "description": description,
                "url": url,
                "picurl": pic_url
            }]
        }
    }
    return _webhook_send(payload)


# ========== 应用消息 API 推送（1v1）==========

def _appmessage_send(msg_data, touser=None, toparty=None, totag=None):
    """
    通过应用消息API发送消息（内部方法）
    :param msg_data: 消息体（含msgtype和对应类型字段）
    :param touser: 接收人userid，多个用|分隔，"@all"表示全员
    :param toparty: 接收部门id，多个用|分隔
    :param totag: 接收标签id，多个用|分隔
    :return: API返回结果
    """
    token = _get_access_token()
    if not token:
        logger.error("[push] 应用消息发送失败：无access_token")
        return {"errcode": -1, "errmsg": "无access_token"}
    if not touser and not toparty and not totag:
        logger.error("[push] 应用消息发送失败：未指定接收人")
        return {"errcode": -1, "errmsg": "未指定接收人"}
    try:
        payload = {
            "touser": touser or "",
            "toparty": toparty or "",
            "totag": totag or "",
            "agentid": config.AGENT_ID,
            **msg_data
        }
        url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
        resp = requests.post(url, json=payload, timeout=15)
        data = resp.json()
        if data.get("errcode") == 0:
            logger.info(f"[push] 应用消息推送成功: {msg_data.get('msgtype', '?')} → {touser or toparty or totag}")
        else:
            logger.error(f"[push] 应用消息推送失败: {data}")
        return data
    except Exception as e:
        logger.error(f"[push] 应用消息推送异常: {e}")
        return {"errcode": -1, "errmsg": str(e)}


def push_to_user(userid, content):
    """
    推送文本消息到指定同事的企微应用（1v1）
    :param userid: 同事的企微userid，多个用|分隔，"@all"表示全员
    :param content: 文本内容（最长2048字节）
    :return: API返回结果
    """
    return _appmessage_send(
        {"msgtype": "text", "text": {"content": content}},
        touser=userid
    )


def push_textcard_to_user(userid, title, description, url, btntxt="详情"):
    """
    推送文本卡片消息到指定同事（1v1）
    :param userid: 同事的企微userid
    :param title: 卡片标题
    :param description: 描述（支持Markdown子集）
    :param url: 点击跳转URL
    :param btntxt: 按钮文字
    :return: API返回结果
    """
    return _appmessage_send(
        {
            "msgtype": "textcard",
            "textcard": {
                "title": title,
                "description": description,
                "url": url,
                "btntxt": btntxt
            }
        },
        touser=userid
    )


def push_markdown_to_user(userid, content):
    """
    推送 Markdown 消息到指定同事（1v1）
    注意：Markdown消息仅在企业微信内展示，微信端不可见
    :param userid: 同事的企微userid
    :param content: Markdown 文本（最长2048字节）
    :return: API返回结果
    """
    return _appmessage_send(
        {"msgtype": "markdown", "markdown": {"content": content}},
        touser=userid
    )


def push_image_to_user(userid, image_path):
    """
    推送图片到指定同事（1v1）
    :param userid: 同事的企微userid
    :param image_path: 本地图片路径
    :return: API返回结果
    """
    media_id = upload_media(image_path, media_type="image")
    if not media_id:
        return {"errcode": -1, "errmsg": "图片上传失败"}
    return _appmessage_send(
        {"msgtype": "image", "image": {"media_id": media_id}},
        touser=userid
    )


def push_file_to_user(userid, file_path):
    """
    推送文件到指定同事（1v1）
    :param userid: 同事的企微userid
    :param file_path: 本地文件路径
    :return: API返回结果
    """
    media_id = upload_media(file_path, media_type="file")
    if not media_id:
        return {"errcode": -1, "errmsg": "文件上传失败"}
    return _appmessage_send(
        {"msgtype": "file", "file": {"media_id": media_id}},
        touser=userid
    )


# ========== 便捷方法 ==========

def push_notification(title, content, to_user=None, to_group=True):
    """
    便捷推送：同时推送到群聊和个人（如果配置了）
    :param title: 标题（群聊作为markdown标题，个人作为textcard标题）
    :param content: 正文内容
    :param to_user: 指定userid，None则不推送个人
    :param to_group: 是否推送群聊
    :return: (群聊结果, 个人结果)
    """
    group_result = None
    user_result = None

    if to_group and config.WEBHOOK_URL:
        markdown_content = f"**{title}**\n\n{content}"
        group_result = push_to_group_markdown(markdown_content)

    if to_user:
        user_result = push_to_user(to_user, f"{title}\n\n{content}")

    return group_result, user_result


# ========== 命令行入口（测试用）==========

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s'
    )

    if len(sys.argv) < 2:
        print("用法:")
        print("  推送群聊文字:  python push.py group \"消息内容\"")
        print("  推送群聊MD:    python push.py group_md \"## 标题\n内容\"")
        print("  推送群聊图片:  python push.py group_img /path/to/image.jpg")
        print("  推送个人文字:  python push.py user <userid> \"消息内容\"")
        print("  推送个人文件:  python push.py user_file <userid> /path/to/file.docx")
        print()
        print(f"  WEBHOOK_URL: {'已配置' if config.WEBHOOK_URL else '未配置'}")
        print(f"  AGENT_ID: {config.AGENT_ID}")
        print(f"  CORP_ID: {config.CORP_ID}")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "group":
        if len(sys.argv) < 3:
            print("请提供消息内容")
            sys.exit(1)
        result = push_to_group(sys.argv[2])
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "group_md":
        if len(sys.argv) < 3:
            print("请提供Markdown内容")
            sys.exit(1)
        result = push_to_group_markdown(sys.argv[2])
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "group_img":
        if len(sys.argv) < 3:
            print("请提供图片路径")
            sys.exit(1)
        result = push_to_group_image(sys.argv[2])
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "user":
        if len(sys.argv) < 4:
            print("用法: python push.py user <userid> \"消息内容\"")
            sys.exit(1)
        result = push_to_user(sys.argv[2], sys.argv[3])
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "user_file":
        if len(sys.argv) < 4:
            print("用法: python push.py user_file <userid> /path/to/file")
            sys.exit(1)
        result = push_file_to_user(sys.argv[2], sys.argv[3])
        print(json.dumps(result, ensure_ascii=False, indent=2))

    else:
        print(f"未知命令: {cmd}")
        sys.exit(1)
