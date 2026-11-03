"""Making pictures for Sera: runs ComfyUI (in image_gen/) headless with the Chroma model and hands back image files.

Her language model and the picture model don't fit in the 12 GB card together, so a picture is made like this: her model is unloaded,
ComfyUI starts (or is already running), draws, and is told to let go of the GPU again. Her next message loads her back (about 3 s
when her file is cached). ComfyUI itself is stopped after it has been idle for a while, which also frees its small fixed use of memory.

Everything is blocking code meant for a worker thread. The bot gives this module what it needs through init(env), so there is no
import of discord_bot.
"""
import json
import os
import random
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

import image_safety
import sera_home

HERE = os.path.dirname(os.path.abspath(__file__))
COMFY_DIR = os.path.join(HERE, "image_gen", "ComfyUI")
PYTHON = os.path.join(HERE, "image_gen", "venv", "Scripts", "python.exe")
CONFIG_PATH = os.path.join(HERE, "image_gen_config.json")
env = None
_lock = threading.Lock()  # one picture at a time
_proc = None
_last_used = 0.0

NEGATIVE = "low quality, ugly, unfinished, out of focus, deformed, disfigure, blurry, smudged, restricted palette, flat colors"


def init(environment):
    global env
    env = environment
    threading.Thread(target=_idle_watch, daemon=True).start()


def _raw():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def cfg():
    """The settings, plus the picture model's files from ~/.sera/models.json ("image"); a config that names its own models wins."""
    c = _raw()
    if not c.get("models"):
        c["models"] = sera_home.model("image") or {}
    return c


def discord_on():
    """Whether pictures may be made from Discord chats (the web page is always allowed). Off until the owner has checked it."""
    return bool(cfg().get("discord", {}).get("enabled", False))


def write_cfg(c):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f, indent=2)
    os.replace(tmp, CONFIG_PATH)  # cfg() reads the file each time, so this is seen at once


def set_discord(on):
    c = _raw()  # not cfg(): the model names from models.json must never be written into this file
    c.setdefault("discord", {})["enabled"] = bool(on)
    write_cfg(c)


def available():
    c = cfg()
    m = c.get("models") or {}
    if not all(m.get(k) for k in ("unet", "clip", "vae")):
        return False  # no picture model chosen in models.json
    have = lambda sub, name: os.path.exists(os.path.join(COMFY_DIR, "models", sub, name))
    return bool(c.get("enabled", True) and os.path.exists(PYTHON) and (have("unet", m["unet"]) or have("diffusion_models", m["unet"]))
                and have("clip", m["clip"]) and have("vae", m["vae"]))


# ------------------------------------------------------------------ ComfyUI as a helper process
def _url(path):
    return f"http://127.0.0.1:{cfg()['port']}{path}"


def _get(path, timeout=10):
    with urllib.request.urlopen(_url(path), timeout=timeout) as r:
        return json.loads(r.read())


def _post(path, body, timeout=30):
    req = urllib.request.Request(_url(path), json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def _up():
    try:
        _get("/system_stats", timeout=2)
        return True
    except (OSError, ValueError):
        return False


def _start():
    global _proc
    if _up():
        return
    c = cfg()
    log = open(os.path.join(HERE, "image_gen", "comfyui.log"), "ab")
    _proc = subprocess.Popen([PYTHON, "main.py", "--listen", "127.0.0.1", "--port", str(c["port"]), "--disable-auto-launch", *c.get("args", [])],
                             cwd=COMFY_DIR, stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for _ in range(180):
        if _up():
            return
        if _proc.poll() is not None:
            raise RuntimeError("ComfyUI stopped while starting; see image_gen/comfyui.log")
        time.sleep(1)
    raise RuntimeError("ComfyUI didn't come up in 3 minutes; see image_gen/comfyui.log")


def stop():
    """Stop ComfyUI altogether (frees everything it holds)."""
    global _proc
    if _proc and _proc.poll() is None:
        _proc.terminate()
        try:
            _proc.wait(15)
        except subprocess.TimeoutExpired:
            _proc.kill()
    _proc = None


def _idle_watch():
    while True:
        time.sleep(30)
        if _proc and _proc.poll() is None and not _lock.locked() and time.time() - _last_used > cfg().get("idle_stop_minutes", 5) * 60:
            stop()


# ------------------------------------------------------------------ the picture
def workflow(prompt, negative, width, height, steps, guidance, seed):
    """The graph from Chroma's own ComfyUI workflow (flow shift 1.0, a guider, the beta scheduler), with the GGUF loader."""
    c = cfg()
    m = c["models"]
    suffix = c.get("suffix", "")
    return {
        "1": ({"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": m["unet"]}} if m["unet"].endswith(".gguf")
              else {"class_type": "UNETLoader", "inputs": {"unet_name": m["unet"], "weight_dtype": "default"}}),
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": m["clip"], "type": "chroma"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": m["vae"]}},
        "4": {"class_type": "T5TokenizerOptions", "inputs": {"clip": ["2", 0], "min_padding": 1, "min_length": 0}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["4", 0], "text": (prompt.rstrip() + " " + suffix).strip()}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["4", 0], "text": negative}},
        "7": {"class_type": "EmptySD3LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "8": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": c.get("flow_shift", 1.0)}},
        "9": {"class_type": "CFGGuider", "inputs": {"model": ["8", 0], "positive": ["5", 0], "negative": ["6", 0], "cfg": guidance}},
        "10": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": c.get("sampler", "euler")}},
        "11": {"class_type": "BetaSamplingScheduler", "inputs": {"model": ["8", 0], "steps": steps, "alpha": 0.45, "beta": 0.45}},
        "12": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "13": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["12", 0], "guider": ["9", 0], "sampler": ["10", 0], "sigmas": ["11", 0],
                                                                 "latent_image": ["7", 0]}},
        "14": {"class_type": "VAEDecode", "inputs": {"samples": ["13", 0], "vae": ["3", 0]}},
        "15": {"class_type": "SaveImage", "inputs": {"images": ["14", 0], "filename_prefix": "sera"}},
    }


def generate(prompt, negative=None, width=None, height=None, steps=None, guidance=None, seed=None, progress=None):
    """Make one picture. Returns {'path', 'seed', 'seconds', ...}. progress(text) is called as it goes. Raises RuntimeError with a plain reason."""
    global _last_used
    c = cfg()
    if not available():
        raise RuntimeError("the picture model isn't set up (models.json, and its files in image_gen/; see the README)")
    say = progress or (lambda t: None)
    width = _snap(width or c["default"]["width"])
    height = _snap(height or c["default"]["height"])
    steps = int(min(max(steps or c["default"]["steps"], 4), c["max_steps"]))
    guidance = float(guidance or c["default"]["guidance"])
    seed = int(seed) if seed not in (None, "", -1) else random.randrange(2 ** 32)
    if width * height > c["max_pixels"]:
        raise RuntimeError(f"{width}x{height} is bigger than the {c['max_pixels']} pixel limit")
    say("checking the request…")
    image_safety.check(prompt, getattr(env, "safety_ask", None))  # raises image_safety.Refused; nothing is started or unloaded for a refused request
    negative = negative if negative is not None else c.get("negative", NEGATIVE)
    drawn_with = (negative + " " + image_safety.avoid_extra()).strip()  # what the model is told to avoid; the file keeps just the user's part
    with _lock:
        started = time.time()
        say("freeing her language model…")
        env.free_gpu()  # the picture model needs the whole card
        say("starting the picture model…")
        _start()
        try:
            queued = _post("/prompt", {"prompt": workflow(prompt, drawn_with, width, height, steps, guidance, seed), "client_id": uuid.uuid4().hex})
        except urllib.error.HTTPError as e:
            raise RuntimeError("ComfyUI rejected the request: " + e.read().decode("utf-8", "replace")[:400])
        pid, last = queued["prompt_id"], ""
        while True:
            time.sleep(1)
            hist = _get(f"/history/{pid}").get(pid)
            if hist:
                break
            if _proc is not None and _proc.poll() is not None:
                raise RuntimeError("ComfyUI crashed while drawing; see image_gen/comfyui.log")
            if time.time() - started > c["timeout_seconds"]:
                stop()
                raise RuntimeError("it took too long and was stopped")
            running = ((_get("/queue").get("queue_running") or [[]])[0] or [None])
            note = "drawing…" if running and running[0] is not None else "waiting its turn…"
            if note != last:
                say(note); last = note
        status = hist.get("status", {})
        if status.get("status_str") == "error":
            msgs = [m[1].get("exception_message", "") for m in status.get("messages", []) if m[0] == "execution_error"]
            raise RuntimeError("ComfyUI failed: " + (msgs[0] if msgs else "unknown error")[:400])
        img = next(i for node in hist["outputs"].values() for i in node.get("images", []))
        src = os.path.join(COMFY_DIR, "output", img.get("subfolder", ""), img["filename"])
        out_dir = env.images_dir
        os.makedirs(out_dir, exist_ok=True)
        dest = os.path.join(out_dir, time.strftime("%Y%m%d-%H%M%S") + f"-{seed}.png")
        os.replace(src, dest)
        with open(dest + ".json", "w", encoding="utf-8") as f:  # what made it, so it can be reproduced
            json.dump({"prompt": prompt, "negative": negative, "width": width, "height": height,
                       "steps": steps, "guidance": guidance, "seed": seed}, f, indent=1)
        say("letting go of the GPU…")
        try:
            _post("/free", {"unload_models": True, "free_memory": True})
        except OSError:
            stop()
        _last_used = time.time()
        return {"path": dest, "seed": seed, "width": width, "height": height, "seconds": round(time.time() - started, 1)}


def _snap(n):
    """The model wants sizes in multiples of 16."""
    return max(256, int(round(int(n) / 16)) * 16)


EXAMPLES_DIR = os.path.join(HERE, "image_examples")


def examples():
    """The hard-coded teaching examples: [{'id', 'title', 'prompt', 'flags', 'note', 'images': [file names]}]. No model is run for these."""
    with open(os.path.join(EXAMPLES_DIR, "examples.json"), encoding="utf-8") as f:
        return json.load(f)


def recent(limit=40):
    """The newest pictures, newest first: [{'name', 'prompt', ...}]."""
    d = env.images_dir
    if not os.path.isdir(d):
        return []
    names = sorted((n for n in os.listdir(d) if n.endswith(".png")), reverse=True)[:limit]
    out = []
    for n in names:
        try:
            with open(os.path.join(d, n + ".json"), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = {}
        out.append({"name": n, **meta})
    return out
