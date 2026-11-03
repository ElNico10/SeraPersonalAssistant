"""Reminders: she pings someone at a set time, within the next few days.

Built like the old daily-digest clock watcher: a small loop checks the clock, anything due is delivered, and the state is kept in
a file so a restart loses nothing (a reminder that came due while she was offline goes out as soon as she is back, marked late).
Unlike her memories, a reminder keeps the exact wording and the exact time, in its own file with a backup, and the pending ones
are put in her notes word for word so she never has to guess what she was asked.

The time is worked out by plain code, not by her model: the model only copies out who, what and the time phrase from the
message, and the phrase must really appear in the message. The bot hands this module what it needs through init(env).
"""
import asyncio
import datetime as dt
import json
import os
import random
import re
import sera_home
import shutil
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "reminders_config.json")
env = None
clock = time.time  # replaceable, so tests can move time

SET_RE = re.compile(r"\b(remind|reminder|set (?:an? )?(?:alarm|timer)|ping me|wake me|nudge me|tell me (?:to|when)|"
                    r"ping \w+ (?:at|in|tomorrow|tonight|on|this))\b", re.I)
LIST_RE = re.compile(r"\b(what|which|show|list|any|my|all)\b.*\breminders?\b|\breminders?\b.*\b(do i have|have i got|are set|pending|left)\b", re.I)
CANCEL_RE = re.compile(r"\b(cancel|delete|remove|forget|clear|never ?mind)\b.*\b(reminders?|alarm|timer|R\d+)\b", re.I)
SCHEMA = {"type": "object",
          "properties": {"who": {"type": "string"}, "task": {"type": "string"}, "when": {"type": "string"}, "offset": {"type": "string"},
                         "direction": {"type": "string", "enum": ["before", "after", "none"]}},
          "required": ["who", "task", "when", "offset", "direction"]}
ACKS = sera_home.lines("ACKS", [
    "On it.",
    "Okay.",
    "Noted.",
    "Got it.",
    "Sure.",
])
PINGS = sera_home.lines("PINGS", [
    "Reminder:",
    "You asked me to remind you:",
    "Heads up:",
    "Don't forget:",
])

WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thur": 3,
            "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}
MONTHS = {m: i + 1 for i, names in enumerate([("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",), ("june", "jun"),
                                               ("july", "jul"), ("august", "aug"), ("september", "sep", "sept"), ("october", "oct"),
                                               ("november", "nov"), ("december", "dec")]) for m in names}
NUMWORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
            "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "sixty": 60}
UNITS = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400}

_cfg = {"mtime": None, "data": None}
_lock = threading.RLock()
_state = None


def cfg():
    mtime = os.path.getmtime(CONFIG_PATH)
    if _cfg["mtime"] != mtime:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            _cfg.update(mtime=mtime, data=json.load(f))
    return _cfg["data"]


def enabled():
    return env is not None and bool(cfg().get("enabled", True))


def init(environment):
    global env
    env = environment


# ------------------------------------------------------------------ time
def _local(ts):
    return dt.datetime.fromtimestamp(ts)


def _to_ts(naive):
    return time.mktime(naive.timetuple())  # the PC's own timezone, daylight saving included


def fmt_time(ts):
    d = _local(ts)
    return f"{d.strftime('%a %b')} {d.day}, {d.hour % 12 or 12}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


def fmt_delta(sec):
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec} s"
    if sec < 5400:
        return f"{round(sec / 60)} min"
    if sec < 2 * 86400:
        h, m = divmod(sec // 60, 60)
        return f"{h} h {m} min" if m else f"{h} h"
    return f"{sec // 86400} d {sec % 86400 // 3600} h"


def _num(token):
    token = token.lower()
    return float(token) if re.fullmatch(r"\d+(?:\.\d+)?", token) else NUMWORDS.get(token)


_QTY = r"(\d+(?:\.\d+)?|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|sixty)"
_UNIT = r"(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)"


def _relative(t):
    """Seconds from now for 'in 2 hours', 'in half an hour', 'in an hour and a half', 'in 1 hour 30 minutes'; None if not relative."""
    m = re.search(r"\b(?:in|within)\s+(.*)$", t)
    if not m:
        return None
    body, total, found = m.group(1), 0.0, False
    if re.search(r"\bhalf an? hour\b", body):
        total, found, body = 1800.0, True, re.sub(r"\bhalf an? hour\b", " ", body)
    last_unit = None
    for q in re.finditer(_QTY + r"\s*" + _UNIT + r"\b", body):
        last_unit = UNITS[q.group(2).rstrip("s")]
        total, found = total + _num(q.group(1)) * last_unit, True
    if found and last_unit and re.search(r"\band a half\b", body):
        total += 0.5 * last_unit
    return total if found else None


def _day(t, d0):
    """(date or None, the text with the day removed, whether a weekday name was used, an error or None)."""
    if re.search(r"\bday after tomorrow\b", t):
        return d0.date() + dt.timedelta(days=2), re.sub(r"\bday after tomorrow\b", " ", t), False, None
    if re.search(r"\btomorrow\b", t):
        return d0.date() + dt.timedelta(days=1), re.sub(r"\btomorrow\b", " ", t), False, None
    if re.search(r"\b(today|tonight|this (?:morning|afternoon|evening))\b", t):
        return d0.date(), t, False, None
    names = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
    m = re.search(r"\b(?:(?:this|next)\s+)?(" + names + r")\b", t)
    if m:
        ahead = (WEEKDAYS[m.group(1)] - d0.weekday()) % 7
        return d0.date() + dt.timedelta(days=ahead), t[:m.start()] + " " + t[m.end():], True, None
    months = "|".join(sorted(MONTHS, key=len, reverse=True))
    m = re.search(r"\b(" + months + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", t)
    form = "md"
    if not m:
        m, form = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + months + r")\b", t), "dm"
    if not m:
        m, form = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t), "slash"
    if not m:
        return None, t, False, None
    try:
        if form == "md":
            month, day, year = MONTHS[m.group(1)], int(m.group(2)), None
        elif form == "dm":
            month, day, year = MONTHS[m.group(2)], int(m.group(1)), None
        else:
            month, day, year = int(m.group(1)), int(m.group(2)), (int(m.group(3)) if m.group(3) else None)
            year = year + 2000 if year and year < 100 else year
        date = dt.date(year or d0.year, month, day)
        if year is None and date < d0.date():
            date = dt.date(d0.year + 1, month, day)
    except ValueError:
        return None, t, False, "That date doesn't exist."
    return date, t[:m.start()] + " " + t[m.end():], False, None


def _clock_time(rest, allow_bare):
    """(hour, minute, is a bare number like 'at 7') or None."""
    defaults = cfg()["defaults"]
    if re.search(r"\bnoon\b", rest):
        return 12, 0, False
    if re.search(r"\bmidnight\b", rest):
        return 0, 0, False
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)(?!\w)", rest)
    if m:
        h, mi, pm = int(m.group(1)), int(m.group(2) or 0), m.group(3).startswith("p")
        return (h % 12) + (12 if pm else 0), mi, False
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", rest)
    if m:
        h = int(m.group(1))
        return h, int(m.group(2)), 1 <= h <= 12 and not m.group(1).startswith("0")
    m = re.search(r"\b(\d{1,2})(\d{2})\b", rest) if allow_bare else None  # "310" is 3:10, "1530" is 15:30, "0845" is 8:45
    if m and not re.fullmatch(r"(?:19|20)\d{2}", m.group(0)) and int(m.group(2)) <= 59 and int(m.group(1)) <= 23:
        h = int(m.group(1))
        return h, int(m.group(2)), len(m.group(0)) == 3 and 1 <= h <= 12  # three digits could be morning or evening, like "at 7"
    if allow_bare:
        m = re.search(r"\b(\d{1,2})\b", rest)
        if m:
            h = int(m.group(1))
            return h, 0, 1 <= h <= 12
    for word in ("tonight", "morning", "afternoon", "evening", "night"):
        if re.search(rf"\b{word}\b", rest):
            h, mi = (int(x) for x in defaults[word].split(":"))
            return h, mi, False
    return None


def _duration(text):
    """Seconds in 'a minute', '10 minutes', 'half an hour', 'a couple of minutes', 'an hour and a half'; 0 if none."""
    t = text.lower()
    total = 0.0
    if re.search(r"\bhalf an? hour\b", t):
        total, t = 1800.0, re.sub(r"\bhalf an? hour\b", " ", t)
    for word, n in (("couple", 2), ("few", 3)):
        for q in re.finditer(r"\ba " + word + r"(?: of)?\s*" + _UNIT + r"\b", t):
            total += n * UNITS[q.group(1).rstrip("s")]
        t = re.sub(r"\ba " + word + r"(?: of)?\s*" + _UNIT + r"\b", " ", t)
    last = None
    for q in re.finditer(_QTY + r"\s*" + _UNIT + r"\b", t):
        last = UNITS[q.group(2).rstrip("s")]
        total += _num(q.group(1)) * last
    if last and re.search(r"\band a half\b", t):
        total += 0.5 * last
    return total


def parse_when(text, now=None):
    """(due timestamp, None) or (None, why not). Understands 'in 20 minutes', 'tomorrow at 3pm', 'friday 5:30pm', 'tonight',
    'oct 31 at 4pm', 'at 7' (the next 7 o'clock). Never guesses past what it can read."""
    now = clock() if now is None else now
    low = " ".join(text.lower().replace(",", " ").split())
    low = re.sub(r"\b(?:tmrw|tmr|tomorow|2morrow|tommorow)\b", "tomorrow", low)  # how people actually type it
    low = re.sub(r"\btonite\b", "tonight", low)
    low = re.sub(r"(\d)ish\b", r"\1", low)  # "5ish"
    low = " ".join(re.sub(r"\b(?:like|roughly|approximately|ish)\b", " ", low).split())  # "at like 310"
    has_at = bool(re.search(r"(?:^|\s)(?:at|@)\s*\d", low))
    t = " ".join(re.sub(r"\b(?:at|on|around|about|by|for|the|of)\b", " ", low).split())
    rel = _relative(t)
    if rel is not None:
        if rel >= 86400:  # days mean the same time on the wall clock, even across a daylight-saving change
            return _check(_to_ts(_local(now) + dt.timedelta(seconds=rel)), now)
        return _check(now + rel, now)
    d0 = _local(now)
    date, rest, weekday_named, err = _day(t, d0)
    if err:
        return None, err
    tod = _clock_time(rest, allow_bare=has_at or date is not None or bool(re.fullmatch(r"\d{3,4}", t)))
    if date is None and tod is None:
        return None, "I couldn't read a time from that. Try 'in 20 minutes' or 'tomorrow at 3pm'."
    if tod is None:
        h, mi = (int(x) for x in cfg()["defaults"]["no_time"].split(":"))
        tod = (h, mi, False)
    h, mi, bare = tod
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None, "That time doesn't exist."
    day = date or d0.date()
    options = [dt.datetime.combine(day, dt.time(h, mi))]
    if bare and h < 12:
        if date is not None:  # "tomorrow at 5": nobody means 5 in the morning; 7 to 11 are mornings, 1 to 6 afternoons
            options = [dt.datetime.combine(day, dt.time(h + 12 if 1 <= h <= 6 else h, mi))]
        else:
            options.append(dt.datetime.combine(day, dt.time(h + 12, mi)))  # "at 7": the 7am or the 7pm, whichever comes next
    soon = now + cfg()["min_seconds"]
    future = [o for o in options if _to_ts(o) > soon]
    if future:
        return _check(_to_ts(min(future)), now)
    # every option today has passed: tomorrow, or next week if a weekday was named ("friday" said on a Friday)
    step = 7 if weekday_named else 1
    return _check(_to_ts(min(options) + dt.timedelta(days=step)), now)


def _check(due, now):
    c = cfg()
    if due <= now:
        return None, "That time has already passed."
    if due < now + c["min_seconds"]:
        return None, f"That's too soon: I need at least {c['min_seconds']} seconds."
    if due > now + c["max_days"] * 86400:
        return None, f"I only do reminders up to {c['max_days']} days ahead, and that's {fmt_time(due)}."
    return due, None


# ------------------------------------------------------------------ the store
def _path():
    return env.state_path


def _load():
    global _state
    if _state is None:
        for p in (_path(), _path() + ".bak"):
            try:
                with open(p, encoding="utf-8") as f:
                    _state = json.load(f)
                break
            except (OSError, ValueError):
                continue
        else:
            _state = {"next_id": 1, "items": []}
    return _state


def _save():
    path = _path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_state, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)
    shutil.copyfile(path, path + ".bak")  # a second copy of the latest good state, in case the file is ever damaged


def reset_cache():
    global _state
    _state = None


def pending(uid=None):
    with _lock:
        items = [i for i in _load()["items"] if i["status"] == "pending"]
    if uid is not None:
        items = [i for i in items if i["uid"] == uid]
    return sorted(items, key=lambda i: i["due"])


def add(uid, name, by, by_name, text, due, channel, request="", note=""):
    with _lock:
        st = _load()
        item = {"id": f"R{st['next_id']}", "uid": uid, "name": name, "by": by, "by_name": by_name, "text": text, "due": due,
                "created": clock(), "channel": channel, "request": request, "note": note, "status": "pending", "tries": 0, "fired_at": 0, "late": 0}
        st["next_id"] += 1
        st["items"].append(item)
        _save()
        env.log(f"⏰ reminder {item['id']} for {name}: {text!r} at {fmt_time(due)}")
        return item


def cancel(rid, by_uid):
    """Cancel a pending reminder; only whoever it is for, whoever set it, or the owner may. Returns the item, 'no' or None."""
    with _lock:
        for i in _load()["items"]:
            if i["id"].lower() == rid.lower() and i["status"] == "pending":
                if by_uid not in (i["uid"], i["by"]) and not env.is_owner(by_uid):
                    return "no"
                i["status"] = "cancelled"
                _save()
                return i
    return None


def _prune(now):
    keep = cfg()["keep_finished_days"] * 86400
    st = _load()
    before = len(st["items"])
    st["items"] = [i for i in st["items"] if i["status"] == "pending" or now - max(i["fired_at"], i["due"]) < keep]
    return len(st["items"]) != before


# ------------------------------------------------------------------ delivering
def compose(item, now):
    late = now - item["due"]
    by = f" (set by {item['by_name']})" if item["by"] != item["uid"] else ""
    note = f"\n*(sorry, {fmt_delta(late)} late: I was offline)*" if late > cfg()["delivery"]["late_note_after_seconds"] else ""
    return f"<@{item['uid']}> ⏰ {random.choice(PINGS)} **{item['text']}**{by}{note}"


async def tick(now=None):
    """One look at the clock: send whatever has come due. Safe to call as often as you like."""
    if not enabled():
        return 0
    now = clock() if now is None else now
    d = cfg()["delivery"]
    with _lock:
        if _prune(now):
            _save()
        due = [i for i in _load()["items"] if i["status"] == "pending" and i["due"] <= now and now >= i.get("retry_at", 0)]
    sent = 0
    for item in due:
        try:
            ok = await env.deliver(item, compose(item, now))
        except Exception:
            env.log("couldn't deliver a reminder:\n" + traceback.format_exc())
            ok = False
        with _lock:
            item["tries"] += 1
            if ok:
                item["status"], item["fired_at"], item["late"] = "fired", now, max(0, int(now - item["due"]))
                sent += 1
                env.log(f"⏰ delivered {item['id']} to {item['name']}")
            elif item["tries"] >= d["max_tries"]:
                item["status"], item["fired_at"] = "failed", now
                env.log(f"⚠️ gave up on reminder {item['id']} for {item['name']}: couldn't reach them")
            else:
                item["retry_at"] = now + d["retry_seconds"]
            _prune(now)
            _save()
        if ok and d.get("speak_in_call") and getattr(env, "announce", None):
            try:
                await env.announce(item["uid"], f"{item['name']}, reminder: {item['text']}")
            except Exception:
                pass  # the text ping already went out
    return sent


# ------------------------------------------------------------------ requests in plain words
def looks_like_request(text):
    return bool(SET_RE.search(text) or LIST_RE.search(text) or CANCEL_RE.search(text))


def _norm(s):
    return " ".join(re.sub(r"[^\w:@/ ]", " ", s.lower()).split())


def _who_line(item):
    return (f"{item['id']} for {item['name']}: \"{item['text']}\" — {fmt_time(item['due'])} (in {fmt_delta(item['due'] - clock())})"
            + (f", {item['note']}" if item.get("note") else ""))


def notes_for(uids):
    """The pending reminders for these people, word for word, for her notes ('' if none)."""
    if not enabled():
        return ""
    ids = {int(u) for u in uids}
    lines = [f"- {_who_line(i)}" for i in pending() if i["uid"] in ids or i["by"] in ids][:8]
    return "\n".join(lines)


async def handle_text(text, author_id, author_name, channel_id, mention_ids=(), is_ignored=lambda u: False):
    """If this message asks her to set, list or cancel a reminder, do it and return what to say; otherwise None."""
    if not enabled():
        return None
    if is_ignored(author_id):
        return None
    now = clock()
    c = cfg()
    if CANCEL_RE.search(text):
        return _handle_cancel(text, author_id)
    if LIST_RE.search(text) and not SET_RE.search(text):
        mine = pending(author_id)
        if not mine:
            return "You've got nothing set."
        return "Your reminders:\n" + "\n".join(f"- {_who_line(i)}" for i in mine)
    if not SET_RE.search(text):
        return None

    # the model only copies out the pieces; the time itself is worked out in code
    prompt = ("Copy pieces out of this message, exactly as written, without changing or converting any words.\n"
              "who: who the reminder is for: 'me' if the speaker asks for themselves, otherwise the name they gave. Sera is who the "
              "speaker is talking to and is never the answer.\n"
              "task: what to remind them about. If no task is stated, use the event they mention (for example 'pizza arrives').\n"
              "when: the words that name the time (for example 'in 20 minutes', 'tomorrow at 3pm', 'at 310').\n"
              "offset: only if they ask for a gap before or after that time (for example 'a minute', '10 minutes'); otherwise empty.\n"
              "direction: 'before' if the reminder should come EARLIER than that time (they need time to get ready or to get there, or "
              "they say 'X before'), 'after' if it should come LATER ('X after'), otherwise 'none'.\n"
              "Examples:\n"
              "'remind me at 4 to call mom' -> when 'at 4', offset '', direction none.\n"
              "'my build finishes at 310 but I need a minute to walk back to my desk, remind me then' -> when 'at 310', offset 'a minute', "
              "direction before (they need that minute to get there, so the reminder comes a minute earlier).\n"
              "'remind me 10 minutes after the meeting ends at 5pm' -> when 'at 5pm', offset '10 minutes', direction after.\n"
              f"The speaker is {author_name}.\nMessage: {text[:500]}")
    try:
        async with env.model_lock:
            data = await asyncio.to_thread(env.extract_call, prompt, SCHEMA)
    except Exception:
        env.log("couldn't read a reminder request:\n" + traceback.format_exc())
        return "I couldn't make sense of that. Say who, what, and when."
    if not isinstance(data, dict):
        return "I couldn't make sense of that. Say who, what, and when."
    when, task, who = str(data.get("when", "")).strip(), str(data.get("task", "")).strip(" .,"), str(data.get("who", "")).strip()
    if not when or _norm(when) not in _norm(text):  # the time phrase must really be in the message: no inventing
        return "When? Give me a time, like 'in 20 minutes' or 'tomorrow at 3pm'."
    task = re.sub(r"^(?:ping|remind|tell)\s+\w+\s*(?:\.{3}|…)?\s*", "", task, flags=re.I)  # "ping Jo ... about the group" -> "about the group"
    task = re.sub(r"^(?:to|that|about|of)\s+", "", task, flags=re.I)  # "to call the dentist" -> "call the dentist"
    if not set(_norm(task).split()) - {"remind", "reminder", "me", "us", "to", "about", "please", "sera", "that", "it", "this", "thing", "something"}:
        return "Remind you about what?"

    # who it is for
    if mention_ids:
        target = int(mention_ids[0])
    elif _norm(who) in ("me", "myself", "i", "", "us", "sera", "seraphim", "you"):  # naming her means they mean themselves
        target = author_id
    else:
        target = env.resolve(who)
        if target is None:
            return f"I don't know who {who!r} is. @mention them."
    if env.is_bot(target):
        return "I don't set reminders for bots."
    if is_ignored(author_id):
        return None
    if env.refuses_favors(author_id):
        return random.choice(env.refusal_lines)
    due, why = parse_when(when, now)
    if why:
        return why
    note, offset, direction = "", str(data.get("offset", "")).strip(), str(data.get("direction", "none")).strip().lower()
    if offset and direction in ("before", "after") and _norm(offset) in _norm(text) and _duration(offset):
        gap, base = _duration(offset), due
        due, why = _check(base - gap if direction == "before" else base + gap, now)
        if why:
            return why
        note = f"{fmt_delta(gap)} {direction} {fmt_time(base)}"
    # limits
    with _lock:
        mine = pending(target)
        if len(mine) >= c["limits"]["pending_per_person"]:
            return f"{env.name_of(target)} already has {len(mine)} reminders. Cancel one first."
        if len(pending()) >= c["limits"]["pending_total"]:
            return "I've got too many reminders going right now."
        if not env.is_owner(author_id):
            recent = [i for i in _load()["items"] if i["by"] == author_id and now - i["created"] < 3600]
            if len(recent) >= c["limits"]["set_per_hour_non_owner"]:
                return "That's enough reminders from you for an hour."
    item = add(target, env.name_of(target), author_id, author_name, task, due, channel_id, request=text[:300], note=note)
    for_whom = "you" if target == author_id else item["name"]
    return (f"{random.choice(ACKS)} {item['id']}: I'll remind {for_whom} **{task}** on {fmt_time(due)} (in {fmt_delta(due - now)})"
            + (f", {note}." if note else "."))


def _handle_cancel(text, author_id):
    m = re.search(r"\bR?(\d+)\b", text, re.I)
    mine = pending(author_id)
    if re.search(r"\ball\b", text, re.I):
        n = sum(1 for i in mine if cancel(i["id"], author_id) not in (None, "no"))
        return f"Cancelled {n}." if n else "You've got nothing set."
    if m:
        rid = f"R{m.group(1)}"
        got = cancel(rid, author_id)
        if got == "no":
            return "That's not yours to cancel."
        return f"Cancelled {rid}: \"{got['text']}\"." if got else f"I don't have a pending {rid}."
    if len(mine) == 1:
        got = cancel(mine[0]["id"], author_id)
        return f"Cancelled {got['id']}: \"{got['text']}\"."
    if not mine:
        return "You've got nothing set."
    return "Which one? " + "; ".join(f"{i['id']}: {i['text']}" for i in mine)


# ------------------------------------------------------------------ the owner's view
def status_text():
    items = pending()
    if not items:
        return "**Reminders:** none pending."
    return "**Reminders:**\n" + "\n".join(f"- {_who_line(i)} (set by {i['by_name']})" for i in items[:20])


def disable_toggle(on):
    c = json.load(open(CONFIG_PATH, encoding="utf-8"))
    c["enabled"] = bool(on)
    json.dump(c, open(CONFIG_PATH, "w", encoding="utf-8"), indent=2)
    _cfg["mtime"] = None  # re-read at once: two quick changes can land in the same file-timestamp tick
