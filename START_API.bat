@echo off
title Arena API (proxy)
cd /d "%~dp0"
echo [Arena API] Starting proxy. On first start a browser window will open -
echo [Arena API] log in to arena.ai ONCE, after that everything is automatic.
python arena_api.py --setup
echo.
echo [Arena API] Starting server...
python arena_api.py
pause
