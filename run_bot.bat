@echo off
cd /d "%~dp0"
echo ========================================================
echo   Starting Supermarket Ops Agent Telegram Bot...
echo ========================================================
.\.venv\Scripts\python.exe bot.py
pause
