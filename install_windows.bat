@echo off
chcp 65001 > nul

echo ===========================================
echo AutoSteamScout 一键安装脚本 (Windows 版)
echo ===========================================

REM 检查是否安装了 Python
python --version > nul 2>&1
if errorlevel 1 (
    echo 错误: 未找到 Python，请先安装 Python
    echo 请访问 https://www.python.org/downloads/ 下载并安装 Python
    pause
    exit /b 1
)

echo ✓ Python 已找到

REM 检查是否安装了 pip
pip --version > nul 2>&1
if errorlevel 1 (
    echo 错误: 未找到 pip，请先安装 pip
    pause
    exit /b 1
)

echo ✓ pip 已找到

echo 正在创建虚拟环境...
python -m venv venv

echo 正在激活虚拟环境...
call venv\Scripts\activate.bat

echo 正在升级 pip...
python -m pip install --upgrade pip

echo 正在安装项目依赖...
pip install -r requirements.txt

echo 正在安装 Playwright 并下载 Chromium 浏览器...
playwright install chromium

echo 正在验证 Playwright 安装...
playwright install-deps chromium

echo 正在安装额外的可能需要的包...
pip install asyncio aiofiles

echo ===========================================
echo 安装完成！
echo ===========================================
echo.
echo 使用说明：
echo 1. 每次运行项目前，请先激活虚拟环境：
echo    venv\Scripts\activate.bat
echo.
echo 2. 或者，你也可以直接运行：
echo    venv\Scripts\python.exe your_script.py
echo.
echo 现在你可以运行你的项目了！
echo ===========================================

pause