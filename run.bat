@echo off
rem jobpilot Windows 一键启动：建环境 → 装依赖 → 起服务 → 打开控制台
rem 默认离线模式（不会访问招聘网站）。需要真实采集/投递时：
rem   先执行  set JOBPILOT_ALLOW_LIVE=1  再运行本脚本
setlocal
cd /d "%~dp0"
chcp 65001 >nul

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo 未找到 Python 3.11+，请先安装：https://www.python.org/downloads/
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo 创建虚拟环境（%PY%）...
  %PY% -m venv .venv || (echo 创建虚拟环境失败 & pause & exit /b 1)
)

echo 安装依赖...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (echo 依赖安装失败，请检查网络 & pause & exit /b 1)

echo 初始化数据库...
".venv\Scripts\python.exe" -c "import db; db.init()"

if "%JOBPILOT_ALLOW_LIVE%"=="" echo 提示：当前为离线模式（不访问招聘网站）。实测请先 set JOBPILOT_ALLOW_LIVE=1 再运行。

start "" http://127.0.0.1:8765
".venv\Scripts\python.exe" app.py

pause
