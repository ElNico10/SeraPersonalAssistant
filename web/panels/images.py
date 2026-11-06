"""The Images panel's python side: starts a picture (one at a time, while her language model is out of the GPU) and lists the gallery."""
import asyncio
import base64
import os
import shutil
import tempfile

import image_edit


def setup(web_ui, env):
    async def handler(data):
        action = data.get("action")
        if action == "list":
            return {"images": await asyncio.to_thread(env.image_list), "ready": env.image_ready()}
        if action == "examples":
            return {"examples": env.image_examples()}
        if action == "generate":
            prompt = str(data.get("prompt", "")).strip()[:4000]
            if not prompt:
                raise ValueError("write a prompt first")
            if not env.image_ready():
                raise RuntimeError("the picture model isn't installed")
            loop = asyncio.get_running_loop()

            def progress(text):
                asyncio.run_coroutine_threadsafe(web_ui.push("images", {"progress": text}), loop)

            async def run():
                try:
                    async with env.model_lock:  # she may be mid-answer: wait for her before taking the GPU
                        await web_ui.push("images", {"progress": "waiting for her to finish…"})
                        result = await asyncio.to_thread(env.image_generate, data, progress)
                    await web_ui.push("images", {"done": {"name": result["path"].replace("\\", "/").split("/")[-1], **result}})
                except Exception as e:
                    await web_ui.push("images", {"failed": str(e)})
            asyncio.create_task(run())
            return {"started": True}
        if action == "edit":
            instruction = str(data.get("prompt", "")).strip()[:2000]
            head, _, b64 = str(data.get("image", "")).partition(",")
            if not instruction or not b64:
                raise ValueError("choose a picture and say what to change")
            if not image_edit.names():
                raise RuntimeError("no editing model is installed")
            raw = base64.b64decode(b64)
            folder = tempfile.mkdtemp(prefix="sera-edit-")
            src = os.path.join(folder, "ref" + (".png" if "png" in head else ".jpg"))
            with open(src, "wb") as f:
                f.write(raw)
            loop = asyncio.get_running_loop()

            def progress(text):
                asyncio.run_coroutine_threadsafe(web_ui.push("images", {"progress": text}), loop)

            async def run_edit():
                try:
                    async with env.model_lock:
                        await web_ui.push("images", {"progress": "waiting for her to finish…"})
                        result = await asyncio.to_thread(image_edit.edit, instruction, src, data.get("model") or None, data.get("steps") or None, data.get("seed"), progress, data.get("megapixels") or None, data.get("scale") or None)
                    await web_ui.push("images", {"done": {"name": result["path"].replace("\\", "/").split("/")[-1], **result}})
                except Exception as e:
                    await web_ui.push("images", {"failed": str(e)})
                finally:
                    shutil.rmtree(folder, ignore_errors=True)
            asyncio.create_task(run_edit())
            return {"started": True}
        raise ValueError(f"unknown action {action!r}")

    web_ui.register_panel("images", "Images", handler)
