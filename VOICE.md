# Sera in a voice call

## One-time setup

1. **Install the voice packages** into the Python that runs the bot (the one `discord.py` is in):

   ```
   python -m pip install -r requirements-voice.txt
   ```

   That is `PyNaCl`, `davey` and `discord-ext-voice-recv`. `davey` is what discord.py uses for Discord's end-to-end encryption on
   calls. `discord-ext-voice-recv` is what lets her *listen*; discord.py can only speak into a call on its own. The handling of
   Discord's encryption for incoming audio is in `voice_chat.py` (search for `dave`).

2. **Give the bot voice permissions** in the server: **Connect** and **Speak** (and **Use Voice Activity**). If your bot's invite
   link didn't include them, give her role those permissions in Server Settings, or in the voice channel's permissions.

3. Restart the bot.

4. **Create the speech environments.** The speech models run in their own Python virtual environments under `voice_envs/`, which are
   not part of this repository and don't touch the Python the bot runs in: `stt` (faster-whisper), `tts` (Chatterbox, plus Windows
   voices) and `tts_gpu` (Chatterbox with a CUDA build of PyTorch). Which environment and options each engine uses is set in
   `voice_config.json`; the engines themselves are in `voice_worker.py`.

## Using it

Join a voice channel yourself, then in any text channel:

| Command (owner only) | What it does |
|---|---|
| `!vc join` | She joins your voice channel and loads the speech models (about 13 s with the model files cached, about 35 s on a cold start; "Voice is ready." when done) |
| `!vc leave` | She leaves and frees the models' memory |
| `!vc status` | Shows the current setup |
| `!vc voice <engine> [voice]` | Swaps the voice. Engines: `chatterbox_turbo`, `chatterbox_multilingual`, `sapi` |
| `!vc stt <model>` | Swaps the speech recognition model, e.g. `small` or `large-v3-turbo` |
| `!vc always on` / `off` | She answers everything she hears, or only when named (plus a 45 s follow-up window) |
| `!vc hear <bot name or id>` | Lets her listen to a bot in the call (bots are muted by default). `!vc hear all`, `!vc hear none`, or just `!vc hear` to see the list |
| `!vc echo on` / `off` | Whether she posts `🎤` what she heard and `🔊` what she said in the text channel |

You can also just ask her, in plain words, in a text channel where she's listening: "sera join vc", "hop in the voice
channel", "sera leave the call". She joins the channel you're in (or moves to it if she's in another one here). Inside a
call you can say "Sera, leave the call" out loud and she says goodbye and goes. Only the owner can do this; anyone else
gets the usual brush-off. The word filter only decides whether a model call is worth making; her model decides what was
meant, so "I'll leave the call early" doesn't make her leave.

While she's in a call, she answers when you say her name ("Sera, ..."), and for 45 seconds after she replies you can
carry on without it. If you talk over her for about a third of a second, she stops.

## Swapping models without changing anything else

Everything is in `voice_config.json`, which is re-read on every utterance, so you can edit it while the bot runs.

- **The voice:** `tts.engine`. Spanish lines automatically use `tts.non_english_engine`.
- **Hearing:** `engines.faster_whisper.options.model`.
- **Her speaking style:** `speak` (answer length), **when she answers:** `listen`.

To add a new engine, write a small class in `voice_worker.py` (copy `SapiTTS` or `ChatterboxTurboTTS`: it needs
`speak(text, voice, language, **params)` returning samples and a sample rate), add it to `TTS_ENGINES`, and add an entry
for it under `engines` in the config pointing at a Python that has its libraries. Nothing else changes. A new speech
recognition engine works the same way with `STT_ENGINES`.

## Several people talking at once

Everyone who finishes speaking goes into one queue. When she gets to it, she takes everything waiting (up to 8 clips or
25 seconds of speech), joins the clips with a second of silence between, and reads them in **one** pass of Whisper. Whisper
works in 30-second windows, so this costs about one pass instead of one per person; every word is handed back to whoever
said it. Lines said to her are answered together in one reply (she is told who spoke), and lines said between other people
go into her history marked "(not to you)", so she knows what the conversation is. A line that waited more than 45 seconds
is dropped. Settings: `listen.batch` and `listen.keep_overheard` in `voice_config.json`.

## How a line moves through (and what runs at the same time)
Four stages, each on its own, so none waits for one it doesn't need:
1. **Listening:** each person's speech is collected until they pause (`listen.silence_seconds`).
2. **Reading** (Whisper, on the CPU, `cpu_threads` 4): everything waiting is read in one pass. It never waits for her, so lines said while she is
   thinking or talking are already read when she is free.
3. **Deciding and answering** (her model on the GPU): takes everything read so far as one batch. If more is being read at that moment it waits
   up to `batch.merge_wait_seconds` (2 s) so it can be answered together. Her words go to the voice a sentence or clause at a time, while she is
   still writing the next (`speak.lookahead_chunks`).
4. **The voice** (Chatterbox, on the GPU): speaks one piece while the next is made.
Her feelings about an exchange (one more model call) are worked out when nothing else is going on, or after 45 s at the latest, so they never sit
between two replies.

**`!vc timing`** (owner) shows the middle value of each stage over the last 50 replies and the latest reply. Every reply also writes one line to the
console (starting with a stopwatch) and to `SeraWorkspace\voice_logs\timing.jsonl`. The stages: in line for the transcriber, transcribing, held while
she was busy (already read, waiting for her to be free), deciding, notes, waiting for the model, the model's first word, first voice synth, **first
sound** (counted from when they stopped talking; the 0.9 s of silence it takes to know they stopped comes before that), and how long she spoke.

## Busy calls (three or more people)
With `listen.busy_humans` (3) or more people in the room, most lines are people talking to each other, so she is stricter:
- A line only counts as hers if she is called by name ("Sera, ...", "hey Sera", "..., Sera?") or it follows what she just said and her
  model agrees it was to her. If her model can't tell, she stays quiet. Talk *about* her ("she's tapped", "Sera's writing") is never hers.
- The follow-up window is `listen.session_seconds_busy` (12 s) instead of `session_seconds` (45 s).
- Only the person she is answering, and the owner, can talk over her and stop her (`listen.barge_in_from`: `"addressed"`, or `"anyone"` for the old behaviour).
- Replies are at most `speak.max_sentences_busy` (2) sentences, so she doesn't hold the floor.

## Call encryption fix

The listening extension (`discord-ext-voice-recv` 0.5.3a180) doesn't remove Discord's end-to-end encryption from received
audio, which showed up on the first live test as `OpusError: corrupted stream` followed by silence. `voice_chat.py` now
removes that layer itself with discord.py's own session (`install_dave_support`) and turns a bad frame into silence
instead of letting it stop the listener. This was only tested with a stand-in encryption session. When it works, the
console prints `end-to-end decryption of received audio is working`. If instead it prints `couldn't decrypt a received
audio frame (...)`, the error text in the brackets says why.

Audio from a sender that doesn't use the call's end-to-end encryption (a bot, an older app) shows up as
`UnencryptedWhenPassthroughDisabled`. Those frames are ordinary audio and are now played as they are (the console says so once) instead of
being dropped, which is what made such a speaker inaudible.

## If something doesn't work

- Logs: `SeraWorkspace\voice_logs\` (the worker processes) and the bot's console (lines starting with 🎙️).
- **She joins but never reacts to speech:** look for `🎙️ <name>: '...'` lines in the console. If none appear after
  you talk, the listening extension isn't receiving audio (likely the encryption issue above).
- **She hears you but doesn't speak:** check she has **Speak** permission in that channel.
- **A line is heard but ignored:** it didn't contain her name and was outside the follow-up window. `!vc always on`
  makes her answer everything.

## Speed, as measured on this PC (CPU only)

About 2 s to transcribe a line, 5-10 s until the first sound of an English reply with `chatterbox_turbo`, and 13-18 s to
finish a 6-9 s answer. Spanish is slower (about 15 s to first sound). `sapi` is much faster but robotic.

## The thinking sound
While she works on a reply she plays a quiet repeating cue (two soft notes, then silence) once the wait is longer than `speak.thinking_sound_delay` (0.5 s after she starts working on it; a reply that is ready sooner never plays it). It stops the instant her real voice is ready (anything of it that hasn't played yet is dropped, so it can never delay her or mix with her), when someone talks over her, or when the reply ends with nothing to say. Settings in `voice_config.json` under `speak`: `thinking_sound` (true/false), `thinking_sound_delay`, `thinking_sound_period` (1.6 s per cycle), `thinking_sound_volume` (0.10 = 10%). Everyone in the call hears it, and so does any bot listening. It starts when she has decided a line is for her, so the time spent reading what you said is still silent.

## How the voice loads (what runs at the same time)
There are no checks slowing the load; the steps simply used to run one after another. When she joins, these now start together: her own model reloads with fewer layers on the GPU (making room), the voice reads its files into memory on the CPU (needs no GPU), and hearing loads on the CPU. Only moving the voice onto the GPU waits, for her model to have made room and for the files to be read. (A Whisper set to run on the GPU still waits for the voice, since it would take its room.) The console line after joining says what each step took, and the worker log (`voice_logs/voice-worker-tts.log`) says where the voice's own time goes: imports, weights, moving to the GPU, and the warm-up run.
