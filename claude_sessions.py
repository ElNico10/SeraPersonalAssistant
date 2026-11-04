"""Claude Code sessions that stay open: one process per model, started the first time that model is needed and kept until the owner turns
Claude off. A kept-open session doesn't start a process or send its system prompt and tool list again for every job. The price is that it
remembers earlier jobs (the context grows a little each time), so a session is replaced after a few jobs or when its context gets big."""
import json, subprocess, threading


class _Session:
    def __init__(self, popen, argv, cwd):
        self.p = popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1)
        self.jobs, self.last_total, self.busy, self.close_after, self.cancelled = 0, 0, False, False, False

    def alive(self):
        return self.p.poll() is None

    def send(self, text, timeout):
        """One job in, its result event out. Raises if the process dies, or takes longer than timeout (then it is killed)."""
        self.p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
        self.p.stdin.flush()
        timer = threading.Timer(timeout, self.p.kill)
        timer.start()
        events = []
        try:
            for line in self.p.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") == "result":
                    ev["_events"] = events  # every step of this job, so the caller can write it down before the session is wiped
                    return ev
                events.append(ev)
            raise RuntimeError("the Claude session ended before it answered")
        finally:
            timer.cancel()

    def close(self):
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.terminate()
        except Exception:
            pass


class SessionPool:
    def __init__(self, popen=subprocess.Popen):
        self.popen, self._s, self._lock = popen, {}, threading.Lock()

    def run(self, model, make_argv, cwd, text, timeout, max_jobs=30, max_context=40000, clear_first=False):
        """Run one job in model's session, opening it first if it isn't open. Returns (result event, how many jobs the session has done).
        clear_first: wipe what the session remembers before this job (the process, its prompt and its tools stay), unless it was just opened."""
        for attempt in (1, 2):
            with self._lock:
                s = self._s.get(model)
                if s and not s.alive():  # it died while idle: start over
                    self._s.pop(model, None)
                    s = None
                fresh = s is None
                if fresh:
                    s = self._s[model] = _Session(self.popen, make_argv(), cwd)
                s.busy = True
            try:
                if clear_first and not fresh and s.send("/clear", 30).get("is_error"):
                    raise RuntimeError("could not clear the session")
                ev = s.send(text, timeout)
            except Exception:
                with self._lock:
                    self._s.pop(model, None)
                    s.close()
                if fresh or attempt == 2 or s.cancelled:  # a cancelled job is not run again on a fresh session
                    raise
                continue  # a session that had been open a while failed: one try on a fresh one
            u = ev.get("usage") or {}
            with self._lock:
                s.busy = False
                s.jobs += 1
                s.last_total = sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                if s.close_after or s.jobs >= max_jobs or s.last_total > max_context:
                    self._s.pop(model, None)  # worn out, or the owner turned Claude off during this job
                    s.close()
                return ev, s.jobs

    def kill(self, model):
        """Stop whatever model's session is doing right now by ending its process; the job running in it fails and is not retried."""
        with self._lock:
            s = self._s.get(model)
            if s and s.busy:
                s.cancelled = True
                s.close()
                return True
        return False

    def status(self):
        """[(model, jobs done, busy)] for the sessions that are open."""
        with self._lock:
            return [(m, s.jobs, s.busy) for m, s in self._s.items() if s.alive()]

    def close_all(self):
        """Close every open session. One that is in the middle of a job is closed as soon as that job ends."""
        with self._lock:
            for model, s in list(self._s.items()):
                if s.busy:
                    s.close_after = True
                else:
                    self._s.pop(model, None)
                    s.close()


pool = SessionPool()
import atexit
atexit.register(lambda: [s.close() for s in list(pool._s.values())])  # never leave a Claude process running after the bot exits
