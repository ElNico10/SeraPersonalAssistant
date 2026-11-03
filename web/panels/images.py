"""The Images panel's python side: starts a picture (one at a time, while her language model is out of the GPU) and lists the gallery."""
import asyncio


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
        raise ValueError(f"unknown action {action!r}")

    web_ui.register_panel("images", "Images", handler)
