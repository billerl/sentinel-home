# AGENTS.md: a guide for AI assistants working on Sentinel Home

You're an AI assistant (Claude Code, Antigravity, ChatGPT/Codex, Gemini, Cursor, Copilot or similar) helping
someone install Sentinel Home, or change it to suit their house. Read this first. Some tools load this file
by themselves; if yours doesn't, the person can just say "read AGENTS.md first".

Sentinel was built by its owner together with Claude Code, one feature at a time, for one real house, and
then published so others could use it. Expect a lot of features, a few house-shaped assumptions, and long
design notes. Everything below comes from that history.

## What it is

A self-hosted home security and smart-home system for a small Linux PC. One Python process (`main.py`)
runs everything: IP cameras (RTSP) with motion and object detection, recordings and a timeline, plain-English
alerts on Telegram, a web page that's the whole user interface, and optional extras: family locations
(OwnTracks), a voice assistant, lights and plugs, routines, TV control, IR/RF remotes, Wi-Fi motion sensing
with ESP32 boards, a Meshtastic pet tracker, Spotify, weather, planes, recipes, notes, backup.

**It must stay free and local.** No paid APIs, no cloud AI, no subscription. New dependencies must be free and
open source, and worth it. The one opt-in exception is "Ask Claude", which uses the person's own Claude Code
sign-in on the PC.

## Rules

1. **Private data never goes into git.** The repo may be public. The person's house lives in git-ignored
   files: `settings.json` (camera passwords, bot token), `camera_admin.json`, `recorded_videos/` (clips and
   the databases of family, places, cars, history), `.web_sessions.json`, `firmware/*/secrets.h`, and
   `*.local.md` notes. Never copy real names, addresses, coordinates, emails, phone numbers or passwords into
   tracked files, tests or commit messages; use made-up examples (Alex, Kate, Elizabeth/Liz, Ben, Grace; the
   example spot 41.1145, -88.1780). Scan the diff for personal details before every push.
2. **Don't print secrets** into the conversation (don't `cat settings.json`; read single keys if needed).
3. **Ask before** anything hard to undo or outside the repo: pushing, deleting recordings or database rows,
   changing the router, `sudo`, flashing hardware. The people in the house may be using it while you work.
4. **User-facing words are plain English** for a non-technical household: the web page's Help (in
   `templates/settings.html`), Telegram replies, spoken answers. Update Help when you add a feature.
5. **Tests must pass** before the changed version runs for real (below).

## Helping someone install

The readme's "Install" section is the path: system packages, clone, venv, `requirements.txt` +
`requirements-piper.txt`, the YOLOX model, `python -m sentinel.web.set_password`, `python main.py`, then the
web page at `http://127.0.0.1:8088/` → Settings → Cameras → Add a camera. Then `installation-guide.md`:
the systemd service (with `xvfb-run`), remote access (Cloudflare Tunnel or Tailscale), Telegram.

How to tell it's working:
- Startup prints `Web viewer: http://127.0.0.1:8088/`. `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8088/login` gives `200`.
- `sentinel_events.log` (JSON lines) gets `engine_started`, `object_detector_ready` (with `"device"`) and one
  `camera_connected` per camera.
- `Web page: no login set yet` means: stop it, run `set_password`, start again.

Common snags: **stop Sentinel before editing `settings.json`** (it saves its settings when it stops, so a hand
edit made while it runs is lost); the service user needs the `audio` group (speaker, mic) and `render` (Intel
GPU detection); a camera brand's stream path may need adjusting (Settings → Cameras → Test connection says
what's wrong); Ring/Blink/Arlo/Nest and most battery cameras have no RTSP and can't be used.

## Where things are

`project-structure.md` maps every file; `handoff_summary.md` explains each feature's design and why (long:
search it for the feature you're touching). The main pieces:

| Piece | Where |
|-------|-------|
| Start-up, the glue between features | `main.py`, `sentinel/engine/engine.py` |
| Cameras, motion, recording | `sentinel/engine/camera.py`, `sentinel/detection/`, `sentinel/recordings/` |
| Each feature's logic | `sentinel/engine/<feature>.py` (family, cars, planes, routines, switches, remotes, wifi_motion, weather, backup, ...) |
| Telegram and voice commands (one registry for both) | `sentinel/engine/commands.py`, `Engine._build_commands` |
| Default settings | `sentinel/settings/manager.py` |
| Web routes, pages, scripts | `sentinel/web/routes/*.py`, `sentinel/web/templates/`, `sentinel/web/static/` |
| Who may use each web address (owner, admin, user, guest) | `sentinel/web/roles.py` (a test fails if a route has no rule) |
| ESP32 firmware, the Meshtastic bridge, dev tools | `firmware/`, `tools/` |

## Changing it safely

- **Tests:** `PYTHONPATH=.:tests venv/bin/python -m unittest discover -s tests`. Every test module imports
  `_test_env` first (it keeps tests off the real log and settings); keep that in new test files. Add tests
  with what you build.
- **Look at UI changes:** `venv/bin/python tools/dev_harness.py [seconds]` runs a second, login-free copy
  on `127.0.0.1:18091` with its own data folder; drive it with a headless browser (Selenium + Firefox).
  Check phone width (~390 px) as well as desktop. It *does* connect to the real cameras: keep runs short.
- **Restart after changes:** if it runs as the systemd service, `systemctl restart sentinel` or send SIGTERM
  (`kill -TERM <pid>`); systemd starts it again in ~10 s and recording pauses only for that. Then check the
  log lines above. Web pages reload themselves after an update.
- **Detection thresholds** (`sentinel/engine/objects.py`) were tuned by replaying real footage
  (`tools/replay_detections.py <date>`); change them the same way, not from intuition.

Pitfalls learned the hard way:
- Never run a second engine without `SENTINEL_BUFFER_ROOT` set (the harness sets it): two engines share
  `/dev/shm/sentinel-buffer` and wipe each other's video buffers.
- Cameras accept only a few RTSP connections; extras get "500 Internal Server Error". After tests that open
  streams, check for leftover `ffmpeg` processes.
- `pgrep -f` / `pkill -f` with a pattern that's also in your own shell command matches the shell itself.
- Telegram polling is shared: a second running bot steals the household's messages.
- `recorded_videos/household.db` holds the family's own labels (whose car is whose): never rebuild it.
- Night pictures are infrared: tell them apart with `is_greyscale`, not saturation.
- TV pages run in old browsers (a 2018 Samsung TV is Chrome 56): plain ES5 JavaScript, no CSS grid gap or
  container units without fallbacks, `XMLHttpRequest` where cookies matter.
- In web editors, never grow JavaScript arrays by setting `.length` (the holes are saved as `null`).

## Notes for one house

Keep what's specific to *this* installation (its machine, network, people, devices, decisions, what's
planned) in a git-ignored notes file next to this one: `AGENTS.local.md` (or `CLAUDE.local.md` for Claude
Code; any `*.local.md` is ignored). Read it at the start of each session and update it as you go, so the
next session, or another assistant, picks up where you left off. Never put those details in tracked files.

Making it fit someone's house is the point: features can be turned off in Settings, removed, or rebuilt
differently. Keep the rules above, keep the tests passing, and keep the Help page truthful.
