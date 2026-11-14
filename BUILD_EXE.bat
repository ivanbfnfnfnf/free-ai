@echo off
title Build Arena IDE (exe)
cd /d "%~dp0"

echo [build] Installing pyinstaller...
pip install pyinstaller || goto :err

echo [build] Building ArenaIDE.exe (GUI + server in one, no console window)...
pyinstaller --onefile --windowed --noconfirm --name ArenaIDE arena_gui.py || goto :err

echo [build] Building ArenaAPI.exe (proxy only, with console)...
pyinstaller --onefile --noconfirm --name ArenaAPI arena_api.py || goto :err

echo.
echo [build] Done. Files in dist\:
dir /b dist\*.exe
echo.
echo Copy .env next to the exe (or rename .env.example).
pause
exit /b 0

:err
echo.
echo [build] FAILED - see messages above.
pause
exit /b 1
