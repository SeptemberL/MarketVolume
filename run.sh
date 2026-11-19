#!/bin/bash
# 启动上证指数量能监控(macOS 状态栏常驻)
# 使用: ./run.sh   或  双击
cd "$(dirname "$0")"
# 清掉可能残留的代理环境变量(沙箱/调试代理会让直连腾讯行情 Connection refused)
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy 2>/dev/null
exec ./venv/bin/python3 volume_menu.py
