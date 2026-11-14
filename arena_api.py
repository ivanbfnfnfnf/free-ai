#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Arena API — бесплатный OpenAI-совместимый API поверх LM Arena (arena.ai).

Даёт доступ к моделям арены (gemini, gpt, claude, deepseek, kimi, grok,
qwen, kimi и др.) через стандартный формат OpenAI API
( /v1/chat/completions ) в любом AI-агенте: VS Code (Cline, Continue,
Roo Code), opencode, Cursor и т.д.

Транспорт — управляемый Chrome (CDP): запросы уходят из реальной страницы
arena.ai, поэтому Cloudflare и reCAPTCHA Enterprise проходят автоматически.
Сессия (вход в аккаунт) живёт в отдельном профиле браузера и настраивается
один раз:

    python arena_api.py --setup     # открыть окно входа (один раз)
    python arena_api.py             # запустить сервер

Режим запросов: direct-battle (Direct) — одна выбранная модель.

Зависимости:  pip install websocket-client
"""

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import websocket  # websocket-client
    _HAS_WS = True
except ImportError:
    _HAS_WS = False

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

if getattr(sys, "frozen", False):  # запуск из .exe (PyInstaller)
    PY = os.path.dirname(sys.executable)
else:
    PY = os.path.dirname(os.path.abspath(__file__))

# ----------------------------------------------------------------------------
# Конфигурация (из переменных окружения или файла .env)
# ----------------------------------------------------------------------------

def _load_env():
    env_path = os.path.join(PY, ".env")
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

ARENA_BASE = os.environ.get("ARENA_BASE_URL", "https://arena.ai").rstrip("/")
HOST = os.environ.get("ARENA_HOST", "127.0.0.1")
PORT = int(os.environ.get("ARENA_PORT", "8081"))
DEFAULT_MODEL = os.environ.get("ARENA_DEFAULT_MODEL", "gemini-3.8-flash-high").strip()

# reCAPTCHA Enterprise v3 (ключ сайта арены, action для чата)
RECAPTCHA_SITEKEY = os.environ.get(
    "ARENA_RECAPTCHA_SITEKEY", "6LeTGMcsAAAAALuIlkVwIxaAuZA8VledA6d3Nnb0").strip()
RECAPTCHA_ACTION = os.environ.get("ARENA_RECAPTCHA_ACTION", "chat_submit").strip()

DEBUG_PORT = int(os.environ.get("ARENA_DEBUG_PORT", "9223"))
PROFILE_DIR = os.environ.get("ARENA_PROFILE_DIR", "").strip() or os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "ArenaIDE", "chrome-profile")
BROWSER_PATH = os.environ.get("ARENA_BROWSER_PATH", "").strip()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")

MODEL_CACHE_TTL = 3600          # каталог моделей, сек
RECAPTCHA_TTL = 100             # жила рекапчи-токена ~115с, берём с запасом
JOB_POLL_INTERVAL = 0.12        # опрос стрима
JOB_TIMEOUT = 600               # максимум на один запрос, сек

# ----------------------------------------------------------------------------
# Журнал
# ----------------------------------------------------------------------------

def log(msg):
    line = "[arena-api] %s" % msg
    try:
        print(line, flush=True)
    except Exception:
        # нет консоли (pythonw / .exe --windowed) — пишем в файл
        try:
            d = os.path.join(os.environ.get("LOCALAPPDATA", "."), "ArenaIDE")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "arena_api.log"), "a",
                      encoding="utf-8", errors="replace") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
        except Exception:
            pass


class ArenaError(Exception):
    pass


# ----------------------------------------------------------------------------
# Генерация UUIDv7 (сервер проверяет формат и «свежесть» времени)
# ----------------------------------------------------------------------------

def uuid7():
    ts = int(time.time() * 1000)
    b = bytearray(16)
    b[0:6] = ts.to_bytes(6, "big")
    rnd = os.urandom(10)
    b[6:16] = rnd
    b[6] = (b[6] & 0x0F) | 0x70   # version 7
    b[8] = (b[8] & 0x3F) | 0x80   # RFC-4122 variant
    h = b.hex()
    return "%s-%s-%s-%s-%s" % (h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])


# ----------------------------------------------------------------------------
# Поиск браузера
# ----------------------------------------------------------------------------

_BROWSER_CANDIDATES = [
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
    r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
]


def find_browser_exe():
    if BROWSER_PATH and os.path.exists(BROWSER_PATH):
        return BROWSER_PATH
    for p in _BROWSER_CANDIDATES:
        fp = os.path.expandvars(p)
        if os.path.exists(fp):
            return fp
    return None


def _free_port(start):
    for p in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start


def _find_existing_devtools(timeout_s=12):
    """Ищет уже запущенный браузер с нашим профилем и портом DevTools.

    Возвращает (порт, CDPPage) или None. Так сервер и IDE делят один инстанс
    браузера и подхватывают браузер, оставшийся от предыдущего запуска
    (--models, --setup и т.п.).
    """
    prof_low = os.path.normcase(PROFILE_DIR).rstrip(os.sep + "/")
    names = ("chrome.exe", "msedge.exe", "brave.exe")
    flt = " OR ".join("Name='%s'" % n for n in names)
    ps = ('Get-CimInstance Win32_Process -Filter "%s" | '
          'Where-Object { $_.CommandLine -ne $null } | '
          'ForEach-Object { $_.CommandLine }' % flt)
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, errors="replace",
            timeout=timeout_s).stdout or ""
    except Exception:
        return None
    ports = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or "--user-data-dir=" not in line:
            continue
        m = re.search(r"--remote-debugging-port=(\d+)", line, re.I)
        ud = re.search(r'--user-data-dir=(?:"([^"]*)"|(\S+))', line, re.I)
        if not m or not ud:
            continue
        path = os.path.normcase((ud.group(1) or ud.group(2) or "").strip())
        path = path.rstrip(os.sep + "/")
        if path == prof_low:
            ports[int(m.group(1))] = True
    for port in sorted(ports):
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/json" % port, timeout=3) as r:
                targets = json.loads(r.read().decode("utf-8", "replace"))
            page = next((t for t in targets
                         if t.get("type") == "page"
                         and "arena.ai" in (t.get("url") or "")), None)
            if not page:
                page = next((t for t in targets if t.get("type") == "page"), None)
            if page and page.get("webSocketDebuggerUrl"):
                log("Найден уже запущенный браузер (порт %d) — подключаюсь." % port)
                return (port, CDPPage(page["webSocketDebuggerUrl"]))
        except Exception:
            continue
    return None


# ----------------------------------------------------------------------------
# CDP-страница (DevTools Protocol через websocket)
# ----------------------------------------------------------------------------

class CDPError(Exception):
    pass


class CDPPage:
    """Одна вкладка браузера: команды через DevTools-протокол."""

    def __init__(self, ws_url):
        if not _HAS_WS:
            raise ArenaError("Нужен модуль websocket-client:  pip install websocket-client")
        self.ws_url = ws_url
        self.ws = None
        self._id = 0
        self.lock = threading.RLock()
        self.connect()

    def connect(self):
        self.ws = websocket.create_connection(self.ws_url, timeout=30)
        self.ws.settimeout(30)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass

    def call(self, method, params=None, timeout=30):
        with self.lock:
            self._id += 1
            mid = self._id
            try:
                self.ws.settimeout(timeout)
                self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                deadline = time.time() + timeout
                while time.time() < deadline:
                    try:
                        raw = self.ws.recv()
                    except websocket.WebSocketTimeoutException:
                        break
                    if not raw:
                        raise CDPError("WS закрыт")
                    msg = json.loads(raw)
                    if msg.get("id") == mid:
                        if "error" in msg:
                            raise CDPError("CDP %s: %s" % (method, json.dumps(msg["error"])[:300]))
                        return msg.get("result") or {}
                raise CDPError("CDP timeout: %s" % method)
            except (websocket.WebSocketException, OSError, ValueError) as e:
                raise CDPError("WS ошибка (%s): %s" % (method, e))

    def evaluate(self, expr, await_promise=False, timeout=30):
        res = self.call("Runtime.evaluate", {
            "expression": expr,
            "awaitPromise": bool(await_promise),
            "returnByValue": True,
        }, timeout=timeout)
        info = res.get("result") or {}
        if info.get("subtype") == "error":
            raise ArenaError("JS: %s" % (info.get("description") or "error")[:300])
        return info.get("value")


# ----------------------------------------------------------------------------
# JS-скрипты (стрим-насос, минтирование рекапчи)
# ----------------------------------------------------------------------------

_PUMP_SITEKEY = json.dumps(RECAPTCHA_SITEKEY)

PUMP_JS = """(() => {
  if (window.__arenaPumpInstalled) return true;
  window.__ARENA_JOBS = {};
  let seq = 0;
  window.__arenaRun = (url, bodyJson, headersJson) => {
    const jid = 'j' + (++seq) + '_' + Date.now();
    const slot = window.__ARENA_JOBS[jid] = { chunks: [], done: false, status: 0, error: null };
    (async () => {
      try {
        const headers = headersJson ? JSON.parse(headersJson) : { 'content-type': 'text/plain;charset=UTF-8' };
        const resp = await fetch(url, { method: 'POST', headers, body: bodyJson });
        slot.status = resp.status;
        if (!resp.ok || !resp.body) {
          const t = await resp.text().catch(() => '');
          slot.chunks.push(JSON.stringify({ __status: resp.status, __error: t.slice(0, 900) }));
          slot.done = true; return;
        }
        const reader = resp.body.getReader(); const dec = new TextDecoder();
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          slot.chunks.push(dec.decode(value, { stream: true }));
        }
      } catch (e) { slot.error = String((e && e.message) || e); }
      slot.done = true;
    })();
    return jid;
  };
  window.__arenaPoll = (jid) => {
    const s = window.__ARENA_JOBS[jid];
    if (!s) return null;
    const out = s.chunks.join('');
    s.chunks.length = 0;
    return { status: s.status, done: s.done, error: s.error, text: out };
  };
  window.__arenaCleanup = (jid) => { delete window.__ARENA_JOBS[jid]; return true; };
  window.__arenaMint = async (action) => {
    const gr = window.grecaptcha && (window.grecaptcha.enterprise || window.grecaptcha);
    if (!gr) return null;
    return await new Promise((resolve) => {
      let done = false;
      const fin = (v) => { if (!done) { done = true; resolve(v); } };
      setTimeout(() => fin(null), 20000);
      try {
        gr.ready(async () => {
          try { fin(await gr.execute(%s, { action: action || 'chat_submit' })); }
          catch (e) { fin(null); }
        });
      } catch (e) { fin(null); }
    });
  };
  window.__arenaPumpInstalled = true;
  return true;
})()""" % _PUMP_SITEKEY


def _js_str(s):
    return json.dumps(s)


# ----------------------------------------------------------------------------
# Клиент Arena (браузер + страница + высокоуровневые операции)
# ----------------------------------------------------------------------------

class ArenaClient:
    def __init__(self):
        self.port = None
        self.page = None
        self.page_ok = False
        self._models = {"at": 0.0, "list": []}
        self._captcha = {"token": None, "at": 0.0}
        self._auth = {"state": None, "at": 0.0}
        self.arena_session = {"id": None, "model_uuid": None}
        self.lock = threading.RLock()
        self.browser_proc = None
        self.started = False

    # --- запуск браузера ---

    def ensure_browser(self, visible=False):
        """Запускает браузер с выделенным профилем и возвращает вкладку arena.ai."""
        with self.lock:
            if self.page and self.page_ok:
                try:
                    if self.page.evaluate("1+1") == 2:
                        return self.page
                except Exception:
                    self.page_ok = False
                    self.page = None

            # 1) уже запущенный браузер с нашим профилем (от --models/--setup
            #    или от предыдущего запуска сервера) — используем его
            found = _find_existing_devtools()
            if found:
                self.port, self.page = found
                self.page_ok = True
                self.started = True
                self.browser_proc = None
                self.show_window(bool(visible))
                return self.page

            exe = find_browser_exe()
            if not exe:
                raise ArenaError(
                    "Не найден Chrome/Edge/Brave. Укажите путь в .env: ARENA_BROWSER_PATH=...")

            self.port = _free_port(DEBUG_PORT)
            if not os.path.isdir(PROFILE_DIR):
                os.makedirs(PROFILE_DIR, exist_ok=True)
            args = [
                exe,
                "--user-data-dir=%s" % PROFILE_DIR,
                "--remote-debugging-port=%d" % self.port,
                "--remote-allow-origins=*",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-session-crashed-bubble",
                "--hide-crash-restore-bubble",
                "--disable-features=Translate",
                "https://arena.ai/",
            ]
            log("Запускаю браузер: %s (профиль %s, порт %d)" % (os.path.basename(exe), PROFILE_DIR, self.port))
            try:
                self.browser_proc = subprocess.Popen(
                    args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as e:
                raise ArenaError("Не удалось запустить браузер: %s" % e)

            ws = self._wait_devtools(30)
            self.page = ws
            self.page_ok = True
            self.started = True
            self.show_window(bool(visible))
            return self.page

    def _wait_devtools(self, timeout_s):
        """Ждёт DevTools и подключается к вкладке arena.ai (создаёт её при нужде)."""
        deadline = time.time() + timeout_s
        last_err = None
        while time.time() < deadline:
            # браузер-спавн умер (например, профиль занят другим окном Chrome
            # без DevTools — второй инстанс молча уходит в существующий)
            if self.browser_proc is not None and self.browser_proc.poll() is not None:
                raise ArenaError(
                    "Браузер завершился сразу после запуска (код %s). Обычно профиль "
                    "уже занят другим окном Chrome без DevTools — закройте окна "
                    "браузера ArenaIDE и запустите снова." % self.browser_proc.returncode)
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/json" % self.port, timeout=3) as r:
                    targets = json.loads(r.read().decode("utf-8", "replace"))
                page = next((t for t in targets
                             if t.get("type") == "page"
                             and "arena.ai" in (t.get("url") or "")), None)
                if not page:
                    # создаём вкладку (в новых Chrome только PUT /json/new)
                    try:
                        req = urllib.request.Request(
                            "http://127.0.0.1:%d/json/new?https://arena.ai/" % self.port,
                            method="PUT")
                        with urllib.request.urlopen(req, timeout=5) as r:
                            page = json.loads(r.read().decode("utf-8", "replace"))
                    except Exception:
                        page = None
                if not page:
                    page = next((t for t in targets if t.get("type") == "page"), None)
                if page and page.get("webSocketDebuggerUrl"):
                    return CDPPage(page["webSocketDebuggerUrl"])
            except Exception as e:
                last_err = e
            time.sleep(0.5)
        raise ArenaError("DevTools браузера недоступен: %s" % (last_err or "timeout"))

    # --- окно ---

    def show_window(self, show):
        try:
            res = self.page.call("Browser.getWindowForTarget")
            wid = res.get("windowId")
            if wid:
                self.page.call("Browser.setWindowBounds", {
                    "windowId": wid,
                    "bounds": {"windowState": "normal" if show else "minimized"},
                })
        except Exception:
            pass

    # --- готовность страницы ---

    def ensure_page_ready(self, timeout_s=45):
        page = self.ensure_browser()
        deadline = time.time() + timeout_s
        cloudflare = False
        while time.time() < deadline:
            try:
                state = page.evaluate(
                    "(function(){ return { url: location.href, ready: document.readyState,"
                    " cf: document.title.indexOf('Just a moment') !== -1,"
                    " gr: !!(window.grecaptcha && (window.grecaptcha.enterprise || window.grecaptcha)) }; })()",
                    timeout=10)
                if isinstance(state, dict):
                    if state.get("cf"):
                        cloudflare = True
                    elif state.get("ready") == "complete":
                        if "arena.ai" not in str(state.get("url", "")):
                            page.call("Page.navigate", {"url": ARENA_BASE + "/"})
                            time.sleep(2)
                            continue
                        page.evaluate(PUMP_JS, timeout=10)
                        return page
            except Exception as e:
                log("Страница ещё не готова (%s)" % str(e)[:120])
                self.page_ok = False
                page = self.ensure_browser()
                deadline = time.time() + timeout_s
                continue
            time.sleep(0.7)
        if cloudflare:
            raise ArenaError(
                "Cloudflare показал проверку. Откройте окно браузера арены и пройдите её вручную, затем повторите.")
        raise ArenaError("Страница arena.ai не загрузилась за %dс" % timeout_s)

    # --- рекапча ---

    def get_captcha_token(self, force=False):
        """Минтит СВЕЖИЙ токен на каждый запрос.

        Токены reCAPTCHA v3 фактически одноразовые: повторная оценка того же
        токена проходит siteverify с провалом, и арена молча отдаёт пустой
        стрим (только heartbeats). Поэтому кэшировать нельзя.
        """
        page = self.ensure_page_ready()
        last_err = None
        for attempt in range(3):
            try:
                tok = page.evaluate(
                    "window.__arenaMint ? window.__arenaMint('chat_submit') : null",
                    await_promise=True, timeout=30)
            except Exception as e:
                tok = None
                last_err = e
            if isinstance(tok, str) and len(tok) > 100:
                self._captcha = {"token": tok, "at": time.time()}
                return tok
            time.sleep(1.5)
            page = self.ensure_page_ready()
        raise ArenaError(
            "reCAPTCHA недоступна на странице (grecaptcha не загружен%s). "
            "Если открыта проверка Cloudflare — пройдите её в окне браузера и повторите."
            % (": %s" % str(last_err)[:120] if last_err else ""))

    # --- проверка входа ---

    def check_auth(self, ttl=600):
        now = time.time()
        if self._auth["state"] is not None and now - self._auth["at"] < ttl:
            return self._auth["state"]
        try:
            page = self.ensure_page_ready()
            me = page.evaluate(
                "fetch('/api/me').then(r => r.json().then(j => ({ s: r.status, u: j && j.user })))",
                await_promise=True, timeout=15)
            state = bool(me and me.get("u") and (me["u"].get("email") or me["u"].get("id")))
            self._auth = {"state": state, "at": now}
            return state
        except Exception as e:
            log("check_auth: %s" % str(e)[:150])
            self._auth = {"state": False, "at": now}
            return False

    # --- каталог моделей ---

    def list_models(self, refresh=False):
        now = time.time()
        if self._models["list"] and not refresh and now - self._models["at"] < MODEL_CACHE_TTL:
            return self._models["list"]
        page = self.ensure_page_ready()
        raw = page.evaluate(
            "fetch('/nextjs-api/model-catalog').then(r => r.json())",
            await_promise=True, timeout=30)
        out = []
        if isinstance(raw, list):
            for arena in raw:
                if not isinstance(arena, dict) or arena.get("arena") != "text":
                    continue
                for m in arena.get("models") or []:
                    if not isinstance(m, dict):
                        continue
                    if m.get("userSelectable") is False:
                        continue
                    out.append({
                        "id": m.get("publicName") or "",
                        "uuid": m.get("id") or "",
                        "org": m.get("organization") or "",
                        "provider": m.get("provider") or "",
                        "input": ((m.get("capabilities") or {}).get("inputCapabilities") or {}),
                    })
        out = [m for m in out if m["id"] and m["uuid"]]
        if not out:
            raise ArenaError("Каталог моделей пуст (страница не готова?)")
        self._models = {"at": now, "list": out}
        return out

    def resolve_model(self, name):
        name = (name or "").strip()
        models = self.list_models()
        low = name.lower()
        for m in models:
            if m["id"].lower() == low:
                return m
        for m in models:
            if m["id"].lower().startswith(low):
                return m
        hits = [m for m in models if low in m["id"].lower()]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise ArenaError("Неоднозначная модель «%s»: %s" % (
                name, ", ".join(h["id"] for h in hits[:8])))
        return None

    # --- чат (стрим) ---

    def chat_stream(self, model_name, content, new_chat=False, fallback_content=None):
        """Генератор событий: ('text'|'reasoning'|'finish'|'meta', данные).

        Обычные запросы ПРОДОЛЖАЮТ текущую сессию арены (post-to-evaluation) —
        контекст диалога хранится на стороне арены. Новый чат создаётся только
        когда: сессии нет, пользователь передал new_chat=True, сменилась модель
        или продолжение не удалось (тогда один откат на create-evaluation
        с полной историей — fallback_content).
        """
        model = self.resolve_model(model_name)
        if not model:
            ids = ", ".join(m["id"] for m in self.list_models()[:20])
            raise ArenaError("Модель «%s» не найдена. Доступны: %s ..." % (model_name, ids))

        with self.lock:
            sess = dict(self.arena_session)
        reuse = (not new_chat and sess.get("id")
                 and sess.get("model_uuid") == model["uuid"])

        page = self.ensure_page_ready()
        page.evaluate(PUMP_JS, timeout=10)

        last_err = None
        for attempt in (1, 2):
            if attempt == 1 and reuse:
                url = "/nextjs-api/stream/post-to-evaluation/%s" % sess["id"]
                evaluation_id = sess["id"]
                use_content = content
                kind_label = "продолжение"
            else:
                url = "/nextjs-api/stream/create-evaluation"
                evaluation_id = uuid7()
                use_content = (fallback_content
                               if fallback_content and fallback_content.strip()
                               else content)
                kind_label = "новый чат"

            token = self.get_captcha_token()
            payload = {
                "id": evaluation_id,
                "mode": "direct-battle",
                "modelAId": model["uuid"],
                "userMessageId": uuid7(),
                "modelAMessageId": uuid7(),
                "userMessage": {
                    "content": use_content,
                    "experimental_attachments": [],
                    "metadata": {},
                },
                "modality": "chat",
                "recaptchaV3Token": token,
            }

            yielded_any = False
            try:
                for ev in self._pump_job(page, url, payload, kind_label):
                    yielded_any = True
                    yield ev
                with self.lock:
                    self.arena_session = {"id": evaluation_id,
                                          "model_uuid": model["uuid"]}
                return
            except ArenaError as e:
                last_err = e
                if not yielded_any and reuse and attempt == 1:
                    log("Продолжение сессии не удалось (%s) — начинаю новый чат "
                        "с полной историей." % str(e)[:160])
                    continue
                raise
        raise last_err or ArenaError("Не удалось выполнить запрос")

    def _pump_job(self, page, url, payload, kind_label):
        """Запускает запрос в странице и стримит события арены.

        Ошибка HTTP-статуса арены приходит первым чанком насоса — поэтому
        ArenaError бросается ДО каких-либо событий контента (важно для отката).
        """
        body = _js_str(json.dumps(payload))
        headers = _js_str(json.dumps({"content-type": "text/plain;charset=UTF-8"}))
        jid = page.evaluate(
            "window.__arenaRun(%s, %s, %s)" % (_js_str(url), body, headers), timeout=15)
        if not jid:
            raise ArenaError("Не удалось запустить запрос в странице")

        buf = ""
        status_seen = None
        error_text = None
        deadline = time.time() + JOB_TIMEOUT
        started = time.time()
        try:
            while True:
                if time.time() > deadline:
                    raise ArenaError("Таймаут запроса к арене (%.0fс)" % JOB_TIMEOUT)
                res = page.evaluate("window.__arenaPoll(%s)" % _js_str(jid), timeout=10)
                if res is None:
                    raise ArenaError("Стрим потерян (страница перезагрузилась). Повторите запрос.")
                raw = res.get("text") or ""
                if raw:
                    buf += raw
                    lines = buf.split("\n")
                    buf = lines.pop()
                    status_hit = False
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        ev = parse_stream_line(line)
                        if not ev:
                            continue
                        kind, data = ev
                        if kind == "__status__":
                            status_seen = data.get("status", 500)
                            error_text = data.get("error") or ""
                            status_hit = True
                            break
                        yield (kind, data)
                    if status_hit:
                        break
                if res.get("error"):
                    raise ArenaError("Ошибка транспорта: %s" % res["error"])
                if res.get("done"):
                    if buf.strip():
                        ev = parse_stream_line(buf.strip())
                        buf = ""
                        if ev:
                            kind, data = ev
                            if kind == "__status__":
                                status_seen = data.get("status", 500)
                                error_text = data.get("error") or ""
                            else:
                                yield (kind, data)
                    break
                time.sleep(JOB_POLL_INTERVAL)
        finally:
            try:
                page.evaluate("window.__arenaCleanup && window.__arenaCleanup(%s)" % _js_str(jid),
                              timeout=5)
            except Exception:
                pass
            log("Запрос (%s) %.1fс, статус %s"
                % (kind_label, time.time() - started,
                   status_seen if status_seen is not None else 200))

        if status_seen is not None and status_seen != 200:
            msg = error_text or ("HTTP %s" % status_seen)
            if status_seen == 429:
                raise ArenaError("Лимит арены (429). %s" % msg[:300])
            if status_seen in (401, 403):
                if "recaptcha" in msg.lower():
                    raise ArenaError("reCAPTCHA отклонена: %s" % msg[:300])
                self._auth = {"state": False, "at": 0.0}
                raise ArenaError(
                    "Нет доступа (HTTP %s): %s. Запустите python arena_api.py --setup и войдите в аккаунт."
                    % (status_seen, msg[:300]))
            raise ArenaError("Арена ответила %s: %s" % (status_seen, msg[:300]))

    # --- HEALTH ---

    def status(self):
        out = {"browser": False, "auth": None, "models": len(self._models["list"])}
        try:
            page = self.ensure_page_ready(timeout_s=20)
            out["browser"] = True
            out["auth"] = self.check_auth()
        except Exception as e:
            out["error"] = str(e)[:200]
        return out


# ----------------------------------------------------------------------------
# Разбор строк стрима арены:  a0:"текст"  ag:"мысли"  ad:{finishReason}  a2:[meta]
# ----------------------------------------------------------------------------

_LINE_RE = re.compile(r"^([a-z])([a-z0-9]):(.*)$", re.S)


def parse_stream_line(line):
    # спец-строка статуса от насоса: {"__status": 401, "__error": "..."}
    if line.startswith("{") and '"__status"' in line:
        try:
            data = json.loads(line)
        except ValueError:
            data = None
        if isinstance(data, dict) and "__status" in data:
            return ("__status__", {"status": data.get("__status", 500),
                                   "error": data.get("__error") or ""})
        return None
    m = _LINE_RE.match(line)
    if not m:
        return None
    stream, code, payload = m.group(1), m.group(2), m.group(3).strip()
    data = None
    if payload[:1] in ('"', "[", "{"):
        try:
            data = json.loads(payload)
        except ValueError:
            data = payload
    else:
        data = payload

    if code == "0":
        if isinstance(data, str) and stream == "a":
            return ("text", data)
        return None
    if code == "g":
        if isinstance(data, str) and stream == "a":
            return ("reasoning", data)
        return None
    if code == "d":
        if isinstance(data, dict) and stream == "a":
            return ("finish", data)
        return None
    if code == "2":
        if isinstance(data, list) and data and isinstance(data[0], dict) \
                and data[0].get("type") == "routed_model":
            return ("meta", "routed:%s" % data[0].get("organization", ""))
        return None
    return None


# ----------------------------------------------------------------------------
# Инструменты (tool calling через prompt injection)
# ----------------------------------------------------------------------------

_TO = chr(0x27E6)   # левая белая квадратная скобка
_TC = chr(0x27E7)   # правая белая квадратная скобка

TOOL_OPEN = _TO + "tool_call" + _TC
TOOL_CLOSE = _TO + "/tool_call" + _TC
TOOL_RESULT_TAG = _TO + "tool_response" + _TC

_OPEN_RE = re.compile(re.escape(TOOL_OPEN))
_CLOSE_RE = re.compile(re.escape(TOOL_CLOSE))


def _open_prefix(s):
    best = 0
    open_marker = TOOL_OPEN
    for k in range(1, len(open_marker)):
        if s.endswith(open_marker[:k]):
            best = max(best, k)
    return best


def compress_schema(schema):
    if not isinstance(schema, dict):
        return "any"
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return " | ".join(json.dumps(v) for v in schema["enum"])
    t = schema.get("type", "any")
    if t == "array":
        return compress_schema(schema.get("items", {})) + "[]"
    if t == "object":
        props = schema.get("properties")
        if not isinstance(props, dict):
            return "object"
        req = set(schema.get("required", []) or [])
        fields = []
        for k, v in props.items():
            fields.append("%s%s: %s" % (k, "" if k in req else "?", compress_schema(v)))
        return "{ " + "; ".join(fields) + " }"
    if isinstance(t, list):
        return " | ".join(compress_schema(dict(schema, type=tt)) for tt in t)
    return str(t)


def tool_system_prompt(tools, last_is_result=False):
    if not tools:
        return ""
    lines = []
    for t in tools:
        fn = t.get("function", t)
        name = fn.get("name", "unknown")
        desc = (fn.get("description") or "").strip()
        sig = compress_schema(fn.get("parameters", {}))
        lines.append("- %s%s\n  %s" % (name, sig, desc) if desc else "- %s%s" % (name, sig))
    rules = [
        "CRITICAL: To accomplish tasks that need a tool, output EXACTLY this and nothing else:",
        TOOL_OPEN,
        '{"name": "tool_name", "arguments": {"arg1": "value1"}}',
        TOOL_CLOSE,
        "",
        "## Available Tools",
        "\n".join(lines),
        "",
        "## Rules",
        "- ONE tool-call block per tool invocation (open marker, JSON on one line, close marker).",
        "- JSON inside MUST be valid, on one logical line.",
        "- Use exact tool names; provide all required arguments.",
        "- Do NOT wrap in code fences or markdown.",
        "- Do NOT explain what you are about to do. Just emit the block.",
        "- Tool results come back as %s ... %s." % (TOOL_RESULT_TAG, TOOL_RESULT_TAG),
    ]
    if last_is_result:
        rules += [
            "",
            "## Tool result received",
            "The tool was already executed and its result is provided above inside the %s tags. "
            "The task is finished. Do NOT call any tool again — write the final answer to the user now."
            % TOOL_RESULT_TAG,
        ]
    return "\n".join(rules)


def fold_tool_messages(messages):
    """Превращает OpenAI tool_calls/tool-сообщения в простой текст."""
    out = []
    call_names = {}
    for msg in messages:
        if not isinstance(msg, dict):
            out.append(msg)
            continue
        role = msg.get("role", "")
        if role == "assistant" and msg.get("tool_calls"):
            blocks = []
            content = msg.get("content") or ""
            for call in msg["tool_calls"]:
                fn = call.get("function", {})
                name = fn.get("name", "tool")
                args = fn.get("arguments", {})
                cid = call.get("id") or "call_%s" % uuid.uuid4().hex[:16]
                call_names[cid] = name
                if isinstance(args, str):
                    payload = '{"name": %s, "arguments": %s}' % (json.dumps(name), args)
                else:
                    payload = json.dumps({"name": name, "arguments": args})
                blocks.append("%s\n%s\n%s" % (TOOL_OPEN, payload, TOOL_CLOSE))
            if blocks:
                content = (content + "\n" + "\n".join(blocks)).strip()
            out.append({"role": "assistant", "content": content})
        elif role == "tool":
            cid = msg.get("tool_call_id", "")
            name = msg.get("name") or call_names.get(cid, "tool")
            content = msg.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content)
            out.append({"role": "user",
                        "content": '%s name="%s"\n%s\n%s'
                                   % (TOOL_RESULT_TAG, name, content, TOOL_RESULT_TAG)})
        else:
            out.append(msg)
    return out


def parse_tool_call(raw):
    text = raw.strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if m:
            try:
                parsed = json.loads(m.group(1))
            except ValueError:
                parsed = None
        else:
            parsed = None
    if isinstance(parsed, dict):
        name = parsed.get("name") or parsed.get("tool") or parsed.get("function")
        args = parsed.get("arguments") or parsed.get("parameters") or parsed.get("args") or {}
        if name:
            return {"name": str(name), "arguments": args}
    name_m = re.search(r"<tool_name>\s*([^<]+?)\s*</tool_name>", text)
    if name_m:
        name = name_m.group(1).strip()
        args = {}
        for km, vm in re.finditer(
                r"<parameter>\s*([^<]+?)\s*</parameter>\s*<value>\s*([\s\S]*?)\s*</value>", text):
            key = km.group(1).strip()
            val = vm.group(2).strip()
            try:
                val = json.loads(val)
            except ValueError:
                pass
            args[key] = val
        return {"name": name, "arguments": args}
    return None


class ToolParser:
    """Стриминговый парсер tool-блоков."""

    def __init__(self):
        self._pending = ""
        self._in_call = False
        self._buf = ""
        self._calls = []

    def push(self, chunk):
        res = {"text": "", "calls": []}
        if not chunk:
            return res
        if self._in_call:
            self._buf += chunk
            m = _CLOSE_RE.search(self._buf)
            if not m:
                return res
            pos = m.start()
            inner = self._buf[:pos]
            rest = self._buf[pos + len(m.group()):]
            self._buf = ""
            self._in_call = False
            payload = parse_tool_call(inner)
            if payload:
                res["calls"].append(payload)
                self._calls.append(payload)
            sub = self.push(rest)
            res["text"] += sub["text"]
            res["calls"].extend(sub["calls"])
            return res
        self._pending += chunk
        m = _OPEN_RE.search(self._pending)
        if m:
            pos = m.start()
            before = self._pending[:pos]
            tail = self._pending[pos + len(m.group()):]
            self._pending = ""
            if before:
                res["text"] += before
            self._in_call = True
            sub = self.push(tail)
            res["text"] += sub["text"]
            res["calls"].extend(sub["calls"])
            return res
        keep = _open_prefix(self._pending)
        if keep:
            res["text"] += self._pending[:-keep]
            self._pending = self._pending[-keep:]
        else:
            res["text"] += self._pending
            self._pending = ""
        return res

    def finish(self):
        res = {"text": "", "calls": []}
        if self._in_call and self._buf:
            payload = parse_tool_call(self._buf)
            if payload:
                res["calls"].append(payload)
                self._calls.append(payload)
            self._buf = ""
            self._in_call = False
        if self._pending:
            res["text"] += self._pending
            self._pending = ""
        return res


# ----------------------------------------------------------------------------
# Сборка сообщения для арены (системные промпты + история сворачиваются в текст)
# ----------------------------------------------------------------------------

def extract_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            t = item.get("type", "")
            if t == "text":
                parts.append(item.get("text", ""))
            elif t == "image_url":
                parts.append("[Изображение прикреплено — vision-режим пока не поддерживается]")
            else:
                parts.append(str(item.get("text", "")))
        return "\n".join(parts)
    return str(content)


def build_arena_message(messages):
    """Из OpenAI messages делает один текст: системные промпты + диалог."""
    system_parts = []
    conv = []
    for msg in messages:
        role = msg.get("role", "")
        text = extract_text(msg.get("content", ""))
        if role == "system":
            system_parts.append(text)
        elif role == "user":
            conv.append(("user", text))
        elif role == "assistant":
            conv.append(("assistant", text))
    if not conv:
        return ""
    if len(conv) == 1 and conv[0][0] == "user" and not system_parts:
        return conv[0][1]
    parts = []
    if system_parts:
        parts.append("\n\n".join(p for p in system_parts if p).strip())
        parts.append("")
    parts.append("[Диалог до текущего момента]")
    prev, prev_text = None, ""
    for role, text in conv:
        if prev == "user" and role == "assistant":
            parts.append("Пользователь: %s" % prev_text.strip())
            parts.append("Ассистент: %s" % text.strip())
            prev, prev_text = None, ""
        else:
            prev, prev_text = role, text
    if prev == "user":
        parts.append("[Конец предыдущего диалога]")
        parts.append("")
        parts.append("Текущий запрос пользователя:")
        parts.append(prev_text.strip())
    else:
        parts.append("[Конец диалога]")
    return "\n".join(parts)


def compose_arena_text(messages, tools):
    """Собирает единый текст для арены: tool-промпт + свёрнутые сообщения."""
    last_is_result = bool(messages) and messages[-1].get("role") == "tool"
    msgs = fold_tool_messages(messages)
    if tools:
        tool_prompt = tool_system_prompt(tools, last_is_result=last_is_result)
        if tool_prompt:
            msgs = [{"role": "system", "content": tool_prompt}] + msgs
    return build_arena_message(msgs)


# ----------------------------------------------------------------------------
# HTTP-сервер (OpenAI-совместимый)
# ----------------------------------------------------------------------------

CLIENT = ArenaClient()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ArenaAPI/1.0"

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, message, etype="api_error"):
        self._json(code, {"error": {"message": message, "type": etype}})

    def _start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _sse(self, data):
        body = data.encode("utf-8")
        self.wfile.write(("%x\r\n" % len(body)).encode("ascii") + body + b"\r\n")
        self.wfile.flush()

    def _sse_end(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _chunk(self, event):
        self._sse("data: %s\n\n" % json.dumps(event, ensure_ascii=False))

    # --- маршруты ---

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/", "/health"):
            return self._health()
        if path == "/v1/models":
            return self._models()
        if path == "/v1/status":
            return self._status()
        self._error(404, "Not found: %s" % path)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/v1/chat/completions":
            return self._chat_completions()
        self._error(404, "Not found: %s" % path)

    def _health(self):
        st = CLIENT.status()
        body = ("<h1>Arena API</h1>"
                "<p>OpenAI-совместимый API для LM Arena (arena.ai).</p>"
                "<ul>"
                "<li>Base URL: <code>http://%s:%s/v1</code></li>"
                "<li>Браузер: %s</li>"
                "<li>Вход в аккаунт: %s</li>"
                "<li>Моделей в каталоге: %s</li>"
                "</ul>"
                "<p>Первый запуск: <code>python arena_api.py --setup</code> — откроется окно "
                "браузера, войдите в arena.ai один раз. Затем: <code>python arena_api.py</code>.</p>"
                ) % (HOST, PORT,
                     "запущен" if st.get("browser") else ("ОШИБКА: %s" % st.get("error", "-")),
                     ("выполнен" if st.get("auth") else "НЕ выполнен (запустите --setup)")
                     if st.get("auth") is not None else "неизвестно",
                     st.get("models", "-"))
        body_bytes = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _status(self):
        try:
            self._json(200, CLIENT.status())
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _models(self):
        try:
            models = CLIENT.list_models()
        except Exception as e:
            return self._error(503, "Каталог моделей недоступен: %s" % e)
        data = [{
            "id": m["id"],
            "object": "model",
            "created": 0,
            "owned_by": m["org"] or "arena",
        } for m in models]
        self._json(200, {"object": "list", "data": data})

    def _chat_completions(self):
        try:
            raw = self._read_body()
            try:
                req = json.loads(raw.decode("utf-8"))
            except ValueError:
                return self._error(400, "Некорректный JSON")
            if not isinstance(req, dict):
                return self._error(400, "Ожидается JSON-объект")

            model = req.get("model") or DEFAULT_MODEL
            messages = req.get("messages", [])
            if not messages:
                return self._error(400, "Пустой список messages")
            stream = bool(req.get("stream", False))
            tools = req.get("tools")
            new_chat = bool(req.get("new_chat", False))

            # Контекст диалога хранится на стороне арены (сессия), поэтому
            # наружу отправляем только хвост от последнего сообщения
            # пользователя. Полная история (fallback_content) уходит лишь
            # при откате на новый чат — она восстановит контекст заново.
            cut = None
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    cut = i
                    break
            tail_msgs = messages[cut:] if cut is not None else messages

            content = compose_arena_text(tail_msgs, tools)
            fallback_content = compose_arena_text(messages, tools)
            if not content.strip():
                return self._error(400, "Пустое сообщение")

            if stream:
                self._stream_response(model, content, tools,
                                      new_chat=new_chat,
                                      fallback_content=fallback_content)
            else:
                self._non_stream_response(model, content, tools,
                                          new_chat=new_chat,
                                          fallback_content=fallback_content)
        except ArenaError as e:
            log("Arena error: %s" % e)
            try:
                self._error(502, str(e))
            except Exception:
                pass
        except Exception as e:
            log("Handler error: %s" % e)
            try:
                self._error(500, "Внутренняя ошибка: %s" % e)
            except Exception:
                pass

    # --- ответы ---

    def _stream_response(self, model, content, tools,
                         new_chat=False, fallback_content=None):
        self._start_sse()
        chat_id = "chatcmpl-%s" % uuid.uuid4().hex[:16]
        parser = ToolParser() if tools else None
        call_idx = 0
        started = False
        try:
            for kind, data in CLIENT.chat_stream(model, content, new_chat=new_chat,
                                                 fallback_content=fallback_content):
                now = int(time.time())
                if kind == "text":
                    started = True
                    if parser:
                        res = parser.push(data)
                        if res["text"]:
                            self._chunk(self._delta(chat_id, now, model, {"content": res["text"]}))
                        for call in res["calls"]:
                            self._chunk(self._tool_delta(chat_id, now, model, call_idx, call))
                            call_idx += 1
                    else:
                        self._chunk(self._delta(chat_id, now, model, {"content": data}))
                elif kind == "reasoning":
                    started = True
                    self._chunk(self._delta(chat_id, now, model,
                                            {"content": None, "reasoning_content": data}))
                elif kind == "finish":
                    fr = "stop"
                    if parser:
                        tail = parser.finish()
                        if tail["text"]:
                            self._chunk(self._delta(chat_id, now, model, {"content": tail["text"]}))
                        for call in tail["calls"]:
                            self._chunk(self._tool_delta(chat_id, now, model, call_idx, call))
                            call_idx += 1
                        if tail["calls"] or parser._calls:
                            fr = "tool_calls"
                    self._chunk({"id": chat_id, "object": "chat.completion.chunk",
                                 "created": now, "model": model,
                                 "choices": [{"index": 0, "delta": {}, "finish_reason": fr}]})
                    started = True
            if not started:
                self._chunk({"error": {"message": "Арена не вернула ответ", "type": "api_error"}})
            self._sse("data: [DONE]\n\n")
        except ArenaError as e:
            self._chunk({"error": {"message": str(e), "type": "api_error"}})
        except Exception as e:
            log("Stream error: %s" % e)
            self._chunk({"error": {"message": str(e), "type": "api_error"}})
        finally:
            self._sse_end()

    def _delta(self, rid, now, model, delta):
        return {"id": rid, "object": "chat.completion.chunk", "created": now,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}

    def _tool_delta(self, rid, now, model, idx, call):
        args = call["arguments"]
        if isinstance(args, dict):
            args = json.dumps(args, ensure_ascii=False)
        return {"id": rid, "object": "chat.completion.chunk", "created": now,
                "model": model,
                "choices": [{"index": 0,
                             "delta": {"tool_calls": [
                                 {"index": idx,
                                  "id": "call_%s" % uuid.uuid4().hex[:16],
                                  "type": "function",
                                  "function": {"name": call["name"], "arguments": args}}]},
                             "finish_reason": None}]}

    def _non_stream_response(self, model, content, tools,
                             new_chat=False, fallback_content=None):
        full = []
        reasoning = []
        parser = ToolParser() if tools else None
        tool_calls = []
        try:
            for kind, data in CLIENT.chat_stream(model, content, new_chat=new_chat,
                                                 fallback_content=fallback_content):
                if kind == "text":
                    if parser:
                        res = parser.push(data)
                        full.append(res["text"])
                        for call in res["calls"]:
                            args = call["arguments"]
                            if isinstance(args, dict):
                                args = json.dumps(args, ensure_ascii=False)
                            tool_calls.append({
                                "id": "call_%s" % uuid.uuid4().hex[:16],
                                "type": "function",
                                "function": {"name": call["name"], "arguments": args}})
                    else:
                        full.append(data)
                elif kind == "reasoning":
                    reasoning.append(data)
                elif kind == "finish":
                    if parser:
                        tail = parser.finish()
                        full.append(tail["text"])
                        for call in tail["calls"]:
                            args = call["arguments"]
                            if isinstance(args, dict):
                                args = json.dumps(args, ensure_ascii=False)
                            key = (call["name"], args)
                            if key not in [(t["function"]["name"], t["function"]["arguments"])
                                           for t in tool_calls]:
                                tool_calls.append({
                                    "id": "call_%s" % uuid.uuid4().hex[:16],
                                    "type": "function",
                                    "function": {"name": call["name"], "arguments": args}})
        except ArenaError:
            raise
        except Exception as e:
            return self._error(500, "Ошибка чтения ответа арены: %s" % e)

        text = "".join(full)
        message = {"role": "assistant", "content": text}
        finish = "stop"
        if tool_calls:
            finish = "tool_calls"
            message["tool_calls"] = tool_calls
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)

        self._json(200, {
            "id": "chatcmpl-%s" % uuid.uuid4().hex[:16],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    def log_message(self, fmt, *args):
        return  # не спамить в консоль


# ----------------------------------------------------------------------------
# Watchdog: держит вкладку арены живой
# ----------------------------------------------------------------------------

def _watchdog():
    while True:
        time.sleep(30)
        if not CLIENT.started:
            continue
        try:
            page = CLIENT.page
            if not page or not CLIENT.page_ok:
                continue
            st = page.evaluate(
                "(function(){ return { url: location.href, gr: !!(window.grecaptcha) }; })()",
                timeout=10)
            url = str((st or {}).get("url", ""))
            if "arena.ai" not in url:
                page.call("Page.navigate", {"url": ARENA_BASE + "/"})
                log("Watchdog: вкладка вернулась на arena.ai")
        except Exception:
            CLIENT.page_ok = False
            try:
                CLIENT.ensure_page_ready(timeout_s=30)
                log("Watchdog: браузер переподключён")
            except Exception as e:
                log("Watchdog: не удалось переподключиться (%s)" % str(e)[:120])


# ----------------------------------------------------------------------------
# Setup (первый вход) и самопроверка
# ----------------------------------------------------------------------------

def cmd_setup():
    print("=" * 60)
    print("Настройка входа в LM Arena (arena.ai)")
    print("=" * 60)
    if not _HAS_WS:
        print("Нужен модуль websocket-client:  pip install websocket-client")
        return 1
    page = CLIENT.ensure_browser(visible=True)
    try:
        page.call("Page.navigate", {"url": ARENA_BASE + "/"})
    except Exception:
        pass
    print()
    print("1) Открылось окно браузера с профилем ArenaIDE.")
    print("2) Войдите в аккаунт arena.ai (любым способом).")
    print("3) Как только войдёте — настройка завершится автоматически.")
    print("   Окно можно свернуть; браузер держит сессию в профиле %s" % PROFILE_DIR)
    print()
    deadline = time.time() + 900
    ok = False
    while time.time() < deadline:
        try:
            page = CLIENT.ensure_page_ready(timeout_s=15)
            me = page.evaluate(
                "fetch('/api/me').then(r => r.json().then(j => ({ s: r.status, u: j && j.user })))",
                await_promise=True, timeout=15)
            user = (me or {}).get("u") or {}
            if user.get("email"):
                print("Вход выполнен: %s" % user["email"])
                ok = True
                break
            elif user.get("id"):
                print("Обнаружен анонимный сеанс. Для надёжного доступа войдите в аккаунт "
                      "(email или Google) в открывшемся окне.")
        except Exception as e:
            print("  жду страницу... (%s)" % str(e)[:80])
        time.sleep(3)
    if ok:
        try:
            models = CLIENT.list_models(refresh=True)
            print("Доступно моделей: %d. Примеры: %s" % (
                len(models), ", ".join(m["id"] for m in models[:6])))
        except Exception as e:
            print("Каталог моделей пока не прочитан: %s" % str(e)[:150])
        print("Готово! Теперь запустите сервер:  python arena_api.py")
        return 0
    print("Время ожидания истекло (15 минут). Запустите --setup ещё раз, когда будете готовы.")
    return 1


def cmd_models():
    models = CLIENT.list_models()
    for m in models:
        print("%-40s [%s]" % (m["id"], m["org"]))
    return 0


def cmd_selftest(full=False):
    import threading
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    def get(path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (PORT, path), timeout=60) as r:
            return r.status, r.read()

    def post(path, body, timeout=120):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (PORT, path),
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    ok = True
    try:
        s, _ = get("/")
        print("GET /            -> %d %s" % (s, "OK" if s == 200 else "FAIL"))
        ok = ok and s == 200

        s, body = get("/v1/models")
        data = json.loads(body)
        ids = [m["id"] for m in data["data"]]
        print("GET /v1/models   -> %d, %d моделей" % (s, len(ids)))
        ok = ok and s == 200 and len(ids) > 0

        if full and ids:
            s, body = post("/v1/chat/completions",
                           {"model": ids[0], "stream": False,
                            "messages": [{"role": "user", "content": "Скажи ровно: PING_OK"}]})
            print("POST /v1/chat/completions -> %d %s" % (
                s, (json.loads(body).get("choices") or [{}])[0].get("message", {}).get("content", "")[:80]))
            ok = ok and s == 200
    except Exception as e:
        print("Selftest error: %s" % e)
        ok = False
    finally:
        server.shutdown()
    print("SELFTEST %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def serve():
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        print("Не удалось запустить сервер: %s" % e)
        print("Похоже, порт %s занят. Закройте старый сервер или смените ARENA_PORT в .env." % PORT)
        return 1
    server.daemon_threads = True
    threading.Thread(target=_watchdog, daemon=True).start()
    print("Arena API запущен:")
    print("  OpenAI Base URL : http://%s:%s/v1" % (HOST, PORT))
    print("  Модель по умолч.: %s" % DEFAULT_MODEL)
    print("  Профиль браузера: %s" % PROFILE_DIR)
    print("  Для VS Code/Cline/Continue/opencode используйте адрес выше.")
    print("  Ctrl+C — остановить (браузер останется свернутым).")
    # прогрев: страница + каталог (без блокировки запуска)
    def _warm():
        try:
            CLIENT.ensure_page_ready(timeout_s=60)
            CLIENT.list_models()
            authed = CLIENT.check_auth()
            if not authed:
                print("! Вход в аккаунт не обнаружен. Запустите:  python arena_api.py --setup")
        except Exception as e:
            print("! Прогрев не удался: %s" % str(e)[:200])
    threading.Thread(target=_warm, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
        try:
            CLIENT.show_window(False)
        except Exception:
            pass
    return 0


def main():
    args = sys.argv[1:]
    if "--setup" in args:
        sys.exit(cmd_setup())
    if "--models" in args:
        try:
            sys.exit(cmd_models())
        except Exception as e:
            print("Ошибка: %s" % e)
            sys.exit(1)
    if "--selftest" in args:
        sys.exit(cmd_selftest(full="--full" in args))
    sys.exit(serve())


if __name__ == "__main__":
    main()
