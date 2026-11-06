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
    g = {
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
    return g


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


def _seedvr_graph(image, up):
    """SeedVR2 (ComfyUI's own nodes): resize to the target size, pad, encode (tiled), one diffusion step, decode (tiled), match the colours."""
    s, t = up["seedvr2"], up["seedvr2"].get("tile_size", 512)
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": image}},
        "2": {"class_type": "ImageScaleBy", "inputs": {"image": ["1", 0], "upscale_method": "lanczos", "scale_by": up["scale"]}},
        "3": {"class_type": "SeedVR2Preprocess", "inputs": {"resized_images": ["2", 0]}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": s["vae"]}},
        "5": {"class_type": "UNETLoader", "inputs": {"unet_name": s["unet"], "weight_dtype": "default"}},
        "6": {"class_type": "VAEEncodeTiled", "inputs": {"pixels": ["3", 0], "vae": ["4", 0], "tile_size": t, "overlap": 128, "temporal_size": 4096, "temporal_overlap": 8}},
        "7": {"class_type": "SeedVR2Conditioning", "inputs": {"model": ["5", 0], "vae_conditioning": ["6", 0]}},
        "8": {"class_type": "KSampler", "inputs": {"model": ["5", 0], "seed": up.get("seed", 0), "steps": 1, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple",
                                                  "positive": ["7", 0], "negative": ["7", 1], "latent_image": ["6", 0], "denoise": 1.0}},
        "9": {"class_type": "VAEDecodeTiled", "inputs": {"samples": ["8", 0], "vae": ["4", 0], "tile_size": t, "overlap": 128, "temporal_size": 4096, "temporal_overlap": 8}},
        "10": {"class_type": "SeedVR2PostProcessing", "inputs": {"images": ["9", 0], "original_resized_images": ["2", 0], "color_correction_method": s.get("color_correction", "lab")}},
        "11": {"class_type": "SaveImage", "inputs": {"images": ["10", 0], "filename_prefix": "sera-edit"}},
    }


def _upscale_graph(image, up):
    """Stage two: the finished edit, made bigger. SeedVR2 by default; SwinIR (it does 4x; a smaller scale is that result shrunk back) if chosen."""
    if up["backend"] == "seedvr2":
        return _seedvr_graph(image, up)
    g = {
        "1": {"class_type": "LoadImage", "inputs": {"image": image}},
        "2": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": up["model"]}},
        "3": {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}},
    }
    last = ["3", 0]
    if abs(up["by"] - 1.0) > 0.01:
        g["4"] = {"class_type": "ImageScaleBy", "inputs": {"image": ["3", 0], "upscale_method": "lanczos", "scale_by": up["by"]}}
        last = ["4", 0]
    g["5"] = {"class_type": "SaveImage", "inputs": {"images": last, "filename_prefix": "sera-edit"}}
    return g


def _run(graph, label, started, timeout, ceiling, hit, peak, say):
    """Queue one graph, wait for it, return the path of the picture it saved. Raises RuntimeError with a plain reason."""
    try:
        queued = image_gen._post("/prompt", {"prompt": graph, "client_id": uuid.uuid4().hex})
    except urllib.error.HTTPError as err:
        raise RuntimeError("ComfyUI rejected the request: " + err.read().decode("utf-8", "replace")[:600])
    pid, last = queued["prompt_id"], ""
    stage = time.time()
    while True:
        time.sleep(1)
        hist = image_gen._get(f"/history/{pid}").get(pid)
        if hist:
            break
        if hit.is_set() and time.time() - stage > 2:
            image_gen.stop()  # the interrupt only lands between steps; if memory is already over, end it now
            raise RuntimeError(f"stopped: it passed the {ceiling} MiB graphics memory limit (peak {peak[0]} MiB)")
        if image_gen._proc is not None and image_gen._proc.poll() is not None:
            raise RuntimeError("ComfyUI crashed while editing; see image_gen/comfyui.log")
        if time.time() - started > timeout:
            image_gen.stop()
            raise RuntimeError("it took too long and was stopped")
        running = ((image_gen._get("/queue").get("queue_running") or [[]])[0] or [None])
        note = label if running and running[0] is not None else "waiting its turn…"
        if note != last:
            say(note); last = note
    status = hist.get("status", {})
    if hit.is_set():
        raise RuntimeError(f"stopped: it passed the {ceiling} MiB graphics memory limit (peak {peak[0]} MiB)")
    if status.get("status_str") == "error":
        msgs = [x[1].get("exception_message", "") for x in status.get("messages", []) if x[0] == "execution_error"]
        raise RuntimeError("ComfyUI failed: " + (msgs[0] if msgs else "unknown error")[:600])
    img = next(i for node in hist["outputs"].values() for i in node.get("images", []))
    return os.path.join(image_gen.COMFY_DIR, "output", img.get("subfolder", ""), img["filename"])


def edit(instruction, image_path, model=None, steps=None, seed=None, progress=None, megapixels=None, scale=None):
    """Edit one picture. Returns {'path', 'seed', 'seconds', 'peak_mb', 'model', 'size', 'scale'}. Raises RuntimeError with a plain reason.
    instruction=None means no edit at all: the picture is only made bigger and sharper (scale defaults to upscale_only_scale)."""
    c = image_gen.cfg()
    e = c.get("edit", {})
    u = e.get("upscale", {})
    only_up = instruction is None
    if only_up:
        if not u.get("enabled"):
            raise RuntimeError("upscaling is switched off (edit.upscale.enabled in image_gen_config.json)")
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        w, h = Image.open(image_path).size
        model, m, base_mp = "upscale", {}, w * h / 1e6
    else:
        model = model or e.get("default", "klein")
        if model not in GRAPHS or not available(model):
            raise RuntimeError(f"the edit model '{model}' isn't set up (available: {', '.join(names()) or 'none'})")
        m = dict(e["models"][model])
        if megapixels:  # asked for in this request; kept inside the range so it can't be made to overrun the card
            m["megapixels"] = min(max(float(megapixels), e.get("min_megapixels", 0.05)), e.get("max_megapixels", 3.0))
        base_mp = m["megapixels"]
    wanted = float(scale) if scale else float(u.get("upscale_only_scale", 4) if only_up else u.get("default_scale", 1))
    eff = min(wanted, u.get("max_scale", 4), (u.get("max_output_megapixels", 24) / base_mp) ** 0.5) if u.get("enabled") else 1.0
    if only_up and eff < 1.05:
        raise RuntimeError(f"that picture is already {base_mp:.0f} megapixels, at the {u.get('max_output_megapixels', 24)} megapixel limit, so there's no room to enlarge it")
    up = None
    if eff > 1.05:
        backend = u.get("backend", "swinir")
        models = os.path.join(image_gen.COMFY_DIR, "models")
        if backend == "seedvr2":
            sv = u.get("seedvr2", {})
            have = os.path.exists(os.path.join(models, "diffusion_models", sv.get("unet", "-"))) and os.path.exists(os.path.join(models, "vae", sv.get("vae", "-")))
        else:
            have = os.path.exists(os.path.join(models, "upscale_models", u.get("model", "-")))
        if not have:
            raise RuntimeError(f"the upscaling model ({backend}) isn't installed (see edit.upscale in image_gen_config.json)")
        up = {"backend": backend, "model": u.get("model"), "by": eff / u.get("native_scale", 4), "scale": eff, "seedvr2": u.get("seedvr2"), "seed": 0}
    say = progress or (lambda t: None)
    seed = int(seed) if seed not in (None, "", -1) else random.randrange(2 ** 32)
    steps = 0 if only_up else int(min(max(steps or m["steps"], 1), m.get("max_steps", 50)))
    vram_guard.require("edit_" + model)  # before anything is unloaded
    if not only_up:
        say("checking the request…")
        image_safety.check(instruction, getattr(image_gen.env, "safety_ask", None))
    ceiling = e.get("ceiling_mb", 11800)
    with image_gen._lock:
        started = time.time()
        say("freeing her language model…")
        image_gen.env.free_gpu()
        say("starting the picture model…")
        image_gen._start()
        inputs = os.path.join(image_gen.COMFY_DIR, "input")
        name = f"sera-ref-{uuid.uuid4().hex[:8]}{os.path.splitext(image_path)[1] or '.png'}"
        mid = None
        shutil.copyfile(image_path, os.path.join(inputs, name))
        stop, hit, peak = threading.Event(), threading.Event(), [0]
        threading.Thread(target=_watch, args=(ceiling, stop, hit, peak), daemon=True).start()
        try:
            src = None if only_up else _run(GRAPHS[model](m, instruction, name, seed, steps), "editing…", started, c["timeout_seconds"], ceiling, hit, peak, say)
            if up:
                say("making it bigger and sharper…")
                if only_up:
                    stage_in = name  # nothing to edit: the attached picture goes straight to the upscaler
                else:  # the editing model's memory is let go first, so the upscaler has the whole card
                    image_gen._post("/free", {"unload_models": True, "free_memory": True})
                    mid = f"sera-mid-{uuid.uuid4().hex[:8]}.png"
                    shutil.move(src, os.path.join(inputs, mid))
                    stage_in = mid
                src = _run(_upscale_graph(stage_in, up), "enlarging…", started, c["timeout_seconds"], ceiling, hit, peak, say)
            os.makedirs(image_gen.env.images_dir, exist_ok=True)
            dest = os.path.join(image_gen.env.images_dir, time.strftime("%Y%m%d-%H%M%S") + f"-edit-{seed}.png")
            os.replace(src, dest)
            if e.get("enlarge_small", True):
                try:
                    from PIL import Image
                    im = Image.open(dest)
                    k = -(-1024 // max(im.size))  # whole-number factor, so every pixel becomes an exact block
                    if k > 1:
                        im.resize((im.width * k, im.height * k), Image.NEAREST).save(dest)
                except Exception:
                    pass  # the small picture is still a fine result
            with open(dest + ".json", "w", encoding="utf-8") as f:
                json.dump({"prompt": instruction or "", "edit_model": model, "megapixels": m.get("megapixels", round(base_mp, 2)), "scale": round(eff, 2) if up else 1, "steps": steps, "seed": seed, "peak_mb": peak[0]}, f, indent=1)
            size = ""
            try:
                from PIL import Image
                Image.MAX_IMAGE_PIXELS = None
                size = "%dx%d" % Image.open(dest).size
            except Exception:
                pass
            return {"path": dest, "seed": seed, "seconds": round(time.time() - started, 1), "peak_mb": peak[0], "model": model, "size": size,
                    "scale": round(eff, 2) if up else 1}
        finally:
            stop.set()
            for f in (name, mid):
                try:
                    if f:
                        os.remove(os.path.join(inputs, f))
                except OSError:
                    pass
            say("letting go of the GPU…")
            try:
                image_gen._post("/free", {"unload_models": True, "free_memory": True})
            except OSError:
                image_gen.stop()
            image_gen._last_used = time.time()
