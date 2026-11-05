"""Spotify over plain HTTPS (no extra packages): log in once, then search and control the Spotify app running on this PC.

Login is the PKCE flow, so there is no client secret anywhere. The only setting is the app's Client ID, in ~/.sera/spotify.json
({"client_id": "...", "redirect_uri": "http://127.0.0.1:8888/callback"}); the login token is kept next to it in spotify_token.json.
Everything here blocks, so the bot calls it through asyncio.to_thread. Playing needs Spotify Premium.
"""
import base64
import hashlib
import http.server
import json
import os
import difflib
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

import sera_home

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing playlist-read-private"
SETTINGS_FILE, TOKEN_FILE = "spotify.json", "spotify_token.json"
DEFAULT_REDIRECT = "http://127.0.0.1:8888/callback"
KINDS = ("track", "album", "artist", "playlist")
TRANSFER_WAIT = 1.0  # seconds the app is given to become the active device before the play request
_token_lock = threading.Lock()


class SpotifyError(Exception):
    """Something to tell the person, in plain words."""


def _settings():
    try:
        with open(sera_home.path(SETTINGS_FILE), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    client_id = str(data.get("client_id", "")).strip()
    if not client_id:
        raise SpotifyError(f"Spotify isn't set up: put your app's Client ID in {sera_home.path(SETTINGS_FILE)} (\"client_id\").")
    return client_id, str(data.get("redirect_uri") or DEFAULT_REDIRECT).strip()


def _http(method, url, headers=None, form=None, body=None):
    """(status, parsed JSON or {}). HTTP errors come back as a status; only an unreachable Spotify raises."""
    if form is not None:
        raw = urllib.parse.urlencode(form).encode()
    elif body is not None:
        raw = json.dumps(body).encode()
    else:
        raw = b"" if method in ("POST", "PUT") else None  # Spotify wants a Content-Length even when there is nothing to send
    req = urllib.request.Request(url, data=raw, method=method, headers=dict(headers or {}))
    if form is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    elif body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            text = r.read()
            try:
                return r.status, (json.loads(text) if text else {})
            except ValueError:  # a success whose body isn't JSON: the request worked, so keep what it said for the log
                return r.status, {"_raw": text[:80].decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            payload["_retry_after"] = e.headers.get("Retry-After")
        return e.code, payload
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SpotifyError(f"Couldn't reach Spotify ({e}).")


# ------------------------------------------------------------------ login and token
def _save_token(token):
    path = sera_home.path(TOKEN_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(token, f)
    os.replace(tmp, path)


def _load_token():
    try:
        with open(sera_home.path(TOKEN_FILE), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _token_from(resp, old=None):
    return {"access_token": resp["access_token"],
            "refresh_token": resp.get("refresh_token") or (old or {}).get("refresh_token"),  # a refresh doesn't always send a new one
            "expires_at": time.time() + int(resp.get("expires_in", 3600))}


def logged_in():
    return bool(_load_token())


def access_token(force=False):
    """A token that is good for at least another minute; refreshed (and saved) when it isn't."""
    with _token_lock:
        token = _load_token()
        if not token or not token.get("refresh_token"):
            raise SpotifyError("Not logged in to Spotify yet: send `!vc spotify login` and approve it in the browser window that opens.")
        if force or token.get("expires_at", 0) - time.time() < 60:
            client_id, _ = _settings()
            status, resp = _http("POST", TOKEN_URL, form={"grant_type": "refresh_token", "refresh_token": token["refresh_token"], "client_id": client_id})
            if status != 200 or "access_token" not in resp:
                raise SpotifyError("Spotify login expired or was revoked: send `!vc spotify login` again.")
            token = _token_from(resp, token)
            _save_token(token)
        return token["access_token"]


def login(timeout=180, say=print):
    """Open the Spotify approval page in the browser on this PC, wait for it to come back to the local address, and save the token."""
    client_id, redirect = _settings()
    parts = urllib.parse.urlparse(redirect)
    if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "::1"):
        raise SpotifyError("redirect_uri must be a loopback address like http://127.0.0.1:8888/callback (Spotify doesn't accept \"localhost\").")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urllib.parse.urlencode({"client_id": client_id, "response_type": "code", "redirect_uri": redirect, "scope": SCOPES,
                                                   "code_challenge_method": "S256", "code_challenge": challenge, "state": state})
    got = {}

    class Catcher(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            target = urllib.parse.urlparse(self.path)
            if target.path != parts.path:
                self.send_response(404)
                self.end_headers()
                return
            got.update({k: v[0] for k, v in urllib.parse.parse_qs(target.query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body style='font-family:sans-serif'><h3>Done. You can close this tab and go back to Discord.</h3></body></html>")

        def log_message(self, *args):
            pass

    try:
        server = http.server.HTTPServer((parts.hostname, parts.port or 80), Catcher)
    except OSError as e:
        raise SpotifyError(f"Couldn't listen on {parts.hostname}:{parts.port} for the login ({e}). Is something else using that port? "
                           "Pick another one in spotify.json AND in the Spotify dashboard.")
    server.timeout = 1
    say(f"Spotify login: opening your browser. If nothing opens, go to:\n{url}")
    try:
        webbrowser.open(url)
        deadline = time.time() + timeout
        while not got and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not got:
        raise SpotifyError("The Spotify login timed out: nothing came back. Try `!vc spotify login` again.")
    if got.get("error") or got.get("state") != state or "code" not in got:
        raise SpotifyError(f"Spotify login was not approved ({got.get('error') or 'bad reply'}).")
    status, resp = _http("POST", TOKEN_URL, form={"grant_type": "authorization_code", "code": got["code"], "redirect_uri": redirect,
                                                  "client_id": client_id, "code_verifier": verifier})
    if status != 200 or "access_token" not in resp:
        raise SpotifyError(f"Spotify wouldn't give a token ({resp.get('error_description') or resp.get('error') or status}).")
    _save_token(_token_from(resp))


# ------------------------------------------------------------------ the API
def _api(method, path, params=None, body=None, _again=True):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    status, data = _http(method, url, headers={"Authorization": "Bearer " + access_token(force=not _again)}, body=body)
    print(f"🎵 spotify {method} {path} {params or ''} {body or ''} -> {status}" + (f" {data}" if status >= 400 or "_raw" in data else ""), flush=True)  # no tokens in here
    if status == 401 and _again:
        return _api(method, path, params, body, _again=False)  # the token was refused: refresh it once and retry
    if status in (200, 201, 202, 204):
        return data
    error = data.get("error") if isinstance(data, dict) else None
    reason = (error or {}).get("reason") if isinstance(error, dict) else None
    message = (error or {}).get("message") if isinstance(error, dict) else str(error or "")
    if status == 404 and reason == "NO_ACTIVE_DEVICE":
        raise SpotifyError("Spotify has nothing to play on: open the Spotify app on this PC, then ask again.")
    if status == 429:
        raise SpotifyError(f"Spotify says slow down: try again in {data.get('_retry_after') or 'a few'} seconds.")
    if status in (401, 403) and "scope" in (message or "").lower():
        raise SpotifyError("Spotify needs a new permission for that: send `!vc spotify login` again and approve it.")
    if status == 403 and (reason == "PREMIUM_REQUIRED" or "premium" in (message or "").lower()):
        raise SpotifyError("Spotify only lets apps control playback on a Premium account.")
    if status == 403:
        raise SpotifyError(f"Spotify refused ({message or 'forbidden'}). Is your account added under User Management in the developer dashboard?")
    raise SpotifyError(f"Spotify error {status}: {message or 'no details'}")


def search(query, kind="track", limit=5):
    """[{uri, name, by, kind}] best match first."""
    kind = kind if kind in KINDS else "track"
    data = _api("GET", "/search", {"q": query, "type": kind, "limit": limit})
    out = []
    for item in (data.get(kind + "s") or {}).get("items") or []:
        if not item:
            continue  # Spotify sometimes returns null entries
        by = [a["name"] for a in item.get("artists", [])] or ([item["owner"]["display_name"]] if item.get("owner") else [])
        out.append({"uri": item["uri"], "name": item["name"], "by": ", ".join(b for b in by if b), "kind": kind, "album": (item.get("album") or {}).get("uri")})
    print("🎵 spotify found: " + " | ".join(f"{o['name']} - {o['by']} ({o['uri']})" for o in out[:3]), flush=True)
    return out


_playlist_cache = {"at": 0.0, "items": []}
PLAYLIST_CACHE_SECONDS = 120  # the list rarely changes; this keeps "play my playlist ..." from paging through it every time


def my_playlists():
    """[{uri, name, by, kind: 'playlist'}] for the playlists in the person's own library (up to 200)."""
    if time.time() - _playlist_cache["at"] < PLAYLIST_CACHE_SECONDS and _playlist_cache["items"]:
        return _playlist_cache["items"]
    out, offset = [], 0
    while offset < 200:
        page = _api("GET", "/me/playlists", {"limit": 50, "offset": offset})
        for p in page.get("items") or []:
            if p:  # Spotify sometimes returns null entries
                out.append({"uri": p["uri"], "name": p["name"], "by": (p.get("owner") or {}).get("display_name") or "", "kind": "playlist"})
        if not page.get("next"):
            break
        offset += 50
    _playlist_cache.update(at=time.time(), items=out)
    return out


def _words(text):
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def match_playlist(items, query):
    """The playlist whose name best fits what was said, or None. A transcriber won't spell a name exactly, so: the name contains the words
    said (best), or looks like them (close spelling). Needs a reasonable fit rather than always returning something."""
    q = _words(query)
    scored = []
    for item in items:
        name = _words(item["name"])
        if not q or not name:
            continue
        score = 1.0 if name == q else 0.95 if q in name else 0.9 if set(q.split()) <= set(name.split()) else difflib.SequenceMatcher(None, q, name).ratio()
        scored.append((score, -len(name), item))
    best = max(scored, key=lambda s: s[:2], default=None)  # ties go to the shorter (closer) name
    return best[2] if best and best[0] >= 0.6 else None


def my_playlist(query):
    return match_playlist(my_playlists(), query)


def devices():
    return [d for d in _api("GET", "/me/player/devices").get("devices", []) if not d.get("is_restricted")]


def pick_device(preferred=""):
    """The Spotify device to use: the one named in the settings, else this PC's app (its device name is the PC's name), else any computer,
    else the one playing. None if Spotify isn't open. This PC comes before "the one playing": only this PC's sound reaches the call."""
    found = devices()
    if preferred:
        found_named = [d for d in found if preferred.lower() in d.get("name", "").lower()]
        if found_named:
            return found_named[0]
    me = socket.gethostname().lower()
    for rule in (lambda d: d.get("name", "").lower() == me, lambda d: d.get("type") == "Computer", lambda d: d.get("is_active"), lambda d: True):
        for d in found:
            if rule(d):
                return d
    return None


def play(item=None, device=None):
    """Start an item (a track on its own; an album, artist or playlist as a context). Without one, resume."""
    body = None
    if item:
        body = {"uris": [item["uri"]]} if item["kind"] == "track" else {"context_uri": item["uri"]}
        if item["kind"] == "track" and item.get("via_album") and item.get("album"):
            body = {"context_uri": item["album"], "offset": {"uri": item["uri"]}}  # how the app plays a song you click: inside its album
    if device and not device.get("is_active"):
        # an app that isn't the active device often takes a bare "play" and loads nothing: move playback to it first
        _api("PUT", "/me/player", body={"device_ids": [device["id"]], "play": False})
        time.sleep(TRANSFER_WAIT)
    _api("PUT", "/me/player/play", {"device_id": device["id"]} if device else None, body)


def _simple(method, path, device, extra=None):
    params = dict(extra or {})
    if device:
        params["device_id"] = device["id"]
    _api(method, path, params or None)


def pause(device=None):
    _simple("PUT", "/me/player/pause", device)


def next_track(device=None):
    _simple("POST", "/me/player/next", device)


def previous_track(device=None):
    _simple("POST", "/me/player/previous", device)


def queue(item, device=None):
    _simple("POST", "/me/player/queue", device, {"uri": item["uri"]})


def set_volume(percent, device=None):
    _simple("PUT", "/me/player/volume", device, {"volume_percent": max(0, min(100, int(percent)))})


def state():
    """What Spotify says is going on right now: {playing, device, volume, item, blocked}. All empty when no player is active."""
    data = _api("GET", "/me/player") or {}
    return {"playing": bool(data.get("is_playing")), "device": (data.get("device") or {}).get("name"), "volume": (data.get("device") or {}).get("volume_percent"),
            "item": (data.get("item") or {}).get("name"), "blocked": (data.get("actions") or {}).get("disallows")}


def now_playing():
    """{name, by, playing} or None when nothing is loaded."""
    data = _api("GET", "/me/player/currently-playing")
    item = (data or {}).get("item")
    if not item:
        return None
    return {"name": item["name"], "by": ", ".join(a["name"] for a in item.get("artists", [])), "playing": bool(data.get("is_playing"))}
