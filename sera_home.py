"""Where the things that make her *someone* live: ~/.sera (or the folder named by SERA_HOME), outside the code.
persona.txt is her system prompt, voice_rules.txt what she is told in a call, lines.json her canned replies, behaviour.json her names,
mood table and other hand-tuned behaviour, sera.env her keys, web_token.txt the web page's token, voice_local.json who she listens to,
models.json which models she runs.
The first four are optional: where the code asks for one it also passes a safe default, written out in plain sight next to the
setting, and that default is used when the file (or the entry in it) is missing. So a fresh checkout is a polite, neutral assistant.
models.json has no default on purpose: a feature whose model is not set says so and stays off (the chat model is needed to start)."""
import json, os

HOME = os.environ.get("SERA_HOME") or os.path.join(os.path.expanduser("~"), ".sera")
_json_cache = {}
_told = set()


def path(name):
    return os.path.join(HOME, name)


def _tell(name, why, then="using the built-in default"):
    if name not in _told:  # said once, so nobody wonders why she sounds generic
        _told.add(name)
        print(f"ℹ️ {path(name)} {why}: {then}.", flush=True)


def text(name, default):
    try:
        with open(path(name), encoding="utf-8", newline="") as f:
            return f.read()
    except OSError:
        _tell(name, "not found")
        return default


def _data(name):
    if name not in _json_cache:
        then = "no models are set" if name == "models.json" else "using the built-in default"
        try:
            with open(path(name), encoding="utf-8") as f:
                _json_cache[name] = json.load(f)
        except OSError:
            _tell(name, "not found", then)
            _json_cache[name] = {}
        except ValueError as e:
            _tell(name, f"is not valid JSON ({e})", then)
            _json_cache[name] = {}
    return _json_cache[name]


def _entry(file, name, default):
    data = _data(file)
    return data[name] if isinstance(data, dict) and name in data else default


def persona(default):
    return text("persona.txt", default)


def lines(name, default):
    value = _entry("lines.json", name, default)
    return value if isinstance(value, list) and value else default  # a list she can pick from, never an empty one


def behaviour(name, default):
    return _entry("behaviour.json", name, default)


def model(key, default=None):
    """A model chosen in models.json: chat, agent, claude, claude_by_difficulty or image. None means nothing is chosen."""
    return _entry("models.json", key, default)
