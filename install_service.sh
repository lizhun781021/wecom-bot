#!/bin/bash
# 企业微信机器人 launchd 持久化服务配置

PLIST_NAME="com.wecom.bot"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"
SCRIPT_DIR="/Users/lizhun/Desktop/星小辰工作空间/wecom-bot"
VENV_PYTHON="${SCRIPT_DIR}/venv/bin/python3"

cat > "$PLIST_PATH" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_NAME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${VENV_PYTHON}</string>
        <string>${SCRIPT_DIR}/server.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${SCRIPT_DIR}/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${SCRIPT_DIR}/stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
EOF

echo "已生成 launchd 配置: $PLIST_PATH"
echo ""
echo "启动服务: launchctl load $PLIST_PATH"
echo "停止服务: launchctl unload $PLIST_PATH"
echo "查看状态: launchctl list | grep wecom"
echo "查看日志: tail -f ${SCRIPT_DIR}/wecom-bot.log"
