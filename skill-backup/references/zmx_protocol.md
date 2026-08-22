---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '175de0b4-3c50-494d-a341-72386a7c0a9e'
  PropagateID: '175de0b4-3c50-494d-a341-72386a7c0a9e'
  ReservedCode1: 'a2827807-9bac-4b84-b05e-b3bab464beb1'
  ReservedCode2: 'a2827807-9bac-4b84-b05e-b3bab464beb1'
---

# 量子密信收发协议详解

## 双模式架构

量子密信支持两种接入模式，通过 `config.ZMX_MODE` 切换：

| | webhook 模式（原有） | DCOOS 模式（v1.0.0 新增） |
|---|---|---|
| **鉴权** | URL `?key=<KEY>` | Headers: `X-APP-ID` + `X-APP-KEY` + `clientId` |
| **消息类型** | text / markdown（图片文件受限） | text / image / file / voice / video / markdown / card |
| **多群推送** | 单个 groupId | groupIds 数组（一次推多群） |
| **可转发控制** | 无 | canForward 字段（禁止转发） |
| **回调** | 明文 POST /webhook | 加密验签 POST /callback（HMAC-SHA256 + AES-256-CBC） |
| **回调场景** | 群聊@机器人 | 单聊 / @所有人 / @部分人（mentionType） |
| **文件上传** | webhook key（实测7001不可用） | AppID/AppKey 鉴权（DCOOS 平台订阅后可用） |

## 平台机制

量子密信（中国电信）机器人是**回调模式**，与企微/QQ 的 WebSocket 长连接不同：
- 群里 @机器人 → 量子密信平台把消息 POST 到开发者指定的回调 URL（需公网可达）
- 开发者处理完 → 用平台回调中携带的 `callBackUrl` 发送回复

**没有长连接 SDK**，这是平台设计决定，不是代码能绕过的。

## 出站发送协议

### 发送文本
```
POST https://imtwo.zdxlz.com/im-external/v1/webhook/send?key=<KEY>
Content-Type: application/json

{
  "type": "text",
  "textMsg": {"content": "消息内容"},
  "phone": "18800001111",
  "groupId": "123456"
}
```

### 发送 Markdown
```
POST https://imtwo.zdxlz.com/im-external/v1/webhook/send?key=<KEY>
Content-Type: application/json

{
  "type": "markdown",
  "markdown": {"title": "卡片标题", "content": "## 正文\n**加粗**"},
  "phone": "18800001111",
  "groupId": "123456"
}
```
> **title 必填**，否则服务端返回 500 且消息不送达。title 从内容首行提取，清洗行首语法标记和行内符号，截断 24 字符。

### 上传附件（图片/文件）— ⚠️ 平台限制，当前不可用
```
POST https://imtwo.zdxlz.com/im-external/v1/webhook/upload-attachment?key=<KEY>&type=1
Content-Type: multipart/form-data

# type=1 图片, type=2 文件
# 返回: {"ok": true, "code": 200, "data": {"fileId": "xxx"}}
```
> **实测**：该端点返回 code 7001"机器人不存在"，webhook key 无上传权限。量子密信目前仅支持文本和 Markdown 消息。旧代码中路径 `/im-api/v1/webhook/upload-attachment` 返回 404，正确路径为 `/im-external/v1/webhook/upload-attachment`。

### 发送图片/文件（用 fileId）
```
POST https://imtwo.zdxlz.com/im-external/v1/webhook/send?key=<KEY>

# 图片
{"type": "image", "imageMsg": {"fileId": "xxx"}, "phone": "...", "groupId": "..."}

# 文件
{"type": "file", "fileMsg": {"fileId": "xxx"}, "phone": "...", "groupId": "..."}
```

### 成功响应
```json
{"ok": true, "code": 200}
```

### 限流
每个 webhook key 约 20 RPM（60 秒窗口 20 条）。

## 入站回调协议

量子密信平台 POST 到开发者指定的回调 URL：

```json
{
  "type": "text",
  "callBackUrl": "https://impre.zdxlz.com/im-external/v1/webhook/send?key=1234567890",
  "callBackMethod": "POST",
  "phone": "18800001111",
  "groupId": "123456",
  "tenantId": 1,
  "robotId": "aaa123456",
  "textMsg": {
    "content": "查询合肥天气"
  }
}
```

**关键字段**：
- `callBackUrl`: **群专属回复地址**，必须用它发送回复（多群隔离，不能用全局配置的 URL）
- `phone`: 发送者手机号
- `groupId`: 群 ID
- `textMsg.content`: 消息内容（目前只支持文本）

**回调地址注册方式**（二选一）：
1. 打通内网（公网入口直接暴露回调服务）
2. 注册到 DCOOS 平台（由密信端订阅，需联系运营人员）

## zmx_adapter.py 架构

### 独立进程模式
仿 `qq_official_adapter.py`，`import server` 复用核心管线，不启动企微 WebSocket 主循环。

### 复用的 server.py 函数
- `server.call_teleagent(prompt, timeout, session_title)` → AI 回复文本
- `server.build_prompt(file_paths, text_content, user_name)` → 构建 prompt
- `server.extract_file_paths(result)` → 提取 AI 回复中的文件路径
- `server.get_session_title("密信", "群聊", user_name, group_id or phone)` → 会话标题（注意：display_name 用 user_name 而非 group_id，session_key 用 group_id or phone）

### 不复用的函数
- `server.get_user_name(phone)` → **不调用**（会触发企微通讯录 API IP 白名单报错，量子密信用户直接用 phone）

### 发送函数签名
```python
zmx_send_text(content, group_id="", phone="", callback_url="")
zmx_send_markdown(content, group_id="", phone="", callback_url="")
zmx_upload_and_send(file_path, group_id="", phone="", as_image=False, callback_url="")
```
> `callback_url` 优先用回调携带的 `callBackUrl`，为空时退回全局 `ZMX_CALLBACK_URL`。

## 公网入口方案：SSH 反向隧道

Mac 在 NAT 后面，用 autossh 主动连公网服务器建反向隧道：

```
量子密信平台 → 公网服务器:1011 → SSH反向隧道 → Mac localhost:1011 → zmx_adapter → localhost:8088 AI
```

### 云服务器配置
1. `/etc/ssh/sshd_config` 设置 `GatewayPorts clientspecified`（让反向隧道监听 0.0.0.0 而非 127.0.0.1）
2. `iptables -I INPUT -p tcp --dport 1011 -j ACCEPT`
3. 天翼云控制台安全组入站规则放行 TCP 1011（**两层防火墙缺一不可**）

### Mac 端配置
1. SSH 免密登录（ed25519 密钥对）
2. autossh 命令：
```bash
AUTOSSH_GATETIME=0 autossh -M 0 -N \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes \
  -R 0.0.0.0:1011:localhost:1011 \
  root@<server_ip>
```
3. launchd 保活（`com.<your_name>.zmx-tunnel.plist`，KeepAlive=true）

### 替代方案
- Cloudflare Tunnel（cloudflared）：免费，无需公网服务器
- ngrok：免费版有限制
- frp：需一台公网服务器自建

---

## DCOOS 平台模式协议（v1.0.0 新增）

### 前置条件
1. DCOOS 平台订阅 SKU（ITSM 审批通过后下发）
2. 开发者后台新建应用 → 获取 AppID / AppKey
3. 应用内新建应用机器人 → 获取 clientId
4. 密信群聊中添加机器人到群

### 鉴权 Headers
```
X-APP-ID:   <应用AppID>
X-APP-KEY:  <应用密钥>
clientId:    <机器人clientId>
Content-Type: application/json
```

### 消息推送接口
- 测试：`https://jt-eop-test.dcoos.189.cn:19443/serviceAgent/rest/forcustomers/robots/message/send`
- 生产：`https://10.141.243.200:8443/serviceAgent/rest/forcustomers/robots/message/send`
- 方法：POST
- Body 通用结构：`{"type": "...", "content": {...}, "groupIds": [...], "canForward": true}`

#### 消息类型速查
| type | content 必填字段 | 可选字段 |
|------|----------------|----------|
| text | content | — |
| image | fileId, width, height, mimeType | altText |
| file | fileId, fileName, size, mimeType | — |
| voice | fileId, duration(ms), mimeType | — |
| video | fileId, duration(ms), width, height, mimeType | thumbnail |
| markdown | title, content | — |
| card | title, content, pcLayout, tailFields | imageFileId, url, msgUid |

#### 卡片消息 tailFields 按钮结构
```json
{
  "name": "确定",
  "type": "button",
  "value": {
    "placeholder": "k1",
    "value": "v1",
    "style": 1,          // 1/2/3 三种按钮样式
    "url": "http://callback",
    "method": "POST",
    "data": {"t1": "aaa"}
  },
  "index": 1,
  "show": true
}
```

#### pcLayout 布局
`[[1,2],[3,4]]` 表示索引 1,2 按钮放第一列，3,4 放第二列。

### 文件上传接口
- 测试：`https://jt-eop-test.dcoos.189.cn:19443/serviceAgent/rest/im-external/v1/webhook/upload-attachment`
- 生产：`https://10.141.243.200:8443/serviceAgent/rest/zdxlz/im-external/v1/webhook/upload-attachment`
- 方法：POST（multipart/form-data）
- Body：`type`（1=图片, 2=文件）+ `file`（二进制流）
- 返回：`{"ok": true, "code": 200, "data": {"id": "fileId", "name": "...", "type": "...", "size": 12345}}`

### 回调加密验签
- 回调路径：`POST /callback`（开发者平台事件订阅中填入）
- Headers：`X-CTQ-Timestamp` / `X-CTQ-Nonce` / `X-CTQ-Signature`
- 加密配置：`encryptedKey`（加密密钥）+ `verificationToken`（验签 Token）

#### 验签算法
```
signature = HMAC-SHA256(key=verificationToken, msg=timestamp+nonce+data) → 小写十六进制
```

#### 解密算法
```
1. base64 解码 data
2. 会话密钥 = SHA-256(encryptedKey + ":" + timestamp + ":" + nonce)
3. IV = 前16字节, ciphertext = 第16字节起
4. AES-256-CBC 解密 + PKCS7 去填充
```

#### 解密后消息结构
```json
{
  "userId": "123456",
  "groupId": "123456",       // 群聊必填，单聊为空
  "mentionType": 1,            // 1=单聊, 2=@所有人, 3=@部分人
  "msgId": "消息ID",
  "type": "text",
  "content": {}
}
```

### 错误码
| 码 | 说明 |
|----|------|
| 5000 | 系统异常 |
| 5001 | 查询机器人失败 |
| 5002 | 机器人未关联群组 |
| 5003 | 机器人不在群组中 |
| 5004 | 不支持的消息类型 |
| 5005 | 解析消息内容失败 |
| 5006 | 按钮解析失败 |
| 5007 | 发送消息异常 |
| 5008 | 参数解析错误 |
| 5016 | 图片不存在 |
| 5017 | 单聊 userId 不能为空 |

### zmx_adapter.py DCOOS 函数签名
```python
# 发送
dcoos_send_text(content, group_ids, can_forward=True)
dcoos_send_markdown(title, content, group_ids, can_forward=True)
dcoos_send_image(file_id, width, height, mime_type, group_ids, alt_text="", can_forward=True)
dcoos_send_file(file_id, file_name, size, mime_type, group_ids, can_forward=True)
dcoos_send_voice(file_id, duration_ms, mime_type, group_ids, can_forward=True)
dcoos_send_video(file_id, duration_ms, width, height, mime_type, group_ids, thumbnail="", can_forward=True)
dcoos_send_card(title, content, group_ids, image_file_id="", url="", msg_uid="", pc_layout=None, tail_fields=None, can_forward=True)

# 上传 + 发送
dcoos_upload_file(file_path, upload_type="2")  # 返回 {"id", "name", "type", "size"}
dcoos_upload_and_send_image(file_path, group_ids, alt_text="")
dcoos_upload_and_send_file(file_path, group_ids)
dcoos_upload_and_send_voice(file_path, duration_ms, group_ids)
dcoos_upload_and_send_video(file_path, duration_ms, group_ids, thumbnail_file_id="")

# 加密验签
_dcoos_verify_signature(timestamp, nonce, verify_token, data, signature) → bool
_dcoos_decrypt(encrypted_key, verify_token, timestamp, nonce, signature, data_b64) → str
```