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
# 功能4：API模式文档能力（智能表格 smartsheet 系列）
# ============================================================
# 企微「智能机器人 → API模式 → 文档能力」以 MCP 方式暴露（create_doc /
# edit_doc_content / smartsheet_add_sheet / get_sheet / add_fields /
# update_fields / get_fields / add_records）。
# 调用前置条件：
#   1. 管理后台 → 智能机器人 → 编辑 → 可使用权限 → 授权「文档」
#      （成员授权有效期 7 天，过期需重新授权）
#   2. 拿到 streamableHTTP URL 或 JSON Config 后配置到本模块
# 机器人只能编辑自己创建的文档。
#
# 本模块提供两层封装：
#   - smartsheet_* 系列：直接按 MCP 工具语义封装（HTTP 调用）
#   - create_smartsheet_with_headers()：一键创建"带表头的智能表格"
#     （内部自动处理"默认字段重命名"的坑）

# MCP 服务地址（streamableHTTP URL 或 JSON Config 里提取）
MCP_BASE_URL = getattr(config, "WECOM_MCP_URL", "").rstrip("/")


def _mcp_call(tool_name, arguments, timeout=60):
    """调用企微文档 MCP 工具（streamableHTTP JSON-RPC 格式）

    Args:
        tool_name: doc_create / doc_contents_overwrite / smartsheet_sheets_list /
                   smartsheet_fields_list / smartsheet_fields_add /
                   smartsheet_records_add 等（实际暴露的工具名，与官方文档名不同）
        arguments: dict，工具入参（与 MCP schema 一致）

    Returns:
        dict: {"success": bool, "data": dict, "error": str}
    """
    if not MCP_BASE_URL:
        return {"success": False, "data": {}, "error": "未配置 WECOM_MCP_URL（请先在企微后台授权文档能力并填入 streamableHTTP URL）"}

    payload = {
        "jsonrpc": "2.0",
        "id": int(time.time() * 1000) % 100000000,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments
        }
    }
    # 必须带 Accept: application/json, text/event-stream，否则返回 HTTP 406
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    # 若在 config 中配置了 Bearer Token（JSON Config 里通常有），则带上
    mcp_token = getattr(config, "WECOM_MCP_TOKEN", "")
    if mcp_token:
        headers["Authorization"] = f"Bearer {mcp_token}"

    try:
        resp = requests.post(MCP_BASE_URL, json=payload, headers=headers, timeout=timeout)
        result = resp.json()
        # MCP 返回格式：{"jsonrpc":"2.0","id":...,"result":{"content":[{"type":"text","text":"..."}]}}
        content_list = result.get("result", {}).get("content", [])
        if content_list:
            text = content_list[0].get("text", "{}")
            try:
                data = json.loads(text)
            except Exception:
                data = {"raw": text}
            if data.get("errcode", 0) == 0 or "docid" in data or "url" in data:
                return {"success": True, "data": data, "error": ""}
            return {"success": False, "data": data, "error": data.get("errmsg", text)}
        # 没有 content 说明是错误响应
        error_info = result.get("error", {})
        return {"success": False, "data": {}, "error": error_info.get("message", json.dumps(result, ensure_ascii=False)[:200])}
    except Exception as e:
        logger.error(f"MCP调用异常 [{tool_name}]: {e}")
        return {"success": False, "data": {}, "error": str(e)}


def mcp_create_doc(doc_name, doc_type="doc", content="", fields=None, sheet_title=None):
    """新建文档/表格/智能表格（MCP 工具 doc_create）

    Args:
        doc_name: 文档标题
        doc_type: "doc" / "sheet" / "smartsheet"（字符串，实测确认，非整数）
        content: 初始纯文本内容（doc 时有效）
        fields: 初始化字段列表，doc_type=smartsheet 时有效
        sheet_title: 子表名称，doc_type=smartsheet 时必须传（实测确认必填）

    Returns:
        dict: {"success": bool, "data": dict, "error": str}
    """
    args = {"doc_name": doc_name, "doc_type": doc_type, "content": ""}
    if fields:
        args["fields"] = fields
    if sheet_title:
        args["sheet_title"] = sheet_title
    return _mcp_call("doc_create", args)


def mcp_overwrite_doc_content(docid, content, content_type="markdown"):
    """全量覆盖 Word 文档内容（MCP 工具名 doc_contents_overwrite）"""
    return _mcp_call("doc_contents_overwrite", {
        "docid": docid, "content": content, "content_type": content_type
    })


def mcp_smartsheet_get_sheets(docid):
    """查询智能表格子表列表（MCP 工具名 smartsheet_sheets_list）"""
    return _mcp_call("smartsheet_sheets_list", {"docid": docid})


def mcp_smartsheet_get_fields(docid, sheet_id):
    """查询智能表格字段列表（MCP 工具名 smartsheet_fields_list）"""
    return _mcp_call("smartsheet_fields_list", {"docid": docid, "sheet_id": sheet_id, "type": "fields"})


def mcp_smartsheet_add_fields(docid, sheet_id, fields):
    """添加智能表格字段（MCP 工具名 smartsheet_fields_add）

    fields: [{"field_title": "...", "field_type": "text"}, ...]
    """
    return _mcp_call("smartsheet_fields_add", {
        "docid": docid, "sheet_id": sheet_id, "type": "add", "fields": fields
    })


def mcp_smartsheet_add_records(docid, sheet_id, records):
    """添加智能表格记录（MCP 工具名 smartsheet_records_add）

    records: [{"values": {"字段标题": 值, ...}}, ...]
    """
    return _mcp_call("smartsheet_records_add", {
        "docid": docid, "sheet_id": sheet_id, "type": "add", "records": records
    })


def create_smart_sheet_with_headers(doc_name, headers):
    """一键创建带表头的智能表格（实测最优方案：doc_create 一步建表）

    实测结论（2026-08-17）：
    - MCP 工具名是 doc_create（不是官方文档里的 create_doc）
    - doc_type 传字符串 "smartsheet"（不是整数 10）
    - 必须传 sheet_title，否则报错"创建智能表格时 sheet_title 为必填项"
    - 直接传 fields 数组即可一步建出指定表头，无需"创建默认表→删默认字段→重命名"
      的弯路（默认建的 5 个字段：文本/数字/日期/单选/人员，会带示例数据，且
      smartsheet_fields_update 无法改标题——实测返回 640027 无效）

    Args:
        doc_name: 智能表格名称
        headers: 表头列表，如 ["时间", "处理人", "客户号码"]

    Returns:
        dict: {"success": bool, "docid": str, "sheet_id": str, "url": str, "error": str}
    """
    if not headers:
        return {"success": False, "docid": "", "sheet_id": "", "url": "", "error": "表头不能为空"}

    fields = [
        {"field_title": h, "field_type": "number" if h in ("金额", "数量", "出账", "费用", "价格") else "text"}
        for h in headers
    ]
    result = mcp_create_doc(doc_name, doc_type="smartsheet", fields=fields, sheet_title="台账")
    if not result.get("success"):
        return {"success": False, "docid": "", "sheet_id": "", "url": "",
                "error": f"创建智能表格失败: {result.get('error')}"}
    docid = result["data"].get("docid", "")
    url = result["data"].get("url", "")
    if not docid:
        return {"success": False, "docid": "", "sheet_id": "", "url": "",
                "error": f"未获得docid: {result['data']}"}
    logger.info(f"智能表格已创建: docid={docid}, url={url}")

    # 查默认子表（第一个 smartsheet 类型子表）
    time.sleep(1)
    sheet_result = mcp_smartsheet_get_sheets(docid)
    if not sheet_result.get("success"):
        return {"success": False, "docid": docid, "sheet_id": "", "url": url,
                "error": f"获取子表失败: {sheet_result.get('error')}"}
    sheets = sheet_result["data"].get("sheets", [])
    smartsheets = [s for s in sheets if s.get("type") == "smartsheet"]
    if not smartsheets:
        return {"success": False, "docid": docid, "sheet_id": "", "url": url,
                "error": "智能表格无默认子表"}
    sheet_id = smartsheets[0].get("sheet_id", "")
    logger.info(f"默认子表: sheet_id={sheet_id}")

    logger.info(f"智能表格表头初始化完成: {headers}")
    return {"success": True, "docid": docid, "sheet_id": sheet_id, "url": url, "error": ""}


def add_smart_sheet_records(docid, sheet_id, records):
    """向智能表格添加记录

    Args:
        docid: 智能表格 docid
        sheet_id: 子表 sheet_id
        records: [{"字段标题": 值, ...}, ...]
            文本字段值格式: [{"type": "text", "text": "内容"}]（数组，不能传单个对象）
            数字: 直接传数字；复选框: true/false；单选/多选: [{"text": "选项"}]
            日期时间: "YYYY-MM-DD HH:MM:SS"
            函数内部会自动包成 MCP 需要的 {"values": {...}} 结构

    Returns:
        dict: {"success": bool, "data": dict, "error": str}
    """
    # MCP 需要 records 元素为 {"values": {"字段标题": 值}}，这里做格式归一
    wrapped = []
    for rec in records:
        if isinstance(rec, dict) and "values" in rec:
            wrapped.append(rec)
        else:
            wrapped.append({"values": rec})
    return mcp_smartsheet_add_records(docid, sheet_id, wrapped)


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
        print("  测试智能表格: python wecom_api.py smartsheet")
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
