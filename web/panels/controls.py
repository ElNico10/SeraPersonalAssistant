"""The Controls panel's python side: every setting in her *_config.json files as something you can flip, plus the one switch that lives only in memory.

It edits the files themselves (one key at a time, atomically), and each module re-reads its file when it changes, so a switch takes effect on her
next message without a restart. Files with built-in defaults (efficiency, vram_guard) show the effective value, so a setting missing from the file is
still listed. web_config.json is left out on purpose: turning the page off from the page would lock you out.
Not here, because it is hard-coded and always on: the under-18 rule in image_safety.py.
"""
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FILES = ["access_config.json", "image_gen_config.json", "vram_guard_config.json", "efficiency_config.json", "memory_config.json", "mood_config.json", "reminders_config.json",
         "message_log_config.json", "agent_config.json", "music_config.json", "voice_config.json"]


def _defaults(name):
    try:
        if name == "efficiency_config.json":
            import notebook
            return notebook.DEFAULTS
        if name == "vram_guard_config.json":
            import vram_guard
            return {k: v for k, v in vram_guard.DEFAULTS.items() if k != "labels"}
    except ImportError:
        pass
    return {}


def _read(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return json.load(f)


def _merge(base, over):
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _tree(d, help_keys=False):
    """The settings as nested {key: value}; the "_help" notes are kept apart so they can be shown as hints."""
    return {k: (_tree(v) if isinstance(v, dict) else v) for k, v in d.items() if not k.startswith("_")}


def _hints(d, path=()):
    out = {}
    for k, v in d.items():
        if k == "_help":
            out["/".join(path)] = v
        elif isinstance(v, dict):
            out.update(_hints(v, path + (k,)))
    return out


def _write(name, path, value):
    """Set one key in the file (creating it and any parent sections if the file didn't have it yet)."""
    full = os.path.join(HERE, name)
    data = _read(name)
    node = data
    for k in path[:-1]:
        node = node.setdefault(k, {})
    node[path[-1]] = value
    tmp = full + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, full)


def _coerce(old, new):
    """The new value must be the same kind as the old one (a switch stays a switch)."""
    if isinstance(old, bool):
        if not isinstance(new, bool):
            raise ValueError("that setting is an on/off switch")
        return new
    if isinstance(old, (int, float)):
        if isinstance(new, bool) or not isinstance(new, (int, float)):
            raise ValueError("that setting is a number")
        return int(new) if isinstance(old, int) and float(new).is_integer() else new
    if isinstance(old, str):
        if not isinstance(new, str):
            raise ValueError("that setting is text")
        return new
    if isinstance(old, list):
        if not isinstance(new, list):
            raise ValueError("that setting is a list")
        return new
    raise ValueError("can't edit that kind of setting")


def setup(web_ui, env):
    def snapshot():
        files = []
        for name in FILES:
            try:
                raw = _read(name)
            except (OSError, ValueError):
                continue
            files.append({"file": name, "tree": _tree(_merge(_defaults(name), raw)), "hints": _hints(raw)})
        am = getattr(env, "agent_mode", None)
        return {"files": files, "runtime": [{"id": "agent_claude", "label": "Coding jobs go to Claude (not the local model) until restart", "value": bool(am and am.get("claude"))}] if am is not None else []}

    def change(data):
        if data.get("runtime") == "agent_claude":
            env.agent_mode["claude"] = bool(data.get("value"))
            return {"saved": True}
        name, path, value = data.get("file"), data.get("path"), data.get("value")
        if name not in FILES or not isinstance(path, list) or not path or not all(isinstance(p, str) and not p.startswith("_") for p in path):
            raise ValueError("unknown setting")
        node = _tree(_merge(_defaults(name), _read(name)))
        for k in path:
            if not isinstance(node, dict) or k not in node:
                raise ValueError("unknown setting")
            node = node[k]
        if isinstance(node, dict):
            raise ValueError("that is a section, not a setting")
        _write(name, path, _coerce(node, value))
        return {"saved": True, "value": _coerce(node, value)}

    async def handler(data):
        if data.get("action") == "list":
            return await asyncio.to_thread(snapshot)
        if data.get("action") == "set":
            return await asyncio.to_thread(change, data)
        raise ValueError("unknown action")

    web_ui.register_panel("controls", "Controls", handler)
