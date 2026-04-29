# Sentinel Handoff Summary

Use this file as the single source of truth across chats.  
At the **start** of each new chat, instruct the assistant to:

1. Read `handoff_summary.md`
2. Continue from current state
3. Update this file at end of session

---

## Project
- Name: Sentinel Home
- Repo: `https://github.com/billerl/sentinel-home.git`
- Primary branch: `main`

## Current Status
- Desktop app with PySide6/OpenCV is functional.
- RTSP sub-stream discovery/preference implemented.
- Motion detection supports zones + entry lines.
- Trigger behavior currently: **zone OR line crossing**.
- Entry line crossing logic updated to use **actual drawn segment** intersection.
- Auto reconnect for dropped stream implemented.
- Collapsible UI sections + fullscreen + MultiView focus mode implemented.
- Detection size slider preview overlay implemented (1s dwell after release, 5s display).
- Night bug filter with schedule implemented.
- Telegram features implemented (alerts, commands, on-demand clip).

## Telegram Features
- Multiple recipient support via `telegram_chat_ids` (comma-separated).
- Cooldown setting exists and is separate from recording cooldown.
- Include-video mode sends one combined event message with clip.
- Commands:
  - `pause <minutes>`
  - `resume`
  - `status`
  - `help`
  - `clip` (records 10 seconds and sends to requester)
- Unknown command returns coherent help text.

## Detection / Gating Notes
- Modes in practice:
  - No zones/no lines -> contour motion trigger
  - Zones only -> zone motion trigger
  - Lines only -> line crossing trigger
  - Zones + lines -> zone OR line trigger
- Direction setting (`outside_to_inside`, `inside_to_outside`, `both`) controls accepted crossing direction only.
- Line crossing now requires movement segment to intersect drawn line segment.

## Known Environment Notes
- `settings.json` is intentionally gitignored (contains secrets and local preferences).
- Live runtime values may differ from defaults; always check `settings.json` when debugging behavior.
- Push from agent environment previously failed due DNS/network (`Could not resolve host: github.com`), but local push should work from user terminal.

## Docs Updated
- `readme.md`
- `installation-guide.md`
- `project-structure.md`

## Safety / Privacy
- No hardcoded secrets should be committed.
- Keep `settings.json` untracked.
- Do not commit tokens/passwords from screenshots or local settings.

## Agent Terminal Operation Notes
- In Cursor Agent mode, the assistant can execute non-interactive terminal commands directly in the project workspace.
- Commands run in a persistent shell session and can read/write repo files.
- For privileged or unrestricted operations (for example pushing to remote), the agent may request elevated permissions; user approval is required by the client when needed.
- The assistant cannot bypass OS permission prompts that require interactive auth (for example `sudo` password entry) unless those commands are already allowed in the environment.
- Good practice for future chats:
  - Ask the assistant to run diagnostics first (`git status`, environment checks) before edits.
  - Ask for a summary of every command's result after long runs.
  - Ask the assistant to update this handoff file before ending the session.

## Piper TTS Handoff (Implemented + Enablement Steps)
- Piper support is already implemented in code at `sentinel/audio/alert_system.py`.
- Runtime behavior:
  - Sentinel tries `piper.voice` first.
  - If Piper package/model is missing, it falls back to `pyttsx3`.
- New repo file: `requirements-piper.txt` (installs `piper-tts`).
- Enable Piper on a fresh machine:
  1. `python3 -m venv venv && source venv/bin/activate`
  2. `pip install -r requirements.txt`
  3. `pip install -r requirements-piper.txt`
  4. Linux audio utility (if missing): `sudo apt install -y alsa-utils`
  5. Create model folder: `mkdir -p models`
  6. Download matching model pair into `models/`:
     - `<voice>.onnx`
     - `<voice>.onnx.json`
  7. Run: `python main.py`
  8. Verify startup log shows `Piper TTS initialised with voice: ...`
- If Piper does not load:
  - check `pip show piper-tts`
  - confirm model pair names match exactly
  - confirm `aplay` exists (`which aplay`)

## Suggested Next-Chat Prompt
- "Read `handoff_summary.md` and continue from current state. Verify Piper is active on this machine, run a quick TTS validation, and report any missing OS/package prerequisites. Update `handoff_summary.md` before finishing."

---

## Next Session Checklist
- [ ] Read this file first.
- [ ] Confirm current user goal for this session.
- [ ] Verify local app behavior against latest logic before changing code.
- [ ] Update this file before ending session:
  - What changed
  - What was verified
  - Any blockers
  - Next suggested actions

---

## Session Update Template

### Date/Time
- 

### Goal Worked On
- 

### Changes Made
- 

### Validation Performed
- 

### Open Issues / Risks
- 

### Next Recommended Step
- 

---

## Latest Session Update

### Date/Time
- Tuesday, Apr 28, 2026 (UTC-4)

### Goal Worked On
- Diagnose full-system lockups, reduce runtime memory pressure in Sentinel, and prepare clean handoff for migration to a Dell OptiPlex host.

### Changes Made
- Ran system diagnostics on the Acer host (CPU/RAM/swap/load/thermal/kernel logs) and found sustained memory-pressure indicators instead of thermal/GPU fault signatures.
- Added lower-load feed defaults in `sentinel/constants.py`:
  - `DEFAULT_FEED_FPS = 15` (new default from 30)
  - bounds: `FEED_FPS_MIN = 5`, `FEED_FPS_MAX = 30`
  - reduced `RECORDING_FPS` from 20 to 15
- Added persisted runtime setting `feed_fps` in `sentinel/settings/manager.py` defaults.
- Updated `sentinel/detection/motion_detector.py` camera start path:
  - `start_camera(..., target_fps=...)` now accepts requested FPS
  - best-effort `CAP_PROP_FPS` apply
  - best-effort `CAP_PROP_BUFFERSIZE = 1` to reduce queued stale frames
- Updated `sentinel/ui/main_window.py`:
  - imported new feed-FPS constants
  - loaded/sanitized persisted `feed_fps`
  - added `Feed FPS (lower = less RAM/CPU)` spinbox in camera controls
  - added `_feed_timer_interval_ms()` and `on_feed_fps_changed()`
  - switched timer start from fixed interval constant to dynamic interval based on selected feed FPS
  - passes `target_fps=self.feed_fps` when connecting a camera session
- Continued prior stream-discovery/profile work already in tree (`network_discovery.py`, `readme.md` updates) and prepared all pending updates for push.
- Added explicit Piper install support file: `requirements-piper.txt`.
- Expanded `installation-guide.md` and `readme.md` with concrete Piper setup/verification instructions for new hosts.
- Added "Agent Terminal Operation Notes" and "Suggested Next-Chat Prompt" sections to this handoff file for seamless chat-to-chat transfer.

### Validation Performed
- Read lints after substantive edits; no linter errors reported in modified files.
- Performed import/smoke check from venv:
  - `./venv/bin/python -c "from sentinel.ui.main_window import MainWindow; from sentinel.constants import DEFAULT_FEED_FPS; assert DEFAULT_FEED_FPS==15; print('ok')"`
  - Result: `ok`
- Verified git working tree includes updated handoff + code changes before commit/push.

### Open Issues / Risks
- `settings.json` continues to store camera credentials in plain text (existing behavior).
- Feed FPS and camera driver FPS controls are best-effort; some RTSP/camera backends may ignore `CAP_PROP_FPS`.
- Host remains 8 GB RAM limited; lower FPS reduces pressure but does not eliminate system-wide contention under heavy multitasking.

### Next Recommended Step
- On OptiPlex migration:
  - install dependencies/venv, then run app with one stream first
  - confirm `Feed FPS` default is 15 and adjust only if stable
  - keep network cameras on sub-stream where possible
- After bootstrapping on new host, run a 20-30 minute soak test with Telegram enabled and recording enabled to validate sustained memory behavior.
