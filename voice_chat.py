"""Sera in a Discord voice call: she listens to each person, answers out loud, and the voice can be swapped from a config file.

Flow: a person speaks -> their audio is collected until they pause -> a speech-to-text worker turns it into text -> if it is
for her (her name, or a short follow-up window) her model answers, streamed sentence by sentence -> a text-to-speech worker
voices each sentence while the next is still being written -> the audio plays into the call.

The bot hands this module what it needs through init(env), so it has no import of discord_bot. The heavy models run in
separate worker processes (voice_worker.py) in their own Python environments; voice_config.json says which.
"""
import asyncio
import sera_home
import vram_guard
import collections
import json
import logging
import os
import queue
import random
import re
import statistics
import subprocess
import tempfile
import threading
import time
import traceback
import wave

import discord
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "voice_config.json")
WORKER_SCRIPT = os.path.join(HERE, "voice_worker.py")
FRAME_BYTES = 3840  # 20 ms of 48 kHz, 16-bit, stereo: what Discord plays
INSTALL_HELP = ("Voice needs three Python packages that aren't installed yet. Run:\n"
                "`python -m pip install PyNaCl davey`\n"
                "`python -m pip install git+https://github.com/imayhaveborkedit/discord-ext-voice-recv`\n"
                "then restart the bot.")

# Added to her prompt in a voice call. The private version is ~/.sera/voice_rules.txt; this default is used without it.
# (The note about her models and their safeguards is above SYSTEM_PROMPT in discord_bot.py.)
VOICE_RULES = sera_home.text("voice_rules.txt", default='''

        === VOICE CALL ===
        - You are in a voice call right now, speaking out loud. Answer in one to three short spoken
          sentences, under 35 words in all, in plain words with full stops. Do not ramble, do not narrate
          what you are doing, and no markdown, lists, emojis, code or links.
        - Say numbers and symbols the way you would say them. Never write your own name or anyone's
          name in square brackets.
''')

VOICE_KEEP_ALIVE = "12h"  # while she is in a call her model stays loaded, so a quiet minute never costs a reload
CALL_ENGINES_LOADED = "3"  # the speech worker keeps up to this many engines in memory at once

env = None  # set by init()


_print_lock = threading.Lock()
_stream_open = False  # a streamed line of her words is on screen and not yet finished


def log(msg):
    global _stream_open
    with _print_lock:
        if _stream_open:  # don't write in the middle of her sentence
            print(flush=True)
            _stream_open = False
        print(f"🎙️ {msg}", flush=True)


def stream_text(piece, first_header, more_header="💭 ... "):
    """Show her words as they are written; if another status line cut in, the sentence carries on a new line."""
    global _stream_open
    with _print_lock:
        if not _stream_open:
            print(first_header if first_header else more_header, end="")
        print(piece, end="", flush=True)
        _stream_open = True


def end_stream():
    global _stream_open
    with _print_lock:
        if _stream_open:
            print(flush=True)
            _stream_open = False


# ------------------------------------------------------------------ config
_cfg_cache = {"mtime": None, "data": None}
LOCAL_PATH = sera_home.path("voice_local.json")  # which bots she listens to (`!vc hear`): other people's names, so not kept in the repo


def _mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:
        return None


def load_config():
    """voice_config.json, re-read whenever the file changes, so swapping a model needs no restart."""
    mtime = (os.path.getmtime(CONFIG_PATH), _mtime(LOCAL_PATH))
    if _cfg_cache["mtime"] != mtime:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        try:
            with open(LOCAL_PATH, encoding="utf-8") as f:
                data.setdefault("listen", {}).update(json.load(f).get("listen", {}))
        except (OSError, ValueError):
            pass  # no private list yet: the config's own (empty) one stands
        _cfg_cache.update(mtime=mtime, data=data)
    return _cfg_cache["data"]


def _write_json(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)


def save_config(cfg):
    public = json.loads(json.dumps(cfg))
    private = {"listen": {"hear_bots": public.get("listen", {}).get("hear_bots", [])}}
    public.setdefault("listen", {})["hear_bots"] = []  # the names stay out of the repo's file
    _write_json(LOCAL_PATH, private)
    _write_json(CONFIG_PATH, public)
    _cfg_cache["mtime"] = None


def engine_entry(cfg, name):
    if name not in cfg["engines"]:
        raise ValueError(f"engine {name!r} is not in voice_config.json (known: {', '.join(cfg['engines'])})")
    entry = cfg["engines"][name]
    python = entry["python"] if os.path.isabs(entry["python"]) else os.path.join(HERE, entry["python"])
    return python, entry.get("options", {}), entry.get("engine", name)  # "engine": which class in voice_worker.py runs this entry


# ------------------------------------------------------------------ workers
class Worker:
    """One speech-worker process: JSON lines in, JSON lines out, one request at a time."""

    def __init__(self, role, python):
        self.role, self.python = role, python
        self.proc, self.lines, self.counter = None, queue.Queue(), 0
        self.lock = threading.Lock()

    def _start(self):
        os.makedirs(env.log_dir, exist_ok=True)
        err = open(os.path.join(env.log_dir, f"voice-worker-{self.role}.log"), "ab")
        self.lines = queue.Queue()
        self.proc = subprocess.Popen([self.python, "-u", WORKER_SCRIPT, self.role], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=err, text=True, encoding="utf-8", errors="replace", bufsize=1,
                                     env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "SERA_VOICE_MAX_LOADED": CALL_ENGINES_LOADED})  # Windows pipes default to cp1252
        threading.Thread(target=self._pump, args=(self.proc, self.lines), daemon=True).start()
        first = self.lines.get(timeout=120)
        if first is None or not first.get("ready"):
            raise RuntimeError(f"the {self.role} worker did not start (see {env.log_dir})")

    @staticmethod
    def _pump(proc, out):
        try:
            for line in proc.stdout:
                try:
                    out.put(json.loads(line))
                except ValueError:
                    pass  # stray text from a library: not ours
        except Exception:
            log("the worker's output could not be read:\n" + traceback.format_exc())
        out.put(None)  # whatever happened, a waiting call is told instead of sitting out its timeout

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        self.proc = None

    def call(self, req, timeout=240):
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()
            self.counter += 1
            req = {**req, "id": self.counter}
            self.proc.stdin.write(json.dumps(req) + "\n")  # plain ASCII on the wire: accents travel as escapes
            self.proc.stdin.flush()
            deadline = time.time() + timeout
            while True:
                try:
                    reply = self.lines.get(timeout=max(0.1, deadline - time.time()))
                except queue.Empty:
                    self.stop()  # a stuck worker is killed; the next call starts a fresh one
                    raise TimeoutError(f"the {self.role} worker took longer than {timeout}s")
                if reply is None:
                    self.stop()
                    raise RuntimeError(f"the {self.role} worker died (see {env.log_dir})")
                if reply.get("id") == self.counter:
                    if not reply.get("ok"):
                        raise RuntimeError(reply.get("error", "worker error"))
                    return reply


_workers = {}


def get_worker(role, python):
    key = (role, python)
    if key not in _workers:
        _workers[key] = Worker(role, python)
    return _workers[key]


def stop_workers():
    for w in _workers.values():
        w.stop()
    _workers.clear()


async def worker_call(role, engine, **req):
    python, options, kind = engine_entry(load_config(), engine)
    worker = get_worker(role, python)
    return await asyncio.to_thread(worker.call, {"engine": kind, "options": options, **req})


# ------------------------------------------------------------------ audio and text helpers
def rms_of(pcm):
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(a * a))) if len(a) else 0.0


def pcm_to_16k(pcm):
    """48 kHz stereo 16-bit PCM from Discord -> 16 kHz mono samples (on the 16-bit scale)."""
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    a = a[: len(a) // 6 * 6].reshape(-1, 2).mean(axis=1)  # stereo -> mono
    return a.reshape(-1, 3).mean(axis=1)                   # 48k -> 16k; averaging three samples is a crude low-pass


def write_wav16(path, a):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.clip(a, -32768, 32767).astype(np.int16).tobytes())


def pcm_to_wav16(pcm, path):
    """48 kHz stereo 16-bit PCM from Discord -> a 16 kHz mono wav for speech recognition."""
    write_wav16(path, pcm_to_16k(pcm))


def read_pcm(path):
    with wave.open(path) as w:
        return w.readframes(w.getnframes())


_ES = set("el la los las de del que y en un una es por con para no se su al lo como más pero sus le ya o este sí porque esta entre "
          "cuando muy sin sobre también me hasta hay donde quien desde todo nos todos uno les ni otros ese eso ante ellos e esto "
          "antes algunos qué unos yo otro otra tú te ti tu tus mi mis estoy está eres soy vamos pues bueno oye".split())
_EN = set("the and is you to of it that in for on with this are was i my me not be have do so what your just like but".split())


def detect_language(text):
    """'es' or 'en', from stopwords: enough to pick which voice engine speaks a line."""
    words = re.findall(r"[a-záéíóúñü]+", text.lower())
    es, en = sum(w in _ES for w in words), sum(w in _EN for w in words)
    if "¿" in text or "¡" in text or (es > en and es >= 2):
        return "es"
    return "en"


_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF‍️]")


def clean_for_speech(text):
    """Strip what is meant for eyes: markdown, links, code, emoji, name tags."""
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"^\s*\[[^\]\n]{1,40}\]:\s*", "", t)
    t = re.sub(r"[*_~>#|]+", " ", t)
    t = _EMOJI.sub("", t)
    return re.sub(r"\s+", " ", t).strip()


def split_sentences(buf, final=False):
    """(complete sentences, the unfinished rest). Decimals like 6.2 stay whole: a split needs a space after the stop."""
    parts = re.split(r"(?<=[.!?…])\s+|\n+", buf)
    if final:
        return [p for p in parts if p.strip()], ""
    return [p for p in parts[:-1] if p.strip()], parts[-1]


def trim_silence(pcm, lead_ms=20, tail_ms=40, floor_db=-35):
    """48 kHz stereo 16-bit speech with its silent edges cut down. Chatterbox leaves 40-100 ms of silence before a clip and
    250-470 ms after it, which is most of the half-second gap heard between two pieces of one reply. The edges get a short fade
    so a cut never clicks."""
    a = np.frombuffer(pcm, dtype=np.int16).reshape(-1, 2).astype(np.float32)
    if len(a) < 960:
        return pcm
    level = np.abs(a).max(axis=1)
    smooth = np.convolve(level, np.ones(480) / 480, mode="same")  # 10 ms
    loud = np.nonzero(smooth > smooth.max() * 10 ** (floor_db / 20))[0]
    if not len(loud):
        return pcm
    start, end = max(0, loud[0] - lead_ms * 48), min(len(a), loud[-1] + tail_ms * 48)
    out = a[start:end].copy()
    fade = min(len(out) // 2, 384)  # 8 ms
    out[:fade] *= np.linspace(0, 1, fade)[:, None]
    out[-fade:] *= np.linspace(1, 0, fade)[:, None]
    return out.astype(np.int16).tobytes()


_BOUNDARY = re.compile(r"[.!?\u2026]+(?=\s)|\n+|[,;:\u2014\u2013](?=\s)")


TYPE_TOOL = {"type": "function", "function": {
    "name": "type_in_chat",
    "description": "Type a message in the text chat of this call, instead of saying it out loud. Use it when asked to type or write something "
                   "in chat, or to send a command to another bot (for example '!play song name'). Put exactly the text to type, nothing else.",
    "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}}
MAX_TYPED, MAX_TYPED_CHARS = 3, 300

SECOND_PERSON = re.compile(r"\b(you|your|yours|you're|youre|you'll|you've|ya|y'all|yall)\b", re.I)
THIRD_PERSON = re.compile(r"\b(she|she's|shes|her|hers|he|he's|him|his|they|them|their)\b", re.I)

GREETING_NAME = re.compile(r"^\W*(?:(?:yo|hey|hi|hello|oi|ay|ayy|aye|ok|okay|yeah|so|um|uh)\W+)+([A-Za-z][\w'-]*)\b", re.I)
NOT_NAMES = {"i", "im", "i'm", "ill", "i'll", "you", "we", "the", "this", "that", "it", "what", "who", "why", "how", "can", "could", "do",
             "does", "did", "is", "are", "so", "um", "uh", "well", "guys", "everyone", "everybody", "man", "bro", "dude", "there", "yo", "hey", "hi",
             "anyone", "someone", "wait", "look", "listen", "no", "yes", "yeah", "nah", "oh", "ok", "okay", "let", "lets", "let's", "my", "your",
             "a", "an", "and", "but", "or", "if", "when", "where", "good", "nice", "great", "thanks", "thank", "sorry", "please", "just", "not"}


def split_chunks(buf, min_chars=12, final=False):
    """Cut her streaming text into pieces the voice can start on, only where the punctuation gives a natural pause: at the end of
    every sentence, and at a comma, semicolon, colon or dash once the piece is at least min_chars long (so a lone "Okay," joins
    the phrase after it). Never in the middle of a phrase. Returns (pieces, the unfinished rest). A stop needs a space after it,
    so "6.2" stays whole."""
    pieces, start = [], 0
    for m in _BOUNDARY.finditer(buf):
        piece = buf[start:m.end()].strip()
        if not piece:
            start = m.end()
            continue
        if m.group()[0] in ".!?\u2026\n" or len(piece) >= min_chars:
            pieces.append(piece)
            start = m.end()
    rest = buf[start:]
    if final and rest.strip():
        pieces.append(rest.strip())
        rest = ""
    return pieces, rest.lstrip()


def make_cue(volume=0.10, period=1.6):
    """The 'thinking' sound: a soft rising pair of notes (D5, G5) and then silence, `period` seconds long in all, as 48 kHz 16-bit stereo like her voice.
    Quiet and not speech-like on purpose; each note fades in and out so it never clicks."""
    rate = 48000
    parts = []
    for freq, secs in ((587.33, 0.11), (783.99, 0.15)):
        n = int(rate * secs)
        t = np.arange(n) / rate
        fade = np.minimum(1.0, np.minimum(np.arange(n), n - 1 - np.arange(n)) / (rate * 0.02))
        parts.append(np.sin(2 * np.pi * freq * t) * fade)
    note = np.concatenate(parts) * min(0.5, max(0.0, float(volume)))
    total = max(len(note), int(rate * float(period)))
    total -= total % (FRAME_BYTES // 4)  # whole 20 ms frames
    mono = np.zeros(total)
    mono[:len(note)] = note
    stereo = np.repeat((mono * 32767).astype("<i2"), 2)
    return stereo.tobytes()


class QueueSource(discord.AudioSource):
    """Audio fed in pieces while it plays. Waits with silence between pieces; ends once closed and drained."""

    def __init__(self):
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.closed = False
        self.finished = threading.Event()

    def feed(self, pcm):
        with self.lock:
            self.buf += pcm

    def close(self):
        self.closed = True

    def clear(self):
        with self.lock:
            self.buf.clear()
        self.closed = True

    def drop_pending(self):
        """Throw away what hasn't played yet but keep the source open (the thinking sound, when her real voice is ready)."""
        with self.lock:
            self.buf.clear()

    def is_opus(self):
        return False

    def read(self):
        with self.lock:
            if len(self.buf) >= FRAME_BYTES:
                data = bytes(self.buf[:FRAME_BYTES])
                del self.buf[:FRAME_BYTES]
                return data
            if self.closed:
                if self.buf:
                    data = bytes(self.buf).ljust(FRAME_BYTES, b"\0")
                    self.buf.clear()
                    return data
                self.finished.set()
                return b""
        return b"\0" * FRAME_BYTES  # the next sentence is not ready yet


# ------------------------------------------------------------------ Discord's call encryption (DAVE)
# Since March 2026 every call is end-to-end encrypted. discord.py handles it for sending, but the receive extension only
# removes the transport encryption, so the audio it hands to the Opus decoder is still scrambled ("OpusError: corrupted
# stream", then its router thread dies and she hears nothing). These two patches finish the job with discord.py's own DAVE
# session, and keep one bad packet from killing the listener.
OPUS_SILENCE = bytes([0xF8, 0xFF, 0xFE])
dave_stats = {"ok": 0, "dropped": 0, "plain": 0, "opus_errors": 0, "last_log": 0.0}


def dave_decrypt(vc, ssrc, data):
    """Remove the end-to-end layer from one received audio frame; silence stands in for a frame that can't be opened."""
    if data == OPUS_SILENCE:
        return data
    connection = getattr(vc, "_connection", None)
    session = getattr(connection, "dave_session", None)
    if session is None or not getattr(connection, "dave_protocol_version", 0) or not getattr(connection, "can_encrypt", False):
        return data  # this call isn't end-to-end encrypted (yet)
    user_id = vc._get_id_from_ssrc(ssrc)
    try:
        if user_id is None:
            raise LookupError("no user known for this stream yet")
        import davey
        out = bytes(session.decrypt(user_id, davey.MediaType.audio, data))
        if dave_stats["ok"] == 0:
            log("end-to-end decryption of received audio is working")
        dave_stats["ok"] += 1
        return out
    except Exception as e:
        if "UnencryptedWhenPassthroughDisabled" in str(e):
            dave_stats["plain"] += 1  # the sender (a bot or an older app) doesn't encrypt: the frame is plain audio, not a damaged one
            if dave_stats["plain"] == 1:
                log("some audio in this call isn't end-to-end encrypted (a bot or an older app): playing it as it is")
            return data
        try:
            if user_id is not None and session.can_passthrough(user_id):
                return data  # a frame sent before encryption switched on
        except Exception:
            pass
        dave_stats["dropped"] += 1
        if time.time() - dave_stats["last_log"] > 5:
            dave_stats["last_log"] = time.time()
            log(f"couldn't decrypt a received audio frame ({type(e).__name__}: {e}); {dave_stats['dropped']} dropped so far")
        return OPUS_SILENCE


def install_dave_support(vc):
    from discord.ext.voice_recv import opus as vropus
    if not getattr(vropus.PacketDecoder, "_sera_safe", False):
        original_decode = vropus.PacketDecoder._decode_packet

        def safe_decode(self, packet):
            try:
                return original_decode(self, packet)
            except discord.opus.OpusError:
                dave_stats["opus_errors"] += 1
                return packet, bytes(FRAME_BYTES)  # a frame of silence, and the listener keeps running

        vropus.PacketDecoder._decode_packet = safe_decode
        vropus.PacketDecoder._sera_safe = True
    reader = getattr(vc, "_reader", None)
    if reader is not None and not getattr(reader.decryptor, "_sera_dave", False):
        transport_decrypt = reader.decryptor.decrypt_rtp
        reader.decryptor.decrypt_rtp = lambda packet: dave_decrypt(vc, packet.ssrc, transport_decrypt(packet))
        reader.decryptor._sera_dave = True


# ------------------------------------------------------------------ the call
class _NoTyping:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class VoiceSession:
    def __init__(self, vc, text_channel):
        self.vc, self.text_channel = vc, text_channel
        self.loop = asyncio.get_running_loop()
        self.hid = vc.channel.id            # her chat history for this call
        self.lock = threading.Lock()
        self.bufs = {}                      # user id -> what that person is saying right now
        self._typed, self._tools = [], None
        self.utterances = asyncio.Queue(maxsize=16)  # (who, their audio, when they finished): drained several at a time
        self.source = None
        self.cue_task, self.cue_on_source = None, False  # the "thinking" sound while she works on a reply
        self.speaking = False
        self.interrupted = False
        self.addressees = set()  # the people she is answering right now: they, and the owner, can talk over her
        self.heard = asyncio.Queue()  # lines already read by the transcriber, waiting for her to be free: (clips, texts, seconds, (start, end))
        self.handling = False         # she is deciding about or answering a batch right now
        self.stt_busy = False         # the transcriber has taken lines and not finished reading them
        self.pending_appraisals = []  # (person, what they said, when): feelings are worked out when nothing else is going on, not between replies
        self.tl = {}                  # when each stage of the reply being made happened
        self.timings = collections.deque(maxlen=50)
        self.told = set()       # bots she has already said she can't hear
        self.bot_streak = 0     # answers to bots in a row, with no person speaking in between
        self.tasks = []

    # --- receiving (called on the voice thread)
    def _may_hear(self, user, cfg):
        """People, always. Bots only if the config says so: music bots would otherwise be transcribed all day, and a bot that
        reads the chat aloud could set off a loop. The first time a bot is skipped, the text channel is told how to allow it."""
        if not getattr(user, "bot", False):
            return True
        allowed = cfg.get("hear_bots", [])
        name = (user.display_name or "").lower()
        if allowed == "all" or (isinstance(allowed, list) and any(str(a).strip() and (str(a) == str(user.id) or str(a).lower() in name) for a in allowed)):
            return True
        if user.id not in self.told:
            self.told.add(user.id)
            msg = f"👂 I can't hear **{user.display_name}**: bots are muted by default. `!vc hear {user.display_name}` lets me listen to it."
            asyncio.run_coroutine_threadsafe(self._say_in_text(msg), self.loop)
        return False

    def on_audio(self, user, pcm):
        if user is None or not pcm:
            return
        cfg = load_config()["listen"]
        if not self._may_hear(user, cfg):
            return
        now = time.monotonic()
        loud = rms_of(pcm) >= cfg["min_rms"]
        with self.lock:
            b = self.bufs.setdefault(user.id, {"user": user, "pcm": bytearray(), "first": now, "last": now, "loud": 0, "run": 0})
            b["pcm"] += pcm
            b["last"] = now
            b["loud"] += loud
            b["run"] = b["run"] + 1 if loud else 0
            run = b["run"]
        if (self.speaking and cfg.get("barge_in") and run >= cfg.get("barge_in_frames", 15) and not getattr(user, "bot", False)  # 300 ms of a person talking over her
                and self._may_interrupt(user, cfg)):
            self.loop.call_soon_threadsafe(self.interrupt)

    def _may_interrupt(self, user, cfg):
        """Side talk between other people doesn't stop her; the person she is answering, and the owner, can."""
        return (cfg.get("barge_in_from", "addressed") == "anyone" or not self.addressees or user.id in self.addressees
                or bool(env.is_owner(user.id)))

    def interrupt(self):
        if self.speaking and not self.interrupted:
            self.interrupted = True
            self._mark("interrupted")
            self._stop_cue()
            if self.source:
                self.source.clear()
            log("interrupted: someone talked over her")

    def _mark(self, name):
        """Note the first time a stage of this reply happens (safe from any thread)."""
        self.tl.setdefault(name, time.time())

    def _finish_timeline(self, names):
        t = self.tl
        gap = lambda a, b: round(t[b] - t[a], 2) if a in t and b in t else None
        row = {"who": names, "in_line": gap("finished", "stt_start"), "transcribing": gap("stt_start", "stt_end"), "held_for_her": gap("stt_end", "handled"),
               "deciding": gap("handled", "decided"), "notes": gap("decided", "notes_done"), "model_wait": gap("llm_requested", "llm_started"),
               "first_word": gap("llm_started", "first_token"), "first_sentence": gap("llm_started", "first_sentence"), "voice_synth": gap("tts_start", "tts_done"),
               "first_sound": gap("finished", "first_play"), "spoke_for": gap("first_play", "played"), "total": gap("finished", "played"),
               "cut_off_at": gap("first_play", "interrupted")}
        row["at"] = time.strftime("%H:%M:%S")
        self.timings.append(row)
        labels = (("in_line", "in line for the transcriber"), ("transcribing", "transcribing"), ("held_for_her", "held while she was busy"), ("deciding", "deciding"),
                  ("notes", "notes"), ("model_wait", "waiting for the model"), ("first_word", "model's first word"), ("voice_synth", "first voice synth"),
                  ("first_sound", "FIRST SOUND (after they stopped talking)"), ("spoke_for", "spoke for"), ("cut_off_at", "cut off at"))
        log(f"\u23f1 {names}: " + " | ".join(f"{label} {row[k]:.1f}s" for k, label in labels if row.get(k) is not None))
        try:
            os.makedirs(env.log_dir, exist_ok=True)
            with open(os.path.join(env.log_dir, "timing.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + chr(10))
        except OSError:
            pass

    def timing_text(self):
        """The last replies' stage times and the middle value of each stage, for !vc timing."""
        rows = list(self.timings)
        if not rows:
            return "No replies timed yet."
        keys = ("in_line", "transcribing", "held_for_her", "deciding", "notes", "model_wait", "first_word", "voice_synth", "first_sound", "spoke_for", "total")
        mid = {k: statistics.median(r[k] for r in rows if r.get(k) is not None) for k in keys if any(r.get(k) is not None for r in rows)}
        last = rows[-1]
        return (f"Last {len(rows)} replies, middle value of each stage (seconds):" + chr(10) + ", ".join(f"{k} {v:.1f}" for k, v in mid.items()) + chr(10)
                + f"Latest ({last['at']}, {last['who']}):" + chr(10) + ", ".join(f"{k} {last[k]:.1f}" for k in keys if last.get(k) is not None))

    async def _watch(self):
        """Close an utterance when its speaker has been quiet for a moment."""
        while True:
            await asyncio.sleep(0.1)
            cfg = load_config()["listen"]
            now = time.monotonic()
            done = []
            with self.lock:
                for uid, b in list(self.bufs.items()):
                    if now - b["last"] >= cfg["silence_seconds"] or now - b["first"] >= cfg["max_utterance_seconds"]:
                        done.append(self.bufs.pop(uid))
            for b in done:
                seconds = len(b["pcm"]) / (48000 * 4)
                if seconds < cfg["min_seconds"] or b["loud"] < 10:
                    continue  # a cough or a click
                try:
                    self.utterances.put_nowait((b["user"], bytes(b["pcm"]), time.monotonic()))
                except asyncio.QueueFull:
                    log(f"too many waiting, dropped {b['user'].display_name}'s line")
            if self.pending_appraisals and not (self.speaking or self.handling):
                quiet = not (self.bufs or not self.heard.empty() or not self.utterances.empty())
                if quiet or time.time() - self.pending_appraisals[0][2] > 45:  # nothing is going on, or it has waited long enough (a call that never goes quiet)
                    for uid, text, _ in self.pending_appraisals[-6:]:
                        env.schedule_appraisal(uid, text, self.hid, self.text_channel)
                    self.pending_appraisals = []

    async def _next_batch(self, cfg):
        """Everything that is waiting, up to what one pass of speech recognition can take. Clips that sat too long are dropped:
        she answers what is being said now, not what was said a minute ago."""
        b = cfg["listen"].get("batch", {})
        max_seconds, max_clips, wait = b.get("max_seconds", 25), b.get("max_clips", 8), b.get("wait_seconds", 0.3)
        max_age = b.get("max_age_seconds", 45)
        batch, total = [], 0.0
        while not batch:
            first = await self.utterances.get()
            if time.monotonic() - first[2] > max_age:
                log(f"dropped a line from {first[0].display_name}: it waited more than {max_age}s")
                continue
            batch, total = [first], len(first[1]) / 192000
        self.stt_busy = True
        await asyncio.sleep(wait)  # a moment, so people who finish together are taken together
        while len(batch) < max_clips and total < max_seconds:
            try:
                item = self.utterances.get_nowait()
            except asyncio.QueueEmpty:
                break
            if time.monotonic() - item[2] > max_age:
                log(f"dropped a line from {item[0].display_name}: it waited more than {max_age}s")
                continue
            batch.append(item)
            total += len(item[1]) / 192000
        return batch

    async def _stt_loop(self):
        """Reads what people said, one pass for everything waiting. It never waits for her: while she thinks or talks, the next lines are
        already being read (on the CPU, while her model and the voice use the GPU), so they are ready the moment she is free."""
        while True:
            batch = await self._next_batch(load_config())
            started = time.time()
            try:
                texts, secs = await self._transcribe_batch([b[1] for b in batch], load_config())
                self.heard.put_nowait((batch, texts, secs, (started, time.time())))
            except Exception:
                log("error reading what was said:" + chr(10) + traceback.format_exc())
            finally:
                self.stt_busy = False

    async def _process_loop(self):
        """Takes everything that has been read while she was busy, as one batch, and decides about it and answers it."""
        while True:
            batch, texts, secs, span = await self.heard.get()
            deadline = time.monotonic() + load_config()["listen"].get("batch", {}).get("merge_wait_seconds", 2.0)
            while (self.stt_busy or not self.utterances.empty()) and time.monotonic() < deadline:
                await asyncio.sleep(0.05)  # more is being read right now: answer it together with this, not in a second reply
            while not self.heard.empty():
                more_batch, more_texts, more_secs, more_span = self.heard.get_nowait()
                batch, texts, secs, span = batch + more_batch, texts + more_texts, max(secs, more_secs), (span[0], more_span[1])
            max_age = load_config()["listen"].get("batch", {}).get("max_age_seconds", 45)
            fresh = [i for i, item in enumerate(batch) if time.monotonic() - item[2] <= max_age]
            if len(fresh) < len(batch):
                log(f"dropped {len(batch) - len(fresh)} line(s) that waited more than {max_age}s")
                batch, texts = [batch[i] for i in fresh], [texts[i] for i in fresh]
                if not batch:
                    continue
            self.handling = True
            try:
                await self._handle_batch(batch, texts, secs, span)
            except Exception:
                log("error handling what was said:" + chr(10) + traceback.format_exc())
            finally:
                self.handling = False

    # --- understanding
    async def _transcribe(self, pcm, cfg):
        path = os.path.join(tempfile.gettempdir(), f"sera-vc-{time.time_ns()}.wav")
        pcm_to_wav16(pcm, path)
        try:
            hints = ", ".join(dict.fromkeys(env.known_names() + cfg["stt"].get("extra_hints", [])))
            r = await worker_call("stt", cfg["stt"]["engine"], op="transcribe", path=path, language=cfg["stt"].get("language", "auto"), hints=hints + ".")
            return r["text"], r.get("language"), r["seconds"]
        finally:
            if os.path.exists(path):
                os.remove(path)

    async def _transcribe_batch(self, pcms, cfg):
        """One text per clip. Several clips are joined with a second of silence between and read in one go; every word is given
        back to the person whose clip it was in. Whisper works in 30-second windows, so this costs about one pass, not one each."""
        if len(pcms) == 1:
            text, _, secs = await self._transcribe(pcms[0], cfg)
            return [text], secs
        gap = np.zeros(int(16000 * cfg["listen"].get("batch", {}).get("gap_seconds", 1.0)), dtype=np.float32)
        parts, bounds, pos = [], [], 0.0
        for pcm in pcms:
            a = pcm_to_16k(pcm)
            bounds.append([pos, pos + len(a) / 16000])
            pos += len(a) / 16000 + len(gap) / 16000
            parts += [a, gap]
        path = os.path.join(tempfile.gettempdir(), f"sera-vc-batch-{time.time_ns()}.wav")
        write_wav16(path, np.concatenate(parts))
        try:
            hints = ", ".join(dict.fromkeys(env.known_names() + cfg["stt"].get("extra_hints", [])))
            r = await worker_call("stt", cfg["stt"]["engine"], op="transcribe", path=path, bounds=bounds,
                                  language=cfg["stt"].get("language", "auto"), hints=hints + ".")
            return r["texts"], r["seconds"]
        finally:
            if os.path.exists(path):
                os.remove(path)

    def _calls_someone_else(self, text):
        """'Yo, Alex' / 'hey Mara, ...' / '..., Jo?': the line opens or ends by calling another person by name."""
        known = {n.lower() for n in env.known_names()} - {t.lower() for t in env.triggers}
        m = GREETING_NAME.match(text)
        if m and (m.group(1).lower() in known or (m.group(1)[0].isupper() and m.group(1).lower() not in NOT_NAMES)):
            return m.group(1)
        m = re.search(r",\s*([A-Za-z][\w'-]+)\W*$", text)
        return m.group(1) if m and m.group(1).lower() in known else None

    def _humans(self):
        return [m for m in getattr(self.vc.channel, "members", []) if not getattr(m, "bot", False)]

    def _busy(self, cfg):
        """A call with several people in it: lines are mostly people talking to each other, so she is stricter and keeps it short."""
        return len(self._humans()) >= cfg["listen"].get("busy_humans", 3)

    def _vocative(self, text):
        """Her name used to call her: it opens the line (not as 'Sera's ...'), follows a greeting, or sits next to a comma, question or end."""
        n = "|".join(re.escape(t) for t in env.triggers)
        return bool(re.search(rf"^\W*(?:(?:hey|yo|hi|hello|ok|okay|so|and|but|wait)\W+)*(?:{n})\b(?!'s)", text, re.I)
                    or re.search(rf"\b(?:{n})\b\s*[,!?]", text, re.I) or re.search(rf",\s*(?:{n})\W*$", text, re.I))

    async def _addressed(self, user, text, cfg):
        """Was this line meant for her? Calling someone else by name says no, even right after she spoke. Her name used to call her says yes.
        In a busy call, talk about her ('she's tapped', 'Sera's writing') says no, and a follow-up without her name has to be answered by her
        model saying it was to her; the window is shorter too. In a small call the old, easier rules apply."""
        listen = cfg["listen"]
        if listen["respond_to"] == "all":
            return True
        busy = self._busy(cfg)
        named = any(re.search(rf"\b{t}\b", text.lower()) for t in env.triggers)
        about_her = bool(THIRD_PERSON.search(text) and not SECOND_PERSON.search(text)) or bool(re.search(r"\b(?:sera|seraphim)'s\b", text, re.I))
        if named and (self._vocative(text) or not (busy and about_her)):
            return True
        if named:
            log(f"{user.display_name} was talking about her, not to her")
            return False
        other = self._calls_someone_else(text)
        if other:
            log(f"{user.display_name} is talking to {other}, not to her")
            return False
        s = env.active_sessions.get((self.hid, user.id))
        window = listen.get("session_seconds_busy", 12) if busy else listen["session_seconds"]
        if not (s and time.time() - s["last_activity"] < window):
            return False
        if not busy and (len(self._humans()) <= 1 or not listen.get("check_follow_ups", True)):
            return True  # nobody else there to be talking to
        if busy and about_her:
            return False
        last = next((m["content"] for m in reversed(self._history()) if m["role"] == "assistant"), "")
        prompt = ("Sera is in a voice call with several friends. She just said: '" + last[:200].replace("'", "") + "'. Someone then said: '"
                  + text[:200].replace("'", "") + "'. Was that said TO Sera (a reply to her, or a question for her)? Or is it for another person, "
                  "about Sera, or to themselves? Reply with 1 word: SERA or OTHER.")
        async with env.model_lock:
            verdict = await asyncio.to_thread(env.classify, prompt, ("SERA", "OTHER"))
        return verdict == "SERA" if busy else verdict != "OTHER"  # busy and unsure: stay quiet

    async def _say_in_text(self, text, mentions=False):
        try:
            await env.send_long(self.text_channel, text, **({"mentions": True} if mentions else {}))  # only asked for on lines she typed on purpose
        except Exception:
            pass

    async def _handle_batch(self, batch, texts=None, stt_secs=None, span=None):
        """texts/span: already read by the transcriber (the normal case). Without them the batch is read here."""
        cfg = load_config()
        t0 = time.time() - (time.monotonic() - min(b[2] for b in batch))  # when the first of them stopped talking
        handled = time.time()
        if texts is None:
            started = time.time()
            texts, stt_secs = await self._transcribe_batch([b[1] for b in batch], cfg)
            span = (started, time.time())
        self.tl = {"finished": t0, "stt_start": span[0], "stt_end": span[1], "handled": handled}
        talk, overheard = [], []  # who spoke to her, and what was said between other people
        for (user, pcm, _), text in zip(batch, texts):
            seconds = len(pcm) / (48000 * 4)
            if not text or (seconds < 2 and text.lower().strip(" .!?,") in {"thank you", "thanks", "you", "bye", "thanks for watching"}):
                continue  # silence is often "transcribed" as a stock phrase
            who = await asyncio.to_thread(env.nickname_of, user.id, user.display_name)
            if env.is_ignored(user.id):
                log(f"{who}: {text!r} (ignoring them, so no reply)")
                continue
            addressed = await self._addressed(user, text, cfg)
            log(f"{who}: {text!r} ({seconds:.1f}s heard, {stt_secs:.1f}s to transcribe{f' with {len(batch) - 1} others' if len(batch) > 1 else ''}, "
                f"{'for her' if addressed else 'not for her'})")
            if cfg["listen"].get("echo_to_text"):
                await self._say_in_text(f"🎤 **{who}:** {text}" + ("" if addressed else "  *(not for me)*"))
            is_bot = bool(getattr(user, "bot", False))
            if not is_bot:
                self.bot_streak = 0
            if not addressed:
                overheard.append((who, text))
                continue
            if is_bot:
                if self.bot_streak >= cfg["listen"].get("bot_reply_limit", 3):
                    log(f"not answering {who} again: it has talked to me {self.bot_streak} times with nobody else speaking")
                    continue
                self.bot_streak += 1
            if env.is_owner(user.id) and looks_like_voice_request(text) and await self._asked_to_leave(text):
                return
            if getattr(env, "handle_reminder_text", None) and env.looks_like_reminder(text):
                said = await env.handle_reminder_text(text, user.id, who, self.text_channel.id)
                if said:
                    await self._say_in_text(f"⏰ {said}")  # the exact time is also written down, in case the voice mangles it
                    await self._speak_aloud(said)
                    continue
            await asyncio.to_thread(env.note_person, user.id, user.display_name, is_bot)
            talk.append((user, who, text))
        self._mark("decided")
        if overheard and cfg["listen"].get("keep_overheard", True):
            self._overhear(overheard)
        if talk:
            await self._respond(talk, cfg, t0)
            self.pending_appraisals += [(user.id, text, time.time()) for user, _, text in talk]  # worked out later, when it won't delay the next reply
            del self.pending_appraisals[:-12]

    def _overhear(self, lines):
        """What other people said to each other goes into her history, marked as not meant for her, so she knows what is going on."""
        hist = self._history()
        for who, text in lines:
            hist.append({"role": "user", "content": f"[{who}]: (not to you) {text}"})
        if len(hist) > 21:
            hist[:] = [hist[0]] + hist[-20:]

    async def _speak_aloud(self, text):
        """Say a short, fixed reply in the call (a confirmation, not something from her model)."""
        try:
            for piece in split_chunks(clean_for_speech(text), final=True)[0]:
                pcm, _, _ = await self._speak_sentence(piece, load_config())
                self._play(pcm)
            self.source.close()
            await asyncio.to_thread(self.source.finished.wait, 30)
        except Exception:
            log("couldn't speak a reply:" + chr(10) + traceback.format_exc())

    async def _asked_to_leave(self, text):
        """The owner asked her, out loud, to leave: say goodbye, then go. True if that's what happened."""
        if await voice_intent(text) != "LEAVE":
            return False
        log("asked to leave the call")
        try:
            pcm, _, _ = await self._speak_sentence(random.choice(LEAVE_LINES), load_config())
            self._play(pcm)
            self.source.close()
            await asyncio.to_thread(self.source.finished.wait, 15)
        except Exception:
            pass  # no voice? she leaves anyway
        asyncio.create_task(disconnect_session(self.vc.guild.id))  # a separate task: leaving cancels this one
        return True

    # --- answering
    def _history(self):
        hist = env.chat_histories.get(self.hid)
        if hist is None:
            hist = env.chat_histories[self.hid] = [{"role": "system", "content": env.system_prompt + VOICE_RULES}]
        return hist

    def _stream_reply(self, messages, cfg, emit):
        """Runs in a thread: her model's answer, cut into sentences as it is written, and shown in the console as it comes."""
        self._header = f"\U0001F4AD {env.bot_name} is writing: "  # printed before her first words only
        try:
            return self._stream_reply_inner(messages, cfg, emit)
        finally:
            end_stream()

    def _stream_reply_inner(self, messages, cfg, emit):
        speak = cfg["speak"]
        min_chars = speak.get("min_chunk_chars", 12)  # a comma break shorter than this joins the next phrase; 0 = break at every punctuation mark
        max_chars = speak.get("max_chars", 320)
        self._max_sentences = min(speak["max_sentences"], speak.get("max_sentences_busy", 2)) if self._busy(load_config()) else speak["max_sentences"]
        buf, said, count, total, closing = "", [], 0, 0, False  # count = finished sentences; the voice may start on a clause before its sentence ends
        extra = {"tools": self._tools} if self._tools else {}
        for chunk in env.chat_stream(model=env.model_name, messages=messages, think=False, keep_alive=env.get_keep_alive(),
                                     options=env.ollama_options(temperature=speak["temperature"], num_predict=speak["max_tokens"]), **extra):
            if self.interrupted:
                break
            for call in getattr(chunk.message, "tool_calls", None) or []:
                if call.function.name == "type_in_chat" and len(self._typed) < MAX_TYPED:
                    text = " ".join(str((call.function.arguments or {}).get("text", "")).split())[:MAX_TYPED_CHARS]
                    if text:
                        self._typed.append(text)
                        log(f"\u2328\ufe0f typing in chat: {text!r}")
            piece = chunk.message.content or ""
            if piece:
                self._mark("first_token")
                stream_text(piece, self._header)
                self._header = None
            buf += piece
            pieces, buf = split_chunks(buf, min_chars=min_chars)
            for s in pieces:
                emit(s)
                said.append(s)
                total += len(s) + 1
                count += s[-1:] in ".!?\u2026"
                closing = closing or total >= max_chars  # long enough: finish this sentence, then stop
                if count >= self._max_sentences or (closing and s[-1:] in ".!?\u2026") or total >= max_chars * 1.5:
                    return " ".join(said)
        if not self.interrupted:
            for s in split_chunks(buf, min_chars=min_chars, final=True)[0]:
                emit(s)
                said.append(s)
        return " ".join(said)

    async def _speak_sentence(self, sentence, cfg):
        lang = detect_language(sentence)
        tts = cfg["tts"]
        engine = tts["engine"] if lang == "en" or not tts.get("non_english_engine") else tts["non_english_engine"]
        out = os.path.join(tempfile.gettempdir(), f"sera-tts-{time.time_ns()}.wav")
        try:
            r = await worker_call("tts", engine, op="speak", text=sentence, out=out, voice=tts.get("voice") or None,
                                  language=lang, params=tts.get("params") or {})
            pcm = read_pcm(out)
        finally:
            if os.path.exists(out):
                os.remove(out)
        speak = cfg.get("speak", {})
        pause = speak.get("sentence_pause_ms", 220) if sentence.rstrip()[-1:] in ".!?\u2026" else speak.get("clause_pause_ms", 40)
        return trim_silence(pcm, lead_ms=20, tail_ms=pause), r, engine

    def _play(self, pcm):
        self._stop_cue()  # her real voice is ready: the thinking sound ends, and what hasn't played of it is dropped so it can't delay her
        if self.source is None or self.source.closed:
            self.source = QueueSource()
            if self.vc.is_playing():
                self.vc.stop()
            self.vc.play(self.source)
        self.source.feed(pcm)

    def _start_cue(self, cfg):
        """While she works on a reply, play a quiet repeating sound once the wait is long enough to notice (speak.thinking_sound)."""
        self._stop_cue()
        sp = cfg.get("speak", {})
        if sp.get("thinking_sound", True):
            self.cue_task = asyncio.create_task(self._cue_loop(sp))

    def _stop_cue(self):
        task, self.cue_task = self.cue_task, None
        if task:
            task.cancel()
        if self.cue_on_source and self.source is not None and not self.source.closed:
            self.source.drop_pending()
        self.cue_on_source = False

    async def _cue_loop(self, sp):
        try:
            await asyncio.sleep(sp.get("thinking_sound_delay", 0.5))  # a reply that is ready sooner never plays it
            pcm = make_cue(sp.get("thinking_sound_volume", 0.10), sp.get("thinking_sound_period", 1.6))
            first = True
            while not self.interrupted:
                if self.source is None or self.source.closed:
                    self.source = QueueSource()
                    if self.vc.is_playing():
                        self.vc.stop()
                    self.vc.play(self.source)
                self.cue_on_source = True
                self.source.feed(pcm)
                if first:
                    first = False
                    self._mark("cue_started")
                    log("\U0001F514 thinking sound")
                await asyncio.sleep(len(pcm) / (48000 * 4))
        except asyncio.CancelledError:
            raise
        except Exception:
            log("couldn't play the thinking sound:" + chr(10) + traceback.format_exc())

    def _typing(self):
        typing = getattr(self.text_channel, "typing", None)
        return typing() if typing else _NoTyping()

    async def _respond(self, lines, cfg, t0):
        self.addressees = {u.id for u, _, _ in lines}
        try:
            async with self._typing():  # "Sera is typing..." shows in Discord while she works and talks
                await self._respond_inner(lines, cfg, t0)
        finally:
            self._stop_cue()
            self.speaking = False
            self.addressees = set()

    async def _respond_inner(self, lines, cfg, t0):
        """lines: (person, name, what they said) for everyone who spoke to her since she last answered: one reply covers them all."""
        hist = self._history()
        for _, who, text in lines:
            hist.append({"role": "user", "content": f"[{who}]: {text}"})
        if len(hist) > 21:
            hist[:] = [hist[0]] + hist[-20:]
        await asyncio.to_thread(env.refresh_notes, self.hid)
        self._mark("notes_done")
        for user, _, _ in lines:
            env.keep_session(self.hid, user.id)
        names = ", ".join(dict.fromkeys(who for _, who, _ in lines))
        note = ([{"role": "user", "content": f"[System note, not a speaker: {names} all spoke to you just now. Answer each of them in one "
                                             "short reply, one or two spoken sentences in all.]"}] if len(lines) > 1 else [])
        self.interrupted, self.speaking, self.source = False, True, None
        self._typed = []
        self._tools = [TYPE_TOOL] if any(env.is_owner(u.id) for u, _, _ in lines) else None  # only the owner can make her type
        sent_q = asyncio.Queue()
        log(f"\U0001F4AD {env.bot_name} is thinking about what {names} said...")
        self._start_cue(cfg)

        ahead = threading.Semaphore(cfg["speak"].get("lookahead_chunks", 2))  # how far the writing may get ahead of the voice

        def emit(sentence):
            self._mark("first_sentence")
            while not ahead.acquire(timeout=0.2):  # blocks while the voice is still busy with earlier pieces
                if self.interrupted:
                    return
            self.loop.call_soon_threadsafe(sent_q.put_nowait, sentence)

        async def think():
            try:
                self._mark("llm_requested")
                async with env.model_lock:
                    self._mark("llm_started")
                    return await asyncio.to_thread(self._stream_reply, list(hist) + note, cfg, emit)
            finally:
                sent_q.put_nowait(None)

        async def voice():
            spoken, first, failure = [], None, None
            while True:
                sentence = await sent_q.get()
                if sentence is None:
                    break
                ahead.release()  # the voice has taken this piece, so the writer may go on to the next
                clean = clean_for_speech(sentence)
                if self.interrupted or not clean:
                    continue
                log(f"\U0001F50A voicing: {clean!r}")
                try:
                    self._mark("tts_start")
                    pcm, r, engine = await self._speak_sentence(clean, cfg)
                    self._mark("tts_done")
                except Exception as e:
                    failure = f"{type(e).__name__}: {e}"
                    log(f"text-to-speech failed: {failure}")
                    spoken.append(sentence)  # she still said it, just not out loud
                    continue
                if self.interrupted:
                    continue
                if first is None:
                    first = time.time() - t0
                    self._mark("first_play")
                    log(f"first sound {first:.1f}s after they stopped talking ({engine}: {r['seconds']:.1f}s for {r['audio_seconds']:.1f}s of speech)")
                if self.source is None or self.source.closed:
                    log("\u25B6 speaking now")
                self._play(pcm)
                spoken.append(sentence)
            return spoken, failure

        try:
            _, (spoken, failure) = await asyncio.gather(think(), voice())
        finally:
            self._stop_cue()
            if self.source:
                self.source.close()
        reply = " ".join(spoken).strip()
        for text in self._typed:
            await self._say_in_text(text, mentions=True)  # what she typed on purpose in the chat (owner only): a name there can be a real ping
            hist.append({"role": "assistant", "content": f"[you typed in the chat: {text}]"})
        if self._typed and not reply and not self.interrupted:
            await self._speak_aloud("Typed it.")
        if reply:
            hist.append({"role": "assistant", "content": reply + (" [you were cut off here]" if self.interrupted else "")})
            if cfg["listen"].get("echo_to_text"):
                await self._say_in_text(f"🔊 **{env.bot_name}:** {reply}" + (f"\n*(voice failed: {failure})*" if failure else ""))
        for user, _, _ in lines:
            env.keep_session(self.hid, user.id)
        if self.source and not failure:
            await asyncio.to_thread(self.source.finished.wait, 90)  # one answer at a time: let it finish playing
        self._mark("played")
        log("\u2705 finished, your turn" if not self.interrupted else "\u2705 stopped (you talked over her), your turn")
        self._finish_timeline(names)

    # --- lifecycle
    def start(self, sink):
        self.vc.listen(sink)
        try:
            install_dave_support(self.vc)
        except Exception:
            log("couldn't add call-encryption support to the listener:" + chr(10) + traceback.format_exc())
        self.tasks = [asyncio.create_task(self._watch()), asyncio.create_task(self._stt_loop()), asyncio.create_task(self._process_loop())]

    async def stop(self):
        for t in self.tasks:
            t.cancel()
        try:
            self.vc.stop_listening()
        except Exception:
            pass
        if self.source:
            self.source.clear()
        await self.vc.disconnect(force=True)


sessions = {}  # guild id -> VoiceSession


async def announce(user_id, text):
    """A reminder came due for someone who is in the call she is in: say it out loud as well."""
    for session in list(sessions.values()):
        members = {m.id for m in getattr(session.vc.channel, "members", [])}
        if user_id in members and not session.speaking:
            await session._speak_aloud(text)
            return


def make_sink(session):
    from discord.ext import voice_recv

    class UtteranceSink(voice_recv.AudioSink):
        def wants_opus(self):
            return False

        def write(self, user, data):
            session.on_audio(user, getattr(data, "pcm", None))

        def cleanup(self):
            pass

    return UtteranceSink()


# ------------------------------------------------------------------ chat commands
HELP = ("`!vc join` (join your voice channel), `!vc leave`, `!vc status`, `!vc voice <engine> [voice]`, `!vc stt <model>`, "
        "`!vc always on|off`, `!vc echo on|off`, `!vc hear <bot>`, `!vc timing`")


async def warm_up(cfg, text_channel):
    """Load both models now, so the first thing she hears isn't a long wait. The steps that don't need each other run together: her own model
    reloads with fewer layers on the GPU (making room), the voice reads its files into memory (needs no GPU), and hearing loads on the CPU.
    Only moving the voice onto the GPU has to wait for her model to have made room."""
    started = time.time()
    done = {}
    tasks = []
    try:
        async def timed(name, coro):
            result = await coro
            done[name] = time.time() - started
            return result

        tts_name, stt_name = cfg["tts"]["engine"], cfg["stt"]["engine"]
        stt_on_gpu = engine_entry(cfg, stt_name)[1].get("device") == "cuda"  # a GPU Whisper would take the voice's room: it goes after, as before
        her_model = asyncio.create_task(timed("her model", asyncio.to_thread(env.warm_model)))
        staged = asyncio.create_task(timed("voice read into memory", worker_call("tts", tts_name, op="prewarm")))
        tasks = [her_model, staged]
        hearing = None
        if not stt_on_gpu:
            hearing = asyncio.create_task(timed("hearing", worker_call("stt", stt_name, op="load")))
            tasks.append(hearing)
        await her_model  # the voice may only go onto the GPU once her model has made room
        await staged
        tts = await timed("voice on the GPU", worker_call("tts", tts_name, op="load"))
        stt = await (hearing if hearing else timed("hearing", worker_call("stt", stt_name, op="load")))
        total = time.time() - started
        where = f"voice on {tts.get('device') or 'cpu'}, hearing on {stt.get('device') or 'cpu'}"
        log(where + f" | ready in {total:.0f}s: " + ", ".join(f"{k} {v:.0f}s" for k, v in sorted(done.items(), key=lambda kv: kv[1])) + " (seconds from the start)")
        await env.send_long(text_channel, f"🎙️ Voice is ready ({where}, {total:.0f}s).")
        other = cfg["tts"].get("non_english_engine")
        if other and other != cfg["tts"]["engine"]:
            await worker_call("tts", other, op="load")  # so the first Spanish line isn't a long wait
    except Exception as e:
        for t in tasks:  # whatever was still loading is left to finish or fail quietly, not reported twice
            t.add_done_callback(lambda f: f.cancelled() or f.exception())
        await env.send_long(text_channel, f"⚠️ Voice models failed to load: {e}")


async def disconnect_session(gid, keep_models=False):
    """Leave the call in this server. Unless she is only moving to another channel, free the speech models and give her
    model's normal settings back."""
    session = sessions.pop(gid, None)
    if session:
        await session.stop()
    if not keep_models and not sessions:
        stop_workers()
        env.set_keep_alive(None)  # back to the normal short timeout
        env.set_gpu_layers(None)  # and all of her layers back on the GPU
        await asyncio.to_thread(env.warm_model)
        flush = getattr(env, "flush_memories", None)
        if flush:
            asyncio.create_task(flush())  # what was said in the call goes into her memories now, not ten minutes later


async def join_call(message, reply):
    """Join the voice channel the person is in; if she is in another channel here, move."""
    cfg = load_config()
    gid = message.guild.id
    voice = getattr(message.author, "voice", None)
    if not voice or not voice.channel:
        return await reply("Join a voice channel first, then ask me again.")
    session = sessions.get(gid)
    if session and session.vc.channel.id == voice.channel.id:
        return await reply("I'm already in there with you.")
    # check the channel before touching anything, so a refused move doesn't leave her in no call at all
    channel, me = voice.channel, getattr(message.guild, "me", None)
    perms = channel.permissions_for(me) if me is not None and hasattr(channel, "permissions_for") else None
    if perms is not None and not perms.connect:
        return await reply(f"I can't get into **{channel.name}**: my role doesn't have **Connect** there.")
    limit = getattr(channel, "user_limit", 0)
    if perms is not None and limit and len(getattr(channel, "members", [])) >= limit and not perms.move_members:
        return await reply(f"**{channel.name}** is full.")
    no_speak = " I don't have **Speak** there, so I can listen but not talk." if perms is not None and not perms.speak else ""
    try:
        from discord.ext import voice_recv
        import nacl  # noqa: F401
    except ImportError:
        return await reply(INSTALL_HELP)
    moving = session is not None
    if not moving:  # moving between channels keeps the models she already has
        fits, why = await asyncio.to_thread(vram_guard.check, "voice")
        if not fits:
            return await reply(why)
    if moving:
        await disconnect_session(gid, keep_models=True)
    try:
        vc = await voice.channel.connect(cls=voice_recv.VoiceRecvClient)
    except Exception as e:
        return await reply(f"⚠️ Couldn't join: {type(e).__name__}: {e}")
    session = sessions[gid] = VoiceSession(vc, message.channel)
    env.set_keep_alive(VOICE_KEEP_ALIVE)  # her model stays loaded until she leaves
    env.set_gpu_layers(cfg.get("call", {}).get("sera_gpu_layers"))  # fewer of her layers on the GPU, so a GPU voice fits beside her
    session.start(make_sink(session))
    await reply(f"🎙️ {'Moved to' if moving else 'Joined'} **{voice.channel.name}**." + ("" if moving else " Loading the voice models…") + no_speak)
    if not moving:
        asyncio.create_task(warm_up(cfg, message.channel))


async def leave_call(message, reply):
    gid = message.guild.id
    if gid not in sessions:
        return await reply("I'm not in a call here.")
    await disconnect_session(gid)
    await reply("Left the call.")


# Asking her in plain words. The word lists only decide whether a model call is worth making; the model decides what was meant.
_JOIN_WORDS = re.compile(r"\b(join|hop in|hop on|jump in|jump on|come (?:to|in|on|over|here|join)|get in|get on|connect|enter|pull up)\b")
_LEAVE_WORDS = re.compile(r"\b(leave|get out|get off|disconnect|hang up|drop off|go away|bounce|exit|log off|stop listening|quit|peace out)\b")
_PLACE_WORDS = re.compile(r"\b(vc|voice|voice chat|voice channel|call|channel)\b")
JOIN_LINES = sera_home.lines("JOIN_LINES", [
    "On my way.",
    "Joining now.",
    "Coming.",
    "One second.",
])
LEAVE_LINES = sera_home.lines("LEAVE_LINES", [
    "Bye.",
    "Leaving now.",
    "See you.",
    "Out.",
])


def looks_like_voice_request(text):
    t = text.lower()
    return bool(_PLACE_WORDS.search(t) and (_JOIN_WORDS.search(t) or _LEAVE_WORDS.search(t)))


async def voice_intent(text):
    """'JOIN', 'LEAVE' or None: did this message ask her to join or leave a voice call?"""
    text = text.replace("'", "").replace(chr(10), " ")[:300]
    prompt = ("Is the user asking Sera to JOIN a voice call or voice channel, to LEAVE the voice call, or neither (NONE)? "
              "Questions about calls, things that happened earlier, or other people joining or leaving are NONE. "
              f"Message: '{text}'. Reply with 1 word: JOIN, LEAVE or NONE.")
    async with env.model_lock:
        intent = await asyncio.to_thread(env.classify, prompt, ("JOIN", "LEAVE", "NONE"))
    return intent if intent in ("JOIN", "LEAVE") else None


async def handle_natural_request(message):
    """True if the message asked her to join or leave a call and she dealt with it (did it, or told them off)."""
    intent = await voice_intent(message.content)
    if not intent:
        return False
    reply = lambda text: message.reply(text, mention_author=False)
    if not env.is_owner(message.author.id):
        await reply(env.not_owner_line())
        return True
    if intent == "JOIN":
        if getattr(getattr(message.author, "voice", None), "channel", None):
            await reply(random.choice(JOIN_LINES))
        await join_call(message, reply)
    else:
        if message.guild.id in sessions:
            await reply(random.choice(LEAVE_LINES))
        await leave_call(message, reply)
    return True


async def handle_command(message):
    reply = lambda text: message.reply(text, mention_author=False)
    if not env.is_owner(message.author.id):
        return await reply(env.not_owner_line())
    parts = message.content.split()
    cmd = parts[1].lower() if len(parts) > 1 else "status"
    cfg = load_config()
    gid = message.guild.id
    session = sessions.get(gid)
    if cmd == "join":
        await join_call(message, reply)
    elif cmd == "leave":
        await leave_call(message, reply)
    elif cmd == "timing":
        await reply(session.timing_text() if session else "I'm not in a call.")
    elif cmd == "voice":
        if len(parts) < 3:
            engines = [e for e in cfg["engines"] if e != cfg["stt"]["engine"]]
            return await reply(f"Voice engine: **{cfg['tts']['engine']}**, voice: {cfg['tts']['voice'] or 'default'}. Engines: {', '.join(engines)}.")
        engine = parts[2]
        if engine not in cfg["engines"]:
            return await reply(f"Unknown engine. Known: {', '.join(cfg['engines'])}")
        cfg["tts"]["engine"] = engine
        if len(parts) > 3:
            cfg["tts"]["voice"] = " ".join(parts[3:])
        save_config(cfg)
        await reply(f"Voice is now **{engine}**." + (" Loading it…" if session else ""))
        if session:
            try:
                await worker_call("tts", engine, op="load")
                await reply("Loaded.")
            except Exception as e:
                await reply(f"⚠️ Failed to load {engine}: {e}")
    elif cmd == "stt" and len(parts) > 2:
        cfg["engines"][cfg["stt"]["engine"]].setdefault("options", {})["model"] = parts[2]
        save_config(cfg)
        await reply(f"Speech recognition model is now **{parts[2]}**.")
    elif cmd in ("always", "echo") and len(parts) > 2:
        on = parts[2].lower() in ("on", "true", "yes")
        if cmd == "always":
            cfg["listen"]["respond_to"] = "all" if on else "name_or_session"
        else:
            cfg["listen"]["echo_to_text"] = on
        save_config(cfg)
        await reply(f"{cmd}: {'on' if on else 'off'}.")
    elif cmd == "hear":
        arg = " ".join(parts[2:]).strip()
        heard = cfg["listen"].get("hear_bots", [])
        if arg.lower() == "all":
            cfg["listen"]["hear_bots"] = "all"
        elif arg.lower() in ("none", "off"):
            cfg["listen"]["hear_bots"] = []
        elif arg:
            names = list(heard) if isinstance(heard, list) else []
            if arg not in names:
                names.append(arg)
            cfg["listen"]["hear_bots"] = names
        if arg:
            save_config(cfg)
            heard = cfg["listen"]["hear_bots"]
        await reply("I listen to people" + (" and to every bot." if heard == "all" else f" and to these bots: {', '.join(heard)}." if heard else ", not bots.")
                    + " `!vc hear <bot name or id>`, `!vc hear all`, `!vc hear none`.")
    elif cmd == "status":
        where = f"in **{session.vc.channel.name}**" if session else "not in a call"
        await reply(f"I'm {where}. Voice: **{cfg['tts']['engine']}** (Spanish: {cfg['tts'].get('non_english_engine') or 'same'}), "
                    f"hearing: **{cfg['engines'][cfg['stt']['engine']].get('options', {}).get('model', '?')}**, "
                    f"answers: {'everything' if cfg['listen']['respond_to'] == 'all' else 'when named, then a short follow-up window'}.\n{HELP}")
    else:
        await reply(HELP)


def init(environment):
    """The bot passes in what this module needs; see the keys used above."""
    global env
    env = environment
    # the receive extension logs every sender report and gateway sequence number at INFO: hundreds of lines a minute
    for name in ("discord.ext.voice_recv.reader", "discord.ext.voice_recv.gateway"):
        logging.getLogger(name).setLevel(logging.WARNING)
