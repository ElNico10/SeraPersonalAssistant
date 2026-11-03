"""A page for using Sera from Chrome, without Discord: http://127.0.0.1:<port>/ (the exact link is printed when the bot starts).

It runs inside the bot, so it shares everything she has: memory, mood, reminders, coding jobs, the one-request-at-a-time lock.
What you type reaches her as a message from the owner, and what she sends comes back to the page. Nothing here knows how she thinks.

Adding a feature = adding a panel. Drop a file in web/panels/:
    web/panels/<id>.js   the panel's screen. First line `// title: Name`. Exports `mount(el, api)`:
                         api.call(data) -> Promise of the python side's answer, api.onEvent(fn) for pushes, api.say(text) to talk to her.
    web/panels/<id>.py   (optional) the panel's python side. Defines `setup(web_ui, env)`, which calls
                         web_ui.register_panel("<id>", "Name", handler) where `async def handler(data) -> dict` answers api.call(data).
    web_ui.push("<id>", {...}) sends something to a panel's onEvent at any time (a progress bar, a finished image).

The bot gives this module what it needs through init(env), so there is no import of discord_bot.
"""
import asyncio
import sera_home
import hmac
import importlib.util
import json
import os
import re
import secrets
import time
import traceback
from types import SimpleNamespace

from aiohttp import web, WSMsgType

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
CONFIG_PATH = os.path.join(HERE, "web_config.json")
TOKEN_PATH = sera_home.path("web_token.txt")
WEB_CHANNEL_ID = 9_000_000_000_000_001  # her chat id for this page; not a real Discord id
BACKLOG = 300  # events kept so a reopened page shows the conversation

env = None
clients = set()
backlog = []
panels = {}  # id -> {"title": ..., "handler": async fn or None}
_counter = [0]
_started = False


def init(environment):
    global env
    env = environment


def register_panel(pid, title, handler=None):
    panels[pid] = {"title": title, "handler": handler}


# ------------------------------------------------------------------ what goes to the page
def _log_path():
    return os.path.join(env.state_dir, "web_chat.json")


def _save_backlog():
    try:
        tmp = _log_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(backlog, f, ensure_ascii=False)
        os.replace(tmp, _log_path())
    except OSError:
        pass


async def emit(event, keep=False):
    """Send an event to every open page. keep=True also writes it in the conversation a reopened page replays."""
    event["t"] = time.time()
    if keep:
        backlog.append(event)
        del backlog[:-BACKLOG]
        _save_backlog()
    for ws in list(clients):
        try:
            await ws.send_json(event)
        except Exception:
            clients.discard(ws)


def push(pid, data):
    """From any thread's event loop-safe call: send data to a panel's onEvent."""
    return emit({"type": "panel", "panel": pid, "data": data})


# ------------------------------------------------------------------ the page as a Discord-shaped chat (so all of her features work)
class _Typing:
    async def __aenter__(self):
        await emit({"type": "typing", "on": True})

    async def __aexit__(self, *exc):
        await emit({"type": "typing", "on": False})


class WebChannel:
    id = WEB_CHANNEL_ID
    name = "web"
    guild = None

    async def send(self, content=None, **kw):
        if content:
            await emit({"type": "say", "text": re.sub(r"<@!?\d+>\s*", "", str(content))}, keep=True)
        _counter[0] += 1
        return SimpleNamespace(id=_counter[0])

    def typing(self):
        return _Typing()

    async def send_image(self, path):
        """A picture in the chat: from the gallery (/images/) or the hard-coded examples (/examples/)."""
        folder = "examples" if os.path.dirname(os.path.abspath(path)) == os.path.abspath(env.examples_dir) else "images"
        await emit({"type": "image", "url": f"/{folder}/" + os.path.basename(path)}, keep=True)

    async def history(self, **kw):  # nothing newer to look for: it is a one-person chat
        return
        yield

    def get_partial_message(self, i):
        return None


CHANNEL = WebChannel()


class WebMessage:
    """What the bot's on_message needs of a message, for text typed in the page. It is always the owner."""
    is_web = True

    def __init__(self, text):
        _counter[0] += 1
        owner = env.owner_id()
        self.id = _counter[0]
        self.content = self.clean_content = text
        self.author = SimpleNamespace(id=owner, name=env.owner_name(), display_name=env.owner_name(), bot=False)
        self.channel = CHANNEL
        self.guild = SimpleNamespace(id=0, name="web", get_member=lambda i: None)
        self.attachments, self.mentions, self.reference = [], [], None

    async def reply(self, text, **kw):
        return await CHANNEL.send(text)


async def _run_message(text):
    try:
        await env.on_message(WebMessage(text))
    except Exception:
        print(f"⚠️ web page message failed:\n{traceback.format_exc()}", flush=True)
        await emit({"type": "error", "text": "That crashed on my side; the details are in the bot's console."}, keep=True)


# ------------------------------------------------------------------ the server
def _token():
    try:
        with open(TOKEN_PATH, encoding="utf-8") as f:
            t = f.read().strip()
        if len(t) >= 32:
            return t
    except OSError:
        pass
    t = secrets.token_urlsafe(32)
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        f.write(t)
    return t


def _config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"enabled": True, "port": 8765}


def _panel_list():
    """Built-in and registered panels, then any panels/<id>.js that has no python side."""
    out = [{"id": p, "title": v["title"]} for p, v in panels.items()]
    folder = os.path.join(WEB_DIR, "panels")
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        pid = name[:-3] if name.endswith(".js") else None
        if pid and pid not in panels:
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                m = re.match(r"//\s*title:\s*(.+)", f.readline())
            out.append({"id": pid, "title": m.group(1).strip() if m else pid})
    return out


def _load_panel_modules():
    folder = os.path.join(WEB_DIR, "panels")
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if name.endswith(".py"):
            try:
                spec = importlib.util.spec_from_file_location(f"web_panel_{name[:-3]}", os.path.join(folder, name))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                mod.setup(__import__(__name__), env)
            except Exception:
                print(f"⚠️ panel {name} didn't load:\n{traceback.format_exc()}", flush=True)


async def _status_panel(data):
    return await asyncio.to_thread(env.status)


def _authorised(request, token):
    return hmac.compare_digest(request.cookies.get("sera_web", ""), token)


def make_app(token, port):
    @web.middleware
    async def guard(request, handler):
        given = request.query.get("t")
        if given and hmac.compare_digest(given, token):  # the printed link: remember it in a cookie, then drop it from the address bar
            r = web.HTTPFound("/")
            r.set_cookie("sera_web", token, httponly=True, samesite="Strict", max_age=86400 * 365)
            raise r
        if not _authorised(request, token):
            raise web.HTTPUnauthorized(text="Open the link printed in the bot's console (it ends in ?t=...).")
        return await handler(request)

    async def index(request):
        return web.FileResponse(os.path.join(WEB_DIR, "index.html"))

    async def panel_js(request):
        name = os.path.basename(request.match_info["name"])
        path = os.path.join(WEB_DIR, "panels", name)
        if not name.endswith(".js") or not os.path.isfile(path):
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})

    async def picture(request):
        name = os.path.basename(request.match_info["name"])
        path = os.path.join(env.images_dir, name)
        if not name.endswith(".png") or not os.path.isfile(path):
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def example_picture(request):
        name = os.path.basename(request.match_info["name"])
        path = os.path.join(env.examples_dir, name)
        if not name.endswith(".jpg") or not os.path.isfile(path):
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def socket(request):
        origin = request.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            raise web.HTTPForbidden(text="another site can't drive her")
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1_000_000)
        await ws.prepare(request)
        clients.add(ws)
        await ws.send_json({"type": "hello", "panels": _panel_list(), "owner": env.owner_name(), "backlog": backlog})
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                if data.get("type") == "say":
                    text = str(data.get("text", "")).strip()[:4000]
                    if text:
                        await emit({"type": "user", "text": text}, keep=True)
                        asyncio.create_task(_run_message(text))
                elif data.get("type") == "panel":
                    asyncio.create_task(_call_panel(ws, data))
        finally:
            clients.discard(ws)
        return ws

    app = web.Application(middlewares=[guard])
    app.add_routes([web.get("/", index), web.get("/panels/{name}", panel_js), web.get("/images/{name}", picture), web.get("/examples/{name}", example_picture), web.get("/ws", socket)])
    return app


async def _call_panel(ws, data):
    p = panels.get(data.get("panel"))
    try:
        if not p or not p["handler"]:
            raise KeyError(f"no such panel: {data.get('panel')}")
        answer = await p["handler"](data.get("data") or {})
        await ws.send_json({"type": "panel", "panel": data["panel"], "ref": data.get("ref"), "data": answer})
    except Exception as e:
        print(f"⚠️ panel {data.get('panel')} failed:\n{traceback.format_exc()}", flush=True)
        await ws.send_json({"type": "panel", "panel": data.get("panel"), "ref": data.get("ref"), "error": str(e)})


async def start():
    """Start serving (once). A busy port or a missing folder only costs the page, never the bot."""
    global _started
    cfg = _config()
    if _started or not cfg.get("enabled", True):
        return
    _started = True
    try:
        try:
            with open(_log_path(), encoding="utf-8") as f:
                backlog[:] = json.load(f)[-BACKLOG:]
        except (OSError, ValueError):
            pass
        register_panel("chat", "Chat")
        register_panel("status", "Status", _status_panel)
        _load_panel_modules()
        port, token = int(cfg.get("port", 8765)), _token()
        runner = web.AppRunner(make_app(token, port))
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()  # this computer only
        print(f"🖥️  Web page: http://127.0.0.1:{port}/?t={token}", flush=True)
    except Exception as e:
        print(f"⚠️ the web page didn't start: {e}", flush=True)
