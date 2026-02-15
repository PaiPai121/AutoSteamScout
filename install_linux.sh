#!/bin/bash

# AutoSteamScout 一键安装脚本 (Linux 版)
# 用于自动安装所有依赖和配置环境

set -e  # 遇到错误时停止执行

echo "==========================================="
echo "AutoSteamScout 一键安装脚本 (Linux 版)"
echo "==========================================="

# 检查是否安装了 Python3
if ! command -v python3 &> /dev/null; then
    echo "错误: 未找到 Python3，请先安装 Python3"
    echo "Ubuntu/Debian: sudo apt update && sudo apt install python3 python3-pip python3-venv"
    echo "CentOS/RHEL/Fedora: sudo yum install python3 python3-pip 或 sudo dnf install python3 python3-pip"
    exit 1
fi

echo "✓ Python3 已找到"

# 检查是否安装了 pip
if ! command -v pip3 &> /dev/null; then
    echo "错误: 未找到 pip3，请先安装 pip"
    echo "Ubuntu/Debian: sudo apt install python3-pip"
    echo "CentOS/RHEL/Fedora: sudo yum install python3-pip 或 sudo dnf install python3-pip"
    exit 1
fi

echo "✓ pip3 已找到"

# 检查是否有虚拟环境模块
if ! python3 -c "import venv" &> /dev/null; then
    echo "错误: Python venv 模块不可用"
    echo "Ubuntu/Debian: sudo apt install python3-venv"
    echo "CentOS/RHEL/Fedora: sudo yum install python3-venv 或 sudo dnf install python3-venv"
    exit 1
fi

echo "✓ venv 模块可用"

echo "正在创建虚拟环境..."


echo "正在激活虚拟环境..."
source venv/bin/activate

echo "正在升级 pip..."
pip install --upgrade pip

echo "正在安装项目依赖..."
pip install -r requirements.txt

echo "正在安装 Playwright 并下载 Chromium 浏览器..."
playwright install chromium

echo "正在验证 Playwright 安装..."
playwright install-deps chromium

echo "正在安装额外的可能需要的包..."
pip install asyncio aiofiles

echo "==========================================="
echo "安装完成！"
echo "==========================================="
echo ""
echo "使用说明："
echo "1. 每次运行项目前，请先激活虚拟环境："
echo "   source venv/bin/activate"
echo ""
echo "2. 或者，你也可以使用以下命令运行项目："
echo "   ./venv/bin/python your_script.py"
echo ""
echo "3. 如果需要退出虚拟环境，可以运行："
echo "   deactivate"
echo ""
echo "现在你可以运行你的项目了！"
echo "==========================================="