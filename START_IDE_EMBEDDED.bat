@echo off
title Arena IDE (all-in-one)
cd /d "%~dp0"
echo [Arena IDE] Starting all-in-one IDE (server starts in background).
echo [Arena IDE] On first start a browser window opens - log in to arena.ai once.
python arena_ide.py %*
pause
