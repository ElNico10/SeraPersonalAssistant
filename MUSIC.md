# Music mode

Sera can join a voice call **only to play music**. In this mode nothing runs on the GPU and her chat model is never loaded: she listens with Whisper on the CPU, waits for her name, and plays what you ask on the Spotify app running on your PC. Say "hey sera, switch to voice" and she changes to the normal voice call.

```
you say "hey sera, play ..."  ->  Whisper (CPU) hears it  ->  Spotify API starts the song in the Spotify app on your PC
Spotify app --plays into--> virtual audio cable --Sera reads it like a microphone--> the voice channel
```

Needs **Spotify Premium** (Spotify only lets apps control playback on Premium accounts).

## Setup (once)

1. **Install the audio package** into the same Python as the bot:
   `python -m pip install sounddevice`
2. **Install a virtual audio cable.** VB-CABLE is free (donationware) from <https://vb-audio.com/Cable/>. Download `VBCABLE_Driver_Pack45.zip` from that page, extract it, run the setup file **as administrator**, then **restart the PC**. It adds two devices: "CABLE Input" (an output) and "CABLE Output" (an input).
3. **Send Spotify's sound into the cable.** Windows Settings > System > Sound > Volume mixer > find Spotify > set its **Output device** to **CABLE Input**. (Open Spotify and play something so it shows up in the list.)
4. **Hear it yourself too.** Spotify's sound now goes only to the cable. Control Panel > Sound > **Recording** tab > double-click **CABLE Output** > **Listen** tab > tick **Listen to this device** and pick your headphones/speakers under "Playback through this device".
5. **Make a Spotify developer app** at <https://developer.spotify.com/dashboard> (log in with the Premium account):
   - Create app. Redirect URI: `http://127.0.0.1:8888/callback` (it must be `127.0.0.1`, not `localhost`; if another program uses port 8888, pick another port and use the same one in step 6).
   - Under "Which API/SDKs are you planning to use?" tick **Web API**.
   - Settings > **User Management**: add your own Spotify account (name and email). Apps in development mode only work for accounts on this list (up to 5).
   - Copy the **Client ID** (the Client Secret is not needed).
6. **Put the Client ID where Sera reads it.** Copy `spotify.example.json` to `~/.sera/spotify.json` (on Windows: `%USERPROFILE%\.sera\spotify.json`) and fill in `client_id`. This file stays out of the repo.
7. **Restart the bot**, then in a text channel send `!vc spotify login`. A browser window opens on the PC; approve it. The login is saved in `~/.sera/spotify_token.json` and renews itself. If you logged in before "play my playlist" existed, log in again once so Spotify can show her your playlist list (the `playlist-read-private` permission).

### Optional: switch Spotify's output automatically

Instead of changing Spotify's output device in the Windows Volume mixer by hand, she can do it: **cable when music mode starts, your normal output when it ends** (including when you switch to a voice call or she leaves).

1. Download NirSoft's SoundVolumeView (`soundvolumeview-x64.zip` from <https://www.nirsoft.net/utils/sound_volume_view.html>), check its SHA-256 against <https://www.nirsoft.net/hash_check/?software=soundvolumeview>, and unpack it so the program is at `tools/soundvolumeview/app/SoundVolumeView.exe` (`tools/` is git-ignored). It is unsigned, so antivirus tools sometimes flag it.
2. List your devices: `SoundVolumeView.exe /scomma devices.csv` and find your headphones' "Command-Line Friendly ID", e.g. `Some Headset\Device\Speakers\Render`.
3. Copy `music_local.example.json` to `~/.sera/music_local.json` and put that ID in `normal_output` (this file is this PC's own and stays out of the repo; it can override any `music_config.json` setting). `normal_output` left empty means "Windows' default output".
4. In Windows, set the **default playback device to your headphones, not the cable**. Otherwise every sound on the PC goes into the call while she is in music mode.

If the tool can't be found or refuses a name, she says so in the text channel and music mode carries on (you would route Spotify by hand). If the bot crashes while in music mode, Spotify stays on the cable until you change it back or she next leaves music mode.

## Using it

Open the Spotify app on the PC (logged in), join a voice channel, then either:

- type `!vc music`, or say to her in chat: "sera, join vc in music mode" / "sera, can we get some music in vc?"
- She joins and says (in text) when hearing is loaded. Then speak to her:

| Say | What happens |
|---|---|
| "hey sera, play *song* [by *artist*]" | searches Spotify, plays the top result, and posts what she picked in the text channel |
| "hey sera, play the album / playlist / artist *name*" | plays that album, playlist or artist from Spotify's catalog |
| "hey sera, play my playlist *name*" (or "play my *name* playlist") | plays one of your own playlists, matched loosely by name (needs the extra login permission, below) |
| "hey sera, queue *song*" | adds it to the queue |
| "pause", "resume", "skip", "previous" | playback controls |
| "raise volume", "lower volume", "volume 40" | volume (also "volume up" / "volume down") |
| "what's playing" | posts the current song |
| "hey sera, switch to voice" | leaves music mode for the normal voice call (loads her voice models; refused if a game holds the GPU) |
| "hey sera, leave" | leaves the call |

To go back the other way, while she is in a normal voice call: say "sera, switch to music" (or "swap to music mode", owner only) or type `!vc music`. She says "Okay, music mode.", gives the GPU back and rejoins in music mode.

She finds her name anywhere in what she heard, so it doesn't matter if you were mid-conversation: "...yeah anyway, hey sera, pause" works. Her name opening the line always counts; later in the line it only counts when what follows is a command, so just mentioning someone called Sarah doesn't start anything. If two commands are in one line she takes the first.

Saying just "hey sera" makes her listen for your next sentence. "Sera, ..." without "hey" also works (turn that off with `need_greeting`).

The text channel stays quiet: one message when she joins, and one line for each song she plays or queues. She also speaks up for problems (Spotify not open, nothing found, a song that won't start, a refused switch to voice) and for "what's playing". Everything else she hears and does is only in the terminal.

## Settings (`music_config.json`, re-read for every line)

| Setting | Meaning |
|---|---|
| `who_can_control` | `owner` (default) or `anyone` in the call who says her name, for everything |
| `who_can_queue` | `anyone` (default) or `owner`: lets people other than the owner add songs with "hey sera, queue ..." (they can't play, pause, skip, change the volume, switch modes or make her leave). Songs go into the owner's Spotify queue |
| `wake_words` | spellings that count as her name. Whisper often hears "Sera" as "Sarah" or "Sierra"; remove one if someone in the call really has that name |
| `need_greeting` | `true` = only "hey sera ..." counts |
| `follow_seconds` | how long the next sentence counts after a lone "hey sera" |
| `capture_device` | part of the name of the audio input Spotify plays into (default `CABLE Output (VB-Audio Virtual Cable)`; the full name keeps it from matching other VB-Audio devices) |
| `spotify_device` | part of a Spotify device name to always use; empty = the one playing, else this PC |
| `volume_step` | percent per "raise volume"/"lower volume" |
| `buffer_ms` | sound kept ready for the call: more = fewer glitches, more delay |
| `silence_seconds` | how long you must be quiet before she takes a line as finished (default 0.5; the voice call uses 0.9). Shorter = quicker commands, but a pause inside a song title can split it |
| `stt_engine` | which engine in `voice_config.json` hears you (default `faster_whisper_music`: the same `small` model as the voice call; its `beam_size` can be lowered to 1 for faster, less exact hearing). It must run on the CPU; music mode refuses a GPU one. For still faster commands at some cost in accuracy, point its `model` at `base.en` (a one-time download from Hugging Face, so check it first) |

Pause, resume, skip, previous and volume are sent to Spotify straight away with no device lookup. The terminal prints how long each command took to be sent to Spotify (and how long reading it took, and the quiet wait before that). A lone "hey sera, play" (or "queue") waits for the title instead of resuming; say "resume" to resume.

## Limits and troubleshooting

- **Whisper mishears song names.** She posts what she picked; say "skip" or ask again more clearly. `!vc stt base` is faster, `!vc stt small` more accurate.
- **Every line spoken in the call is read by Whisper on the CPU** to check for her name (nothing is logged or kept unless it was for her).
- **"I can't see the Spotify app"**: open Spotify on the PC and log in; it must appear as a device.
- **Silence in the call**: check step 3 (Spotify's output is the cable), that the song is actually playing, and that `capture_device` matches the cable's input name. If the cable isn't found she tells you what inputs she can see.
- **You hear nothing yourself**: step 4.
- **"Spotify refused"**: your account isn't on the dashboard's User Management list, or the app owner isn't Premium.
- Sending music through a call is for personal use among friends; follow Spotify's terms.
