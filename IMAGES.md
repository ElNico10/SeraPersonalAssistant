# Sera's pictures

Model: Chroma1-HD (fp8), run by ComfyUI in `image_gen/`. Settings: `image_gen_config.json` (re-read on every picture).

## Where
- **Web page** (`http://127.0.0.1:8765/`, link printed when the bot starts): the **Images** tab, or just ask her in the **Chat** tab.
- **Discord** (off until you switch it on): `!image <what to draw>`, or ask her in plain words in the owner's chat.
- Saved in `SeraWorkspace\images\`: `<date>-<time>-<seed>.png` plus a `.json` with the prompt and settings.

## Discord switch
Shipped **off**. `!image on` / `!image off` (owner only) flips `discord.enabled` in `image_gen_config.json`.
While it's off, the owner is told so, everyone else gets no reply, and she isn't offered the draw tool in Discord chats.
`discord.owner_only` (default true) keeps it to you. If you open it up: `cooldown_seconds` between one person's pictures and
`max_waiting` queued at once apply to everyone but you.

## Examples
`!image examples` (Discord or the web chat) and the **Examples** section of the Images tab show pictures with the exact prompts and options
that made them, and a tip for each. They are fixed files in `image_examples/` (`examples.json` plus small JPEGs): no model runs to show them.
Add or edit entries there.

## What she won't draw (`image_safety.py`)
Checked before anything is unloaded or started, on every route: the web page, `!image`, and her own draw tool.
- **Always on:** sexual or nude content involving anyone who is, looks or is described as under 18. Caught by word and age patterns, and again by
  her model for rewordings. The switch below does not touch it, and its terms go into every picture's avoid list.
- **Switchable** (`!image safety on|off`, owner only; categories in `image_gen_config.json` -> `safety.categories`): real, identifiable people,
  sexual violence or coercion, extreme gore (off by default). Plus `safety.avoid`, extra words added to every picture's avoid list.
- If her model can't be asked, nothing is drawn (`safety.fail_closed`). Her refusal says why and never repeats the request.
- Limits: it works on the request, not the finished picture, and a determined rewording can slip past any filter.

## `!image` options
`--portrait` (832x1216), `--landscape` (1216x832), `--square` (1024x1024, the default), `--seed <n>`, `--steps <n>`.

## The GPU
Her language model and the picture model don't fit in the card together. A picture unloads her model, draws, and lets go of the
GPU; she loads back on her next message. A picture waits while she is mid-answer. ComfyUI stops itself after 5 idle minutes.

## Models
The picture model is chosen in `~/.sera/models.json`, under `"image"`: `unet`, `clip` and `vae` are file names inside
`image_gen/ComfyUI/models/` (`diffusion_models` or `unet`, `clip`, `vae`). The author uses `Chroma1-HD-fp8_scaled_rev2.safetensors`, `t5xxl_fp8_e4m3fn.safetensors`
and `chroma_vae.safetensors`, and keeps a slower `Chroma1-HD-Q6_K.gguf` build as a fallback. With no `image` entry in `models.json`, pictures stay off.
`image_gen.py` builds its ComfyUI workflow for Chroma-style models, so a very different picture model would need that function adapted.
