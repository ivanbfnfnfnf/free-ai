#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Arena IDE — точка входа «всё в одном»: GUI-IDE + сервер в одном процессе.

Обычная схема (два процесса):
    python arena_api.py   # окно 1 — прокси
    python arena_gui.py   # окно 2 — GUI-IDE

Схема «всё в одном» (GUI сам поднимает прокси в фоне):
    python arena_ide.py

Именно arena_gui.py компилируется в ArenaIDE.exe (см. BUILD_EXE.bat) —
этот файл оставлен как удобный альтернативный вход.
"""

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import arena_gui  # noqa: E402

if __name__ == "__main__":
    arena_gui.main()
