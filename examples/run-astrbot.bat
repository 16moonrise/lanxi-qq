@echo off
rem 示例启动脚本：把三个变量改成你自己的路径
set ASTRBOT_DIR=C:\path\to\AstrBot
set PY=%ASTRBOT_DIR%\.venv\Scripts\python.exe
set LOG=C:\path\to\astrbot.log

cd /d %ASTRBOT_DIR%
"%PY%" -m astrbot.cli run >> "%LOG%" 2>&1
