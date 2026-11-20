# Sentinel Home

Sentinel Home is a home security and smart-home system you run yourself, on a small Linux PC in your
house. It watches your IP cameras, records clips, tells you what it saw in plain English ("🚗 A red car
pulled into the Driveway"), and gives you one web page for everything: live cameras, a timeline of what
happened, family locations, lights and plugs, routines and more. **Everything runs on your own PC, for
free.** No cloud account, no subscription, nothing paid. The only optional exception is asking Claude
the questions Sentinel can't answer itself.

> **Built with Claude Code, and your AI assistant is welcome too.** This whole project was written by
> its owner together with [Claude Code](https://claude.com/claude-code), one feature at a time, for one
> real house. You're welcome to have *your* AI assistant help you install it and change it to suit your
> home: Claude Code, Antigravity, ChatGPT/Codex, Gemini, Cursor or anything else. Point it at
> **[`AGENTS.md`](AGENTS.md)**, which explains the project to an AI assistant: how it fits together, how to
> check changes, and the rules that keep it safe and private.

---

## What you need

- **A Linux PC that stays on.** A small mini PC is plenty (this one is an Intel i5 office mini PC with
  Ubuntu). An Intel graphics chip is used for detection when there is one; otherwise the CPU does it.
  About 3 GB of disk for the software, plus room for recordings (30 days by default).
- **IP cameras** that offer an RTSP stream (most wired and plug-in Wi-Fi cameras do: Amcrest, Dahua, Lorex, Hikvision, Annke, Reolink, Uniview, TP-Link Tapo,
  Foscam, Anpviz and others; Ring, Blink, Arlo, Nest and most battery cameras don't).
- Everything else is optional. See [Optional extras](#optional-extras) below.

## Install (about 15 minutes)

**1. System packages** (Ubuntu/Debian):

```bash
sudo apt install git curl python3-venv alsa-utils xvfb
```

**2. Get Sentinel and its Python packages** (about 1.5 GB; takes a few minutes):

```bash
git clone https://github.com/billerl/sentinel-home.git
cd sentinel-home
python3 -m venv venv
venv/bin/pip install -r requirements.txt -r requirements-piper.txt
```

**3. Download the object detector** (required: it's what tells people, cars and animals apart):

```bash
mkdir -p models/detector && curl -L -o models/detector/yolox_tiny.onnx \
  https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx
```

**4. Choose your web login** (the username and password for Sentinel's web page):

```bash
venv/bin/python -m sentinel.web.set_password
```

**5. Start Sentinel:**

```bash
venv/bin/python main.py
```

It prints `Web viewer: http://127.0.0.1:8088/`. Open that address in a browser **on this PC** and sign in.
(Ctrl+C stops it.)

**6. Add your first camera:** on the web page, **Settings → Cameras → Add a camera**. Pick the brand,
type the camera's address and its own username and password. It appears on the Cameras page a few
seconds later. Then draw where it should watch under **Zones**.

**7. Keep it running all the time:** set it up as a service so it starts with the PC and restarts itself.
See [installation-guide.md → Always-on setup](installation-guide.md#always-on-setup-how-sentinel-is-normally-run).
That guide also covers using it from your phone when you're away (Cloudflare Tunnel or Tailscale).

That's a working system: cameras, recordings, the timeline and alerts on the web page. Add the extras
you want from the list below, whenever you like.

> **Tip:** stop Sentinel before editing `settings.json` by hand (or running step 4 again): it saves its
> settings when it stops, so an edit made while it runs would be overwritten. Most settings are on the
> web page's Settings anyway.

## Optional extras

Each one is independent. Most are switched on from the web page's **Settings**; the guide has details.

| Extra | What you need | Where |
|-------|---------------|-------|
| **Telegram alerts and questions** ("did anyone come by?", "videos 2pm-4pm") | A free Telegram bot | Settings → Telegram ([guide](installation-guide.md#telegram-setup-optional)) |
| **Spoken alerts** | A speaker on the PC | Settings → Audio |
| **Voice assistant** ("Hey Jarvis, who's home?") | A USB speakerphone (e.g. Jabra Speak), plus the speech model below | Settings → Audio |
| **Family locations, timeline and arrive/leave messages** | The free OwnTracks app on each phone, and remote access | Family page |
| **Your own cars by name** ("Kate's car left") | Nothing extra (better with the car models below) | Vehicles page |
| **Weather** on the cameras page and severe-weather warnings (US) | Your town or ZIP code | Settings → Weather |
| **Lights and plugs, routines** ("at sunset turn the porch light on") | TP-Link Kasa or Tasmota plugs on your Wi-Fi | Found automatically; Routines page |
| **TV control** (Samsung, Fire TV): show a camera on the TV | The TV on your Wi-Fi | Found automatically |
| **IR/RF remotes** (heater, soundbar, ceiling fans) | A BroadLink RM4 Pro or mini | Remotes page |
| **Wi-Fi motion sensing** (movement inside, no cameras) | ESP32-S3 boards with the firmware in `firmware/` | Movement inside page |
| **Pet GPS tracker and mesh chat** | A Meshtastic collar tracker and radio base (`tools/meshtastic_bridge.py`) | Pet tracker page |
| **Planes overhead** ("what plane is that?"), recipes, notes | Nothing extra | Home page |
| **Spotify by voice** | Spotify Premium and a free developer app | Settings → Spotify |
| **Ask Claude** (questions Sentinel can't answer) | The Claude Code app signed in on this PC | Settings → Telegram |
| **Nightly backup** to a USB drive and/or another PC | The drive, or an SSH-reachable PC | Settings → Backup |

Tiles for hardware you haven't got wait under **Add more** on the home page, and move up by themselves
once the hardware shows up on your network.

<details>
<summary><b>Optional model downloads</b> (car types, telling your cars apart, natural voice, speech recognition, Spotify speaker)</summary>

```bash
# delivery vans and vehicle types (CLIP ViT-B/32, MIT)
mkdir -p models/classifier && curl -L -o models/classifier/clip_vit_b32_vision.onnx \
  https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/onnx/vision_model.onnx
# telling your own cars apart (DINOv2-small, Apache-2.0; without it CLIP is used, less accurately)
curl -L -o models/classifier/dinov2_small.onnx https://huggingface.co/Xenova/dinov2-small/resolve/main/onnx/model.onnx
# natural voice (Kokoro, Apache-2.0)
mkdir -p models/kokoro && for f in kokoro-v1.0.onnx voices-v1.0.bin; do curl -L -o models/kokoro/$f \
  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/$f; done
# voice assistant speech recognition (faster-whisper base.en, MIT)
venv/bin/python -c "from huggingface_hub import snapshot_download as d; d('Systran/faster-whisper-base.en', local_dir='models/whisper/base.en')"
# this PC as a Spotify speaker (spotifyd, GPL-3.0; sign in under Settings -> Spotify)
mkdir -p bin && curl -L https://github.com/Spotifyd/spotifyd/releases/download/v0.4.2/spotifyd-linux-x86_64-slim.tar.gz \
  | tar xz -C bin   # check it against the release's .sha512 file
```

Restart Sentinel after adding a model.
</details>

## The web page

- **Home:** weather, who's home, and a tile for each part of Sentinel.
- **Cameras:** the live grid (tap one for full screen) and the timeline underneath: a mark for every
  clip, coloured by what was seen (people, vehicles, deliveries, animals). Tap a mark to play it.
- **Zones:** draw where each camera should watch (**+ Zone**) or a line something must cross (**+ Line**).
- **Settings:** cameras, detection, Telegram, recording, backup, audio and voice, Spotify, weather, remote access, and **Help**, which
  explains every feature in plain English.

Each family member can have their own login with their own role (owner, admin, user, guest):
Family page → **Make login link**.

### How motion triggers an alert

| Zones / lines drawn | Motion counts when |
|---------------------|--------------------|
| **None** | Anything moves enough |
| **Zones only** | The bottom of the moving thing is inside a zone, or 30% of it overlaps one |
| **Lines only** | Something crosses a line in the arrow's direction |
| **Both** | Either of the above |

Shadows, whole-picture light changes (clouds, headlights, night vision switching) and anything that
doesn't last 3 frames are ignored. Then object detection decides what it was, and parked cars don't alert.

## Docs map

| File | What's in it |
|------|--------------|
| `readme.md` | This page |
| [`installation-guide.md`](installation-guide.md) | Always-on service, remote access, Telegram, audio, backup, troubleshooting |
| [`AGENTS.md`](AGENTS.md) | For AI assistants helping you install or change Sentinel |
| [`handoff_summary.md`](handoff_summary.md) | How each feature is designed and why (long) |
| [`project-structure.md`](project-structure.md) | What each file and folder is |

## Your data and secrets

Everything about *your* house is created on first run and never goes into git: `settings.json`
(camera passwords, bot token; owner-only file permissions), `recorded_videos/` (clips, and databases
of family, places, cars and history), `.web_sessions.json`, `camera_admin.json`. Don't share them.
Logs (`sentinel_events.log`) have passwords removed.

## Tests

```bash
PYTHONPATH=.:tests venv/bin/python -m unittest discover -s tests
```

All tests should pass before running a changed version.

## License

[MIT](LICENSE): free to use, change and share, including in your own projects. Keep the copyright
notice with it. It comes with no warranty: it's a home project, so test it on your own setup.
