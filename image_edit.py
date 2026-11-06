"""Editing a picture from an instruction ("make it night", "remove the hat"), with a reference image. Chroma can't do this; an edit model can.
Runs in the same ComfyUI helper process as image_gen.py (it borrows that module's start/stop/lock), blocking code for a worker thread.

Model (named in image_gen_config.json under "edit"; files are in image_gen/ComfyUI/models/): klein = FLUX.2 Klein 4B, 4 steps.
Qwen-Image-Edit-2511 (Q4_0) was tried and removed: it drew backwards legs and garbled figures. GRAPHS is where another model goes.

Memory protection, two layers: (1) vram_guard refuses to START an edit when the card hasn't got room; (2) while it runs, the card is
polled every second and the edit is interrupted and ComfyUI stopped if usage passes edit.ceiling_mb. Windows lets a card overflow into
shared memory instead of failing, which would be slow rather than a clear error, so this stops it first.
"""
import json
import os
import random
import shutil
import threading
import time
import urllib.error
import uuid

import image_gen
import image_safety
import vram_guard


def _edit_cfg():
    return image_gen.cfg().get("edit", {})


def available(model):
    m = _edit_cfg().get("models", {}).get(model)
    if not m or not image_gen.available():
        return False
    models = os.path.join(image_gen.COMFY_DIR, "models")
    return all(os.path.exists(os.path.join(models, sub, name)) for sub, name in m["files"])


def names():
    return [k for k in _edit_cfg().get("models", {}) if available(k)]


def _klein(m, text, image, seed, steps):
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": m["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": m["clip"], "type": "flux2"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": m["vae"]}},
        "4": {"class_type": "LoadImage", "inputs": {"image": image}},
        "5": {"class_type": "ImageScaleToTotalPixels", "inputs": {"image": ["4", 0], "upscale_method": "lanczos", "megapixels": m["megapixels"], "resolution_steps": 16}},
        "6": {"class_type": "VAEEncode", "inputs": {"pixels": ["5", 0], "vae": ["3", 0]}},
        "7": {"class_type": "GetImageSize", "inputs": {"image": ["5", 0]}},
        "8": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": text}},
        "9": {"class_type": "ReferenceLatent", "inputs": {"conditioning": ["8", 0], "latent": ["6", 0]}},
        "10": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["8", 0]}},
        "11": {"class_type": "ReferenceLatent", "inputs": {"conditioning": ["10", 0], "latent": ["6", 0]}},
        "12": {"class_type": "CFGGuider", "inputs": {"model": ["1", 0], "positive": ["9", 0], "negative": ["11", 0], "cfg": m["cfg"]}},
        "13": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "14": {"class_type": "Flux2Scheduler", "inputs": {"steps": steps, "width": ["7", 0], "height": ["7", 1]}},
        "15": {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": ["7", 0], "height": ["7", 1], "batch_size": 1}},
        "16": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "17": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["16", 0], "guider": ["12", 0], "sampler": ["13", 0], "sigmas": ["14", 0], "latent_image": ["15", 0]}},
        "18": {"class_type": "VAEDecode", "inputs": {"samples": ["17", 0], "vae": ["3", 0]}},
        "19": {"class_type": "SaveImage", "inputs": {"images": ["18", 0], "filename_prefix": "sera-edit"}},
    }


GRAPHS = {"klein": _klein}


def _watch(ceiling_mb, stop, hit, peak):
    """Poll the card; past the ceiling, tell ComfyUI to interrupt (and let the caller stop it if that isn't enough)."""
    while not stop.wait(1):
        gpu = vram_guard.gpu_mb()
        if gpu:
            peak[0] = max(peak[0], gpu[1])
            if gpu[1] > ceiling_mb and not hit.is_set():
                hit.set()
                try:
                    image_gen._post("/interrupt", {})
                except OSError:
                    pass


def edit(instruction, image_path, model=None, steps=None, seed=None, progress=None):
    """Edit one picture. Returns {'path', 'seed', 'seconds', 'peak_mb', 'model'}. Raises RuntimeError with a plain reason."""
    c = image_gen.cfg()
    e = c.get("edit", {})
    model = model or e.get("default", "klein")
    if model not in GRAPHS or not available(model):
        raise RuntimeError(f"the edit model '{model}' isn't set up (available: {', '.join(names()) or 'none'})")
    m = e["models"][model]
    say = progress or (lambda t: None)
    seed = int(seed) if seed not in (None, "", -1) else random.randrange(2 ** 32)
    steps = int(min(max(steps or m["steps"], 1), m.get("max_steps", 50)))
    vram_guard.require("edit_" + model)  # before anything is unloaded
    say("checking the request…")
    image_safety.check(instruction, getattr(image_gen.env, "safety_ask", None))
    ceiling = e.get("ceiling_mb", 11800)
    with image_gen._lock:
        started = time.time()
        say("freeing her language model…")
        image_gen.env.free_gpu()
        say("starting the picture model…")
        image_gen._start()
        name = f"sera-ref-{uuid.uuid4().hex[:8]}{os.path.splitext(image_path)[1] or '.png'}"
        ref = os.path.join(image_gen.COMFY_DIR, "input", name)
        shutil.copyfile(image_path, ref)
        stop, hit, peak = threading.Event(), threading.Event(), [0]
        threading.Thread(target=_watch, args=(ceiling, stop, hit, peak), daemon=True).start()
        try:
            try:
                queued = image_gen._post("/prompt", {"prompt": GRAPHS[model](m, instruction, name, seed, steps), "client_id": uuid.uuid4().hex})
            except urllib.error.HTTPError as err:
                raise RuntimeError("ComfyUI rejected the request: " + err.read().decode("utf-8", "replace")[:600])
            pid, last = queued["prompt_id"], ""
            while True:
                time.sleep(1)
                hist = image_gen._get(f"/history/{pid}").get(pid)
                if hist:
                    break
                if hit.is_set() and time.time() - started > 2:
                    image_gen.stop()  # the interrupt only lands between steps; if memory is already over, end it now
                    raise RuntimeError(f"stopped: it passed the {ceiling} MiB graphics memory limit (peak {peak[0]} MiB)")
                if image_gen._proc is not None and image_gen._proc.poll() is not None:
                    raise RuntimeError("ComfyUI crashed while editing; see image_gen/comfyui.log")
                if time.time() - started > c["timeout_seconds"]:
                    image_gen.stop()
                    raise RuntimeError("it took too long and was stopped")
                running = ((image_gen._get("/queue").get("queue_running") or [[]])[0] or [None])
                note = "editing…" if running and running[0] is not None else "waiting its turn…"
                if note != last:
                    say(note); last = note
            status = hist.get("status", {})
            if hit.is_set():
                raise RuntimeError(f"stopped: it passed the {ceiling} MiB graphics memory limit (peak {peak[0]} MiB)")
            if status.get("status_str") == "error":
                msgs = [x[1].get("exception_message", "") for x in status.get("messages", []) if x[0] == "execution_error"]
                raise RuntimeError("ComfyUI failed: " + (msgs[0] if msgs else "unknown error")[:600])
            img = next(i for node in hist["outputs"].values() for i in node.get("images", []))
            src = os.path.join(image_gen.COMFY_DIR, "output", img.get("subfolder", ""), img["filename"])
            os.makedirs(image_gen.env.images_dir, exist_ok=True)
            dest = os.path.join(image_gen.env.images_dir, time.strftime("%Y%m%d-%H%M%S") + f"-edit-{seed}.png")
            os.replace(src, dest)
            with open(dest + ".json", "w", encoding="utf-8") as f:
                json.dump({"prompt": instruction, "edit_model": model, "steps": steps, "seed": seed, "peak_mb": peak[0]}, f, indent=1)
            return {"path": dest, "seed": seed, "seconds": round(time.time() - started, 1), "peak_mb": peak[0], "model": model}
        finally:
            stop.set()
            try:
                os.remove(ref)
            except OSError:
                pass
            say("letting go of the GPU…")
            try:
                image_gen._post("/free", {"unload_models": True, "free_memory": True})
            except OSError:
                image_gen.stop()
            image_gen._last_used = time.time()
