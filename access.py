"""Who may use which command. The owner can always use everything. For the commands listed in COMMANDS the owner picks a level:
  "owner"    only the owner (the default for all but pictures)
  "friends"  the owner and the people on the friends list
  "everyone" anyone who can talk to her
The friends list and the levels live in access_config.json (re-read when it changes, editable from the web page's Controls panel or with
`!allow` in chat). A level that isn't one of the three is treated as "owner", so a typo can only make her stricter.

Not here on purpose: !update, !allow and the voice-call commands, which stay owner-only whatever the file says.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "access_config.json")
LEVELS = ("owner", "friends", "everyone")
COMMANDS = {
    "image": "!image <prompt>: drawing pictures",
    "image_edit": "!image edit, or asking her to change a picture you attach",
    "memory": "!memory",
    "mood": "!mood",
    "agent": "!agent",
    "remind": "!remind (the list of everyone's reminders)",
}
DEFAULTS = {
    "_help": "Who may use which command. friends: Discord user ids. commands: owner, friends or everyone for each command. The owner can always do everything.",
    "friends": [],
    "commands": {c: "owner" for c in COMMANDS},
}
_cache = {"mtime": None, "data": DEFAULTS}


def cfg():
    try:
        m = os.path.getmtime(PATH)
    except OSError:
        return DEFAULTS
    if m != _cache["mtime"]:
        try:
            with open(PATH, encoding="utf-8") as f:
                raw = json.load(f)
            _cache.update(mtime=m, data={**DEFAULTS, **raw, "commands": {**DEFAULTS["commands"], **(raw.get("commands") or {})}})
        except (OSError, ValueError):
            _cache.update(mtime=m, data=DEFAULTS)
    return _cache["data"]


def friends():
    return {int(x) for x in cfg().get("friends", []) if str(x).lstrip("-").isdigit()}


def level(command):
    v = cfg()["commands"].get(command, "owner")
    return v if v in LEVELS else "owner"


def allows(command, user_id, owner):
    """owner: whether this user is the owner (the caller knows). Unknown commands are owner-only."""
    if owner:
        return True
    lv = level(command)
    return lv == "everyone" or (lv == "friends" and int(user_id) in friends())


def _save(data):
    out = {k: v for k, v in data.items()}
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    os.replace(tmp, PATH)
    _cache["mtime"] = None


def add_friend(user_id):
    d = dict(cfg())
    d["friends"] = sorted(friends() | {int(user_id)})
    _save(d)


def remove_friend(user_id):
    d = dict(cfg())
    d["friends"] = sorted(friends() - {int(user_id)})
    _save(d)


def set_level(command, lv):
    if command not in COMMANDS or lv not in LEVELS:
        raise ValueError("unknown command or level")
    d = dict(cfg())
    d["commands"] = {**d["commands"], command: lv}
    _save(d)
