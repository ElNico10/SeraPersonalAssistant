"""Music mode for a voice call: she joins with only speech recognition loaded (on the CPU, so the GPU stays free), waits for her name
("hey sera, ..."), and plays what is asked on the Spotify app running on this PC.

Spotify's own sound reaches the call through a virtual audio cable: the Spotify app is set to play into the cable, and this module reads the
cable like a microphone and hands it to the call. No text-to-speech and no chat model are involved; answers go in the text channel.
Settings: music_config.json (re-read for every line). The Spotify login and Client ID: spotify_client.py.
"""
import asyncio
import json
import os
import re
import subprocess
import threading
import time
import traceback

import discord

import sera_home
import spotify_client as sp

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "music_config.json")
LOCAL_PATH = sera_home.path("music_local.json")  # this PC's own settings (device names); read over music_config.json, so they stay out of the repo
FRAME_BYTES = 3840  # 20 ms of 48 kHz, 16-bit, stereo: what Discord plays
CHECK_DELAY = 1.5   # seconds after starting a song before asking Spotify whether it is playing
DEFAULTS = {
    "who_can_control": "owner",
    "who_can_queue": "anyone",
    "wake_words": ["sera", "seraphim", "sarah", "sara", "sierra", "cera", "serra"],
    "need_greeting": False,
    "follow_seconds": 10,
    "capture_device": "CABLE Output (VB-Audio Virtual Cable)",
    "spotify_device": "",
    "volume_step": 10,
    "buffer_ms": 300,
    "silence_seconds": 0.5,
    "log_heard": False,
    "stt_engine": "faster_whisper_music",
    "routing": {"enabled": False, "tool": "tools/soundvolumeview/app/SoundVolumeView.exe", "app": "Spotify.exe",
                "music_output": "VB-Audio Virtual Cable\\Device\\CABLE Input\\Render", "normal_output": ""},
}


def cfg():
    """DEFAULTS, then music_config.json, then ~/.sera/music_local.json (this PC's device names). `routing` is merged key by key."""
    data = {**DEFAULTS, "routing": dict(DEFAULTS["routing"])}
    for path in (CONFIG_PATH, LOCAL_PATH):
        try:
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
        except OSError:
            continue  # the local file is optional
        except ValueError as e:  # a typo in a file must not be silent: she would quietly run on the built-in settings
            print(f"⚠️ {os.path.basename(path)} isn't valid JSON ({e}): ignoring it until it is fixed.", flush=True)
            continue
        data["routing"].update(loaded.pop("routing", None) or {})
        data.update(loaded)
    return data


def route_audio(to_music):
    """Point the Spotify app's output at the virtual cable (music mode on) or back at the normal output (off), with NirSoft's
    SoundVolumeView. Returns None when done or switched off, else a sentence saying what went wrong. Never raises."""
    r = cfg()["routing"]
    if not r.get("enabled"):
        return None
    tool = r["tool"] if os.path.isabs(r["tool"]) else os.path.join(HERE, r["tool"])
    target = r["music_output"] if to_music else (r.get("normal_output") or "DefaultRenderDevice")
    try:
        done = subprocess.run([tool, "/SetAppDefault", target, "all", r["app"]], capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        return f"the audio tool isn't at {tool}"
    except (OSError, subprocess.SubprocessError) as e:
        return f"the audio tool failed ({e})"
    if done.returncode != 0:
        return f"the audio tool said no (code {done.returncode}) for {target!r}: check the device name in music_local.json"
    return None


# ------------------------------------------------------------------ the sound
class LiveSource(discord.AudioSource):
    """A computer audio input (the cable Spotify plays into), handed to the call 20 ms at a time. It keeps at most buffer_ms of sound,
    so a hiccup never builds up into a growing delay; if the input runs dry, the call gets silence."""

    def __init__(self, device_name, buffer_ms=300):
        self.device_name, self.max_bytes = device_name, int(48000 * 4 * buffer_ms / 1000) // FRAME_BYTES * FRAME_BYTES or FRAME_BYTES
        self.buf, self.lock, self.stream = bytearray(), threading.Lock(), None

    def start(self):
        try:
            import sounddevice as sd
        except ImportError:
            raise RuntimeError("Music mode needs the sounddevice package: run `python -m pip install sounddevice` and restart the bot.")
        wanted = self.device_name.lower()
        inputs = [(i, d) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
        # the same cable is listed once per Windows audio API: take the 48 kHz stereo one, which needs no conversion
        match = min((m for m in inputs if wanted in m[1]["name"].lower()),
                    key=lambda m: (m[1].get("default_samplerate") != 48000, m[1]["max_input_channels"] != 2, m[0]), default=None)
        if match is None:
            raise RuntimeError(f"No audio input named \"{self.device_name}\". Is the virtual cable installed (and the PC restarted after)? "
                               f"Inputs I can see: {', '.join(sorted({d['name'] for _, d in inputs})) or 'none'}.")
        self.stream = sd.InputStream(device=match[0], samplerate=48000, channels=2, dtype="int16", blocksize=960, callback=self._capture)
        self.stream.start()

    def _capture(self, indata, frames, time_info, status):
        with self.lock:
            self.buf += indata.tobytes()
            if len(self.buf) > self.max_bytes:
                del self.buf[:len(self.buf) - self.max_bytes]

    def is_opus(self):
        return False

    def read(self):
        with self.lock:
            if len(self.buf) >= FRAME_BYTES:
                data = bytes(self.buf[:FRAME_BYTES])
                del self.buf[:FRAME_BYTES]
                return data
        return bytes(FRAME_BYTES)

    def stop(self):
        stream, self.stream = self.stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def cleanup(self):
        self.stop()


# ------------------------------------------------------------------ understanding what was said
GREETINGS = {"hey", "hay", "hi", "hello", "yo", "ok", "okay", "oi", "ay", "um", "uh", "so"}


def split_wake(text, names, need_greeting=False):
    """(heard her name, what comes after it). Her name opening the line (after an optional "hey") always counts. Anywhere later in the
    line it counts only if what follows is a command, so earlier chatter doesn't hide "... hey sera, pause", and a passing mention of
    someone called Sarah doesn't start anything. The spellings in `names` cover what the transcriber tends to make of "Sera"; take one
    out of music_config.json if someone in the call really has it as a name."""
    words = list(re.finditer(r"[\w']+", text.lower()))
    start = 0
    while start < len(words) and words[start].group() in GREETINGS:
        start += 1
    for i, w in enumerate(words):
        if w.group() not in names:
            continue
        greeted = i > 0 and words[i - 1].group() in GREETINGS
        if need_greeting and not greeted:
            continue
        rest = text[w.end():].lstrip(" \t,.!?:;-")
        if i == start:
            return True, rest
        bare = rest.strip(" ,.!?").lower()
        if (greeted and not bare) or bare in OPEN_VERBS or parse_command(rest)[0] != "none":
            return True, rest  # shortcut: the first such mention wins; two commands in one line are not told apart
    return False, text


OPEN_VERBS = {"play", "queue", "add", "put on", "play me", "play some", "play the song", "queue up"}  # a request that stops here is waiting for its title
_POLITE = re.compile(r"^(?:(?:please|can you|could you|would you|will you|can we|could we|go ahead and|just|now|and|then|um|uh)\s+)+")
_THANKS = re.compile(r"\s+(?:please|for me|thanks|thank you)$")
_VAGUE = {"music", "some music", "something", "anything", "the music", "it", "again", "some tunes", "tunes", "songs", "some songs"}


def _kind_and_query(q):
    """'album dark side' -> ('album', 'dark side'); anything else is a song."""
    m = re.match(r"my (?:own )?playlist\s+(.+)", q) or re.match(r"my (?:own )?(.+?)\s+playlist$", q)
    if m:
        return "mine", m.group(1)  # one of the person's own playlists
    m = re.match(r"(?:the |some )?(album|playlist|artist|band)\s+(.+)", q)
    if m:
        return ("artist" if m.group(1) == "band" else m.group(1)), m.group(2)
    m = re.match(r"(.+?)\s+(album|playlist)$", q)
    if m:
        return m.group(2), m.group(1)
    return "track", re.sub(r"^(?:the )?(?:song|track|tune)\s+", "", q)


def parse_command(text):
    """(action, argument): play (kind, query), queue (kind, query), pause, resume, next, previous, volume (+/-/number), now, voice, leave, none."""
    t = re.sub(r"[.,!?\"“”]", " ", text.lower())
    t = re.sub(r"\s+", " ", t).strip()
    t = _THANKS.sub("", _POLITE.sub("", t))
    if not t:
        return "none", None
    m = re.match(r"(?:queue(?: up)?|add)\s+(.+?)(?:\s+(?:to|in|on) (?:the )?(?:queue|line))?$", t)
    if m:
        return "queue", _kind_and_query(m.group(1))
    m = re.match(r"(?:play|put on|throw on|start playing|i (?:want|wanna) (?:to )?hear|let's hear|lets hear|(?:can|could) (?:we|i) (?:hear|get))\s+(.+)$", t)
    if m:
        q = m.group(1).strip()
        return ("resume", None) if q in _VAGUE else ("play", _kind_and_query(q))
    if re.search(r"\b(?:switch|swap|change|go|come|back)\b.*\bvoice\b|^voice mode$|\bnormal mode\b", t):
        return "voice", None
    if re.match(r"(?:leave|disconnect|goodbye|bye|get out|go away|hang up|exit|quit)\b", t) or re.search(r"\bleave(?: the)?(?: call| vc| voice| channel| chat)?$", t):
        return "leave", None
    m = re.search(r"\bvolume\b.*?\b(?:to|at)\s+(\d{1,3})\b|\bvolume\s+(\d{1,3})\b|^(\d{1,3})\s*(?:percent|%) volume$", t)
    if m:
        return "volume", next(g for g in m.groups() if g)
    if re.search(r"\b(?:raise|increase)(?: the)? volume\b|\bvolume up\b", t):
        return "volume", "+"
    if re.search(r"\b(?:lower|decrease|reduce)(?: the)? volume\b|\bvolume down\b", t):
        return "volume", "-"
    if re.match(r"(?:pause|stop|hold on|shut up|quiet|silence|mute)\b", t):
        return "pause", None
    if re.match(r"(?:resume|continue|unpause|play|keep (?:playing|going)|start(?: the)? music)$", t):
        return "resume", None
    if re.match(r"(?:skip|next)\b", t):
        return "next", None
    if re.match(r"(?:previous|last (?:song|track)|go back|back)\b", t):
        return "previous", None
    if re.search(r"\bwhat(?:'s|s| is)? (?:this|playing|the song|that song|on)\b|\bwhat song\b|\bnow playing\b|\bwho (?:sings|is this|plays)\b", t):
        return "now", None
    return "none", t


# ------------------------------------------------------------------ the controller
class Controller:
    """Lives inside a VoiceSession in music mode: reads the lines the transcriber produced and acts on the ones meant for her.
    leave / to_voice are coroutine functions the call code gives it (it never imports that code)."""

    def __init__(self, session, env, log, leave, to_voice):
        self.s, self.env, self.log, self.leave, self.to_voice = session, env, log, leave, to_voice
        self.sent_at = None  # when the Spotify command for the line being handled went out (the timing log stops there, not after the follow-up check)
        self.armed = {}  # user id -> (until when, the start of the request so far): a lone "hey sera", or "hey sera, play" waiting for its end
        self.source = None

    def start(self):
        c = cfg()
        self.source = LiveSource(c["capture_device"], c["buffer_ms"])
        self.source.start()
        self.s.vc.play(self.source)
        self._route(True)

    def stop(self):
        if self.source:
            self.source.stop()
        self._route(False)  # Spotify goes back to the normal output when music mode ends, however it ends

    def _route(self, to_music):
        problem = route_audio(to_music)
        if problem:
            self.log(f"🎵 couldn't switch Spotify's output: {problem}")
            try:
                asyncio.get_running_loop().create_task(self.s._say_in_text(f"⚠️ Couldn't switch Spotify's output {'to the cable' if to_music else 'back'}: {problem}."))
            except RuntimeError:
                pass  # no event loop (a plain script): the log line is all there is

    def silence_seconds(self):
        return float(cfg()["silence_seconds"])

    def allowed(self, user):
        return cfg()["who_can_control"] == "anyone" or bool(self.env.is_owner(user.id))

    def names(self):
        mine = {str(t).lower() for t in getattr(self.env, "triggers", []) if re.fullmatch(r"[\w']+", str(t))}
        return mine | {w.lower() for w in cfg()["wake_words"]}

    async def run(self):
        """Takes what the transcriber read, line by line."""
        while True:
            batch, texts, stt_secs, _ = await self.s.heard.get()
            for (user, _pcm, when), text in zip(batch, texts):
                try:
                    await self.handle_text(user, text, when, stt_secs)
                except Exception:
                    self.log("music mode error:\n" + traceback.format_exc())

    async def handle_text(self, user, text, when=None, stt_secs=None):
        """when: the clock time the line was closed (for the timing log); stt_secs: how long reading it took."""
        c = cfg()
        text = (text or "").strip()
        if not text:
            return
        if c["log_heard"]:  # debugging: every line the transcriber produced, whoever it was for
            self.log(f"🎵 heard {user.display_name}: {text!r} (read in {stt_secs or 0:.1f}s)")
        woke, rest = split_wake(text, self.names(), c["need_greeting"])
        until, lead = self.armed.get(user.id, (0, ""))
        armed = until > time.monotonic()
        full = self.allowed(user)  # everything; others may only queue songs, if who_can_queue lets them
        if not (woke or armed) or not (full or c["who_can_queue"] == "anyone"):
            return  # nothing is logged for lines that weren't for her
        self.armed.pop(user.id, None)
        said = rest if woke else f"{lead} {text}".strip()  # lead: the start of a request whose end she was waiting for
        bare = said.strip(" ,.!?").lower()
        if not bare or bare in OPEN_VERBS:  # a lone "hey sera", or "hey sera, play" that was cut off before the title
            self.armed[user.id] = (time.monotonic() + c["follow_seconds"], bare)
            self.log(f"🎵 {user.display_name}: {text!r} -> waiting for the rest")
            return
        action, arg = parse_command(said)
        if not full and action != "queue":
            self.log(f"🎵 {user.display_name}: {text!r} -> {action}, ignored (they may only queue songs)")
            return
        self.log(f"🎵 {user.display_name}: {text!r} -> {action}")
        self.sent_at = None
        await self.do(action, arg, c)
        if when is not None:  # `when` is the moment the line was closed, i.e. after the quiet wait
            self.log(f"🎵 {action} sent {(self.sent_at or time.monotonic()) - when:.1f}s after the line closed (reading it {stt_secs or 0:.1f}s, quiet wait {c['silence_seconds']}s before that)")

    async def do(self, action, arg, c):
        say = self.s._say_in_text
        try:
            if action == "leave":
                asyncio.create_task(self.leave())  # a separate task: leaving cancels this one
            elif action == "voice":
                problem = await self.to_voice()
                if problem:
                    await say(problem)
            elif action in ("play", "queue"):
                kind, query = arg
                if action == "queue" and kind != "track":
                    return await say("🎵 I can only queue single songs; use play for an album, playlist or artist.")
                if kind == "mine":
                    mine = await asyncio.to_thread(sp.my_playlist, query)
                    found = [mine] if mine else []
                else:
                    found = await asyncio.to_thread(sp.search, query, kind)
                if not found:
                    return await say(f"🎵 No playlist like \"{query}\" in your library." if kind == "mine" else f"🎵 Nothing on Spotify for \"{query}\".")
                device = await self._device(c)
                if device is None:
                    return
                top = found[0]
                # a song plays inside its album: on this PC's Spotify app a bare track address was accepted but never loaded
                first = {**top, "via_album": True} if action == "play" else top
                await asyncio.to_thread(sp.play if action == "play" else sp.queue, first, device)
                self.sent_at = time.monotonic()
                await say(f"🎵 {'Playing' if action == 'play' else 'Queued'}: **{top['name']}**" + (f" — {top['by']}" if top["by"] else ""))
                if action == "play":
                    await asyncio.sleep(CHECK_DELAY)  # Spotify can accept a request and then not play it: ask it what it is doing
                    now = await asyncio.to_thread(sp.state)
                    inactive = {**device, "is_active": False}  # makes the next try move playback to the app first, even if Spotify calls it active
                    for item, dev in ((first, inactive), (top, inactive)):  # then: the same with a forced move to the app, then the bare song
                        if now["playing"]:
                            break
                        await asyncio.to_thread(sp.play, item, dev)
                        await asyncio.sleep(CHECK_DELAY)
                        now = await asyncio.to_thread(sp.state)
                    if not now["playing"]:
                        await say(f"⚠️ Spotify accepted that but isn't playing. It says: device {now['device']}, track {now['item']}, blocked actions {now['blocked']}.")
            elif action == "none":
                pass  # not a music request: the terminal log has the line, the chat stays quiet
            elif action == "now":
                now = await asyncio.to_thread(sp.now_playing)
                await say(f"🎵 {now['name']} — {now['by']}" + ("" if now["playing"] else " (paused)") if now else "🎵 Nothing is playing.")
            else:  # pause / resume / next / previous / volume: no device lookup, so they go out at once (Spotify uses the one that is playing)
                if action == "pause":
                    await asyncio.to_thread(sp.pause)
                elif action == "resume":
                    await asyncio.to_thread(sp.play)
                elif action == "next":
                    await asyncio.to_thread(sp.next_track)
                elif action == "previous":
                    await asyncio.to_thread(sp.previous_track)
                elif action == "volume":
                    if arg.isdigit():
                        level = int(arg)
                    else:  # relative: one look at the current volume first
                        current = (await asyncio.to_thread(sp.state))["volume"]
                        level = (50 if current is None else current) + (c["volume_step"] if arg == "+" else -c["volume_step"])
                    await asyncio.to_thread(sp.set_volume, max(0, min(100, level)))
        except sp.SpotifyError as e:
            await say(f"🎵 {e}")

    async def _device(self, c):
        device = await asyncio.to_thread(sp.pick_device, c["spotify_device"])
        if device is None:
            await self.s._say_in_text("🎵 I can't see the Spotify app: open it on this PC (logged in), then ask again.")
        return device
