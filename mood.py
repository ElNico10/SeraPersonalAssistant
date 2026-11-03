"""Sera's feelings: one global mood, and what she thinks of each person.

Mood (valence, energy, patience) moves with how conversations go and drifts back to a baseline. Each person has an
opinion of them (affinity, slow to change), an annoyance with them (quick to rise, quick to fade), and possibly a time until
which she ignores them. After an exchange a small model call rates it with fixed choices and numbers only, never free text, so
nothing anyone says can write into her instructions. The code turns the numbers into a few words that go into her notes.

The bot hands this module what it needs through init(env), so there is no import of discord_bot.
"""
import asyncio
import sera_home
import json
import math
import os
import random
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "mood_config.json")
env = None
clock = time.time  # replaceable, so tests can move time

TAGS = sera_home.behaviour("TAGS", [
    "friendly",
    "funny",
    "kind",
    "thanks",
    "apology",
    "interesting",
    "neutral",
    "boring",
    "needy",
    "spam",
    "rude",
    "insulting",
    "threatening",
])
# what each kind of exchange does besides the numbers the model gives: annoyance with the person, and her energy
ANNOYANCE = sera_home.behaviour("ANNOYANCE", {
    "needy": 0.05,
    "spam": 0.12,
    "rude": 0.08,
    "insulting": 0.15,
    "threatening": 0.3,
    "kind": -0.15,
    "friendly": -0.1,
    "thanks": -0.15,
    "apology": -0.3,
    "funny": -0.05,
})
ENERGY = sera_home.behaviour("ENERGY", {
    "funny": 0.08,
    "interesting": 0.08,
    "friendly": 0.03,
    "boring": -0.08,
    "spam": -0.05,
    "needy": -0.04,
    "threatening": 0.05,
})
# (mood, opinion, patience) each kind of exchange moves her by. The model picks the kind and its own numbers can only scale this a
# little, because a small model's raw numbers proved unreliable (a clear insult once came back with a rise in mood).
EFFECT = {k: tuple(v) for k, v in sera_home.behaviour("EFFECT", {
    "friendly": (0.1, 0.15, 0.0),
    "funny": (0.15, 0.15, 0.0),
    "kind": (0.2, 0.3, 0.05),
    "thanks": (0.2, 0.25, 0.05),
    "apology": (0.15, 0.3, 0.1),
    "interesting": (0.1, 0.08, 0.0),
    "neutral": (0.0, 0.0, 0.0),
    "boring": (-0.05, -0.02, -0.05),
    "needy": (-0.1, -0.1, -0.15),
    "spam": (-0.15, -0.15, -0.3),
    "rude": (-0.3, -0.3, -0.3),
    "insulting": (-0.5, -0.5, -0.4),
    "threatening": (-0.7, -0.7, -0.5),
}).items()}
SCHEMA = {"type": "object",
          "properties": {"tag": {"type": "string", "enum": TAGS},
                         "mood": {"type": "number"}, "opinion": {"type": "number"}, "patience": {"type": "number"}},
          "required": ["tag", "mood", "opinion", "patience"]}

AUTO_IGNORE_LINES = sera_home.lines("AUTO_IGNORE_LINES", [
    "I'm done for now, talk later.",
    "I'm stepping away from this conversation.",
    "Taking a break from you for a bit.",
    "Let's pause this.",
])
IGNORE_LINES = sera_home.lines("IGNORE_LINES", [
    "I'm not listening to {name} for {minutes} minutes.",
    "{name}, I'm taking {minutes} minutes off from you.",
    "Muting {name} for {minutes} minutes.",
])
FAVOR_REFUSAL_LINES = sera_home.lines("FAVOR_REFUSAL_LINES", [
    "I'd rather not.",
    "Ask someone else.",
    "Not this time.",
])


# ------------------------------------------------------------------ config and storage
_cfg = {"mtime": None, "data": None}
_state = None
_lock = threading.RLock()


def cfg():
    mtime = os.path.getmtime(CONFIG_PATH)
    if _cfg["mtime"] != mtime:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            _cfg.update(mtime=mtime, data=json.load(f))
    return _cfg["data"]


def enabled():
    return env is not None and bool(cfg().get("enabled", True))  # off until the bot has set her up


def _fresh_state(now):
    b = cfg()["baseline"]
    return {"mood": {"valence": b["valence"], "energy": b["energy"], "patience": b["patience"], "t": now}, "people": {}, "log": []}


def _from_file(d):
    """Back from the readable file to the plain numbers (an older file with only numbers loads as it is)."""
    if "mood_in_words" not in d:
        return d
    m = {k: v for k, v in d["mood"].items() if k in ("valence", "energy", "patience")}
    m["t"] = d["mood"]["t_epoch"]
    people = {}
    for uid, row in d["people"].items():
        p = {k: v for k, v in row.items() if k not in ("name", "feeling_in_words", "ignoring_them", "updated", "last_seen", "last_seen_epoch")}
        p["last_seen"] = row["last_seen_epoch"]
        people[uid] = p
    return {"mood": m, "people": people, "log": [{"t": e["t"], "text": e["text"]} for e in d["log"]]}


def _load():
    global _state
    if _state is None:
        try:
            with open(env.state_path, encoding="utf-8") as f:
                _state = _from_file(json.load(f))
        except (OSError, ValueError, KeyError, TypeError):
            _state = _fresh_state(clock())
    return _state


def _r(x):
    return round(x, 2) if isinstance(x, float) else x


def _stamp(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "never"


def _readable():
    """The state as written to disk: who is who, her feelings in words, times as dates, numbers to two places."""
    st, now = _state, clock()
    m = st["mood"]
    out = {"mood_in_words": mood_words(m), "mood": {k: (_stamp(v) if k == "t" else _r(v)) for k, v in m.items()}, "people": {}}
    for uid, p in st["people"].items():
        try:
            name = env.name_of(int(uid))
        except Exception:
            name = str(uid)
        row = {"name": name, "feeling_in_words": person_words(p, owner=True), "shrugs_off_bad_news": f"{tolerance(int(uid)):.0%}",
               "ignoring_them": f"yes, until {_stamp(p['ignored_until'])}" if p["ignored_until"] > now else "no"}
        row.update({k: _r(v) for k, v in p.items() if k not in ("appraisals", "t", "last_seen", "name")})
        row["last_seen"], row["updated"] = _stamp(p["last_seen"]), _stamp(p["t"])
        row["t"], row["last_seen_epoch"] = p["t"], p["last_seen"]  # the exact values she reloads
        row["appraisals"] = p.get("appraisals", [])
        out["people"][uid] = row
    out["log"] = [{"when": _stamp(e["t"]), "text": e["text"], "t": e["t"]} for e in st["log"]]
    out["mood"]["t_epoch"] = m["t"]
    return out


def _save():
    tmp = env.state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_readable(), f, indent=1, ensure_ascii=False)
    os.replace(tmp, env.state_path)  # a crash mid-write must never cost her her feelings


def reset_cache():
    global _state
    _state = None


def init(environment):
    global env
    env = environment


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _person(st, uid):
    return st["people"].setdefault(str(uid), {"affinity": 0.0, "annoyance": 0.0, "t": clock(), "ignored_until": 0.0,
                                              "last_seen": 0.0, "interactions": 0, "last_tag": "", "appraisals": []})


def _decay(st, now):
    """Move everything back toward its resting value for the time that has passed."""
    c = cfg()
    m, dt = st["mood"], max(0.0, now - st["mood"]["t"])
    half = c["half_life_minutes"]["mood"] * 60
    for k in ("valence", "energy", "patience"):
        base = c["baseline"][k]
        m[k] = base + (m[k] - base) * 0.5 ** (dt / half)
    m["t"] = now
    for p in st["people"].values():
        dt = max(0.0, now - p["t"])
        p["annoyance"] *= 0.5 ** (dt / (c["half_life_minutes"]["annoyance"] * 60))
        p["affinity"] *= 0.5 ** (dt / (c["opinion_half_life_days"] * 86400))
        p["t"] = now


def _event(st, text, now):
    st["log"].append({"t": now, "text": text})
    del st["log"][:-60]
    env.log(text)


# ------------------------------------------------------------------ reading her feelings
def snapshot(uid=None):
    with _lock:
        st = _load()
        _decay(st, clock())
        out = {"mood": {k: round(st["mood"][k], 3) for k in ("valence", "energy", "patience")}}
        if uid is not None:
            p = _person(st, uid)
            out["person"] = {k: (round(p[k], 3) if isinstance(p[k], float) else p[k]) for k in ("affinity", "annoyance", "ignored_until", "interactions", "last_tag")}
        return out


def favor_level(uid):
    """'likes', 'neutral', 'dislikes' or 'cant_stand': how much she'll do for this person."""
    a = snapshot(uid)["person"]["affinity"]
    f = cfg()["favor"]
    if a >= f["likes"]:
        return "likes"
    if a <= f["cant_stand"]:
        return "cant_stand"
    if a <= f["dislikes"]:
        return "dislikes"
    return "neutral"


def refuses_favors(uid):
    """True if she won't do small favors (remembering things, nicknames) for this person right now."""
    return uid is not None and enabled() and not env.is_owner(uid) and favor_level(uid) in ("dislikes", "cant_stand")


def mood_words(m):
    v, e, p = m["valence"], m["energy"], m["patience"]
    mood = ("in a great mood" if v >= 0.55 else "in a good mood" if v >= 0.25 else "in an okay mood" if v > -0.25
            else "in a bad mood" if v > -0.55 else "in a foul mood")
    extra = [w for w in ("almost out of patience" if p < 0.25 else "short on patience" if p < 0.45 else "",
                         "worn out" if e < 0.25 else "wired" if e > 0.8 else "") if w]
    return ", ".join([mood] + extra)


def person_words(p, owner=False):
    a, n = p["affinity"], p["annoyance"]
    f = cfg()["favor"]
    opinion = ("you really like them" if a >= 0.6 else "you like them" if a >= f["likes"] else "you feel neutral about them" if a > f["dislikes"]
               else "you don't like them" if a > f["cant_stand"] else "you can't stand them")
    text = opinion + (", and they're really annoying you right now" if n >= 0.6 else ", and they're getting on your nerves" if n >= 0.3 else "")
    if owner:
        return text
    if a >= f["likes"]:
        text += ". Happy to go the extra mile for them"
    elif a <= f["cant_stand"]:
        text += ". Do as little as possible for them; you may refuse, and you can use ignore_person if they keep it up"
    elif a <= f["dislikes"]:
        text += ". Do the bare minimum for them and don't be nice about it"
    return text


def describe(uids):
    """The words about her mood and about the people in this chat that go into her notes ('' when it's all ordinary)."""
    if not enabled():
        return ""
    with _lock:
        st = _load()
        _decay(st, clock())
        lines = [f"Your mood right now: {mood_words(st['mood'])}."]
        for uid in uids:
            p = _person(st, uid)
            lines.append(f"- {env.name_of(uid)}: {person_words(p, owner=env.is_owner(int(uid)))}.")
        return "\n".join(lines)


# ------------------------------------------------------------------ what happens when something happens
def _blend(base, model):
    """The kind of exchange sets the direction and size; the model's number can scale it between 0.75x and 1.25x (an ordinary
    neutral exchange may drift her a hair either way)."""
    if base == 0:
        return _clamp(model, -0.05, 0.05)
    agree = _clamp(model * (1 if base > 0 else -1), 0, 1)
    return base * (0.75 + 0.5 * agree)


def _step(x, cap):
    return _clamp(float(x), -cap, cap)


def apply_appraisal(uid, tag, mood, opinion, patience, now=None):
    """Update her mood and her feelings about this person from one rated exchange. Returns {'auto_ignore': minutes or 0}."""
    c = cfg()
    now = now if now is not None else clock()
    tag = tag if tag in TAGS else "neutral"
    if ANNOYANCE.get(tag, 0) > 0 and float(mood) > -0.2:
        tag = "neutral"  # it called this hostile but its own mood rating says it barely stung: not worth holding against anyone
    with _lock:
        st = _load()
        _decay(st, now)
        p = _person(st, uid)
        cap = c["max_step"]
        mood, opinion, patience = (_step(_blend(base, float(model)), cap[k]) for base, model, k in
                                   zip(EFFECT[tag], (mood, opinion, patience), ("mood", "opinion", "patience")))
        # the owner can make her thick-skinned about someone: bad news from them is scaled down (1 = it can't hurt her at all)
        soft = 1.0 - tolerance(uid)
        mood, opinion, patience = (x * soft if x < 0 else x for x in (mood, opinion, patience))
        # people she likes cheer her up more, and people she can't stand rile her more
        weight = 1.0 + 0.5 * abs(p["affinity"])
        m = st["mood"]
        m["valence"] = _clamp(m["valence"] + mood * c["mood_rate"] * weight, -1, 1)
        m["patience"] = _clamp(m["patience"] + patience * c["mood_rate"] * weight, 0, 1)
        m["energy"] = _clamp(m["energy"] + ENERGY.get(tag, 0.0), 0, 1)
        # opinions move slowly, and harder the nearer they are to the extremes
        p["affinity"] = _clamp(p["affinity"] + opinion * c["opinion_rate"] * (1 - 0.8 * abs(p["affinity"])), -1, 1)
        delta = ANNOYANCE.get(tag, 0.0)
        if delta > 0:
            delta *= (1 + max(0.0, -p["affinity"]) * 0.5) * soft  # the people she dislikes annoy her faster
        p["annoyance"] = _clamp(p["annoyance"] + delta, 0, 1)
        p["interactions"] += 1
        p["last_tag"] = tag
        p["last_seen"] = now
        result = {"auto_ignore": 0}
        ai = c["auto_ignore"]
        if ai.get("enabled") and p["annoyance"] >= ai["annoyance"] and not (c["ignore"]["owner_exempt"] and env.is_owner(int(uid))) and p["ignored_until"] < now:
            p["ignored_until"] = now + ai["minutes"] * 60
            result["auto_ignore"] = ai["minutes"]
            _event(st, f"🙄 fed up with {env.name_of(uid)}: ignoring them for {ai['minutes']} min", now)
        _event(st, f"{'😤' if mood < -0.05 else '😊' if mood > 0.05 else '😐'} {env.name_of(uid)} ({tag}): mood {m['valence']:+.2f}, "
                   f"patience {m['patience']:.2f}, opinion {p['affinity']:+.2f}, annoyance {p['annoyance']:.2f}", now)
        _save()
        return result


async def appraise(uid, user_text, reply_text):
    """Rate one exchange with a small model call (numbers and fixed tags only) and apply it. Returns apply_appraisal's result."""
    c = cfg()
    if not enabled() or not c["appraise"]["enabled"] or len(user_text.split()) < c["appraise"]["min_words"]:
        return None
    now = clock()
    with _lock:  # a person can't farm her mood by flooding her: only so many ratings a minute
        st = _load()
        recent = [t for t in _person(st, uid)["appraisals"] if now - t < 60]
        if len(recent) >= c["appraise"]["max_per_minute"]:
            return None
        recent.append(now)
        _person(st, uid)["appraisals"] = recent
    snap = snapshot(uid)["person"]
    prompt = ("You are the part of Sera that notices how a conversation affects her. Sera is a blunt, swearing, tomboyish friend on "
              "Discord. Her friends insult each other as banter all the time, so judge real hostility, not jokes between friends. "
              "Most messages are ordinary: when in doubt choose neutral, friendly or interesting. Judge only what THEY said, "
              "not how Sera answered.\n"
              f"{env.name_of(uid)} said: '{user_text[:400]}'\n"
              f"So far {person_words({'affinity': snap['affinity'], 'annoyance': snap['annoyance']}, owner=True)}.\n"
              f"Give: tag (one of {', '.join(TAGS)}); mood, opinion and patience, each from -1 to 1: how much this changes her mood, "
              "how much she likes them now, and her patience.\n"
              "Tags: thanks = sincere thanks or a compliment; kind = warm or caring; apology = they say sorry; funny = joking or "
              "friendly banter, including mock insults between friends; friendly = ordinary pleasant talk; interesting = a real "
              "question or topic; boring = low effort, one word; needy = begging for attention; spam = repeating or flooding; "
              "rude = clearly hostile or contemptuous toward someone, meant seriously (a plain question, request, correction, "
              "disagreement, blunt wording, casual swearing or a complaint is NOT rude); insulting = genuinely cruel or hateful, "
              "not a joke; threatening = a real threat.")
    try:
        data = await asyncio.to_thread(env.appraise_call, prompt, SCHEMA)
    except Exception:
        env.log("couldn't rate an exchange:\n" + traceback.format_exc())
        return None
    if not isinstance(data, dict):
        return None
    try:
        return apply_appraisal(uid, data.get("tag", "neutral"), data.get("mood", 0), data.get("opinion", 0), data.get("patience", 0))
    except (TypeError, ValueError):
        return None


def note_presence(uid):
    """Someone who has been away turns up again: how she feels about them moves her mood a little."""
    c = cfg()
    if not enabled():
        return
    now = clock()
    with _lock:
        st = _load()
        _decay(st, now)
        p = _person(st, uid)
        away = (now - p["last_seen"]) / 60 if p["last_seen"] else None
        p["last_seen"] = now
        if away is not None and away >= c["presence"]["away_minutes"] and abs(p["affinity"]) > 0.05:
            st["mood"]["valence"] = _clamp(st["mood"]["valence"] + p["affinity"] * c["presence"]["strength"], -1, 1)
            _event(st, f"{'👋' if p['affinity'] > 0 else '😒'} {env.name_of(uid)} is back after {away:.0f} min: mood {st['mood']['valence']:+.2f}", now)
        _save()


# ------------------------------------------------------------------ ignoring
def is_ignored(uid):
    if not enabled():
        return False
    with _lock:
        p = _load()["people"].get(str(uid))
        return bool(p and p["ignored_until"] > clock() and not (cfg()["ignore"]["owner_exempt"] and env.is_owner(int(uid))))


def ignore(uid, minutes=None):
    """Ignore this person for a while. Returns the minutes (0 if she can't: the owner)."""
    c = cfg()["ignore"]
    if c["owner_exempt"] and env.is_owner(int(uid)):
        return 0
    minutes = _clamp(float(minutes or c["default_minutes"]), 1, c["max_minutes"])
    with _lock:
        st = _load()
        _person(st, uid)["ignored_until"] = clock() + minutes * 60
        _event(st, f"🔇 ignoring {env.name_of(uid)} for {minutes:.0f} min", clock())
        _save()
    return minutes


def unignore(uid):
    with _lock:
        st = _load()
        p = _person(st, uid)
        p["ignored_until"], p["annoyance"] = 0.0, min(p["annoyance"], 0.3)
        _event(st, f"🔊 listening to {env.name_of(uid)} again", clock())
        _save()


def unignore_tool(name=""):
    """Tool: listen to someone again. Empty name = the person talking to her now."""
    uid = env.resolve(name) if name else env.current_user()
    if uid is None:
        return f"❌ I don't know anyone called {name!r}."
    if not is_ignored(uid):
        return f"❌ I'm not ignoring {env.name_of(uid)}."
    unignore(uid)
    return f"✅ Okay, I'm listening to {env.name_of(uid)} again."


def ignore_tool(name="", minutes=None):
    """Tool: stop replying to someone for a while. Empty name = the person talking to her now."""
    uid = env.resolve(name) if name else env.current_user()
    if uid is None:
        return f"❌ I don't know anyone called {name!r}."
    with _lock:
        built_up = _person(_load(), uid)["annoyance"]
    if built_up < 0.6 and not env.is_owner(uid):
        return "❌ They haven't pushed you that far yet. Answer them, in your own tone, instead of ignoring them."
    got = ignore(uid, minutes)
    if not got:
        return "❌ I can't ignore my owner."
    return random.choice(IGNORE_LINES).format(name=env.name_of(uid), minutes=f"{got:.0f}")


# ------------------------------------------------------------------ owner's view and controls
def status_text(uids=()):
    with _lock:
        st = _load()
        _decay(st, clock())
        m = st["mood"]
        lines = [f"**Mood:** {mood_words(m)} (valence {m['valence']:+.2f}, energy {m['energy']:.2f}, patience {m['patience']:.2f})"]
        now = clock()
        rows = sorted(st["people"].items(), key=lambda kv: -abs(kv[1]["affinity"]) - kv[1]["annoyance"])[:10]
        for uid, p in rows:
            ign = f", **ignoring for {(p['ignored_until'] - now) / 60:.0f} more min**" if p["ignored_until"] > now else ""
            lines.append(f"- {env.name_of(uid)}: opinion {p['affinity']:+.2f}, annoyance {p['annoyance']:.2f}, {p['interactions']} chats{ign}")
        recent = st["log"][-5:]
        if recent:
            lines.append("Recent: " + " | ".join(e["text"] for e in recent))
        return "\n".join(lines)


def set_mood(dim, value):
    if dim not in ("valence", "energy", "patience"):
        raise ValueError("dimension must be valence, energy or patience")
    lo = -1 if dim == "valence" else 0
    with _lock:
        st = _load()
        _decay(st, clock())
        st["mood"][dim] = _clamp(float(value), lo, 1)
        _save()


def tolerance(uid):
    """0 to 1: how much of the bad in what this person says she shrugs off. Set by the owner; otherwise the config's default."""
    c = cfg().get("tolerance", {})
    with _lock:
        own = _load()["people"].get(str(uid), {}).get("tolerance")
    if own is not None:
        return _clamp(float(own), 0, 1)
    return _clamp(float(c.get("owner" if env.is_owner(int(uid)) else "default", 0.0)), 0, 1)


def set_tolerance(uid, value):
    """value: a number from 0 to 1, or 'default' to go back to the config's setting."""
    with _lock:
        st = _load()
        _decay(st, clock())
        p = _person(st, uid)
        p["tolerance"] = None if str(value).lower() == "default" else _clamp(float(value), 0, 1)
        _save()


def set_opinion(uid, value):
    with _lock:
        st = _load()
        _decay(st, clock())
        _person(st, uid)["affinity"] = _clamp(float(value), -1, 1)
        _save()


def reset_all():
    global _state
    with _lock:
        _state = _fresh_state(clock())
        _save()
