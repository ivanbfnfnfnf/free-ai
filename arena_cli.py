#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""arena_cli — консольная IDE поверх LM Arena (бесплатные модели).

Работает через локальный прокси arena_api.py (OpenAI-совместимый API):
    http://127.0.0.1:8081/v1
Запуск прокси (отдельным процессом):  python arena_api.py
Запуск IDE:  python arena_cli.py  (или двойной клик START_IDE.bat)

Команды IDE:
    /models            — список моделей арены (с номерами)
    /model <имя|номер> — переключить модель (принимает часть имени)
    /help              — справка
и т.д. (полный список в /help)
"""

import argparse
import glob as globmod
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------------------
# Загрузка .env
# ---------------------------------------------------------------------------

def _load_env():
    base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
        else os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(base, ".env")
    try:
        with open(env_path, "r", encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip().lstrip("\ufeff")
        os.environ.setdefault(key, v.strip().strip('"').strip("'"))

_load_env()

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

_DEFAULT_URL = "http://localhost:8081"
BASE_URL = os.environ.get("ARENA_API_URL", _DEFAULT_URL).rstrip("/") + "/v1"
API_KEY = os.environ.get("ARENA_API_KEY", "arena")
DEFAULT_MODEL = os.environ.get("ARENA_MODEL",
                               os.environ.get("ARENA_DEFAULT_MODEL", "gemini-3.8-flash-high"))

WORKDIR = os.getcwd()
SESSIONS_DIR = os.path.join(os.path.expanduser("~"), ".arena_sessions")

# ---------------------------------------------------------------------------
# Цвета
# ---------------------------------------------------------------------------

USE_COLOR = True


def _enable_vt():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        h = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint()
        if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
            kernel32.SetConsoleMode(h, mode.value | 4)
    except Exception:
        pass


_enable_vt()

try:
    import colorama
    colorama.init(autoreset=False)
except ImportError:
    pass

C = {
    "reset":     "\033[0m",
    "bold":      "\033[1m",
    "dim":       "\033[2m",
    "user":      "\033[1;36m",
    "assistant": "\033[1;32m",
    "tool":      "\033[1;33m",
    "err":       "\033[1;31m",
    "sys":       "\033[1;35m",
    "info":      "\033[0;34m",
}


def color(name, text):
    if not USE_COLOR:
        return text
    return "%s%s%s" % (C.get(name, ""), text, C["reset"])


# ---------------------------------------------------------------------------
# Системный промпт
# ---------------------------------------------------------------------------

_DEFAULT_SYSTEM = (
    "Ты — опытный программный инженер, работающий в терминальной IDE. "
    "Твоя задача — помогать пользователю писать, читать, править и анализировать код в его проекте.\n\n"
    "Правила работы с инструментами:\n"
    "- Для любых действий с файлами и запуска команд обязательно используй предоставленные инструменты, "
    "а не просто описывай действия текстом.\n"
    "- Прочитай файл (read_file) перед тем, как править его.\n"
    "- Для правки существующего фрагмента используй edit_file с точным old_text.\n"
    "- Для создания нового файла — create_file.\n"
    "- Для поиска по проекту используй glob_files и search_content.\n"
    "- Запускай команды оболочки (run_shell) для сборки/тестов/гита, когда это нужно.\n"
    "- После выполнения инструментов кратко сообщай пользователю, что сделал, и показывай суть изменений.\n"
    "- Отвечай на языке пользователя."
)

SYSTEM_PROMPT = _DEFAULT_SYSTEM

# ---------------------------------------------------------------------------
# Инструменты (tools)
# ---------------------------------------------------------------------------

def _resolve(path):
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(WORKDIR, path))


_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Прочитать содержимое файла по пути. Возвращает текст файла.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Путь к файлу (относительно проекта или абсолютный)."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": "Создать новый файл (или перезаписать существующий) с указанным содержимым.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу."},
                    "content": {"type": "string", "description": "Полное содержимое файла."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Заменить точное вхождение old_text на new_text в файле.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу."},
                    "old_text": {"type": "string", "description": "Точный фрагмент, который нужно заменить."},
                    "new_text": {"type": "string", "description": "Новый фрагмент."},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "Показать содержимое каталога (файлы и подпапки).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Путь к каталогу. По умолчанию — корень проекта."}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_files",
            "description": "Найти файлы по glob-шаблону (например 'src/**/*.py').",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string", "description": "Glob-шаблон."}},
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_content",
            "description": "Поиск по содержимому файлов через регулярное выражение. Возвращает совпадения с номерами строк.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Регулярное выражение."},
                    "path": {"type": "string", "description": "Каталог для поиска (по умолчанию — проект)."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Выполнить команду оболочки в каталоге проекта (сборка, тесты, git и т.п.).",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "Команда для выполнения."}},
                "required": ["command"],
            },
        },
    },
]

_RISKY = re.compile(r"\b(rm\s+-rf|rd\s+/s|format|mkfs|del\s+/[fq]|shutdown|:\(|:\))", re.I)
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".opencode"}


def _confirm(prompt):
    try:
        ans = input(color("tool", prompt + " [y/N] ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes", "д", "да")


def _safe_run(cmd):
    if _RISKY.search(cmd):
        if not _confirm("Команда выглядит опасной:\n  %s\nВсё равно выполнить?" % cmd):
            return "Отменено пользователем."
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=WORKDIR, capture_output=True, text=True,
            errors="replace", timeout=180,
        )
    except subprocess.TimeoutExpired:
        return "Превышен таймаут (180с)."
    out = []
    if proc.stdout:
        out.append(proc.stdout)
    if proc.stderr:
        out.append("STDERR:\n" + proc.stderr)
    out.append("exit code: %d" % proc.returncode)
    return "\n".join(out)


def execute_tool(name, args):
    try:
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
            old = args.get("old_text", "")
            new = args.get("new_text", "")
            if not old:
                return "Ошибка: old_text пуст."
            if old not in text:
                return "Ошибка: old_text не найден в файле (проверьте точность, включая отступы)."
            count = text.count(old)
            text = text.replace(old, new)
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
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
            pat = args.get("pattern", "")
            matches = sorted(globmod.glob(os.path.join(WORKDIR, pat), recursive=True))
            matches = [m for m in matches if os.path.isfile(m)]
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
                            rel = fp[len(WORKDIR) + 1:]
                            hits.append("%s:%d: %s" % (rel, i, line.rstrip("\n")))
                            if len(hits) >= 200:
                                break
                if len(hits) >= 200:
                    break
            return "\n".join(hits) if hits else "Совпадений не найдено."

        if name == "run_shell":
            return _safe_run(args.get("command", ""))

        return "Неизвестный инструмент: %s" % name
    except Exception as e:
        return "Ошибка выполнения инструмента %s: %s" % (name, e)


# ---------------------------------------------------------------------------
# HTTP-запросы к прокси
# ---------------------------------------------------------------------------

def _http_post(path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE_URL + path, data=data,
        headers={"Content-Type": "application/json", "Authorization": "Bearer %s" % API_KEY},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=600)


def list_models_remote():
    try:
        req = urllib.request.Request(
            BASE_URL + "/models",
            headers={"Authorization": "Bearer %s" % API_KEY},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode("utf-8"))
        return [m["id"] for m in d.get("data", [])]
    except Exception:
        return []


def _check_api_available():
    try:
        req = urllib.request.Request(
            BASE_URL + "/models",
            headers={"Authorization": "Bearer %s" % API_KEY},
        )
        urllib.request.urlopen(req, timeout=8)
        return True, ""
    except urllib.error.URLError as e:
        return False, str(e.reason)
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Выбор модели (нумерованный список + нечёткий поиск)
# ---------------------------------------------------------------------------

_MODELS_CACHE = {"list": [], "at": 0.0}


def get_models(force=False):
    if not force and _MODELS_CACHE["list"] and time.time() - _MODELS_CACHE["at"] < 600:
        return _MODELS_CACHE["list"]
    models = list_models_remote()
    if models:
        _MODELS_CACHE["list"] = models
        _MODELS_CACHE["at"] = time.time()
    return models


def cmd_models_list():
    models = get_models(force=True)
    if not models:
        print(color("err", "Не удалось получить список моделей. Прокси запущен? (python arena_api.py)"))
        return
    print(color("sys", "Доступные модели LM Arena:"))
    for i, m in enumerate(models, 1):
        mark = color("assistant", " <- текущая") if m == DEFAULT_MODEL else ""
        print(color("info", "  %2d) %s%s" % (i, m, mark)))


def cmd_model_switch(arg):
    global DEFAULT_MODEL
    if not arg:
        print(color("sys", "Текущая модель: %s" % DEFAULT_MODEL))
        return
    models = get_models()
    if not models:
        print(color("err", "Прокси недоступен — список моделей получить нельзя."))
        return
    pick = None
    if arg.isdigit():
        idx = int(arg)
        if 1 <= idx <= len(models):
            pick = models[idx - 1]
    if not pick:
        low = arg.lower().strip()
        exact = [m for m in models if m.lower() == low]
        if exact:
            pick = exact[0]
        else:
            starts = [m for m in models if m.lower().startswith(low)]
            if len(starts) == 1:
                pick = starts[0]
            elif len(starts) > 1:
                print(color("err", "Неоднозначно («%s»): %s" % (arg, ", ".join(starts[:8]))))
                return
            else:
                hits = [m for m in models if low in m.lower()]
                if len(hits) == 1:
                    pick = hits[0]
                elif len(hits) > 1:
                    print(color("err", "Неоднозначно («%s»): %s" % (arg, ", ".join(hits[:8]))))
                    return
    if not pick:
        print(color("err", "Модель «%s» не найдена. /models — список." % arg))
        return
    DEFAULT_MODEL = pick
    if messages and messages[0]["role"] == "system":
        pass  # модель — не часть промпта, просто переключаем
    print(color("sys", "Модель переключена на: %s" % DEFAULT_MODEL))


# ---------------------------------------------------------------------------
# Стриминг ответа
# ---------------------------------------------------------------------------

def _stream_chat(messages, model, tools):
    """Стримит ответ; печатает текст/мышление/вызовы инструментов; возвращает
    словарь assistant-сообщения (content + tool_calls)."""
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "tools": tools,
        "tool_choice": "auto",
    }
    resp = _http_post("/chat/completions", payload)
    sys.stdout.write(color("assistant", "Arena[%s]: " % model.split("-")[0]))
    sys.stdout.flush()

    text_parts = []
    reasoning_parts = []
    tool_acc = {}
    announced = set()
    t_start = time.time()
    token_count = 0

    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line or not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if data_str == "[DONE]":
            break
        try:
            ev = json.loads(data_str)
        except ValueError:
            continue
        if "error" in ev:
            msg = ev["error"].get("message", "unknown error")
            print(color("err", "\n[ошибка прокси] %s" % msg))
            return {"role": "assistant", "content": "", "tool_calls": []}
        choices = ev.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta", {})

        if delta.get("reasoning_content"):
            reasoning_parts.append(delta["reasoning_content"])
            if USE_COLOR:
                sys.stdout.write(C["dim"] + delta["reasoning_content"] + C["reset"])
                sys.stdout.flush()

        if delta.get("content") is not None:
            chunk = delta["content"]
            text_parts.append(chunk)
            sys.stdout.write(chunk)
            sys.stdout.flush()
            token_count += len(chunk.split())

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
            if slot["name"] and idx not in announced:
                announced.add(idx)
                print(color("tool", "\n[вызов инструмента: %s]" % slot["name"]))
                sys.stdout.flush()

    elapsed = time.time() - t_start
    print()
    if token_count > 0 and elapsed > 0.5:
        tps = token_count / elapsed
        print(color("dim", "  (~%.0f tok/s, %.1fs)" % (tps, elapsed)))

    tool_calls = []
    for idx in sorted(tool_acc):
        slot = tool_acc[idx]
        try:
            args = json.loads(slot["arguments"] or "{}")
        except ValueError:
            args = {"__raw__": slot["arguments"]}
        tool_calls.append({
            "id": slot["id"] or "call_%d" % idx,
            "name": slot["name"],
            "arguments": args,
        })
    return {
        "role": "assistant",
        "content": "".join(text_parts),
        "reasoning_content": "".join(reasoning_parts) or None,
        "tool_calls": tool_calls,
    }


# ---------------------------------------------------------------------------
# Цикл агента
# ---------------------------------------------------------------------------

def run_turn(messages, model):
    max_iter = 20
    for _ in range(max_iter):
        assistant = _stream_chat(messages, model, _TOOLS_SCHEMA)
        messages.append(assistant)
        if not assistant.get("tool_calls"):
            return
        for tc in assistant["tool_calls"]:
            print(color("tool", "→ %s(%s)" % (tc["name"], json.dumps(tc["arguments"], ensure_ascii=False)[:300])))
            result = execute_tool(tc["name"], tc["arguments"])
            print(color("tool", "  ↳ %s" % result[:800]))
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "name": tc["name"],
                "content": result,
            })


# ---------------------------------------------------------------------------
# Сессии
# ---------------------------------------------------------------------------

def _session_path(name):
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    return os.path.join(SESSIONS_DIR, "%s.json" % name)


def save_session(messages, name):
    path = _session_path(name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"model": DEFAULT_MODEL, "messages": messages}, f, ensure_ascii=False, indent=2)
    print(color("sys", "Сессия сохранена: %s" % path))


def load_session(messages, name):
    path = _session_path(name)
    if not os.path.isfile(path):
        print(color("err", "Сессия не найдена: %s" % path))
        return messages
    with open(path, "r", encoding="utf-8") as f:
        loaded = json.load(f)
    print(color("sys", "Сессия загружена: %s (%d сообщений)" % (path, len(loaded.get("messages", loaded)))))
    return loaded.get("messages", loaded)


def list_sessions():
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    files = sorted(f for f in os.listdir(SESSIONS_DIR) if f.endswith(".json"))
    if not files:
        print(color("info", "Нет сохранённых сессий."))
        return
    print(color("sys", "Сохранённые сессии:"))
    for f in files:
        name = f[:-5]
        fp = os.path.join(SESSIONS_DIR, f)
        size = os.path.getsize(fp)
        print("  %s  (%d байт)" % (name, size))


# ---------------------------------------------------------------------------
# Многострочный ввод
# ---------------------------------------------------------------------------

def _read_multiline(prompt_text):
    lines = []
    in_block = False
    while True:
        try:
            line = input(prompt_text if not lines else "... ")
        except EOFError:
            return "\n".join(lines) if lines else None
        except KeyboardInterrupt:
            print()
            return "\n".join(lines)
        if not in_block and line.strip() == "```multiline":
            in_block = True
            continue
        if in_block:
            if line.strip() == "":
                break
            lines.append(line)
            continue
        if line.endswith("\\"):
            lines.append(line[:-1])
            continue
        lines.append(line)
        break
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Справка
# ---------------------------------------------------------------------------

def print_help():
    print(color("sys", "\n=== Arena IDE — команды ==="))
    cmds = [
        ("/help",              "показать эту справку"),
        ("/clear",             "очистить историю диалога (начать новый)"),
        ("/new",               "начать новый диалог (то же, что /clear)"),
        ("/system <текст>",    "сменить системный промпт"),
        ("/system",            "показать текущий системный промпт"),
        ("/model <имя|номер>", "сменить модель (принимает часть имени, например /model claude)"),
        ("/model",             "показать текущую модель"),
        ("/models",            "список моделей арены с номерами"),
        ("/history",           "показать историю диалога"),
        ("/save [имя]",        "сохранить сессию (~/.arena_sessions/<имя>.json)"),
        ("/load [имя]",        "загрузить сессию"),
        ("/sessions",          "список сохранённых сессий"),
        ("/exit, /quit",       "выход"),
    ]
    for cmd, desc in cmds:
        print("  %-28s — %s" % (cmd, desc))
    print()
    print(color("info", "Многострочный ввод:"))
    print("  Строка, заканчивающаяся на \\  — продолжение на следующей строке.")
    print("  Введите  ```multiline  — начало блока; пустая строка — конец блока.")
    print()
    print(color("info", "Рабочий каталог: %s" % WORKDIR))
    print(color("info", "Сессии: %s" % SESSIONS_DIR))
    print()


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

messages = [{"role": "system", "content": SYSTEM_PROMPT}]


def main():
    global DEFAULT_MODEL, SYSTEM_PROMPT, BASE_URL, API_KEY, USE_COLOR, messages

    ap = argparse.ArgumentParser(
        description="Arena IDE — терминальная IDE-агент поверх LM Arena (бесплатно)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  python arena_cli.py\n"
            "  python arena_cli.py --model deepseek-v4.1-flash-high\n"
            "  python arena_cli.py --url http://localhost:8081\n"
            "  python arena_cli.py --system 'Ты — эксперт по Python'\n"
            "  python arena_cli.py --session mysession\n"
        ),
    )
    ap.add_argument("--url",     default=None, help="URL прокси (по умолчанию ARENA_API_URL или http://localhost:8081)")
    ap.add_argument("--key",     default=None, help="API-ключ (по умолчанию 'arena')")
    ap.add_argument("-m", "--model",   default=None, help="Модель (по умолчанию ARENA_MODEL или gemini-3.8-flash-high)")
    ap.add_argument("--system",  default=None, help="Системный промпт")
    ap.add_argument("--session", default=None, help="Загрузить сессию при старте")
    ap.add_argument("--no-color", action="store_true", help="Отключить цвета")
    args = ap.parse_args()

    if args.no_color:
        USE_COLOR = False
    if args.url:
        BASE_URL = args.url.rstrip("/") + "/v1"
    if args.key:
        API_KEY = args.key
    if args.model:
        DEFAULT_MODEL = args.model
    if args.system:
        SYSTEM_PROMPT = args.system
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    print(color("sys", "\n╔══════════════════════════════════════════╗"))
    print(color("sys", "║   Arena IDE  v1.0 — модели LM Arena      ║"))
    print(color("sys", "╚══════════════════════════════════════════╝"))
    print(color("info", "  URL    : %s" % BASE_URL))
    print(color("info", "  Модель : %s" % DEFAULT_MODEL))
    print(color("info", "  Каталог: %s" % WORKDIR))
    print(color("info", "  Сессии : %s" % SESSIONS_DIR))
    print(color("dim",  "  /help — справка, /models — список моделей\n"))

    ok, err = _check_api_available()
    if not ok:
        print(color("err", "⚠  Прокси недоступен (%s)" % err))
        print(color("err", "   Запусти сначала: python arena_api.py"))
        print(color("err", "   Или укажи другой URL: --url http://localhost:8081\n"))
    else:
        print(color("sys", "✓  Прокси доступен\n"))

    if args.session:
        messages = load_session(messages, args.session)
        loaded = None
        try:
            with open(_session_path(args.session), "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if loaded.get("model"):
                DEFAULT_MODEL = loaded["model"]
                print(color("sys", "Модель из сессии: %s" % DEFAULT_MODEL))
        except Exception:
            pass

    while True:
        try:
            user = _read_multiline(color("user", "you> "))
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if user is None:  # EOF (stdin закрыт) — выходим, а не крутимся в цикле
            print()
            break
        user = user.strip()
        if not user:
            continue

        if user.startswith("/"):
            parts = user[1:].split(None, 1)
            cmd = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""

            if cmd in ("quit", "exit"):
                print(color("sys", "До свидания!"))
                break

            elif cmd == "help":
                print_help()

            elif cmd in ("clear", "new"):
                messages = [{"role": "system", "content": SYSTEM_PROMPT}]
                print(color("sys", "Новый диалог начат."))

            elif cmd == "system":
                if arg:
                    SYSTEM_PROMPT = arg
                    if messages and messages[0]["role"] == "system":
                        messages[0]["content"] = SYSTEM_PROMPT
                    print(color("sys", "Системный промпт обновлён."))
                else:
                    print(color("sys", "Текущий системный промпт:"))
                    print(color("dim", SYSTEM_PROMPT))

            elif cmd == "model":
                cmd_model_switch(arg)

            elif cmd == "models":
                cmd_models_list()

            elif cmd == "history":
                if len(messages) <= 1:
                    print(color("info", "История пуста."))
                else:
                    print(color("sys", "История диалога (%d сообщений):" % (len(messages) - 1)))
                    for i, msg in enumerate(messages):
                        if msg["role"] == "system":
                            continue
                        role_label = {"user": "Вы", "assistant": "Arena", "tool": "Tool"}.get(msg["role"], msg["role"])
                        content = msg.get("content") or ""
                        if isinstance(content, list):
                            content = str(content)
                        preview = content[:200].replace("\n", " ")
                        print(color("dim", "  [%d] %s: %s%s" % (i, role_label, preview, "…" if len(content) > 200 else "")))

            elif cmd == "save":
                save_session(messages, arg or "default")

            elif cmd == "load":
                messages = load_session(messages, arg or "default")

            elif cmd == "sessions":
                list_sessions()

            else:
                print(color("err", "Неизвестная команда: /%s (введите /help)" % cmd))
            continue

        messages.append({"role": "user", "content": user})
        try:
            run_turn(messages, DEFAULT_MODEL)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:500]
            print(color("err", "Ошибка HTTP %s от прокси: %s" % (e.code, body)))
        except urllib.error.URLError as e:
            print(color("err", "\n⚠  Не удалось подключиться к прокси: %s" % e.reason))
            print(color("err", "   Запусти сначала: python arena_api.py"))
            print(color("err", "   Или укажи другой URL: --url http://localhost:8081"))
        except KeyboardInterrupt:
            print(color("sys", "\n[прервано]"))
        except Exception as e:
            print(color("err", "Ошибка: %s" % e))


if __name__ == "__main__":
    main()
