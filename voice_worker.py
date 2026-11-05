"""Speech worker: turns audio into text (role "stt") or text into audio (role "tts").

The bot starts one of these per role and talks to it with one JSON object per line on stdin/stdout. Every engine lives in
this file and imports its heavy libraries only when it is loaded, so each engine can run in its own Python environment
(see voice_config.json). To add an engine, write a class with the same methods and add it to STT_ENGINES or TTS_ENGINES.
"""
import inspect
import json
import os
import subprocess
import sys
import tempfile
import time
import wave
from collections import OrderedDict

import numpy as np

PROTOCOL = sys.stdout
sys.stdout = sys.stderr  # engines print progress bars; only protocol lines may go to the real stdout

OUT_RATE = 48000  # what Discord wants: 48 kHz, 16-bit, stereo


def send(obj):
    PROTOCOL.write(json.dumps(obj) + "\n")  # ASCII only: accents travel as escapes, so no console encoding can garble them
    PROTOCOL.flush()


def write_discord_wav(path, samples, rate):
    """Mono float samples at any rate -> 48 kHz stereo 16-bit wav."""
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    if rate != OUT_RATE and len(x) > 1:
        n = int(round(len(x) * OUT_RATE / rate))
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    if peak > 0.99:
        x = x * (0.99 / peak)
    pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(OUT_RATE)
        w.writeframes(np.repeat(pcm[:, None], 2, axis=1).tobytes())
    return len(x) / OUT_RATE


def read_wav_mono(path):
    with wave.open(path) as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        if w.getnchannels() > 1:
            data = data.reshape(-1, w.getnchannels()).mean(axis=1)
        return data, w.getframerate()


# ---------------------------------------------------------------- speech to text
def free_vram_mib():
    """Free GPU memory right now, in MiB (None if it can't be read). Asked before loading anything onto the GPU."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout.split(",")
        return int(out[0]) - int(out[1])
    except Exception:
        return None


def require_vram(needed_mib, what):
    free = free_vram_mib()
    if free is not None and free < needed_mib:
        raise MemoryError(f"only {free} MiB of GPU memory is free and {what} needs about {needed_mib} MiB")


def add_nvidia_dll_paths():
    """pip's NVIDIA packages keep their DLLs in site-packages/nvidia/<name>/bin; Windows only finds them if told where."""
    import glob
    import site
    roots = set(site.getsitepackages() + [os.path.join(sys.prefix, "Lib", "site-packages")])
    for root in roots:
        for folder in glob.glob(os.path.join(root, "nvidia", "*", "bin")):
            os.add_dll_directory(folder)
            os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")


class FasterWhisperSTT:
    """Whisper through faster-whisper. options: model ("small", "large-v3-turbo", ...), device ("cpu" or "cuda"), compute_type, cpu_threads
    (on the CPU: how many cores it may use, so reading what people said while she talks doesn't fight her own CPU layers).
    A GPU that fails its self-test falls back to the CPU, so a bad driver or a missing library never leaves her deaf."""

    def __init__(self, model="small", device="cpu", compute_type=None, cpu_threads=0, beam_size=5, **_):
        from faster_whisper import WhisperModel
        self.device, self.beam_size = device, beam_size  # beam_size 1 = greedy decoding: about twice as fast, slightly less exact
        if device == "cuda":
            add_nvidia_dll_paths()
            try:
                require_vram(1200, "Whisper")
                self.model = WhisperModel(model, device="cuda", compute_type=compute_type or "float16")
                list(self.model.transcribe(np.zeros(16000, dtype=np.float32), language="en")[0])  # forces the GPU code to run now
                return
            except Exception as e:
                print(f"[worker] Whisper on the GPU failed ({type(e).__name__}: {e}); using the CPU instead", file=sys.stderr, flush=True)
                self.device = "cpu (GPU failed)"
        self.model = WhisperModel(model, device="cpu", compute_type="int8" if device == "cuda" else (compute_type or "int8"), cpu_threads=cpu_threads)

    def transcribe(self, path, language=None, hints=None, bounds=None):
        """One clip: its text. Several clips joined into one file (bounds = [[start, end], ...] in seconds): one text per clip,
        found by giving each word to the clip it was spoken in. Whisper always works on 30-second windows, so five 3-second
        clips cost five passes apart and about one together."""
        kwargs = dict(language=None if language in (None, "", "auto") else language, beam_size=self.beam_size, vad_filter=True, initial_prompt=hints or None)
        if not bounds:
            segments, info = self.model.transcribe(path, **kwargs)
            return " ".join(s.text.strip() for s in segments).strip(), info.language
        segments, info = self.model.transcribe(path, word_timestamps=True, **kwargs)
        words = [[] for _ in bounds]
        for seg in segments:
            for w in seg.words or []:
                mid = (w.start + w.end) / 2
                # the clip whose span is nearest to the middle of the word (a word never falls in the silence between, but may sit on its edge)
                nearest = min(range(len(bounds)), key=lambda i: 0 if bounds[i][0] <= mid <= bounds[i][1] else min(abs(mid - bounds[i][0]), abs(mid - bounds[i][1])))
                words[nearest].append(w.word)
        return ["".join(ws).strip() for ws in words], info.language


STT_ENGINES = {"faster_whisper": FasterWhisperSTT}


# ---------------------------------------------------------------- text to speech
class SapiTTS:
    """Windows' built-in voices: robotic, but nothing to download. voice: a name such as "Microsoft Zira Desktop"."""

    def __init__(self, **_):
        pass

    def speak(self, text, voice=None, language=None, rate=0, **_):
        out = os.path.join(tempfile.gettempdir(), f"sapi-{os.getpid()}-{time.time_ns()}.wav")
        script = (
            "Add-Type -AssemblyName System.Speech;"
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            "if ($env:SERA_VOICE) { try { $s.SelectVoice($env:SERA_VOICE) } catch {} };"
            "$s.Rate = [int]$env:SERA_RATE;"
            "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(22050, 'Sixteen', 'Mono');"
            "$s.SetOutputToWaveFile($env:SERA_OUT, $fmt);"
            "$s.Speak($env:SERA_TEXT); $s.Dispose()")
        env = {**os.environ, "SERA_TEXT": text, "SERA_VOICE": voice or "", "SERA_RATE": str(rate), "SERA_OUT": out}
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env=env, check=True,
                       capture_output=True, timeout=60)
        try:
            samples, sr = read_wav_mono(out)
        finally:
            if os.path.exists(out):
                os.remove(out)
        return samples, sr


class ChatterboxTurboTTS:
    """Resemble's Chatterbox Turbo (English). voice: path to a reference wav to copy, or empty for the built-in voice.
    options: device ("cpu" or "cuda"); precision (cuda only): "mixed" (default; the speech-token model in bfloat16, the audio
    decoder in full precision: 2.5 GB of VRAM, tested intelligible) or "full" (3.3 GB). Plain float16 garbled the speech.
    A GPU without enough free memory, or that fails to load, falls back to the CPU."""

    # Loading is two parts so they can overlap with other work. STAGING reads the model into memory on the CPU: it needs no GPU memory, so it can
    # run while her own language model is still being moved off the GPU to make room. FINISHING moves it onto the GPU and runs one line to warm
    # it up. stage_only=True does just the first part; the bot then asks for "load" once the GPU has room, which does the second.
    needs_finish = False

    def __init__(self, device="cpu", precision="mixed", stage_only=False, **_):
        t = time.time()
        import torch
        from chatterbox.tts_turbo import ChatterboxTurboTTS as Model
        self.torch, self.Model, self.device, self.precision, self.mixed, self.model = torch, Model, device, precision, False, None
        imported = time.time() - t
        if device != "cuda":
            torch.set_num_threads(max(1, (os.cpu_count() or 8) - 2))
            self.model = Model.from_pretrained(device="cpu")
            return
        self.model = Model.from_pretrained(device="cpu")  # on the CPU first, so nothing oversized ever touches the GPU
        self.needs_finish = True
        print(f"[worker] chatterbox_turbo staged on the CPU: imports {imported:.1f}s, weights {time.time() - t - imported:.1f}s", file=sys.stderr, flush=True)
        if not stage_only:
            self.finish()

    def finish(self):
        """Move the staged model onto the GPU and warm it up. A GPU without room, or one that fails, falls back to the CPU."""
        if not self.needs_finish:
            return
        self.needs_finish = False
        torch, t = self.torch, time.time()
        try:
            require_vram(2900 if self.precision == "mixed" else 3700, "the voice")
            model = self.model
            if self.precision == "mixed":
                model.t3.to(torch.bfloat16)
            for name in ("t3", "s3gen", "ve"):
                getattr(model, name).to("cuda")
            model.device = "cuda"
            model.conds = model.conds.to("cuda")
            self.mixed = self.precision == "mixed"
            moved = time.time() - t
            self.speak("Warming up the voice.")  # the first run on a GPU spends 20 seconds setting itself up
            print(f"[worker] chatterbox_turbo on the GPU: move {moved:.1f}s, warm-up {time.time() - t - moved:.1f}s", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"[worker] Chatterbox on the GPU failed ({type(e).__name__}: {e}); using the CPU instead", file=sys.stderr, flush=True)
            self.device, self.mixed, self.model = "cpu (GPU failed)", False, None
            torch.cuda.empty_cache()
            torch.set_num_threads(max(1, (os.cpu_count() or 8) - 2))
            self.model = self.Model.from_pretrained(device="cpu")

    def speak(self, text, voice=None, language=None, temperature=None, **opts):
        import contextlib
        if self.needs_finish:  # staged but never finished (nobody asked for "load"): do it now rather than fail
            self.finish()
        kwargs = {"audio_prompt_path": voice} if voice else {}
        if temperature is not None:  # lower = steadier, less varied delivery (its default is 0.8)
            kwargs["temperature"] = temperature
        with (self.torch.autocast("cuda", dtype=self.torch.bfloat16) if self.mixed else contextlib.nullcontext()):
            wav = self.model.generate(text, **kwargs)
        return wav.squeeze().float().cpu().numpy(), self.model.sr


class ChatterboxMultilingualTTS:
    """Chatterbox Multilingual (23+ languages, English and Spanish included). language: a code such as "es"."""

    def __init__(self, device="cpu", **_):
        import torch
        torch.set_num_threads(max(1, (os.cpu_count() or 8) - 2))
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS as Model
        self.model = Model.from_pretrained(device=device)

    def speak(self, text, voice=None, language="en", exaggeration=0.5, cfg_weight=0.5, temperature=None, **opts):
        kwargs = {"audio_prompt_path": voice} if voice else {}
        if temperature is not None:
            kwargs["temperature"] = temperature
        wav = self.model.generate(text, language_id=language or "en", exaggeration=exaggeration, cfg_weight=cfg_weight, **kwargs)
        return wav.squeeze().cpu().numpy(), self.model.sr


TTS_ENGINES = {"sapi": SapiTTS, "chatterbox_turbo": ChatterboxTurboTTS, "chatterbox_multilingual": ChatterboxMultilingualTTS}  # a GPU or CPU copy of an engine is just another entry in voice_config.json


# ---------------------------------------------------------------- the loop
MAX_LOADED = int(os.environ.get("SERA_VOICE_MAX_LOADED", "2"))  # engines kept in memory at once; the oldest is dropped
loaded = OrderedDict()


def get_engine(registry, name, options, stage_only=False):
    key = (name, json.dumps(options or {}, sort_keys=True))
    if key in loaded:
        loaded.move_to_end(key)
        return loaded[key]
    if name not in registry:
        raise ValueError(f"unknown engine {name!r}; this worker has: {', '.join(registry)}")
    while len(loaded) >= MAX_LOADED:
        loaded.popitem(last=False)
    t = time.time()
    extra = {"stage_only": True} if stage_only and "stage_only" in inspect.signature(registry[name].__init__).parameters else {}
    loaded[key] = registry[name](**(options or {}), **extra)
    print(f"[worker] loaded {name} in {time.time() - t:.1f}s" + (" (staged: the GPU part comes with 'load')" if extra else ""), file=sys.stderr, flush=True)
    return loaded[key]


def handle(role, req):
    registry = STT_ENGINES if role == "stt" else TTS_ENGINES
    op = req["op"]
    if op == "ping":
        return {"echo": req.get("text")}  # a health check that also proves text arrives unscrambled
    if op == "prewarm":  # the part of loading that needs no GPU memory, so it can overlap with freeing some; engines without one load fully
        engine = get_engine(registry, req["engine"], req.get("options"), stage_only=True)
        return {"staged": bool(getattr(engine, "needs_finish", False))}
    engine = get_engine(registry, req["engine"], req.get("options"))
    if op == "load":
        if getattr(engine, "needs_finish", False):
            engine.finish()
        return {"device": getattr(engine, "device", None)}
    t = time.time()
    if op == "transcribe":
        text, lang = engine.transcribe(req["path"], language=req.get("language"), hints=req.get("hints"), bounds=req.get("bounds"))
        return {("texts" if req.get("bounds") else "text"): text, "language": lang, "seconds": time.time() - t}
    if op == "speak":
        samples, rate = engine.speak(req["text"], voice=req.get("voice"), language=req.get("language"), **(req.get("params") or {}))
        audio_seconds = write_discord_wav(req["out"], samples, rate)
        return {"audio_seconds": audio_seconds, "seconds": time.time() - t}
    raise ValueError(f"unknown op {op!r}")


def main():
    role = sys.argv[1]
    send({"ready": True, "role": role})
    for line in sys.stdin:
        if not line.strip():
            continue
        req = {}
        try:
            req = json.loads(line)
            send({"id": req.get("id"), "ok": True, **handle(role, req)})
        except Exception as e:  # one bad request must not kill the worker
            import traceback
            print(traceback.format_exc(), file=sys.stderr, flush=True)
            send({"id": req.get("id"), "ok": False, "error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()
