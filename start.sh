#!/bin/bash
# 企业微信智能机器人 - 长连接模式 一键启动脚本
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================="
echo "  企业微信智能机器人（长连接模式）"
echo "========================================="

# 1. 检查 Python
PYTHON=""
for p in python3.11 python3 /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3; do
    if command -v "$p" &>/dev/null; then
        PYTHON="$p"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "[错误] 未找到 Python3"
    exit 1
fi
echo "[信息] 使用 Python: $PYTHON"

# 2. 创建虚拟环境并安装依赖
if [ ! -d "venv" ]; then
    echo "[信息] 创建虚拟环境..."
    $PYTHON -m venv venv
fi

echo "[信息] 安装依赖..."
source venv/bin/activate
pip install -r requirements.txt -q

# 3. 检查配置
python3 -c "
import sys
sys.path.insert(0, '.')
import config
if not config.BOT_ID or not config.BOT_SECRET:
    print('[错误] 请先编辑 config.py 填入 BOT_ID 和 BOT_SECRET')
    print('[提示] 获取方式：企业微信客户端 → 工作台 → 智能机器人 → API模式(长连接)')
    sys.exit(1)
else:
    print('[信息] 配置检查通过')
"

# 4. 启动服务
echo "[信息] 启动长连接服务..."
echo "[信息] 无需公网IP，无需内网穿透！"
echo "========================================="
python3 server.py
