#!/bin/bash
# GLM Plan Dashboard - macOS 启动脚本

cd "$(dirname "$0")"

# 检查依赖
if ! python3 -c "import rumps" 2>/dev/null; then
    echo "正在安装依赖..."
    pip3 install -r requirements-macos.txt
fi

# 启动应用
python3 main-macos.py
