"""Runs the coding agent's Python files inside a locked-down Docker container.

It never runs them on this PC: if Docker or the image is missing it says so and runs nothing.
Each run is a fresh container that is thrown away afterwards, so nothing the code does is kept.
"""
import os
import subprocess
import threading
import time
import uuid

IMAGE = 'python:3.11-slim'
TIMEOUT = 20        # seconds of wall time per run
MAX_OUTPUT = 8000   # characters handed back to the agent
NO_WINDOW = 0x08000000


def _shorten(text, limit):
    """Keep the start and the end with a marker for what was cut: a traceback is at the end, so a plain cut hides it."""
    if len(text) <= limit:
        return text
    head = limit // 3
    return f"{text[:head]}\n[... {len(text) - limit} characters cut from the middle ...]\n{text[-(limit - head):]}"


def _remove(name):
    try:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=30, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        pass  # shortcut: if the daemon is wedged the container may linger; `docker rm -f` on sera-run-* clears it


def run_python(filename, args, agent_dir):
    script = os.path.basename(str(filename).strip())
    if not script.endswith('.py'):
        return "❌ Only .py files can be run."
    if not os.path.isfile(os.path.join(agent_dir, script)):
        return f"❌ File `{script}` does not exist."
    name = f"sera-run-{uuid.uuid4().hex[:12]}"
    cmd = ['docker', 'run', '--rm', '--name', name, '--pull', 'never',
           '--network', 'none',                           # no internet and no access to this PC's network
           '--memory', '512m', '--memory-swap', '512m',   # the scratch space below counts against this too
           '--cpus', '1', '--pids-limit', '64',           # one core, and no fork bombs
           '--read-only', '--tmpfs', '/work:rw,exec,size=64m,mode=1777', '--tmpfs', '/tmp:rw,size=16m',
           '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--user', '65534:65534',
           # the agent's folder, read-only: the code can't change or delete anything on this PC
           '--mount', f'type=bind,source={os.path.abspath(agent_dir)},target=/src,readonly',
           '-w', '/work', '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'PYTHONIOENCODING=utf-8',
           IMAGE, 'sh', '-c', 'cp -r /src/. /work/ && exec python "$@"', 'sh', script, *[str(a) for a in (args or [])]]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                creationflags=NO_WINDOW)
    except FileNotFoundError:
        return "❌ The sandbox is unavailable (Docker isn't installed), so nothing was run."

    chunks, size, flood = [], [0], threading.Event()

    def pump():
        for part in iter(lambda: proc.stdout.read(4096), b''):
            if size[0] < MAX_OUTPUT * 4:  # keep enough that _shorten can show the end as well as the start
                chunks.append(part)
            size[0] += len(part)
            if size[0] > MAX_OUTPUT * 20:  # a flood of output: stop reading and let the main thread end the run
                flood.set()
                return

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    deadline = time.monotonic() + TIMEOUT
    while proc.poll() is None and not flood.is_set() and time.monotonic() < deadline:
        time.sleep(0.05)
    stopped = proc.poll() is None
    if stopped:
        proc.kill()      # drop the output stream first, so a flood can't block the next step
        _remove(name)    # then force-remove the container (this also kills whatever is running in it)
    reader.join(5)

    out = b''.join(chunks).decode('utf-8', errors='replace')
    if 'docker API' in out or 'Cannot connect to the Docker daemon' in out or 'error during connect' in out:
        return "❌ The sandbox is unavailable (Docker isn't running), so nothing was run."
    if 'No such image' in out:
        return f"❌ The sandbox is unavailable (the `{IMAGE}` image isn't installed), so nothing was run."
    note = ''
    if stopped:
        note = "\n[stopped: too much output]" if flood.is_set() else f"\n[stopped after {TIMEOUT} seconds]"
    head = '' if stopped else f"Exit code {proc.returncode}." + (" (137 usually means it ran out of memory)" if proc.returncode == 137 else '')
    return f"{head}{note}\n{_shorten(out, MAX_OUTPUT)}".strip()


def smoke_test(agent_dir, max_files=5, budget=60, failing=None):
    """What happens when each .py file in the folder is syntax-checked and run once with no arguments.

    The script does this before the agent starts, so it begins from real errors instead of guessing, and again
    after it finishes. If `failing` is a list, it gets the name of every test file that did not run cleanly and
    every file that does not parse (a program that needs arguments printing its usage and exiting 1 is normal).
    """
    try:
        files = sorted(f for f in os.listdir(agent_dir)
                       if f.endswith('.py') and os.path.isfile(os.path.join(agent_dir, f)))
    except OSError:
        return ''
    if not files:
        return ''
    lines = ["Smoke test: before you started, each Python file here was syntax-checked and run once with no "
             "arguments in the sandbox. This is what happened:"]
    started = time.monotonic()
    for f in files[:max_files]:
        try:
            with open(os.path.join(agent_dir, f), encoding='utf-8') as fh:
                compile(fh.read(), f, 'exec')  # only parses: nothing runs on this PC
        except SyntaxError as e:
            lines.append(f"- {f}: SYNTAX ERROR: {e}")
            if failing is not None:
                failing.append(f"{f} (syntax error)")
            continue
        except Exception as e:  # unreadable, bad encoding, or too deeply nested to parse
            lines.append(f"- {f}: could not be checked ({type(e).__name__}: {e})")
            continue
        if time.monotonic() - started > budget:
            lines.append(f"- {f}: syntax OK, not run (time budget used up)")
            continue
        try:
            out = run_python(f, [], agent_dir)
        except Exception as e:
            out = f"❌ The sandbox failed: {e}"
        if out.startswith('❌ The sandbox'):
            lines.append(f"- {out}")
            break  # nothing else can run either
        if failing is not None and (f.startswith('test') or f.endswith('_test.py')) and not out.startswith('Exit code 0'):
            failing.append(f"{f} ({out.splitlines()[0][:40] if out else 'no output'})")
        lines.append(f"- {f}: syntax OK. {_shorten(out, 1500)}")
    if len(files) > max_files:
        lines.append(f"({len(files) - max_files} more files were not checked.)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------------------------------
# The general sandbox: any command, any language the image has. Same container rules as run_python (no network, no capabilities, not
# root, the agent's folder mounted read-only and copied into a throwaway scratch space), with room for a compiler to work.
GENERAL_IMAGE = 'sera-sandbox:latest'  # built from sandbox/Dockerfile: several languages. Until it exists, the Python image is used
GENERAL_TIMEOUT, GENERAL_MAX_TIMEOUT = 30, 120
GENERAL_LANGUAGES = {GENERAL_IMAGE: "python3, C/C++ (gcc, g++, make, cmake), C# (mono: mcs, mono), go, rust (rustc, cargo), java (javac, java), node (JavaScript), "
                                    "tsc (TypeScript), R (Rscript), ruby, php, lua, perl, gfortran, sqlite3, sh/bash, git, jq, curl, tidy. HTML pages: `render FILE.html` loads "
                                    "the page in a real headless browser and prints the final DOM and any console errors",
                     IMAGE: "python3 and the standard shell tools only (the multi-language image isn't built yet)"}
_image_cache = {}


def general_image():
    """The image the general sandbox uses: the multi-language one when it is installed, else the Python one. Asked of Docker at most once a minute."""
    now = time.monotonic()
    hit = _image_cache.get('v')
    if hit and now - hit[0] < 60:
        return hit[1]
    try:
        ok = subprocess.run(['docker', 'image', 'inspect', GENERAL_IMAGE], capture_output=True, timeout=20, creationflags=NO_WINDOW).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    image = GENERAL_IMAGE if ok else IMAGE
    _image_cache['v'] = (now, image)
    return image


def run_command(command, agent_dir, timeout=None):
    """Run a shell command in the sandbox with a copy of the agent's folder as the working directory. Nothing is kept: changes go to a
    throwaway scratch copy, never to the folder or this PC. Returns text the agent can read (exit code, then the output)."""
    command = str(command or '').strip()
    if not command:
        return "❌ Give a command to run."
    if len(command) > 4000:
        return "❌ That command is too long; put it in a script file and run the file."
    try:
        timeout = min(GENERAL_MAX_TIMEOUT, max(1, int(float(timeout)))) if timeout else GENERAL_TIMEOUT
    except (TypeError, ValueError):
        timeout = GENERAL_TIMEOUT
    image = general_image()
    name = f"sera-run-{uuid.uuid4().hex[:12]}"
    cmd = ['docker', 'run', '--rm', '--name', name, '--pull', 'never',
           '--network', 'none',                           # no internet and no access to this PC's network
           '--memory', '1g', '--memory-swap', '1g',       # a compiler needs more than a script; the scratch space counts against this too
           '--cpus', '2', '--pids-limit', '256',          # two cores, and no fork bombs (a build starts many processes)
           '--read-only', '--tmpfs', '/work:rw,exec,size=256m,mode=1777', '--tmpfs', '/tmp:rw,exec,size=128m,mode=1777',
           '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--user', '65534:65534',
           '--mount', f'type=bind,source={os.path.abspath(agent_dir)},target=/src,readonly',
           '-w', '/work', '-e', 'HOME=/work', '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'PYTHONIOENCODING=utf-8', '-e', 'GOCACHE=/tmp/gocache',
           '-e', 'GOPATH=/tmp/gopath', '-e', 'CARGO_HOME=/tmp/cargo', '-e', 'CARGO_TARGET_DIR=/tmp/target', '-e', 'NPM_CONFIG_CACHE=/tmp/npm',
           image, 'sh', '-c', 'cp -r /src/. /work/ 2>/dev/null; exec sh -c "$1"', 'sh', command]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
    except FileNotFoundError:
        return "❌ The sandbox is unavailable (Docker isn't installed), so nothing was run."

    chunks, size, flood = [], [0], threading.Event()

    def pump():
        for part in iter(lambda: proc.stdout.read(4096), b''):
            if size[0] < MAX_OUTPUT * 4:
                chunks.append(part)
            size[0] += len(part)
            if size[0] > MAX_OUTPUT * 20:
                flood.set()
                return

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    while proc.poll() is None and not flood.is_set() and time.monotonic() < deadline:
        time.sleep(0.05)
    stopped = proc.poll() is None
    if stopped:
        proc.kill()
        _remove(name)
    reader.join(5)

    out = b''.join(chunks).decode('utf-8', errors='replace')
    if 'docker API' in out or 'Cannot connect to the Docker daemon' in out or 'error during connect' in out:
        return "❌ The sandbox is unavailable (Docker isn't running), so nothing was run."
    if 'No such image' in out:
        return f"❌ The sandbox is unavailable (the `{image}` image isn't installed), so nothing was run."
    note = ''
    if stopped:
        note = "\n[stopped: too much output]" if flood.is_set() else f"\n[stopped after {timeout} seconds]"
    head = '' if stopped else f"Exit code {proc.returncode}." + (" (137 usually means it ran out of memory)" if proc.returncode == 137 else '')
    if not stopped and proc.returncode == 127:
        head += f" (command not found: this sandbox has {GENERAL_LANGUAGES.get(image, 'a limited set of tools')})"
    return f"{head}{note}\n{_shorten(out, MAX_OUTPUT)}".strip()
