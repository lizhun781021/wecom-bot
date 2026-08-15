#!/usr/bin/env python3
"""
企业微信开放API封装模块
借鉴 wecom-cli 的文档/表格/待办能力，在 wecom-bot 内直接调用企微开放API。

功能：
1. 配餐结果写入企微在线表格（新建表格 + 表头 + 追加数据行）
2. AI自动创建待办提醒（通过 wecom-cli todo create_todo）
3. AI生成企微文档替代纯文字回复（新建文档 + 写入Markdown内容）

API端点参考：
- 新建文档/表格: POST /cgi-bin/wedoc/create_doc
- 编辑文档内容: POST /cgi-bin/wedoc/document/batch_update
- 表格追加行: POST /cgi-bin/wedoc/spreadsheet/append_data
- 表格区域写入: POST /cgi-bin/wedoc/spreadsheet/update_range
- 表格信息: POST /cgi-bin/wedoc/spreadsheet/get_sheet
- 待办: 通过 wecom-cli todo create_todo
"""

import json
import time
import logging
import subprocess
import requests

import config

logger = logging.getLogger(__name__)

WECOM_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"


def _get_access_token():
    """获取企微 access_token（复用 server.py 的缓存机制，避免重复获取）

    优先从 server.py 的缓存中取，如果没有则自己获取。
    """
    try:
        import server
        token = server._get_access_token()
        if token:
            return token
    except Exception:
        pass

    # 自己获取
    try:
        url = f"{WECOM_API_BASE}/gettoken"
        params = {
            "corpid": config.CORP_ID,
            "corpsecret": config.CORP_SECRET
        }
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        if data.get("errcode") == 0:
            return data["access_token"]
        else:
            logger.error(f"获取access_token失败: {data}")
            return None
    except Exception as e:
        logger.error(f"获取access_token异常: {e}")
        return None


def _wecom_cli(category, method, params):
    """通过 wecom-cli 调用企微 API（统一封装 JSON-RPC 解包）

    Args:
        category: 命令品类（doc / todo / contact 等）
        method: 方法名（create_doc / sheet_get_info 等）
        params: dict 参数

    Returns:
        dict: 解包后的 API 返回结果
    """
    try:
        cmd = ["wecom-cli", category, method, json.dumps(params)]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.returncode != 0:
            logger.error(f"wecom-cli 调用失败 [{category}.{method}]: {result.stderr}")
            return {"errcode": -1, "errmsg": result.stderr.strip()}

        output = result.stdout.strip()
        raw = json.loads(output) if output else {}

        # wecom-cli 返回 JSON-RPC 格式，需要解包
        if "result" in raw:
            content_list = raw.get("result", {}).get("content", [])
            if content_list:
                return json.loads(content_list[0].get("text", "{}"))
            return {}
        return raw

    except subprocess.TimeoutExpired:
        logger.error(f"wecom-cli 调用超时 [{category}.{method}]")
        return {"errcode": -1, "errmsg": "命令执行超时"}
    except Exception as e:
        logger.error(f"wecom-cli 调用异常 [{category}.{method}]: {e}")
        return {"errcode": -1, "errmsg": str(e)}


def _api_post(path, body, timeout=30):
    """通用 POST 请求封装（直接走企微开放API，需要自建应用有对应权限）"""
    token = _get_access_token()
    if not token:
        return {"errcode": -1, "errmsg": "无法获取access_token"}

    url = f"{WECOM_API_BASE}{path}?access_token={token}"
    try:
        resp = requests.post(url, json=body, timeout=timeout)
        result = resp.json()
        if result.get("errcode", 0) != 0:
            logger.error(f"API调用失败 [{path}]: {result}")
        return result
    except Exception as e:
        logger.error(f"API调用异常 [{path}]: {e}")
        return {"errcode": -1, "errmsg": str(e)}


# ============================================================
# 功能1：配餐结果写入企微在线表格
# ============================================================

# 配餐台账表格的表头
PEICAN_SHEET_HEADERS = [
    "时间", "处理人", "客户号码", "当前套餐", "出账金额",
    "推荐套餐", "套餐月费", "配餐路径", "提值空间", "备注"
]

# 持久化保存表格 docid 的文件
_SHEET_DOCID_FILE = None
_sheet_docid = None
_sheet_id = None


def _get_sheet_docid_file():
    """获取表格docid缓存文件路径"""
    global _SHEET_DOCID_FILE
    if _SHEET_DOCID_FILE is None:
        import os
        _SHEET_DOCID_FILE = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "peican_sheet_cache.json"
        )
    return _SHEET_DOCID_FILE


def _load_sheet_cache():
    """从本地加载表格docid和sheet_id缓存"""
    global _sheet_docid, _sheet_id
    import os
    cache_file = _get_sheet_docid_file()
    try:
        if os.path.exists(cache_file):
            with open(cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                _sheet_docid = data.get("docid")
                _sheet_id = data.get("sheet_id")
                logger.info(f"已加载配餐台账缓存: docid={_sheet_docid}, sheet_id={_sheet_id}")
    except Exception as e:
        logger.warning(f"加载配餐台账缓存失败: {e}")


def _save_sheet_cache(docid, sheet_id):
    """保存表格docid和sheet_id到本地"""
    global _sheet_docid, _sheet_id
    _sheet_docid = docid
    _sheet_id = sheet_id
    import os
    cache_file = _get_sheet_docid_file()
    try:
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump({"docid": docid, "sheet_id": sheet_id}, f, ensure_ascii=False)
        logger.info(f"配餐台账缓存已保存: docid={docid}, sheet_id={sheet_id}")
    except Exception as e:
        logger.warning(f"保存配餐台账缓存失败: {e}")


def _create_peican_sheet():
    """创建配餐台账在线表格，写入表头，返回 (docid, sheet_id) 或 (None, None)"""
    # Step 1: 新建空白表格（通过 wecom-cli，有文档权限）
    result = _wecom_cli("doc", "create_doc", {
        "doc_type": 4,  # 在线表格
        "doc_name": f"配餐台账_{time.strftime('%Y%m')}"
    })
    if result.get("errcode", -1) != 0:
        logger.error(f"创建配餐台账表格失败: {result}")
        return None, None

    docid = result.get("docid", "")
    url = result.get("url", "")
    logger.info(f"配餐台账表格已创建: docid={docid}, url={url}")

    if not docid:
        return None, None

    # Step 2: 获取表格信息，拿到默认子表的 sheet_id
    time.sleep(1)  # 等待表格创建完成
    info = _wecom_cli("doc", "sheet_get_info", {"docid": docid})
    if info.get("errcode", -1) != 0:
        logger.error(f"获取表格信息失败: {info}")
        return docid, None

    sheets = info.get("sheets", [])
    if not sheets:
        logger.error("表格没有子表")
        return docid, None

    sheet_id = sheets[0].get("sheet_id", "")
    logger.info(f"默认子表: sheet_id={sheet_id}, title={sheets[0].get('title')}")

    # Step 3: 写入表头（第一行）
    header_row = {
        "values": [
            {"cell_value": {"text": h}, "cell_format": {}}
            for h in PEICAN_SHEET_HEADERS
        ]
    }
    write_result = _wecom_cli("doc", "sheet_update_range_data", {
        "docid": docid,
        "sheet_id": sheet_id,
        "grid_data": {
            "start_row": 0,
            "start_column": 0,
            "rows": [header_row]
        }
    })
    if write_result.get("errcode", -1) != 0:
        logger.error(f"写入表头失败: {write_result}")
    else:
        logger.info(f"配餐台账表头已写入: {PEICAN_SHEET_HEADERS}")

    return docid, sheet_id


def _ensure_sheet():
    """确保表格已创建且有缓存的 docid/sheet_id，返回 (docid, sheet_id) 或 (None, None)"""
    global _sheet_docid, _sheet_id

    # 先检查内存缓存
    if _sheet_docid and _sheet_id:
        return _sheet_docid, _sheet_id

    # 加载本地缓存
    _load_sheet_cache()
    if _sheet_docid and _sheet_id:
        return _sheet_docid, _sheet_id

    # 没有缓存，创建新表格
    docid, sheet_id = _create_peican_sheet()
    if docid and sheet_id:
        _save_sheet_cache(docid, sheet_id)
        return docid, sheet_id

    return None, None


def append_peican_record(record_data):
    """向配餐台账追加一行数据

    Args:
        record_data: dict，key 对应 PEICAN_SHEET_HEADERS，如：
            {
                "时间": "2026-08-16 10:00",
                "处理人": "李准",
                "客户号码": "139xxxx",
                "当前套餐": "99元不限量",
                "出账金额": "120元",
                "推荐套餐": "129元5G融合",
                "套餐月费": "129元",
                "配餐路径": "平替升级",
                "提值空间": "30元/月",
                "备注": "客户同意办理"
            }

    Returns:
        dict: {"success": bool, "url": str, "error": str}
    """
    docid, sheet_id = _ensure_sheet()
    if not docid or not sheet_id:
        return {"success": False, "url": "", "error": "无法创建或获取配餐台账表格"}

    # 按表头顺序构造行数据
    values = []
    for header in PEICAN_SHEET_HEADERS:
        text = str(record_data.get(header, ""))
        values.append({"cell_value": {"text": text}, "cell_format": {}})

    result = _wecom_cli("doc", "sheet_append_data", {
        "docid": docid,
        "sheet_id": sheet_id,
        "row": {"values": values}
    })

    if result.get("errcode", -1) != 0:
        # 表格可能被删了，尝试重建
        logger.warning(f"追加数据失败，尝试重建表格: {result}")
        global _sheet_docid, _sheet_id
        _sheet_docid = None
        _sheet_id = None
        docid, sheet_id = _create_peican_sheet()
        if docid and sheet_id:
            _save_sheet_cache(docid, sheet_id)
            # 重试追加
            result = _wecom_cli("doc", "sheet_append_data", {
                "docid": docid,
                "sheet_id": sheet_id,
                "row": {"values": values}
            })
            if result.get("errcode", -1) != 0:
                return {"success": False, "url": "", "error": f"追加失败: {result}"}
        else:
            return {"success": False, "url": "", "error": "重建表格失败"}

    sheet_url = f"https://doc.weixin.qq.com/sheet/{docid}"
    logger.info(f"配餐记录已追加到台账: {sheet_url}")
    return {"success": True, "url": sheet_url, "error": ""}


# ============================================================
# 功能2：AI自动创建待办提醒
# ============================================================

def create_todo(content, follower_userid, end_time=None, remind_type_list=None):
    """通过 wecom-cli 创建企微待办

    Args:
        content: 待办内容
        follower_userid: 参与人 userid（如准哥的 sscblizhun）
        end_time: 截止时间，格式 "YYYY-MM-DD HH:mm:ss"，默认3天后
        remind_type_list: 提醒方式列表，默认 [1]（到期时提醒）
            0=不提醒, 1=到期时, 3=提前15分钟, 5=提前1小时,
            6=提前2小时, 7=提前1天, 8=提前2天, 9=提前1周

    Returns:
        dict: {"success": bool, "todo_id": str, "error": str}
    """
    if not end_time:
        # 默认3天后
        from datetime import datetime, timedelta
        dt = datetime.now() + timedelta(days=3)
        end_time = dt.strftime("%Y-%m-%d %H:%M:%S")

    if remind_type_list is None:
        remind_type_list = [1]  # 到期时提醒

    params = {
        "content": content,
        "follower_list": {
            "followers": [
                {"follower_id": follower_userid, "follower_status": 1}
            ]
        },
        "end_time": end_time,
        "remind_type_list": remind_type_list
    }

    try:
        cmd = ["wecom-cli", "todo", "create_todo", json.dumps(params)]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode != 0:
            logger.error(f"wecom-cli 待办创建失败: {result.stderr}")
            return {"success": False, "todo_id": "", "error": result.stderr.strip()}

        output = result.stdout.strip()
        # wecom-cli 返回 JSON-RPC 格式，需要解包
        raw = json.loads(output) if output else {}
        if "result" in raw:
            # JSON-RPC 包装格式
            content_list = raw.get("result", {}).get("content", [])
            if content_list:
                data = json.loads(content_list[0].get("text", "{}"))
            else:
                data = {}
        else:
            data = raw

        if data.get("errcode", -1) == 0:
            todo_id = data.get("todo_id", "")
            logger.info(f"待办已创建: todo_id={todo_id}, content={content[:50]}")
            return {"success": True, "todo_id": todo_id, "error": ""}
        else:
            logger.error(f"wecom-cli 待办创建返回错误: {data}")
            return {"success": False, "todo_id": "", "error": str(data)}

    except subprocess.TimeoutExpired:
        logger.error("wecom-cli 待办创建超时")
        return {"success": False, "todo_id": "", "error": "命令执行超时"}
    except Exception as e:
        logger.error(f"创建待办异常: {e}")
        return {"success": False, "todo_id": "", "error": str(e)}


# ============================================================
# 功能3：AI生成企微文档替代纯文字回复
# ============================================================

def create_wecom_doc(doc_name, markdown_content):
    """创建企微文档并写入Markdown内容

    Args:
        doc_name: 文档标题
        markdown_content: Markdown格式的内容

    Returns:
        dict: {"success": bool, "url": str, "docid": str, "error": str}
    """
    # Step 1: 新建空白文档（通过 wecom-cli，有文档权限）
    result = _wecom_cli("doc", "create_doc", {
        "doc_type": 3,  # 普通文档
        "doc_name": doc_name
    })
    if result.get("errcode", -1) != 0:
        logger.error(f"创建文档失败: {result}")
        return {"success": False, "url": "", "docid": "", "error": f"创建文档失败: {result}"}

    docid = result.get("docid", "")
    url = result.get("url", "")
    logger.info(f"文档已创建: docid={docid}, url={url}")

    if not docid:
        return {"success": False, "url": "", "docid": "", "error": "未获得docid"}

    # Step 2: 写入内容（通过 wecom-cli edit_doc_content，支持 Markdown）
    time.sleep(1)  # 等待文档创建完成

    # edit_doc_content 支持直接写入 Markdown 内容（content_type=1）
    write_result = _wecom_cli("doc", "edit_doc_content", {
        "docid": docid,
        "content": markdown_content,
        "content_type": 1  # Markdown 格式
    })

    if write_result.get("errcode", -1) != 0:
        logger.warning(f"edit_doc_content 写入失败，尝试 batch_update 降级: {write_result}")
        # 降级：用 batch_update 逐行插入（单次最多30个操作，每25行一批）
        lines = markdown_content.split('\n')
        requests_list = []
        for line in lines:
            requests_list.append({
                "insert_text": {
                    "text": line + "\n",
                    "location": {"index": 1}
                }
            })
            if len(requests_list) >= 25:
                _api_post("/wedoc/document/batch_update", {
                    "docid": docid,
                    "requests": requests_list
                })
                requests_list = []
        # 写入剩余的行
        if requests_list:
            batch_result = _api_post("/wedoc/document/batch_update", {
                "docid": docid,
                "requests": requests_list
            })
            if batch_result.get("errcode", -1) != 0:
                logger.error(f"batch_update 降级也失败: {batch_result}")
                return {"success": False, "url": url, "docid": docid,
                        "error": f"内容写入失败: edit_doc_content={write_result}, batch_update={batch_result}"}
    else:
        logger.info(f"文档内容写入成功: {doc_name}")

    return {"success": True, "url": url, "docid": docid, "error": ""}


# ============================================================
# 测试入口
# ============================================================

if __name__ == '__main__':
    import sys

    if len(sys.argv) < 2:
        print("用法:")
        print("  测试表格: python wecom_api.py sheet")
        print("  测试待办: python wecom_api.py todo")
        print("  测试文档: python wecom_api.py doc")
        sys.exit(1)

    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

    cmd = sys.argv[1]

    if cmd == "sheet":
        print("=== 测试配餐台账表格 ===")
        result = append_peican_record({
            "时间": time.strftime("%Y-%m-%d %H:%M"),
            "处理人": "测试",
            "客户号码": "13900000000",
            "当前套餐": "99元不限量",
            "出账金额": "120元",
            "推荐套餐": "129元5G融合",
            "套餐月费": "129元",
            "配餐路径": "平替升级",
            "提值空间": "30元/月",
            "备注": "自动测试记录"
        })
        print(f"结果: {result}")

    elif cmd == "todo":
        print("=== 测试创建待办 ===")
        result = create_todo(
            content="【测试】星小辰自动创建的待办，请忽略",
            follower_userid="sscblizhun",
            end_time=time.strftime("%Y-%m-%d 18:00:00"),
            remind_type_list=[1]
        )
        print(f"结果: {result}")

    elif cmd == "doc":
        print("=== 测试创建文档 ===")
        result = create_wecom_doc(
            doc_name=f"星小辰测试文档_{time.strftime('%Y%m%d_%H%M')}",
            markdown_content="# 配餐方案\n\n客户：13900000000\n\n## 当前套餐\n99元不限量\n\n## 推荐套餐\n129元5G融合\n\n## 配餐路径\n平替升级\n\n## 提值空间\n30元/月\n"
        )
        print(f"结果: {result}")
