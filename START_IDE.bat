@echo off
title Arena IDE
cd /d "%~dp0"
echo [Arena IDE] Starting GUI IDE (server starts automatically if not running).
echo [Arena IDE] On first start a browser window opens - log in to arena.ai once.
python arena_gui.py %*
if errorlevel 1 pause
