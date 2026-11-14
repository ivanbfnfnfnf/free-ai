#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Arena IDE GUI — IDE-агент поверх LM Arena (бесплатные модели).

Дизайн в духе современных IDE:
  - верхняя панель с поиском по проекту (Ctrl+K) и статусом прокси;
  - слева: activity-бар + EXPLORER (дерево файлов проекта, превью по двойному клику);
  - центр: чат со стримом («мышление» приглушено) + нижняя панель вкладок
    ТЕРМИНАЛ / ПРОСМОТР / ПОИСК;
  - справа: карточка проекта, выбор модели, QUICK ACTIONS, сессии;
  - статус-бар.

Функции:
  - стрим ответа, «мышление», tool-calling (read_file, create_file, edit_file,
    list_dir, glob_files, search_content, run_shell с подтверждением);
  - «Новый чат» начинает свежую сессию на арене; обычные запросы продолжают
    текущую сессию (контекст хранится на стороне арены);
  - сессии (сохранение/загрузка в ~/.arena_sessions);
  - автозапуск встроенного прокси (arena_api.py), если он ещё не запущен.

Запуск:  python arena_gui.py   (или START_IDE.bat). Собирается в ArenaIDE.exe.
"""

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

# --- защита вывода для сборки без консоли (pythonw / .exe --windowed) ---
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
if sys.stderr is None:
    sys.stderr = sys.stdout

# ---------------------------------------------------------------------------
# Тема (в духе тёмных IDE: почти чёрные панели, фиолетово-синий акцент)
# ---------------------------------------------------------------------------

C = {
    "bg":       "#000000",    # фон окна — чистый чёрный
    "panel":    "#070707",    # боковые панели
    "panel2":   "#030303",    # шапка, вкладки, терминал, статус-бар
    "card":     "#0d0d0d",    # карточки
    "border":   "#1a1a1a",
    "field":    "#0a0a0a",    # поля ввода
    "fg":       "#e8e8e8",
    "fg_dim":   "#808080",
    "fg_faint": "#545454",
    "accent":   "#ff2d55",    # неоново-красный акцент
    "accent2":  "#ff7a45",    # вторая нота градиента логотипа
    "accent_h": "#ff5c7c",
    "user_bub": "#161616",
    "user_fg":  "#f2f2f2",
    "user_head": "#ff8fa6",
    "ai_head":  "#ff2d55",
    "reason":   "#666666",
    "tool":     "#e5b567",
    "tool_bg":  "#0a0a0a",
    "tool_fg":  "#bdbdbd",
    "sys":      "#808080",
    "error":    "#ff5c5c",
    "ok":       "#4ade80",
    "wait":     "#ffb74d",
    "btn":      "#101010",
    "btn_h":    "#1c1c1c",
    "scroll":   "#232323",
    "sel":      "#3a1420",
    "tree_sel": "#1a1a1a",
}


def _lerp_hex(a, b, t):
    a = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _load_env():
    def script_dir():
        if getattr(sys, "frozen", False):
            return os.path.dirname(sys.executable)
        return os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(script_dir(), ".env"), "r",
                  encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip().lstrip("\ufeff"),
                              v.strip().strip('"').strip("'"))


_load_env()

BASE_URL = os.environ.get("ARENA_API_URL", "http://localhost:8081").rstrip("/") + "/v1"
API_KEY = os.environ.get("ARENA_API_KEY", "arena")
DEFAULT_MODEL = os.environ.get("ARENA_MODEL",
                               os.environ.get("ARENA_DEFAULT_MODEL",
                                              "gemini-3.8-flash-high"))
WORKDIR = os.getcwd()
PROJECT_NAME = os.path.basename(WORKDIR) or "Project"
SESSIONS_DIR = os.path.join(os.path.expanduser("~"), ".arena_sessions")

MAX_TOOL_ITERATIONS = 20
TOOL_PREVIEW_LEN = 700
PREVIEW_LIMIT = 300_000

SYSTEM_PROMPT = (
    "Ты — опытный программный инженер, работающий в IDE на компьютере пользователя. "
    "Твоя задача — помогать писать, читать, править и анализировать код в его проекте.\n\n"
    "Правила работы с инструментами:\n"
    "- Для любых действий с файлами и запуска команд используй предоставленные инструменты, "
    "а не просто описывай действия текстом.\n"
    "- Прочитай файл (read_file) перед тем, как править его.\n"
    "- Для правки существующего фрагмента используй edit_file с точным old_text.\n"
    "- Для создания нового файла — create_file.\n"
    "- Для поиска по проекту используй glob_files и search_content.\n"
    "- Запускай команды оболочки (run_shell) для сборки/тестов/гита, когда это нужно.\n"
    "- После выполнения инструментов кратко сообщай пользователю, что сделал, и показывай суть изменений.\n"
    "- Отвечай на языке пользователя. Будь краток и по делу."
)

# ---------------------------------------------------------------------------
# Инструменты
# ---------------------------------------------------------------------------

_TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Прочитать содержимое файла по пути. Возвращает текст файла.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Путь к файлу (относительно проекта или абсолютный)."}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "create_file",
        "description": "Создать новый файл (или перезаписать существующий) с указанным содержимым.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Путь к файлу."},
            "content": {"type": "string", "description": "Полное содержимое файла."}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Заменить точное вхождение old_text на new_text в файле.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Путь к файлу."},
            "old_text": {"type": "string", "description": "Точный фрагмент, который нужно заменить."},
            "new_text": {"type": "string", "description": "Новый фрагмент."}},
            "required": ["path", "old_text", "new_text"]}}},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "Показать содержимое каталога (файлы и подпапки).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Путь к каталогу. По умолчанию — корень проекта."}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "glob_files",
        "description": "Найти файлы по glob-шаблону (например 'src/**/*.py').",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Glob-шаблон."}},
            "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "search_content",
        "description": "Поиск по содержимому файлов через регулярное выражение. Возвращает совпадения с номерами строк.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Регулярное выражение."},
            "path": {"type": "string", "description": "Каталог для поиска (по умолчанию — проект)."}},
            "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "run_shell",
        "description": "Выполнить команду оболочки в каталоге проекта (сборка, тесты, git и т.п.).",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "Команда для выполнения."}},
            "required": ["command"]}}},
]

_RISKY = re.compile(r"\b(rm\s+-rf|rd\s+/s|format|mkfs|del\s+/[fq]|shutdown|reg\s+add)", re.I)
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
              "dist", "build", ".opencode", ".idea", ".vscode"}


def _resolve(path):
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(WORKDIR, path))


def execute_tool_local(name, args):
    """Файловые инструменты (без run_shell)."""
    if name == "read_file":
        p = _resolve(args.get("path", ""))
        if not os.path.isfile(p):
            return "Ошибка: файл не найден: %s" % p
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    if name == "create_file":
        p = _resolve(args.get("path", ""))
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(args.get("content", ""))
        return "Файл создан/перезаписан: %s (%d байт)" % (p, len(args.get("content", "")))

    if name == "edit_file":
        p = _resolve(args.get("path", ""))
        if not os.path.isfile(p):
            return "Ошибка: файл не найден: %s" % p
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        old, new = args.get("old_text", ""), args.get("new_text", "")
        if not old:
            return "Ошибка: old_text пуст."
        if old not in text:
            return "Ошибка: old_text не найден (проверьте точность, включая отступы)."
        count = text.count(old)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text.replace(old, new))
        return "Изменено вхождений: %d. Файл: %s" % (count, p)

    if name == "list_dir":
        p = _resolve(args.get("path", "") or ".")
        if not os.path.isdir(p):
            return "Ошибка: каталог не найден: %s" % p
        items = []
        for nm in sorted(os.listdir(p)):
            full = os.path.join(p, nm)
            items.append(("D " if os.path.isdir(full) else "F ") + nm)
        return "\n".join(items) if items else "(пусто)"

    if name == "glob_files":
        import glob as globmod
        pat = args.get("pattern", "")
        matches = [m for m in sorted(globmod.glob(os.path.join(WORKDIR, pat), recursive=True))
                   if os.path.isfile(m)]
        if not matches:
            return "Ничего не найдено по шаблону: %s" % pat
        return "\n".join(m[len(WORKDIR) + 1:] for m in matches)

    if name == "search_content":
        pat = args.get("pattern", "")
        root = _resolve(args.get("path", "") or ".")
        try:
            rx = re.compile(pat, re.I | re.M)
        except re.error as e:
            return "Ошибка регулярного выражения: %s" % e
        hits = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                except Exception:
                    continue
                for i, line in enumerate(lines, 1):
                    if rx.search(line):
                        hits.append("%s:%d: %s" % (fp[len(WORKDIR) + 1:], i, line.rstrip("\n")))
                        if len(hits) >= 200:
                            break
            if len(hits) >= 200:
                break
        return "\n".join(hits) if hits else "Совпадений не найдено."

    return "Неизвестный инструмент: %s" % name


def run_tool(q, stop_event, name, args):
    """Выполняет инструмент; run_shell — с подтверждением опасных команд."""
    try:
        if name == "run_shell":
            cmd = args.get("command", "")
            if _RISKY.search(cmd):
                ev = threading.Event()
                holder = {"v": False}
                q.put(("confirm",
                       "Команда выглядит опасной:\n\n%s\n\nВыполнить?" % cmd, ev, holder))
                ev.wait(timeout=600)
                if not holder["v"]:
                    return "Отменено пользователем."
            try:
                proc = subprocess.run(cmd, shell=True, cwd=WORKDIR, capture_output=True,
                                      text=True, errors="replace", timeout=180)
            except subprocess.TimeoutExpired:
                return "Превышен таймаут (180с)."
            out = []
            if proc.stdout:
                out.append(proc.stdout)
            if proc.stderr:
                out.append("STDERR:\n" + proc.stderr)
            out.append("exit code: %d" % proc.returncode)
            return "\n".join(out)
        return execute_tool_local(name, args)
    except Exception as e:
        return "Ошибка выполнения инструмента %s: %s" % (name, e)


# ---------------------------------------------------------------------------
# Клиент прокси (OpenAI-совместимый, стрим)
# ---------------------------------------------------------------------------

def _api_reachable():
    try:
        req = urllib.request.Request(BASE_URL + "/models",
                                     headers={"Authorization": "Bearer %s" % API_KEY})
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False


def stream_chat(q, messages, model, stop_event, new_chat=False):
    """Один стрим-запрос; события в очередь; возвращает assistant-сообщение."""
    payload = {"model": model, "messages": messages, "stream": True,
               "tools": _TOOLS_SCHEMA, "tool_choice": "auto",
               "new_chat": bool(new_chat)}
    req = urllib.request.Request(
        BASE_URL + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % API_KEY},
        method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=600)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            body = ""
        q.put(("error", "HTTP %s от прокси: %s" % (e.code, body)))
        return None
    except Exception as e:
        q.put(("error", "Прокси недоступен: %s" % e))
        return None

    q.put(("assistant_start", model))
    text_parts, reasoning_parts = [], []
    tool_acc, announced = {}, set()
    try:
        for raw in resp:
            if stop_event.is_set():
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                ev = json.loads(data_str)
            except ValueError:
                continue
            if "error" in ev:
                q.put(("error", ev["error"].get("message", "ошибка прокси")))
                return None
            choices = ev.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta", {})

            r = delta.get("reasoning_content")
            if r:
                reasoning_parts.append(r)
                q.put(("reasoning", r))

            c = delta.get("content")
            if c:
                text_parts.append(c)
                q.put(("text", c))

            for tc in (delta.get("tool_calls") or []):
                idx = tc.get("index", 0)
                slot = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function", {}) or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
                if slot["name"] and slot["name"] not in announced:
                    announced.add(slot["name"])
                    q.put(("tool_announce", slot["name"]))
    finally:
        try:
            resp.close()
        except Exception:
            pass

    if stop_event.is_set():
        q.put(("stopped", None))
        return None

    tool_calls = []
    for idx in sorted(tool_acc):
        slot = tool_acc[idx]
        try:
            args = json.loads(slot["arguments"] or "{}")
        except ValueError:
            args = {"__raw__": slot["arguments"]}
        tool_calls.append({"id": slot["id"] or "call_%d" % idx,
                           "name": slot["name"], "arguments": args})
    return {"role": "assistant",
            "content": "".join(text_parts),
            "reasoning_content": "".join(reasoning_parts) or None,
            "tool_calls": tool_calls}


def agent_worker(q, messages, model, stop_event, new_chat=False):
    """Цикл агента: стрим -> инструменты -> стрим ..."""
    first_call = bool(new_chat)
    try:
        for _ in range(MAX_TOOL_ITERATIONS):
            if stop_event.is_set():
                return
            assistant = stream_chat(q, messages, model, stop_event,
                                    new_chat=first_call)
            first_call = False
            if assistant is None:
                return
            messages.append(assistant)
            calls = assistant.get("tool_calls") or []
            if not calls:
                return
            for tc in calls:
                if stop_event.is_set():
                    return
                name = tc.get("name", "?")
                q.put(("tool_start", name, tc.get("arguments", {})))
                result = run_tool(q, stop_event, name, tc.get("arguments", {}))
                q.put(("tool_result", name, result))
                messages.append({"role": "tool", "tool_call_id": tc.get("id", "call"),
                                 "name": name, "content": result})
    except Exception as e:
        q.put(("error", "Ошибка агента: %s" % e))
    finally:
        q.put(("done", None))


# ---------------------------------------------------------------------------
# Автозапуск прокси + загрузка списка моделей
# ---------------------------------------------------------------------------

def bootstrap(q):
    """Фон-поток: поднимаем прокси (если нет) и тянем список моделей."""
    if _api_reachable():
        q.put(("srv", "ok", "готов"))
    else:
        q.put(("srv", "wait", "запускаю сервер..."))
        try:
            import arena_api
        except Exception as e:
            q.put(("srv", "fail", "импорт arena_api: %s" % e))
            return
        threading.Thread(target=arena_api.serve, daemon=True).start()
        ok = False
        for _ in range(240):
            if _api_reachable():
                ok = True
                break
            time.sleep(0.5)
        if not ok:
            q.put(("srv", "fail", "сервер не поднялся"))
            return
        q.put(("srv", "ok", "готов"))

    for _ in range(15):
        try:
            req = urllib.request.Request(BASE_URL + "/models",
                                         headers={"Authorization": "Bearer %s" % API_KEY})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
            ids = [m["id"] for m in data.get("data", [])]
            if ids:
                q.put(("models", ids))
                return
        except Exception:
            pass
        time.sleep(2)
    q.put(("srv", "fail", "список моделей недоступен"))


def fetch_models(q):
    for _ in range(10):
        try:
            req = urllib.request.Request(BASE_URL + "/models",
                                         headers={"Authorization": "Bearer %s" % API_KEY})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
            ids = [m["id"] for m in data.get("data", [])]
            if ids:
                q.put(("models", ids))
                return
        except Exception:
            pass
        time.sleep(2)
    q.put(("srv", "fail", "список моделей недоступен"))


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

SEARCH_PLACEHOLDER = "Search anything...   Ctrl+K"


class ArenaGUI:

    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.model = DEFAULT_MODEL
        self.stop_event = threading.Event()
        self.busy = False
        self.new_chat_next = False
        self.sidebar_visible = True

        root.title("Arena IDE")
        root.geometry("1280x820")
        root.minsize(1020, 660)
        root.configure(bg=C["bg"])

        self._setup_style()
        self._build()
        self._poll_queue()
        threading.Thread(target=bootstrap, args=(self.q,), daemon=True).start()

    # ------------------------------------------------------------------
    # Стиль ttk
    # ------------------------------------------------------------------

    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure(".", background=C["bg"], foreground=C["fg"],
                        font=("Segoe UI", 10))
        style.configure("TFrame", background=C["bg"])
        style.configure("TLabel", background=C["bg"], foreground=C["fg"])

        # кнопки
        style.configure("Accent.TButton", background=C["accent"],
                        foreground="#ffffff", borderwidth=0, focusthickness=0,
                        padding=(14, 9), font=("Segoe UI", 10, "bold"))
        style.map("Accent.TButton",
                  background=[("active", C["accent_h"]), ("disabled", "#26262e")],
                  foreground=[("disabled", "#6a6f79")])

        style.configure("Soft.TButton", background=C["btn"], foreground=C["fg"],
                        borderwidth=0, focusthickness=0, padding=(10, 7),
                        font=("Segoe UI", 9), anchor="w")
        style.map("Soft.TButton",
                  background=[("active", C["btn_h"]), ("disabled", "#17181d")],
                  foreground=[("disabled", "#565b66"), ("active", "#ffffff")])

        style.configure("Tiny.TButton", background=C["btn"], foreground=C["fg_dim"],
                        borderwidth=0, focusthickness=0, padding=(6, 2),
                        font=("Segoe UI", 9))
        style.map("Tiny.TButton", background=[("active", C["btn_h"])])

        style.configure("Stop.TButton", background=C["btn"], foreground=C["error"],
                        borderwidth=0, focusthickness=0, padding=(12, 9),
                        font=("Segoe UI", 10, "bold"))
        style.map("Stop.TButton",
                  background=[("active", "#3a2430"), ("disabled", "#17181d")],
                  foreground=[("disabled", "#5c4248"), ("active", "#ff8a87")])

        # комбобокс
        style.configure("TCombobox", fieldbackground=C["field"], background=C["field"],
                        foreground=C["fg"], arrowcolor=C["fg_dim"], borderwidth=0,
                        padding=(8, 6), font=("Segoe UI", 9))
        style.map("TCombobox",
                  fieldbackground=[("readonly", C["field"]), ("active", C["btn_h"])],
                  background=[("readonly", C["field"]), ("active", C["btn_h"])],
                  foreground=[("readonly", C["fg"])])
        self.root.option_add("*TCombobox*Listbox.background", C["panel"])
        self.root.option_add("*TCombobox*Listbox.foreground", C["fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", C["accent"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.root.option_add("*TCombobox*Listbox.font", ("Segoe UI", 9))

        # скроллбары
        style.configure("TScrollbar", background=C["scroll"], troughcolor=C["panel2"],
                        borderwidth=0, arrowsize=11, gripcount=0)
        style.map("TScrollbar", background=[("active", "#3a3e4a")])

        # дерево файлов
        style.configure("Treeview", background=C["panel"], fieldbackground=C["panel"],
                        foreground=C["fg"], rowheight=24, borderwidth=0,
                        font=("Segoe UI", 9))
        style.map("Treeview",
                  background=[("selected", C["tree_sel"])],
                  foreground=[("selected", "#ffffff")])

        # вкладки нижней панели
        style.configure("TNotebook", background=C["bg"], borderwidth=0,
                        tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", background=C["bg"], foreground=C["fg_dim"],
                        padding=(16, 8), font=("Segoe UI", 9, "bold"), borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", C["panel2"])],
                  foreground=[("selected", "#ffffff"), ("active", C["fg"])])

    # ------------------------------------------------------------------
    # Построение интерфейса
    # ------------------------------------------------------------------

    def _build(self):
        # ── верхняя панель ────────────────────────────────────────────
        top = tk.Frame(self.root, bg=C["header"] if "header" in C else C["panel2"],
                       height=52)
        top.pack(fill="x")
        top.pack_propagate(False)
        top.configure(bg=C["panel2"])

        # статус-бар пакуем ДО основного тела, иначе он сжимается в ноль
        sb = tk.Frame(self.root, bg=C["panel2"], height=26)
        sb.pack(fill="x", side="bottom")
        sb.pack_propagate(False)

        # логотип
        logo = tk.Canvas(top, width=26, height=26, bg=C["panel2"],
                         highlightthickness=0)
        logo.pack(side="left", padx=(14, 8), pady=13)
        for i in range(26):
            col = _lerp_hex(C["accent"], C["accent2"], i / 25.0)
            logo.create_rectangle(i, 4, i + 1, 22, fill=col, width=0)
        logo.create_oval(2, 2, 24, 24, fill="", outline="")
        logo.create_text(13, 13, text="◆", fill="#ffffff", font=("Segoe UI", 9, "bold"))

        tk.Label(top, text="Arena IDE", bg=C["panel2"], fg="#ffffff",
                 font=("Segoe UI", 12, "bold")).pack(side="left")

        # поиск по проекту
        self.search_var = tk.StringVar()
        self.search_box = tk.Frame(top, bg=C["field"], highlightthickness=1,
                                   highlightbackground=C["border"],
                                   highlightcolor=C["accent"])
        self.search_box.pack(side="left", fill="x", expand=True, padx=24, pady=10)
        self.search_entry = tk.Entry(self.search_box, textvariable=self.search_var,
                                     bg=C["field"], fg=C["fg_faint"], relief="flat",
                                     insertbackground=C["fg"],
                                     font=("Segoe UI", 10), bd=0)
        self.search_entry.pack(fill="x", padx=10, pady=5)
        self.search_entry.insert(0, SEARCH_PLACEHOLDER)
        self.search_entry.bind("<FocusIn>", self._search_focus_in)
        self.search_entry.bind("<FocusOut>", self._search_focus_out)
        self.search_entry.bind("<Return>", self._run_search)
        self.root.bind_all("<Control-k>", lambda e: self._focus_search())
        self.root.bind_all("<Control-K>", lambda e: self._focus_search())

        self.srv_label = tk.Label(top, text="● проверяю...", bg=C["panel2"],
                                  fg=C["fg_dim"], font=("Segoe UI", 9))
        self.srv_label.pack(side="right", padx=14)

        gear = tk.Label(top, text="⚙", bg=C["panel2"], fg=C["fg_dim"],
                        font=("Segoe UI", 12), cursor="hand2")
        gear.pack(side="right")
        gear.bind("<Button-1>", lambda e: self._about())
        gear.bind("<Enter>", lambda e: gear.config(fg=C["fg"]))
        gear.bind("<Leave>", lambda e: gear.config(fg=C["fg_dim"]))

        # ── основное тело ────────────────────────────────────────────
        body = tk.Frame(self.root, bg=C["bg"])
        body.pack(fill="both", expand=True)

        # activity-бар (иконки слева)
        self.activity = tk.Frame(body, bg=C["panel2"], width=52)
        self.activity.pack(side="left", fill="y")
        self.activity.pack_propagate(False)
        self._build_activity()

        # левая панель: EXPLORER
        self.sidebar = tk.Frame(body, bg=C["panel"], width=264)
        self.sidebar_visible = True
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self._build_sidebar()

        # правая панель: PROJECT
        right = tk.Frame(body, bg=C["bg"], width=286)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        self._build_right(right)

        # центр: чат + нижняя панель
        center = tk.Frame(body, bg=C["bg"])
        center.pack(side="left", fill="both", expand=True)
        self._build_center(center)

        # ── статус-бар ───────────────────────────────────────────────
        self.sb_model = tk.Label(sb, text="", bg=C["panel2"], fg=C["ai_head"],
                                 font=("Segoe UI", 8, "bold"))
        self.sb_model.pack(side="left", padx=(14, 10))
        self.sb_turns = tk.Label(sb, text="", bg=C["panel2"], fg=C["fg_dim"],
                                 font=("Segoe UI", 8))
        self.sb_turns.pack(side="left", padx=(0, 10))
        self.sb_proxy = tk.Label(sb, text="", bg=C["panel2"], fg=C["fg_dim"],
                                 font=("Segoe UI", 8))
        self.sb_proxy.pack(side="right", padx=14)
        tk.Label(sb, text="UTF-8", bg=C["panel2"], fg=C["fg_faint"],
                 font=("Segoe UI", 8)).pack(side="right", padx=10)
        self._update_status()

    def _build_activity(self):
        items = [
            ("explorer", "🗂", "Проводник (файлы проекта)"),
            ("search", "🔍", "Поиск по проекту (Ctrl+K)"),
            ("chat", "💬", "К чату"),
            ("terminal", "▶", "Терминал"),
        ]
        self.act_btns = {}
        for key, glyph, tip in items:
            lbl = tk.Label(self.activity, text=glyph, bg=C["panel2"],
                           fg=C["fg_dim"], font=("Segoe UI", 13),
                           cursor="hand2", width=3, pady=9)
            lbl.pack(fill="x")
            lbl.bind("<Button-1>", lambda e, k=key: self._activity(k))
            lbl.bind("<Enter>", lambda e, l=lbl, k=key: self._act_hover(l, k, True))
            lbl.bind("<Leave>", lambda e, l=lbl, k=key: self._act_hover(l, k, False))
            self.act_btns[key] = lbl

        tk.Frame(self.activity, bg=C["border"], height=1).pack(fill="x", pady=8)

        settings = tk.Label(self.activity, text="⚙", bg=C["panel2"],
                            fg=C["fg_dim"], font=("Segoe UI", 12),
                            cursor="hand2", pady=8)
        settings.pack(fill="x", side="bottom")
        settings.bind("<Button-1>", lambda e: self._about())
        settings.bind("<Enter>", lambda e: settings.config(fg=C["fg"]))
        settings.bind("<Leave>", lambda e: settings.config(fg=C["fg_dim"]))

        self._act_hover(self.act_btns["explorer"], "explorer", True)

    def _act_hover(self, lbl, key, on):
        if key == "explorer" and self.sidebar_visible:
            lbl.config(bg=C["sel"] if on else C["sel"],
                       fg="#ffffff" if on else C["accent"])
            return
        lbl.config(fg=C["fg"] if on else C["fg_dim"],
                   bg=C["btn_h"] if on else C["panel2"])

    def _activity(self, key):
        if key == "explorer":
            self.sidebar_visible = not self.sidebar_visible
            if self.sidebar_visible:
                self.sidebar.pack(side="left", fill="y", before=self.center_frame)
                self.sidebar.pack_propagate(False)
            else:
                self.sidebar.pack_forget()
            self._act_hover(self.act_btns["explorer"], "explorer", True)
        elif key == "search":
            self._focus_search()
        elif key == "chat":
            self.input.focus_set()
        elif key == "terminal":
            self.notebook.select(self.tab_terminal)

    # --- левая панель (explorer) ---

    def _build_sidebar(self):
        head = tk.Frame(self.sidebar, bg=C["panel"])
        head.pack(fill="x", padx=12, pady=(12, 4))
        tk.Label(head, text="EXPLORER", bg=C["panel"], fg=C["fg_dim"],
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        refresh = tk.Label(head, text="⟳", bg=C["panel"], fg=C["fg_dim"],
                           font=("Segoe UI", 11), cursor="hand2")
        refresh.pack(side="right")
        refresh.bind("<Button-1>", lambda e: self._tree_reload())
        refresh.bind("<Enter>", lambda e: refresh.config(fg=C["fg"]))
        refresh.bind("<Leave>", lambda e: refresh.config(fg=C["fg_dim"]))

        self.tree = ttk.Treeview(self.sidebar, show="tree", selectmode="browse",
                                 style="Treeview")
        tscroll = ttk.Scrollbar(self.sidebar, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tscroll.set)
        tscroll.pack(side="right", fill="y", padx=(0, 2))
        self.tree.pack(fill="both", expand=True, padx=(8, 0), pady=(0, 8))
        self.tree.bind("<<TreeviewOpen>>", self._tree_open)
        self.tree.bind("<Double-1>", self._tree_open_file)
        self._tree_reload()

    def _tree_reload(self):
        self.tree.delete(*self.tree.get_children(""))
        self._tree_populate("", WORKDIR)

    def _tree_populate(self, parent_iid, path):
        try:
            names = sorted(os.listdir(path), key=lambda s: s.lower())
        except OSError:
            return
        dirs = [n for n in names if os.path.isdir(os.path.join(path, n))
                and n not in _SKIP_DIRS and not n.startswith(".")]
        files = [n for n in names if not os.path.isdir(os.path.join(path, n))
                 and not n.startswith(".")]
        for n in dirs:
            iid = os.path.join(path, n)
            self.tree.insert(parent_iid, "end", iid=iid,
                             text=" 📁 " + n, open=False)
            self.tree.insert(iid, "end", iid=iid + "::__dummy__", text="")
        for n in files:
            iid = os.path.join(path, n)
            self.tree.insert(parent_iid, "end", iid=iid, text=" 📄 " + n)

    def _tree_open(self, _event=None):
        iid = self.tree.focus()
        if not iid or iid.endswith("__dummy__"):
            return
        dummy = iid + "::__dummy__"
        if self.tree.exists(dummy):
            self.tree.delete(dummy)
            self._tree_populate(iid, iid)

    def _tree_open_file(self, _event=None):
        iid = self.tree.focus()
        if not iid or iid.endswith("__dummy__") or os.path.isdir(iid):
            return
        try:
            with open(iid, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(PREVIEW_LIMIT)
        except OSError as e:
            content = "Не удалось прочитать файл: %s" % e
        self._set_preview(os.path.relpath(iid, WORKDIR), content)
        self.notebook.select(self.tab_preview)

    # --- правая панель (project) ---

    def _build_right(self, right):
        pad = dict(fill="x", padx=14)

        # карточка проекта
        card = tk.Frame(right, bg=C["card"], highlightthickness=1,
                        highlightbackground=C["border"])
        card.pack(fill="x", padx=14, pady=(14, 6))
        av = tk.Canvas(card, width=44, height=44, bg=C["card"], highlightthickness=0)
        av.pack(side="left", padx=(12, 10), pady=12)
        for i in range(44):
            col = _lerp_hex(C["accent"], C["accent2"], i / 43.0)
            av.create_rectangle(i, 0, i + 1, 44, fill=col, width=0)
        av.create_oval(3, 3, 41, 41, fill="#2b2350", outline="")
        av.create_oval(7, 7, 37, 37, fill=C["accent"], outline="")
        av.create_text(22, 22, text="◆", fill="#ffffff",
                       font=("Segoe UI", 11, "bold"))
        info = tk.Frame(card, bg=C["card"])
        info.pack(side="left", fill="x", expand=True, pady=10)
        tk.Label(info, text=PROJECT_NAME, bg=C["card"], fg="#ffffff",
                 font=("Segoe UI", 11, "bold"), anchor="w").pack(fill="x")
        tk.Label(info, text="LM Arena · прокси 8081", bg=C["card"],
                 fg=C["fg_dim"], font=("Segoe UI", 8), anchor="w").pack(fill="x")

        # модель
        tk.Label(right, text="МОДЕЛЬ", bg=C["bg"], fg=C["fg_dim"],
                 font=("Segoe UI", 8, "bold")).pack(fill="x", padx=16,
                                                    pady=(12, 4))
        self.model_var = tk.StringVar(value=self.model)
        self.model_combo = ttk.Combobox(right, textvariable=self.model_var,
                                        state="readonly")
        self.model_combo.pack(fill="x", padx=14, pady=(0, 6))
        self.model_combo.bind("<<ComboboxSelected>>", self._on_model_selected)
        ttk.Button(right, text="⟳  Обновить список моделей",
                   style="Soft.TButton", command=self._refresh_models).pack(fill="x")

        self._sep(right)

        # quick actions
        tk.Label(right, text="QUICK ACTIONS", bg=C["bg"], fg=C["fg_dim"],
                 font=("Segoe UI", 8, "bold")).pack(fill="x", padx=16,
                                                    pady=(2, 4))
        for text, cmd in [
            ("▶   Выполнить команду...", self._qa_run),
            ("＋   Новый файл...", self._qa_new_file),
            ("🔍   Поиск в проекте", self._focus_search),
            ("⌨   Терминал", lambda: self.notebook.select(self.tab_terminal)),
        ]:
            ttk.Button(right, text=text, style="Soft.TButton",
                       command=cmd).pack(fill="x", padx=14, pady=3)

        self._sep(right)

        # сессия
        tk.Label(right, text="СЕССИЯ", bg=C["bg"], fg=C["fg_dim"],
                 font=("Segoe UI", 8, "bold")).pack(fill="x", padx=16,
                                                    pady=(2, 4))
        self.turns_label = tk.Label(right, text="Сообщений: 0 · та же сессия арены",
                                    bg=C["bg"], fg=C["fg_dim"],
                                    font=("Segoe UI", 8), anchor="w")
        self.turns_label.pack(fill="x", padx=16, pady=(0, 4))
        ttk.Button(right, text="＋ Новый чат", style="Accent.TButton",
                   command=self._new_chat).pack(fill="x", padx=14, pady=(2, 3))
        row = tk.Frame(right, bg=C["bg"])
        row.pack(fill="x", padx=14, pady=(0, 10))
        ttk.Button(row, text="Сохранить", style="Soft.TButton",
                   command=self._save_session).pack(side="left", fill="x",
                                                    expand=True, padx=(0, 3))
        ttk.Button(row, text="Загрузить", style="Soft.TButton",
                   command=self._load_session).pack(side="left", fill="x",
                                                    expand=True, padx=(3, 0))

    def _sep(self, parent):
        tk.Frame(parent, bg=C["border"], height=1).pack(fill="x", padx=14,
                                                        pady=10)

    # --- центр (чат + нижняя панель) ---

    def _build_center(self, center):
        self.center_frame = center

        chatwrap = tk.Frame(center, bg=C["bg"])
        chatwrap.pack(fill="both", expand=True)

        self.txt = tk.Text(chatwrap, wrap="word", font=("Segoe UI", 10),
                           bg=C["bg"], fg=C["fg"], relief="flat", bd=0,
                           padx=18, pady=14, state="disabled", cursor="arrow",
                           insertbackground=C["fg"], selectbackground=C["sel"],
                           highlightthickness=0, spacing1=1, spacing3=1)
        scroll = ttk.Scrollbar(chatwrap, command=self.txt.yview)
        self.txt.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)

        self.txt.tag_configure("userhead", foreground=C["user_head"],
                               font=("Segoe UI", 8, "bold"),
                               lmargin1=110, lmargin2=110, spacing1=6)
        self.txt.tag_configure("user", background=C["user_bub"],
                               foreground=C["user_fg"],
                               font=("Segoe UI", 10),
                               lmargin1=110, lmargin2=110, rmargin=14,
                               spacing3=8)
        self.txt.tag_configure("ahead", foreground=C["ai_head"],
                               font=("Segoe UI", 8, "bold"), spacing1=6)
        self.txt.tag_configure("assistant", foreground=C["fg"],
                               font=("Segoe UI", 10), spacing3=6)
        self.txt.tag_configure("reasoning", foreground=C["reason"],
                               font=("Segoe UI", 9, "italic"), spacing3=4)
        self.txt.tag_configure("sys", foreground=C["sys"],
                               font=("Segoe UI", 9), justify="center")
        self.txt.tag_configure("tool", foreground=C["tool"],
                               font=("Consolas", 9), spacing1=4)
        self.txt.tag_configure("toolout", foreground=C["tool_fg"],
                               background=C["tool_bg"],
                               font=("Consolas", 8), lmargin1=18, lmargin2=18)
        self.txt.tag_configure("error", foreground=C["error"],
                               font=("Segoe UI", 10, "bold"))

        # поле ввода
        bottom = tk.Frame(chatwrap, bg=C["bg"])
        bottom.pack(fill="x", pady=(6, 8))

        box = tk.Frame(bottom, bg=C["field"], highlightthickness=1,
                       highlightbackground=C["border"],
                       highlightcolor=C["accent"])
        box.pack(side="left", fill="both", expand=True)
        self.input = tk.Text(box, height=3, wrap="word", font=("Segoe UI", 10),
                             bg=C["field"], fg=C["fg"], relief="flat", bd=0,
                             padx=12, pady=9, insertbackground=C["fg"],
                             selectbackground=C["sel"], highlightthickness=0)
        self.input.pack(fill="both", expand=True)
        self.input.bind("<Return>", self._on_enter)
        self.input.bind("<KeyRelease>", self._update_send_state)
        self.input.bind("<FocusIn>",
                        lambda e: box.config(highlightbackground=C["accent"]))
        self.input.bind("<FocusOut>",
                        lambda e: box.config(highlightbackground=C["border"]))

        right_btns = tk.Frame(bottom, bg=C["bg"])
        right_btns.pack(side="left", fill="y", padx=(10, 0))
        self.send_btn = ttk.Button(right_btns, text="Отправить ▸",
                                   style="Accent.TButton", state="disabled",
                                   command=self._send)
        self.send_btn.pack(fill="x")
        self.stop_btn = ttk.Button(right_btns, text="■ Стоп",
                                   style="Stop.TButton", state="disabled",
                                   command=self._stop)
        self.stop_btn.pack(fill="x", pady=(6, 0))

        # ── нижняя панель вкладок ─────────────────────────────────────
        self.notebook = ttk.Notebook(center, style="TNotebook")
        self.notebook.pack(fill="x", side="bottom")

        # ТЕРМИНАЛ
        self.tab_terminal = tk.Frame(self.notebook, bg=C["panel2"])
        self.notebook.add(self.tab_terminal, text="  ТЕРМИНАЛ  ")
        self.term = tk.Text(self.tab_terminal, height=10, wrap="word",
                            font=("Consolas", 9), bg=C["panel2"], fg=C["tool_fg"],
                            relief="flat", bd=0, padx=16, pady=10,
                            state="disabled", cursor="arrow",
                            insertbackground=C["fg"], selectbackground=C["sel"],
                            highlightthickness=0)
        tscroll = ttk.Scrollbar(self.tab_terminal, command=self.term.yview)
        self.term.configure(yscrollcommand=tscroll.set)
        tscroll.pack(side="right", fill="y")
        self.term.pack(fill="both", expand=True)

        # ПРОСМОТР
        self.tab_preview = tk.Frame(self.notebook, bg=C["panel2"])
        self.notebook.add(self.tab_preview, text="  ПРОСМОТР  ")
        self.preview_head = tk.Label(self.tab_preview, text="",
                                     bg=C["panel2"], fg=C["fg_dim"],
                                     font=("Consolas", 8), anchor="w")
        self.preview_head.pack(fill="x", padx=16, pady=(8, 0))
        self.preview = tk.Text(self.tab_preview, height=9, wrap="none",
                               font=("Consolas", 9), bg=C["panel2"], fg=C["fg"],
                               relief="flat", bd=0, padx=16, pady=8,
                               state="disabled", cursor="arrow",
                               insertbackground=C["fg"], selectbackground=C["sel"],
                               highlightthickness=0)
        pscroll = ttk.Scrollbar(self.tab_preview, command=self.preview.yview)
        self.preview.configure(yscrollcommand=pscroll.set)
        pscroll.pack(side="right", fill="y")
        self.preview.pack(fill="both", expand=True)

        # ПОИСК
        self.tab_search = tk.Frame(self.notebook, bg=C["panel2"])
        self.notebook.add(self.tab_search, text="  ПОИСК  ")
        self.search_head = tk.Label(self.tab_search, text="Ctrl+K — поиск по проекту",
                                    bg=C["panel2"], fg=C["fg_dim"],
                                    font=("Consolas", 8), anchor="w")
        self.search_head.pack(fill="x", padx=16, pady=(8, 0))
        self.search_out = tk.Text(self.tab_search, height=9, wrap="word",
                                  font=("Consolas", 9), bg=C["panel2"],
                                  fg=C["tool_fg"], relief="flat", bd=0,
                                  padx=16, pady=8, state="disabled",
                                  cursor="arrow", insertbackground=C["fg"],
                                  selectbackground=C["sel"], highlightthickness=0)
        sscroll = ttk.Scrollbar(self.tab_search, command=self.search_out.yview)
        self.search_out.configure(yscrollcommand=sscroll.set)
        sscroll.pack(side="right", fill="y")
        self.search_out.pack(fill="both", expand=True)

    # ------------------------------------------------------------------
    # Утилиты чата
    # ------------------------------------------------------------------

    def _append(self, text, tag):
        self.txt.configure(state="normal")
        self.txt.insert("end", text, tag)
        self.txt.configure(state="disabled")
        self.txt.see("end")

    def _nl(self):
        self.txt.configure(state="normal")
        self.txt.insert("end", "\n")
        self.txt.configure(state="disabled")

    def _term(self, text):
        self.term.configure(state="normal")
        self.term.insert("end", text.rstrip("\n") + "\n")
        try:
            last_line = int(float(self.term.index("end-1c")))
        except Exception:
            last_line = 0
        if last_line > 4000:
            self.term.delete("1.0", "2000.0")
        self.term.configure(state="disabled")
        self.term.see("end")

    def _set_preview(self, relpath, content):
        self.preview_head.config(text="  " + relpath)
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("end", content)
        self.preview.configure(state="disabled")

    def _render_history(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")
        for msg in self.messages:
            role = msg.get("role")
            if role == "system":
                continue
            content = msg.get("content") or ""
            if role == "user":
                self._nl()
                self._append("ТЫ", "userhead")
                self._append("\n" + content + "\n", "user")
            elif role == "assistant":
                self._nl()
                self._append("ARENA", "ahead")
                self._append("\n", "assistant")
                if msg.get("reasoning_content"):
                    self._append("(мышление) " + msg["reasoning_content"] + "\n",
                                 "reasoning")
                if content:
                    self._append(content + "\n", "assistant")
                for tc in (msg.get("tool_calls") or []):
                    fn = tc.get("function", {})
                    self._append("⚙ %s(%s)\n" % (fn.get("name"), fn.get("arguments")),
                                 "tool")
            elif role == "tool":
                name = msg.get("name", "tool")
                self._append("↳ результат %s:\n" % name, "tool")
                self._append((msg.get("content") or "")[:TOOL_PREVIEW_LEN] + "\n",
                             "toolout")

    def _on_enter(self, event):
        if event.state & 0x0001:      # Shift+Enter — перенос строки
            return None
        self.root.after(10, self._send_if_ready)
        return "break"

    def _send_if_ready(self):
        if not self.busy and self.input.get("1.0", "end-1c").strip():
            self._send()

    def _update_send_state(self, _event=None):
        if not self.busy:
            self.send_btn.config(
                state="normal" if self.input.get("1.0", "end-1c").strip() else "disabled")

    def _update_status(self):
        self.sb_model.config(text="◆ %s" % self.model)
        n = len([m for m in self.messages if m.get("role") != "system"])
        self.sb_turns.config(text="%d сообщ." % n)
        self.sb_proxy.config(text="прокси: %s" %
                             ("готов" if getattr(self, "_srv_ok", False) else "...")
                             if hasattr(self, "_srv_ok") else "прокси: ...")
        if hasattr(self, "turns_label"):
            self.turns_label.config(
                text="Сообщений: %d · %s" % (n, "новая сессия в след. сообщении"
                                             if self.new_chat_next
                                             else "продолжаем сессию арены"))

    # ------------------------------------------------------------------
    # Поиск по проекту
    # ------------------------------------------------------------------

    def _search_focus_in(self, _e=None):
        if self.search_var.get() == SEARCH_PLACEHOLDER:
            self.search_var.set("")
            self.search_entry.config(fg=C["fg"])

    def _search_focus_out(self, _e=None):
        if not self.search_var.get():
            self.search_var.set(SEARCH_PLACEHOLDER)
            self.search_entry.config(fg=C["fg_faint"])

    def _focus_search(self):
        self.search_entry.focus_set()
        self.search_entry.select_range(0, "end")

    def _run_search(self, _e=None):
        pattern = self.search_var.get().strip()
        if not pattern or pattern == SEARCH_PLACEHOLDER:
            return
        self.notebook.select(self.tab_search)
        self._set_search_head("  поиск «%s»..." % pattern)
        self._search_out_text("ищу «%s»...\n" % pattern)
        threading.Thread(target=self._search_worker, args=(pattern,),
                         daemon=True).start()

    def _search_worker(self, pattern):
        text = execute_tool_local("search_content", {"pattern": pattern})
        self.q.put(("search_done", pattern, text))

    def _set_search_head(self, text):
        self.search_head.config(text=text)

    def _search_out_text(self, text):
        self.search_out.configure(state="normal")
        self.search_out.delete("1.0", "end")
        self.search_out.insert("end", text)
        self.search_out.configure(state="disabled")
        self.search_out.see("start")

    # ------------------------------------------------------------------
    # Quick actions
    # ------------------------------------------------------------------

    def _qa_run(self):
        cmd = simpledialog.askstring(
            "Выполнить команду", "Команда (shell, каталог проекта):",
            initialvalue="python --version")
        if not cmd:
            return
        self._term("$ " + cmd)
        threading.Thread(target=self._qa_run_worker, args=(cmd,),
                         daemon=True).start()

    def _qa_run_worker(self, cmd):
        result = run_tool(self.q, self.stop_event, "run_shell", {"command": cmd})
        self.q.put(("tool_result", "shell", result))

    def _qa_new_file(self):
        path = simpledialog.askstring("Новый файл",
                                      "Путь нового файла (относительно проекта):",
                                      initialvalue="new_file.py")
        if not path:
            return
        result = execute_tool_local("create_file", {"path": path, "content": ""})
        self._term(result)
        self._tree_reload()

    def _about(self):
        messagebox.showinfo(
            "Arena IDE",
            "Arena IDE — IDE-агент поверх LM Arena (arena.ai).\n\n"
            "Прокси: %s\nПрофиль браузера и сессия: %s\nНастройки: .env рядом "
            "с программой.\n\nМодели предоставляются ареной бесплатно; "
            "запросы идут через управляемый Chrome." % (BASE_URL, "~\\AppData\\Local\\ArenaIDE"))

    # ------------------------------------------------------------------
    # Действия чата
    # ------------------------------------------------------------------

    def _send(self):
        if self.busy:
            return
        text = self.input.get("1.0", "end-1c").strip()
        if not text:
            return
        self.input.delete("1.0", "end")
        self._update_send_state()

        self._append("ТЫ", "userhead")
        self._append("\n" + text + "\n", "user")

        self.messages.append({"role": "user", "content": text})
        nc = self.new_chat_next
        self.new_chat_next = False
        self.stop_event.clear()
        self.busy = True
        self.send_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self._update_status()
        threading.Thread(target=agent_worker,
                         args=(self.q, self.messages, self.model, self.stop_event, nc),
                         daemon=True).start()

    def _stop(self):
        self.stop_event.set()

    def _new_chat(self):
        if self.busy:
            messagebox.showwarning("Занято", "Дождитесь завершения ответа.")
            return
        if len(self.messages) > 1 and not messagebox.askyesno(
                "Новый чат", "Очистить текущий диалог?\n\nСледующее сообщение "
                "начнёт новую сессию на арене."):
            return
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.new_chat_next = True
        self._render_history()
        self._append("Новый чат: следующее сообщение начнёт новую сессию на арене.\n",
                     "sys")
        self._update_status()

    def _save_session(self):
        if self.busy:
            messagebox.showwarning("Занято", "Дождитесь завершения ответа.")
            return
        name = simpledialog.askstring(
            "Сохранить сессию", "Имя сессии:", initialvalue="session")
        if not name:
            return
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        path = os.path.join(SESSIONS_DIR, "%s.json" % re.sub(r"[^\w\- ]", "_", name))
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"model": self.model, "messages": self.messages},
                      f, ensure_ascii=False, indent=2)
        self._append("Сессия сохранена: %s\n" % path, "sys")

    def _load_session(self):
        if self.busy:
            messagebox.showwarning("Занято", "Дождитесь завершения ответа.")
            return
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        names = sorted(f[:-5] for f in os.listdir(SESSIONS_DIR) if f.endswith(".json"))
        if not names:
            messagebox.showinfo("Сессии", "Нет сохранённых сессий.")
            return
        name = simpledialog.askstring("Загрузить сессию",
                                      "Имя сессии (%s):" % ", ".join(names[:6]))
        if not name:
            return
        path = os.path.join(SESSIONS_DIR, "%s.json" % name)
        if not os.path.isfile(path):
            messagebox.showerror("Ошибка", "Сессия не найдена: %s" % path)
            return
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.messages = data.get("messages") or [{"role": "system",
                                                 "content": SYSTEM_PROMPT}]
        if data.get("model"):
            self._set_model(data["model"])
        self._render_history()
        self._append("Сессия загружена: %s (%d сообщений)\n"
                     % (name, len(self.messages) - 1), "sys")
        self._update_status()

    def _refresh_models(self):
        self.srv_label.config(text="● обновляю модели...", fg=C["wait"])
        threading.Thread(target=fetch_models, args=(self.q,), daemon=True).start()

    def _on_model_selected(self, _event=None):
        self._set_model(self.model_var.get())

    def _set_model(self, name):
        if not name:
            return
        if name != self.model and len(self.messages) > 1:
            self._append("Модель: %s — для неё арена начнёт свежую сессию.\n" % name,
                         "sys")
        self.model = name
        self.model_var.set(name)
        self._update_status()

    # ------------------------------------------------------------------
    # Очередь событий
    # ------------------------------------------------------------------

    def _poll_queue(self):
        try:
            while True:
                ev = self.q.get_nowait()
                kind = ev[0]
                if kind == "text":
                    self._append(ev[1], "assistant")
                elif kind == "reasoning":
                    self._append(ev[1], "reasoning")
                elif kind == "assistant_start":
                    self._nl()
                    self._append("ARENA · %s" % ev[1], "ahead")
                    self._append("\n", "assistant")
                elif kind == "tool_announce":
                    self._append("⚙ Вызов: %s " % ev[1], "tool")
                elif kind == "tool_start":
                    try:
                        args = json.dumps(ev[2], ensure_ascii=False)
                    except Exception:
                        args = str(ev[2])
                    self._append("(%s)\n" % args[:300], "tool")
                    self._append("↳ ", "tool")
                    self._term("⚙ %s %s" % (ev[1], args[:200]))
                elif kind == "tool_result":
                    res = str(ev[2])
                    self._append(res[:TOOL_PREVIEW_LEN] +
                                 ("…" if len(res) > TOOL_PREVIEW_LEN else "") + "\n",
                                 "toolout")
                    self._term("↳ %s" % res[:2000])
                    if ev[1] in ("create_file", "edit_file"):
                        self._tree_reload()
                elif kind == "error":
                    self._nl()
                    self._append("[ошибка] %s\n" % ev[1], "error")
                    self._term("[ошибка] %s" % ev[1])
                elif kind == "stopped":
                    self._append("[остановлено]\n", "sys")
                elif kind == "done":
                    self.busy = False
                    self.stop_btn.config(state="disabled")
                    self._update_send_state()
                    self._update_status()
                elif kind == "confirm":
                    _prompt, evt, holder = ev[1], ev[2], ev[3]
                    try:
                        holder["v"] = bool(messagebox.askyesno(
                            "Подтверждение", _prompt, icon="warning"))
                    except Exception:
                        holder["v"] = False
                    evt.set()
                elif kind == "srv":
                    state, msg = ev[1], ev[2]
                    colors = {"ok": C["ok"], "wait": C["wait"], "fail": C["error"]}
                    self.srv_label.config(text="● прокси: %s" % msg,
                                          fg=colors.get(state, C["fg_dim"]))
                    self._srv_ok = (state == "ok")
                    self._update_status()
                elif kind == "models":
                    ids = ev[1]
                    self.model_combo.config(values=ids)
                    if self.model not in ids:
                        self._set_model(ids[0] if ids else DEFAULT_MODEL)
                    self._update_status()
                elif kind == "search_done":
                    pattern, text = ev[1], ev[2]
                    count = len([l for l in text.splitlines() if ":" in l])
                    self._set_search_head("  поиск «%s» — %d строк" %
                                          (pattern, count))
                    self._search_out_text(text + "\n")
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)


def main():
    root = tk.Tk()
    try:
        from tkinter import font as tkfont
        tkfont.nametofont("TkDefaultFont").configure(family="Segoe UI", size=10)
    except Exception:
        pass
    ArenaGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
