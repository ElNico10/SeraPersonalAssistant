# Sera

A local Discord bot with a personality, memory, mood, voice-call support, image generation and coding agents. Her brain is a local
model run through Ollama, so what people say to her is not sent to a hosted AI service. The one optional exception is the owner-only
Claude coding agent, which sends its coding task to Anthropic through the Claude Code CLI and your own Claude plan. (Discord itself, of
course, sees the chat.)

> Built for one owner on one Windows PC (RTX 5070, 12 GB). Nothing that makes her *someone* is in this repo: her persona, canned
> replies, model choices, keys and web token live in `~/.sera/` (your user folder), and her workspace is `~/Documents/SeraWorkspace`
> (or the folder named by the `SERA_WORKSPACE` environment variable). Without a persona file she runs on a polite default.

## Models

**No model is built into the code.** Which models she runs is set in `~/.sera/models.json` (see Setup), and any others can be used
without changing the code. The chat and coding models can be any Ollama model that supports tools; Claude's are model ids for the
Claude Code CLI; the picture model is a set of ComfyUI files (the picture workflow in `image_gen.py` is built for Chroma-style
models, so a very different picture model would need that one function adapted). If a model is not set, the feature that needs it
says so and stays off, and she will not start without a chat model.

What follows is what the author runs, and why. The chat and picture models are uncensored on purpose: the bot lives among friends
who trust each other, and it needs to keep up with their inside jokes and conversations. It is not left unchecked: the owner can
make her ignore or restrict a person, pictures go through a safety filter, the tools that touch the host are guarded in code, and the
default prompt that ships with the code has its own safety rules. Nothing here requires an uncensored model.

| Job | Model | Where it runs |
|---|---|---|
| Chat, intent checks, planning | `igorls/gemma-4-12B-it-heretic-GGUF` (7.4 GB on disk) | Ollama, GPU. 32,768-token context, unloads 2 min after the last call |
| Default coding agent (public) | `qwen3.6:35b-a3b-coding` (22 GB on disk) | Ollama |
| Coding agent (owner only, `!agent claude`) | Claude Haiku 5.5 / Sonnet 5.5 / Opus 5.5, picked per task or by name | Claude Code CLI |
| Speech to text | faster-whisper `small` | CPU, 4 threads |
| Text to speech | Chatterbox Turbo (`mixed` precision) | GPU; multilingual variant on CPU for other languages |
| Pictures | Chroma1-HD fp8 + T5-XXL fp8 through ComfyUI | GPU, stops after 5 idle minutes |

Other models are installed in Ollama (Nemotron, DeepSeek-R1, Phi-4 mini, a 4B Qwen) but the bot does not call them.

### Fitting it all on one 12 GB card

| Change | Effect |
|---|---|
| Chat model run at Q4_K_M (11.9B parameters) with the context capped at 32,768 tokens instead of the model's default 131,072 | The default pre-reserves a huge KV cache in VRAM. Measured: about 8.3 GB above the desktop baseline |
| Voice model (Chatterbox Turbo) in **mixed precision**: speech-token model in bfloat16, audio decoder in full precision | 2.5 GB of VRAM instead of 3.3 GB for full precision. Plain float16 was tried and garbled the speech, so it is not used |
| Speech recognition (faster-whisper `small`) on the CPU with int8 | Keeps about 1.2 GB of VRAM free; the GPU path needs that much and is available as a setting |
| Picture model (Chroma1-HD) as fp8, with the T5-XXL encoder also fp8 | Fits the card; a slower Q6_K build is kept as a fallback |
| During a voice call, 44 of the chat model's 48 layers sit on the GPU (`call.sera_gpu_layers` in `voice_config.json`); the rest run from system RAM. All layers return to the GPU when the call ends | Frees VRAM so the GPU voice model fits beside her |
| Her language model is unloaded for a picture and when she hands a job to the coding model, and models unload after 2 idle minutes | The chat and coding models cannot both stay loaded (measured: loading Qwen evicts Gemma) |
| The coding model (22 GB) runs split across GPU and system RAM | Measured: 8.3 GB on the GPU, 12.8 GB in RAM |

### Measured (RTX 5070, bot stopped)

| | Chat model | Coding model |
|---|---|---|
| Cold load | 4.3 s | 27.1 s (8.9 s on a later run) |
| Unload | 0.1 s | 1.0 s |
| Speed | about 70 tokens/s | about 68-85 tokens/s |
| Switch from the other model | 4.4 s | 9.3 s |

Voice stack (measured on its own, bot stopped): speech recognition loads in about 1.2 s (CPU). The voice model loads in about 13 s
with files cached (10 s on the CPU, then 0.5 s to move to the GPU and about 3 s to warm up), and about 35 s on the first start
after the files had gone cold. It uses about 2.4 GB of VRAM. It speaks at 3.3-4.1x real time (under 1 s to start a short sentence),
and recognition of a 6-7 s clip takes about 1.6-1.9 s. Chat model beside the loaded voice model: with all layers on the GPU it uses 11.6 GB of 12.2 GB and runs at about 71 tokens/s; with the
call setting (44 of 48 layers on the GPU) it uses 10.9 GB and runs at about 38 tokens/s. The voice model spoke a 3.3 s clip in 0.9 s
beside the chat model.

Her chat/plan/code classification step got 22 of 22 right on a hand-labelled set of 22 short messages (median 0.34 s per call). Those
prompts were short, so real replies with her full prompt take longer. Voice and picture models were not benchmarked.

## What she does

- **Chat** with per-person nicknames, remembered facts, a mood that drifts with how people treat her, and reminders (up to 7 days out).
- **Efficiency:** long plans, tool results and files go to a per-chat notebook and come back with `look_back`; one merged triage call
  decides whether a message is for her and whether it is chat, plan or code. See `EFFICIENCY.md`.
- **Voice calls:** listens, replies out loud, keeps up with several speakers, plays a soft thinking sound while she works. See `VOICE.md`.
- **Coding agents:** work happens in a folder under the workspace, and code runs only in a Docker sandbox (no network, read-only
  filesystem, 256 MB scratch, 30 s default limit). The `sera-sandbox` image carries C/C++, C#, Go, Rust, Java, Node/TypeScript,
  Python, R, Ruby, PHP, Lua, Perl, Fortran, SQLite and a headless browser for HTML.
- **Claude mode (owner only):** `!agent claude` swaps the agents to Claude; `!agent model <name>` and `!agent effort <level>` pin the
  model and effort. Sessions stay open between jobs and are cleared with `/clear`; transcripts go to `.history/`. Resets on restart.
- **Pictures:** see `IMAGES.md`.
- **Pings:** `@Name` in her replies becomes a real mention only for an exact Discord name, never a nickname.
- **Web page:** a small local page on port 8765, protected by the token in `~/.sera/web_token.txt`.

## Commands

`!help` lists them. Owner-only: `!agent`, `!mood`, `!vc`.

## Setup

1. Install [Ollama](https://ollama.com) and pull the models you choose (`ollama pull <name>`).
2. Install Docker Desktop and build the sandbox: `docker build -t sera-sandbox sandbox/`.
3. Python 3.11: `pip install -r requirements.txt` (and `pip install -r requirements-voice.txt` for voice calls).
4. Create the folder `~/.sera/` (on Windows, `C:\Users\<you>\.sera`) and put these files in it. None of them belong in git:
   - `sera.env` with your Discord bot token (`DISCORD_TOKEN`) and `SERA_OWNER_ID` (your Discord user id);
   - `models.json`, which models she runs (required: at least `chat`). Only `chat` is needed to start; each other entry turns a
     feature on. For example (use your own model names):

     ```json
     {
       "chat": "<an Ollama model name>",
       "agent": "<an Ollama model with tool support, for the coding agent>",
       "claude": {"small": "<a Claude model id>", "big": "<a Claude model id>"},
       "claude_by_difficulty": {"easy": "small", "normal": "small", "hard": "big"},
       "image": {"unet": "<file in image_gen/ComfyUI/models/unet or diffusion_models>", "clip": "<file in .../clip>", "vae": "<file in .../vae>"}
     }
     ```

     The tier names in `claude` are yours: you can say "use big" in a message and the owner's Claude jobs will use that tier.
   - `persona.txt`, her system prompt (optional);
   - `lines.json`, her canned replies (busy, ignoring someone, joining a call, and so on) (optional);
   - `behaviour.json`, her trigger names and mood tables (optional); `voice_rules.txt`, what she is told in a call (optional).

   Without the optional files (all but `models.json`) she runs on safe defaults written out in the code, right next to where each is used (search for
   `sera_home.` in `discord_bot.py`, `mood.py`, `reminders.py` and `voice_chat.py`). To make her your own, copy a default into
   `~/.sera` under the name above and edit it. A note above `SYSTEM_PROMPT` in `discord_bot.py` says which of her models are
   uncensored, why, and what limits her.

   The web page's token (`web_token.txt`) is created in the same folder the first time she runs.
5. Optional extras that are **not in this repository** (they are large, and ignored by git). Each feature stays off without its part:
   - **Pictures:** ComfyUI installed in `image_gen/ComfyUI` with its own virtual environment at `image_gen/venv`, and the model files
     named in `models.json` placed under `image_gen/ComfyUI/models/`. See `IMAGES.md`.
   - **Voice models:** separate Python virtual environments in `voice_envs/stt` (faster-whisper), `voice_envs/tts` and
     `voice_envs/tts_gpu` (Chatterbox; the GPU one with a CUDA build of PyTorch). Their paths are set in `voice_config.json`. See `VOICE.md`.
6. For Claude mode, install the Claude Code CLI and sign in with your plan.
7. `python discord_bot.py`

## Files

| File | Purpose |
|---|---|
| `discord_bot.py` | the bot: chat, triage, tools, agents, commands |
| `sera_home.py` | reads her private files from `~/.sera` (persona, canned replies, models), with safe defaults |
| `console_guard.py` | keeps a paused Windows console from freezing the bot |
| `notebook.py` | notebook and efficiency settings |
| `claude_sessions.py` | kept-open Claude CLI sessions |
| `agent_sandbox.py`, `sandbox_mcp.py`, `sandbox/` | Docker sandbox and its tool server |
| `voice_chat.py`, `voice_worker.py` | voice calls and the speech workers |
| `memory.py`, `mood.py`, `reminders.py` | memories, feelings, reminders |
| `image_gen.py`, `image_safety.py` | pictures |
| `web_ui.py`, `web/` | local web page |
| `*_config.json` | settings, re-read while she runs |

## Not tested / known limits

- Discord bots cannot run slash commands, so she cannot use another bot's slash commands.
- Whether another bot accepts commands that a bot typed is up to that bot.
- The sandbox has no network, so `pip` and `npm install` do not work in it.
- The voice timings above were measured with the workers on their own, not during a live Discord call; picture speed and quality were
  not benchmarked.
- Only the author's setup (Windows 11, RTX 5070) has been run end to end. Linux and macOS are untested.
- The offline test suites are not part of this repository.
