"""Lets her stay up while the owner uses the GPU for something else (a game, say).

Before her language model is loaded, a voice call joined, a picture drawn or a coding job started, this asks the card how much
memory is free. If the feature won't fit (with a little headroom) it is refused and she says so plainly. Nothing already loaded is
ever unloaded by this: it only decides whether to START something. Numbers are in vram_guard_config.json (measured on the
author's RTX 5070; re-measure for another card), and that file is re-read on every check.
"""
import json
import os
import subprocess
import time

import sera_home

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "vram_guard_config.json")

DEFAULTS = {
    "enabled": True,
    "headroom_mb": 600,          # left free on top of what a feature needs, because a game's use moves around
    "needs_mb": {                # measured as growth in used VRAM when the feature loads (see README)
        "chat": 8330,            # her language model, all layers on the GPU, 32,768-token context
        "chat_in_call": 7590,    # the same with 44 of 48 layers on the GPU (what a voice call leaves her with)
        "voice": 2400,           # the voice model
        "picture": 9500,         # the picture model at its peak while drawing
        "picture_idle": 5500,    # what the picture model keeps while it sits idle before it stops itself
        "coding": 9610,          # the local coding model (the rest of it sits in system RAM)
        "edit_klein": 8900,      # the picture-editing model at its peak (see image_edit.py)
    },
    "labels": {                  # how each feature is named in what she says
        "chat": "chatting", "voice": "a voice call", "picture": "pictures", "coding": "the coding agent",
        "edit_klein": "picture editing",
    },
}

_cache = {"mtime": None, "cfg": DEFAULTS}
_gpu = {"at": 0.0, "value": None}


def cfg():
    """The settings, with anything missing from the file filled in from DEFAULTS."""
    try:
        m = os.path.getmtime(CONFIG)
    except OSError:
        return DEFAULTS
    if m != _cache["mtime"]:
        try:
            raw = json.load(open(CONFIG, encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"⚠️ vram_guard_config.json unreadable ({e}); using the built-in numbers")
            raw = {}
        merged = {**DEFAULTS, **{k: v for k, v in raw.items() if not k.startswith("_")}}
        for k in ("needs_mb", "labels"):
            merged[k] = {**DEFAULTS[k], **(raw.get(k) or {})}
        _cache.update(mtime=m, cfg=merged)
    return _cache["cfg"]


def gpu_mb():
    """(total, used) VRAM in MiB, or None if the card can't be asked. Cached for 2 s."""
    if time.time() - _gpu["at"] < 2:
        return _gpu["value"]
    try:
        out = subprocess.check_output(["nvidia-smi", "--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"],
                                      timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).decode().splitlines()[0]
        total, used = (int(x) for x in out.split(","))
        value = (total, used)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        value = None
    _gpu.update(at=time.time(), value=value)
    return value


def loaded_models():
    """Names (without the tag) of the Ollama models in memory right now."""
    import ollama
    return {m.model.split(":")[0] for m in ollama.ps().models}


def picture_model_up():
    try:
        import image_gen
        return image_gen._up()
    except Exception:
        return False


def _want(feature, c, chat_in, coding_in, pic_up):
    """(MiB this start needs free, MiB it can take back from what Sera already holds)."""
    n = c["needs_mb"]
    held = (n["chat"] if chat_in else 0) + (n["coding"] if coding_in else 0) + (n["picture_idle"] if pic_up else 0)
    if feature == "chat":  # Ollama evicts the coding model to make room for her chat model, so what it holds comes back
        return (0 if chat_in else n["chat"]), (n["coding"] if coding_in else 0)
    if feature == "coding":
        return (0 if coding_in else n["coding"]), (n["chat"] if chat_in else 0)  # her chat model steps out for it
    if feature == "picture" or feature.startswith("edit_"):
        return (0 if pic_up else n[feature]), (n["chat"] if chat_in else 0) + (n["coding"] if coding_in else 0)
    if feature == "voice":
        back = n["coding"] if coding_in else 0
        if chat_in:  # joining reloads her model on fewer layers, which gives back the difference
            return n["voice"], n["chat"] - n["chat_in_call"] + back
        return n["voice"] + n["chat_in_call"], back
    return 0, held


def check(feature):
    """(True, "") if the feature can start now, else (False, what she says). Fails open if the card or Ollama can't be asked."""
    c = cfg()
    if not c["enabled"] or os.environ.get("SERA_VRAM_GUARD") == "off":  # the env switch lets offline tests ignore the real card
        return True, ""
    gpu = gpu_mb()
    if gpu is None:
        return True, ""
    try:
        names = loaded_models()
    except Exception:
        return True, ""
    chat, agent = sera_home.model("chat"), sera_home.model("agent")
    chat_in = bool(chat) and chat.split(":")[0] in names
    coding_in = bool(agent) and agent.split(":")[0] in names
    need, reclaim = _want(feature, c, chat_in, coding_in, picture_model_up() if feature == "picture" or feature.startswith("edit_") else False)
    total, used = gpu
    if need == 0 or total - used + reclaim >= need + c["headroom_mb"]:
        return True, ""
    return False, no_room_text(feature)


def no_room_text(feature):
    template = sera_home.behaviour("NO_ROOM_TEMPLATE",
                                   "My owner is using the PC right now and left no room for {feature}. Try again in a bit.")
    return template.format(feature=cfg()["labels"].get(feature, feature))


class NoRoom(RuntimeError):
    """Raised instead of loading her model when the card is taken."""


def require(feature):
    ok, text = check(feature)
    if not ok:
        raise NoRoom(text)
