# Sentinel Handoff Summary

Use this file as continuity across chats. At the **start** of a new session, ask the assistant to read this file, continue from **Current Status**, and update this file before wrapping up.

---

## Project

- **Name:** Sentinel Home  
- **Repo:** `https://github.com/billerl/sentinel-home.git`  
- **Primary branch:** `main`  
- **Clone path:** `git clone … && cd sentinel-home` (repo folder name `sentinel-home`)

---

## Current Status (October 2026 restructure — supersedes the Qt notes below)

- **Headless engine.** The PySide6 GUI was removed. It only ever ran invisibly under `xvfb-run`.
  `main.py` → `sentinel/engine/engine.py` (`Engine`) → one `CameraPipeline` thread per camera
  profile (`sentinel/engine/camera.py`). The web viewer is the UI; Telegram is the remote control.
  Feature glue lives in small classes the engine builds and hands what they need (late-starting parts such
  as `presence`/`packages`/`audio` as lambdas, so tests can swap them): `Engine.family`
  (`sentinel/engine/family.py`: phones, pets, places, the Family/Timeline pages) and `Engine.news`
  (`sentinel/engine/news.py`: package, recap, routine and weather messages to every unpaused chat).
  `Engine.cars` (`sentinel/engine/cars.py`): the Vehicles page, the "whose car is this?" and "Wrong car?"
  Telegram buttons, and the driveway's arrive/leave events and car statuses.
  Engine code uses only Telegram's public methods (`is_paused`, `unpaused_chats`, `commands_help_text`).
- **Deploy:** `sentinel.service` runs `xvfb-run venv/bin/python main.py`. Xvfb is no longer
  needed, but the unit still works with it. To deploy without sudo, SIGTERM the python process;
  systemd restarts it in about 10 s, and SIGTERM finalizes in-progress clips.
- **Web viewer:** listens on `127.0.0.1:8088` only and is reached through the Cloudflare tunnel.
  - `/`: live grid plus the motion timeline and clip player.
  - `/zones`: zone/line editor. It uses `/api/config/cameras`; POSTs need JSON plus the `X-Sentinel-Request: 1` header.
  - Cameras get opaque per-process ids, so RTSP URLs never reach the browser.
- **Recordings:**
  - Files are `recorded_videos/YYYY-MM-DD/<camera-ip>_HHMMSS_ms.avi`, indexed in `recorded_videos/recordings.db` by `sentinel/recordings/store.py`.
  - Retention: 30 days, plus a low-disk safety net that always keeps the last 24 hours.
  - Clips are recorded clean, with no overlays, at the camera's real fps.
  - Browsers get H.264 MP4 converted on demand with the bundled ffmpeg (`imageio-ffmpeg`).
- **Motion quality:**
  - Analysis runs at 320 px width.
  - MOG2 shadows are thresholded away, and frames where more than 45% of the picture changes are ignored.
  - Motion must persist for 3 frames.
  - Zone test: the bottom-centre point is in the zone, or the box overlaps it by at least 30%.
- **Cameras** (IPC-D3E43WD-S): main streams were set to 2560x1440 H.264 at 4 Mbps (not used yet).
  Substreams are 640x360 at 12 fps and are used for live view and detection.
- **HD clips** (`sentinel/recordings/hd_recorder.py`):
  - ffmpeg copies each main stream into 2 s **Matroska** segments in `/dev/shm/sentinel-buffer/<ip>`.
  - MPEG-TS segments from these cameras segfault the bundled static ffmpeg 7.0.2 when read back.
  - Finished clips are joined into `<clip>_hd.mp4` with 5 s pre-roll and 3 s post-roll. `recordings.hd_path` stores it; playback and Telegram prefer it.
- **Object detection:**
  - `sentinel/detection/objects.py` runs YOLOX-tiny ONNX on OpenVINO, CPU only. The user isn't in the `render` group and there's no OpenCL runtime.
  - `sentinel/engine/objects.py` has a single `DetectionWorker` thread and a per-camera `CameraObjectTracker`.
  - Tracker rules: 2 hits needed; distance fallback for matching fast movers; label voting; vehicles must move; motionless people need score ≥ 0.65; learned static spots.
  - It also writes the plain-English `describe()` text.
  - Models live in `models/detector/` (gitignored); download them from the YOLOX GitHub releases.
  - Delivery / vehicle-type recognition: `sentinel/detection/vehicle_type.py` runs the CLIP ViT-B/32 image
    encoder (`models/classifier/clip_vit_b32_vision.onnx`, gitignored, see readme) on each alerting
    vehicle's largest crop and compares it with precomputed text embeddings
    (`sentinel/detection/vehicle_prompts.npz`). After editing `VEHICLE_TYPES`, rebuild them with
    `tools/build_vehicle_prompts.py`. Recognised deliveries add a `delivery` clip label and alert group;
    vehicles are re-checked (up to 3 times) as they get closer, which can send a second "📦" alert.
  - Far vehicles (box < 1/4 of the frame width) are checked on the HD stream instead: a background
    thread (`DetectionWorker.run_later`) waits for the segment holding the sighting to close, decodes its
    keyframe (`HDRecorder.keyframe_near`, ~0.6 CPU-s), re-finds the vehicle with YOLOX near the scaled box
    and classifies the sharp crop; the alert photo becomes an HD close-up. Adds ~3 s to those alerts;
    at most one grab per camera per 2 s, and skipped when two jobs are already queued.
  - `web.show_zones` (live-page "Zones: on/off" button) only hides the drawn outlines.
  - Camera substreams were raised to 1280x720 @12 fps, 2048 kbps VBR (2026-10-22): sharper live view and
    car close-ups; engine ~95% of one core (was ~43% at 640x360). If more cameras need the headroom,
    drop substreams back to 640x360 first. The grid asks `/snapshot.jpg?w=` for tile-sized JPEGs
    (rounded to 320/480/640/960, capped at 1.5x device pixels) so phones don't pull full frames.
  - Camera settings changed via ISAPI with the admin logins (not stored): OSD names, NTP hourly with
    correct Eastern DST, HLC on the Garage camera. Smart supplement light (IR + white light on events)
    was already on. No SD cards installed.
  - Other camera brands: the Add-a-camera form has a brand menu (paths in `camera_probe.BRAND_PATHS` and the
    page's BRAND_PATHS), an optional high-res path (saved as `record_source`), and "Test connection"
    (`camera_probe.probe_camera`: TCP reach, one RTSP DESCRIBE with digest/basic auth for clear
    401/404 answers, then a real frame). `main_stream_url` guesses high-res paths for Anpviz/Hikvision,
    Dahua/Amcrest, Reolink, Uniview, Tapo and Foscam. These Anpviz cameras answer unknown paths with the
    main stream, so the probe warns when the "substream" is wider than 1280 px.
  - Household vehicles (`sentinel/engine/household.py`, page `/vehicles`, DB `recorded_videos/household.db`,
    photos in `recorded_videos/.household/`): the user names groups of photos; those become per-car CLIP
    references (separately for day and infrared). `PresenceTracker` checks every camera once a minute:
    parked = same spot in 2 scans, left = missing 3 scans (8 when the spot can't be compared in this light).
    At night the detector misses parked cars, so scans also run on an equalizeHist copy, and a missed car
    whose spot still looks the same (`spot_look`, a day and an IR picture per spot, NCC >= 0.6) is still
    there; people over an empty spot only pause the count. Paint colour (`crop_colour`) counts only in
    daylight or under Sentinel's own white light, never lamp light; a daytime photo clearly closer in
    colour to another car isn't auto-filed (`COLOUR_VETO`: ten Edge photos once snowballed under the
    Fusion). White light for cars (`camera_light.py`): at night a moving car turns the camera's white light
    on for 150 s via ISAPI with the admin login in git-ignored `camera_admin.json` (Sentinel's own camera
    login can only watch); colour then picks between close shape matches (`LIT_POOL`). Identity sticks to the parking spot, unknown
    cars are retried each scan and their arrival is recorded (time = first seen) once named. Arrivals and
    departures go into the events table (grp `household`) and answer "when did X get home / leave",
    "is X home", "what cars are in the driveway". Infrared detection (`is_greyscale`) uses per-pixel
    channel spread (IR ≈ 3, day 20+). CLIP separates cars well by day, poorly at night — rely on spot
    stickiness and naming at dawn rather than lowering thresholds. A guessed name (`unchecked`: any name the family didn't
    give, in the dark or by elimination) is re-checked by daylight (`_recheck`, ruling out only other spots' sure names): paint colour doubting it + the same other car matched
    RENAME_AGREE (3) checks in a row renames it (`on_renamed` relabels its "got home" event); DOUBT_SCANS (10)
    doubts without a better match drop the name (event removed, whose-car question, re-recorded once named;
    the doubted car is `ruled_out` for that parking). Names the family gave are never overruled.
    Fingerprints: DINOv2-small (`car_fingerprint.py`) when its model is installed, else CLIP; each model has
    its own thresholds (`BARS`, DINOv2's calibrated on the household's photos, incl. a "far ahead of every other
    car" rule). `HouseholdStore.use_fingerprint` redoes saved photos made with the other model from their crops
    at startup (engine thread `_use_car_fingerprints`; `ready` False meanwhile, so nothing is named); the
    `clip` column keeps CLIP's embedding for the body type used in suggestion groups.
  - Live page on phones (≤760 px wide, or a short touch screen in landscape): page scrolls, cameras stack
    (two across in landscape), and "Live / Events" tabs at the bottom; Events adds a tap-friendly clip
    list (newest first) under the timeline bars. Desktop layout is unchanged.
  - HD ffmpeg processes die with Sentinel (PR_SET_PDEATHSIG) and the buffer folder is recreated if
    missing. Run any second/test engine with `SENTINEL_BUFFER_ROOT` set: two engines sharing
    /dev/shm/sentinel-buffer wipe each other's buffers, and orphaned ffmpeg hold camera sessions (the
    cameras then refuse new RTSP sessions with "500 Internal Server Error").
  - Tracker matching (`CameraObjectTracker.update`): overlap matches first for every track, then nearness
    (reach = one vehicle length) for leftovers, never onto a box where another live track stands. Before
    this, a parked car missed for a frame at night took its neighbour's box and "drove" across the
    driveway (29 false alerts replayed from 2026-10-24/25; 3 real ones after).
  - Never-ending motion (`sentinel/detection/nuisance.py`): a 16x9 grid; a cell active in 30 of the last
    90 ten-second slots is muted (motion wholly inside muted cells doesn't start recordings), unmuted after
    5 quiet minutes or at once when the object detector reports a real subject there (`note_subject`, which
    also starts a recording). Object detection still runs on muted motion (`activity_streak`). Made for a
    spider web swinging in front of the Garage lens in IR (~1500 clips one night).
  - Weather tile (`sentinel/engine/weather.py`, settings `weather_location`/`weather_units`, `/api/weather`):
    Open-Meteo, free/no key, server-side, 10-min cache; shown only in a grid cell that would be empty.
  - QR sign-in for the Fire TV: `/login` shows a QR on wide screens (`POST /login/pair`), a signed-in phone
    approves at `/pair/<code>`, the TV's poll (`/login/pair/<code>`) gets the session cookie. Codes: one use,
    3 min, bound to the requesting IP, 3 per IP. Sessions persist as SHA-256 token hashes in
    `.web_sessions.json` (0600), so restarts don't sign anyone out. The grid reloads itself when
    `X-Sentinel-Boot` changes (after an update) and has a Reload button (Fire TV has no F5).
  - "Keep me signed in" (and QR-paired TVs) = a remembered session: lasts `session_max_days` (365) with no
    idle limit and no IP binding — phones on mobile data change IP constantly. Unticked logins keep the
    60-min idle limit and IP binding.
  - Claude fallback (`sentinel/engine/assistant.py`, settings `assistant_enabled`/`assistant_daily_limit`/
    `assistant_model`): messages none of the built-in parsers understood are queued (`consume_free_text`) and
    answered by the official `claude -p` (user's own Max login on this PC; allowed for personal use of the
    unmodified binary; `--bare` can't be used with a subscription login). Locked down: `--tools ""`, fixed
    `--system-prompt`, `--max-turns 1`, no session files, empty MCP config, cwd ~/.cache/sentinel-assistant,
    ANTHROPIC_API_KEY removed from env. Context = last 48 h of events (150 lines max), cars home, cameras,
    weather, command list (~9k chars; ~5 s with Sonnet). Every miss is logged as `telegram_not_understood`
    so common phrasings can be taught to the rule-based parser later.
  - Voice assistant (2026-10-27, `sentinel/audio/voice.py`, settings `voice_enabled`, `voice_follow_up`,
    `voice_wake_threshold` 0.5 (Settings > Audio field since 2026-11-04), `voice_gain` 4, `voice_wake_model`, `audio_output`): one `arecord` on
    plughw:0,0 (front "Headphone Mic"; mixer set by Sentinel at start since it isn't persisted) -> 150-4000 Hz
    band-pass -> openWakeWord hey_jarvis (gain 4; ~0.35 core) -> after the wake word, recording until 1 s of
    quiet (thresholds follow the room's noise) -> normalised -> faster-whisper base.en int8, hinted with the
    household's names from the Family/Vehicles pages (`whisper_prompt`, `Engine._voice_names`) (~2.3 s per
    question; segments with no_speech_prob > 0.6 and stock inventions like "Thank you" dropped). Recording
    starts at the wake word, so one-breath "Hey Jarvis, who's home?" works. `Engine.answer_voice`: time,
    weather, enable/disable (asks first), household and event questions locally (`to_speech` strips emoji,
    bullets, links, long lists), else Claude. Requests both Telegram and voice understand (recap, package taken
    in, pets going out / recall, where is X, family cars, movement inside) are registered once each in
    `Engine._build_commands` (`sentinel/engine/commands.py`): Telegram's question filter, `answer_question` and
    `answer_voice` all ask that registry. Then Claude with `VOICE_SYSTEM_PROMPT`; Claude's COMMAND needs a spoken
    "yes". Follow-ups without the wake word for 5 s, at most 5 turns. A follow-up Whisper doubts (no-speech > `FOLLOW_UP_DOUBT` 0.3)
    is acted on only if Sentinel understands it itself (`answer_voice(doubtful=True)` returns None before Claude),
    else ignored (`voice_ignored`): room talk had been answered by Claude, but real "Pause"/"Stop" score ~0.55. Alerts stay silent during a
    conversation (`AudioAlertSystem.busy`/play lock). openWakeWord's `reset()` keeps its audio buffer, so
    2 s of silence is pushed through after each conversation or it re-wakes on the old "Hey Jarvis".
    `audio_output: remote` plays via `paplay` to /run/user/<uid>/pulse/native (the xrdp session), falling
    back to aplay; "speakers" = `aplay -D plughw:0,0` (front line-out jack; the service has no PipeWire, so
    ALSA "default" fails with "Host is down"). main.py ends with `os._exit(0)` after `engine.stop()`: CTranslate2/onnxruntime crashed in
    interpreter teardown and the core dump delayed a restart by a minute.
  - Spotify (2026-10-27, `sentinel/engine/spotify.py`, settings `spotify_client_id`, `spotify_device`,
    `spotify_refresh_token` [never sent to the browser]): Web API only, music plays on the family's own
    Connect devices (Fire TV app, phones, "Alex's Echo Dot"). PKCE; one-shot listener on 127.0.0.1:8888 or
    paste-the-address. `parse_music_command` handles play/pause/skip/previous/volume/now/move incl. Whisper's
    "Place on Tom Petty". Search: exact artist -> artist, exact album, matching track, "some <genre>" ->
    first non-Spotify-owned playlist (Spotify's editorial playlists are blocked for new apps since Nov 2024).
    Dev-mode app: the account's email must be listed under User Management. Never run a second
    SpotifyService on the live settings (a refresh can rotate the token and log the engine out; the dev
    harness blanks it). Live page: now-playing bar (/api/spotify, polled 5 s; ticker on phones).
    An unknown speaker name by voice ("play Spotify on the kitchen") offers the list (VoiceReply.confirm).
    "Play X on the TV" when no TV is a speaker (2026-11-03): `SpotifyService.open_on_tv` (engine wires it to
    `SmartSwitches.open_app`) turns the TV on and opens its Spotify app, says "Opening Spotify on the TV", then
    polls the speaker list for up to 30 s (`TV_APPEAR_SECONDS`). A TV Spotify still lists but that answers 404
    (switched off with its own remote) gets the same treatment. Only words naming a TV do this.
  - The PC as a Spotify speaker (2026-10-28, `sentinel/audio/spotify_player.py`, setting
    `spotify_speaker_name` [default "Sentinel"]): supervises `bin/spotifyd` (v0.4.2 release binary, not in
    git) with `.spotifyd/spotifyd.conf` (generated each start) and `.spotifyd/oauth/credentials.json` (from
    `spotifyd authenticate`, port 8898; pasted redirects are forwarded to its listener). Discovery is off
    (ufw is on; no firewall change needed). Runs only once signed in; restarts with back-off. Audio:
    `alsa-sentinel.conf` via `ALSA_CONFIG_PATH=/usr/share/alsa/alsa.conf:<it>` (only Sentinel and its
    children): dmix on hw:PCH,0; `sentinel_speaker` (voice) and `sentinel_music` (softvol control
    "Sentinel Music", ducked to 60% ≈ -20 dB while `AudioAlertSystem.busy`). "Here", "this speaker",
    "the computer" = this speaker. Voice volume commands (`parse_volume_command`): "turn the PC down" =
    Master (setting `pc_volume`, re-applied at start since ALSA forgets it), "speak up" = `volume`.
  - Family locations (2026-10-27, `sentinel/engine/location.py`, `recorded_videos/location.db` [mode 600]):
    OwnTracks HTTP mode posts to public `POST /owntracks` (HTTP Basic: per-person username + random token;
    20 failures/10 min per IP -> 429; no Cloudflare Access in front, checked). Tables: people (with a 7-day
    `invite` code for `/join/<code>`, the public setup page: consent, App Store/Play links, `owntracks:///config?
    inline=` link + .otrc download + manual values, permission steps, polls `/join/<code>/status`), places
    (name as said after "at"; "Home" special; radius 50-2000 m), fixes (one row per person: latest position,
    current place, place_since NULL = there when tracking began), visits (arrived/left, pruned at 30 days).
    Every location post is also kept in `points` (person_id, ts PK; late/duplicate posts kept once) for the
    Family timeline, until the person deletes it (2026-10-31: the user asked for a Google-Timeline-style
    history; the setup page's consent text says so). Geofence: arrive inside radius with acc <= max(250, 2r); leave beyond
    radius + clamp(acc, 100..300) with acc <= 500; phone transition events (waypoints in the setup config)
    trusted by place name; late out-of-order fixes ignored. Rule from the user: the phone is the person,
    the car is separate ("Elizabeth is at the salon. Elizabeth's Fusion is at home."); person<->car link = the
    vehicle's `owner` matching the person's name/aliases. Questions (`location_question`) are checked before
    car questions in Telegram and voice; anything naming a car word stays a car question. Alerts: settings
    `location_alerts_enabled` + per-place checkbox, 15 min cooldown per person+place, respects Pause.
    Telegram: a 📍 pin then a name adds/moves a place (`text_interceptor`); "where" answers send map pins
    for people between places. Live page: `/api/family` -> lights row in the weather tile (tile gets class
    `fam` to tighten spacing; measured to fit at 1280x800, 1920x1080 and phone width).
  - Pets with collar trackers (2026-11-06): a person with `tracker` set (Meshtastic node id) is a pet. A LoRa base
    station (Pi + meshtasticd) runs `tools/meshtastic_bridge.py`, which posts the tracker's positions to /owntracks
    with the pet's own login (acc from precisionBits/HDOP, so public-channel blur never counts) and checks in
    every 20 s with `{"_type": "status"}`. The yard (table `yard`, one outline tapped on the Family page) gives
    `left_yard`/`back_in_yard` changes (outside = beyond the edge by max(8 m, acc); fixes > 50 m ignored;
    `fixes.in_yard`; a change needs two readings in a row, `_yard_pending`, since 2026-11-07: indoor strays) -> Telegram + map pin + speaker, 5 min cooldown. Pets skip place alerts and person routines
    and don't count for `Family.nobody_home`. "The dogs are going out" (`outing_command`): for an hour, check-ins
    get `reportLocation` every 30 s and the base asks the collar (`sendPosition(wantResponse=True)`).
  - Mesh chat (2026-11-08, `sentinel/engine/mesh.py`, page `/mesh`): the same bridge also checks in at
    POST /mesh/bridge (Basic auth = the first tracker's login; `bridge_checkin` refuses non-trackers) with the
    base's id/name, channels (public = default/no key), invite links and every text it heard; Sentinel answers
    with messages to send (`send`, handed out until the base reports sent/relayed/delivered/failed; given up
    after 10 min), `want_nodes` (node list at most once a minute; kept in memory) and one-shot
    `make_channel`/`rename`. Conversations: peer = node id (direct, only those addressed to the base) or
    "#<channel index>"; inbound deduped on (sender, packet id). Direct messages go out on the public channel
    (or the private one the radio was heard on), never the primary "Sentinel" one (the collars'); with known
    keys the firmware encrypts them end to end. Invites: the base's contact URL (meshtastic.org/v/, public key
    only) and, per private secondary channel, an add-only channel URL (meshtastic.org/e/?add=true#, holds the
    key; the page response is no-store). The primary channel is never offered. Telegram: direct + private
    channels, setting `mesh_alerts_enabled`, respects Pause. Pets' collars are left out of the radio list.
    Nicknames (table `nicknames`, node -> name; Sentinel-only, never sent to the mesh) replace the radio's name in
    chats, the radio list, thread senders and Telegram; `radio` / `nick` fields keep the radio's own name.
    Unread (direct + private channels only): `unread()` -> /api/mesh/unread, polled every 20 s by grid.js, a
    pulsing 📡 pill at the end of the weather tile's family row (`.wx-family a.mesh`; checked at 960x540 with 6-8
    lights). Jarvis announces (`_announce`, setting `mesh_speak_enabled`, quiet 22-7, 2 min per sender) and
    "read my mesh messages" (`read_command`, in the command registry: Telegram + voice) reads up to 5 and marks
    them read. The wrong-login lockout for phones, setup links and the base station is now shared
    (`WebContext.device_login_blocked/failed`).
  - Recipes (2026-11-08, `sentinel/engine/recipes.py`, page `/recipes`, recipes.db + recorded_videos/recipes/
    photos): save a link (page, Telegram `save_link_command`, Android share target in manifest.webmanifest ->
    /recipes?url=). Saving is a background job (`start_save` -> /api/recipes/job/<id>). Pages: schema.org Recipe
    JSON-LD (`recipe_data`, handles @graph/HowToSection/ISO durations), else Claude reads the page text
    (`Assistant.complete`, a new one-off call with its own system prompt, same lockdown and daily limit).
    Videos (`VIDEO_LINK`: Instagram reels, TikTok, Shorts, Facebook): yt-dlp in the venv (keep it current;
    Instagram reels download without a login as of 2026-11-08), audio via the bundled ffmpeg -> Whisper base.en
    (faster-whisper's own decoder fails with this PyAV); when caption + speech hold < 3 amounts, on-screen text
    is read too (RapidOCR in the venv, a frame per 1.5 s, unchanged frames skipped, max 40; ~50 s for a 1-min
    reel; Instagram's auto captions aren't downloadable). Claude writes it up marking guessed amounts "(est.)",
    the cover (or the sharpest frame) is the photo, the URL is kept ("▶ Watch the reel"). Pasted text works too.
    Fetching only reaches public addresses (`_public`, every redirect checked). A recipe has `original` and
    `ours` (the family's version; edits/swaps/twists are drafts in the page until "Save as our version").
    Swap = Claude options per ingredient (amounts as written, step rewrites); twist = Claude rewrites keeping
    ingredient ids. Cooking mode posts its step to /api/recipes/cooking; Jarvis/Telegram cooking commands
    (`cooking_command`: next/back/repeat/go to step N/how much X/ingredients/timer) only while it's open (4 h),
    the page polls to follow; the timer is spoken by Jarvis when done. "What can I make with X" ranks the box
    locally (`can_make`). The TV tile's "Open remote" is now its own big button beside the power tile.
  - Backup (2026-11-08, `sentinel/engine/backup.py`, Settings → Backup, state in recorded_videos/backup.json):
    nightly at `backup_time` (03:30, `due()` like the recap, 3 h window) and "Back up now". Finds USB filesystems
    with lsblk (transport passed down to partitions) and mounts them with `udisksctl mount` — the service is
    outside the desktop session, so that needs the polkit rule in installation-guide.md "Backup drive". Only a
    drive with a "Sentinel Backup" folder is used ("Use this drive" makes it + README.txt); `_remove` refuses any
    path outside that folder (the family keeps their own files on the drive). Layout: snapshots/<date>/ (every
    *.db via SQLite's backup API, the small files and folders of recorded_videos, settings.json; last 7 nights),
    mirror/ (wifi_motion logs, models/, firmware/ minus build dirs; kept in step), videos/<day>/ (the last
    `backup_video_days` days, newest first; when full, older days give way). `backup_remote` (SSH host,
    validated against option smuggling) gets the snapshot by rsync into dated folders hard-linked to the night
    before (last 7). Problems (drive gone after it was set up, copy errors, remote failure) go to the first
    Telegram chat.
  - Family timeline + personal logins (2026-10-31; restore point: tag `pre-timeline` +
    `../checkpoints/pre-timeline/`). `/timeline`: person chips, month calendar (days with points), the day's
    stops and drives (`sentinel/engine/timeline.py`: positions with acc <= 300 m clustered within 120 m;
    a stop lasts 5+ min and isn't all >= 10 km/h; a phone quiet > 10 min before a position elsewhere stayed
    until the drive there (30 mph guess); one-point GPS blips glued back; same-place stops joined) on a
    Leaflet map (vendored in static/leaflet, OSM tiles darkened by CSS filter; tile requests send the site
    origin as Referer, which OSM's policy wants). Unnamed stops: "1.0 mi northeast of Home" + "Name this
    place" (POST /api/places with the stop's coordinates, radius 100). The user chose: everyone signed in sees
    everyone's whole history (all immediate family); only the person, signed in as themselves, can delete
    theirs (`/api/timeline/<id>/delete`, 403 otherwise; the Family page's "Forget location" also clears it).
    Personal logins: people.web_hash (bcrypt) + one-use 7-day `/account/<code>` link from the Family page
    ("Make login link"; they choose a password, 8+ chars, and are signed in). /login tries the shared
    household login first, then the person's (username = their OwnTracks username; "alex" is both, the
    password decides); person sessions are stored as username "person:<username>" (`PERSON_PREFIX`).
    Everyone has the same rights for now; the user plans to make the shared login admin-only once
    personal logins are proven. Settings → Web password changes your own when signed in as yourself.
  - Voices: Kokoro (`models/kokoro/`, kokoro-onnx, 3 threads) with voice ids `kokoro:<name>`; household uses
    `kokoro:af_heart`. Sentences are synthesised while the previous one plays, into one raw aplay/paplay
    stream (~1.3 s to the first word). Piper stays loaded as the fallback. By voice, Claude may answer
    general questions (VOICE_SYSTEM_PROMPT); house facts still only from DATA. Claude limit 100/day.
  - CPU: the Dell BIOS locked the i5-9500T at 800 MHz (unrecognised adapter). Root service
    `sentinel-cpu-unlock` (source ~/Documents/sentinel_home/cpu-unlock/, not in this repo) re-enables turbo
    via MSRs and caps RAPL at 30/40 W. Timings in older notes were measured at 800 MHz.
  - Presence flip-flops fixed (2026-10-25): a parked car partly hidden (box IoU 0.2–0.5, doors open) or
    covered by a person no longer counts as missing; an unknown parked car is named only after
    IDENTIFY_AGREE (2) confident checks in a row (it's re-checked every scan, so one lucky match used to
    stick); MATCH_MIN/MARGIN raised to 0.88/0.03 (leave-out test on the household photos); "arrived"/"left"
    only when the car isn't also parked in another spot. Photos are auto-filed only when the photo itself
    passes the strict AUTO_MATCH bar (by day) or the parking was clearly recognised by day (night): filing on
    the spot's identity alone had put 18 photos of an unnamed grey sunroof sedan under the blue Edge (and 2 of
    a white Crown Vic under the Jeep). Those 20 were unfiled (vehicle_id NULL); backup
    recorded_videos/household.db.bak-20261025. The grey sedan (middle spot, probably Elizabeth's Fusion from the
    re-aimed camera's angle) is now unknown until the family names it.
  - Car fixes (2026-10-27, from the family's voice notes; backup ../checkpoints/pre-car-fixes/):
    - Parking spots decide motion alerts (camera `_parked_car_stays`): a vehicle track on a spot the
      presence check saw (before the track began) stays quiet while that spot is occupied (a car box IoU>0.3,
      or a person covering it); one that started on a spot and left it says "X is leaving", named from the
      spot. Was: box changes (all four doors open) read as "Alex's Truck is driving past" on a parked Mercedes.
      2026-11-06: "leaving" also needs the spot to stop *looking* like its car (`PresenceTracker.still_parked`:
      `spot_look` vs the scans' looks and the camera thread's last view of the car there, `Parked.seen_look`, so
      the white light switching on doesn't break the match) for LEAVE_EMPTY_FRAMES frames and LEAVE_EMPTY_SECONDS
      in a row; one matching frame resets it. Rain and a cobweb across the infrared picture had the detector lose
      the parked Edge and its track hop onto the next car: six false "Edge is leaving" in 15 minutes. The white
      light ignores tracks still on a spot that looks the same (it had hidden the Civic from the driveway check
      for 8 minutes, which then said it left). Replay of 06:10-06:36: 6 false alerts -> 0; five real departures
      (day, dusk, night) alert as before.
    - Naming: `identify(exclude=, colour=)`. Cars parked elsewhere are ruled out; near-ties by day are
      settled by paint colour (`colour_signature`, HSV histogram of the crop's middle; COLOUR_POOL 0.03,
      COLOUR_MARGIN 0.08). Leave-one-day-out on the 140 family-named day photos: 38 right/2 wrong -> 69/6.
      A trained classifier (logistic regression on CLIP) was no better.
    - "Not ours" snowball fixed: auto-hidden photos (ignored=1, auto=1) no longer hide more photos; only the
      user's marks (auto=0) count, and only when closer than any family car by AUTO_MARGIN. It had hidden
      18 -> 96 photos/day. 195 hidden since 2026-10-21 were given back (all night ones, plus clear family-car
      day ones); 99 day ones kept as "not ours".
    - The 3-a-day "whose car?" limit is kept in .household/asks.json (restarts used to reset it: 10 asks).
    - Parked cars survive a restart in .household/presence.json, including each spot's day/night look:
      without it, a car the detector misses at night "left" 3 minutes after a restart (2026-11-07).
    - Telegram car alerts carry "✏️ Wrong car?" / "🚗 Whose car is this?" (`fx:` buttons): pick a family car
      or "Not ours"; the photo is filed as a user label and a still-parked spot takes the new identity.
  - VLM evaluation (2026-10-25): Tencent WeVisOne-VL-2B on this CPU took 3.5–6.5 min per image (good
    answers). Not usable without an NVIDIA GPU; YOLOX-tiny + CLIP stays.
- **Alerts:**
  - `alert_mode` is `objects` by default: a Telegram photo plus a natural sentence, then the clip.
  - The per-camera/per-type cooldown is `object_alert_cooldown`.
  - The user's `telegram_motion_alerts` was **off**; it was left off.
- **Reminders** (`sentinel/engine/reminders.py`, `recorded_videos/reminders.db`): Claude turns "remind me
  Wednesday at 9 ..." into the exact command `Remind YYYY-MM-DD HH:MM <what>`; it is saved only after a ✅ tap
  (Telegram) or a spoken yes, then sent on Telegram at that time (voice ones to every chat; late ones say so).
  `Reminders` lists, `Cancel reminder N` removes. Checked every 15 s in the command loop.
- **Samsung TVs** (`sentinel/engine/samsung_tv.py`, a second SmartSwitches driver; TV state, token and app
  list in `recorded_videos/samsung_tv.json`, mode 600): found by SSDP; REST :8001 (who it is, apps, launch)
  and the remote-control websocket :8002 (keys; minimal RFC 6455 client, no new dependency). First use
  needs "Allow" on the TV. Answering = on (2018 models have no PowerState; the network drops within ~5 s
  of the picture). On = Wake-on-LAN until it answers (~10 s, needs "Power On with Mobile"), then KEY_POWER
  (the packet only wakes its network). `tv_parse`/`tv_command` in switches.py: open/put on <app>, volume,
  mute, "what's on"; `understands()` routes Telegram/Claude suggestions. Can't pick channels inside apps.
  Playback (2026-11-07): pause/play (KEY_PAUSE/KEY_PLAY), rewind/fast forward N seconds/minutes = arrow
  presses of SEEK_STEP (10 s, sent in the background), "mute for N" = KEY_MUTE + a timer that presses it again
  (cancelled by a Sentinel mute/unmute meanwhile). By voice, said without "TV" they still mean the TV while it's
  on (the family's rule: TV unless Spotify/"the music" is named); Telegram's bare "pause" stays the alerts'.
  Power state is tracked by Sentinel (dark network wake-ups every few minutes look like "on"; SCREEN_AFTER).
  KEY_POWER only works once the TV is fully awake (port 9197 answers; ~30 s after a wake, and closed in the
  shutdown minutes after "off"), so `_power_on` waits for it, then presses again if the TV drops away.
  KEY_POWERON/KEY_POWEROFF are ignored by a 2018 model. "Off" presses power whenever the TV answers unless
  Sentinel itself switched it off <4 min ago (a TV on via its own remote showing cable looks "off" at first).
  Web remote (grid.html `#tvr`, "Open remote" on the TV tile): `/api/tv/<id>` (+ `/key` from the TV_KEYS
  list, `/open`, `/type` = SendInputString, `/icon/<app>` = the TV's own icon via `ed.apps.icon`, cached in
  `recorded_videos/.tv_icons/`). Keys go over a kept-open connection (`_send`, closed after 60 s idle).
- **Fire TVs** (`sentinel/engine/fire_tv.py`, third SmartSwitches driver, needs `adb-shell`): only ones with
  ADB debugging on (port 5555; first connection = "Allow USB debugging?" on the TV; key in
  `recorded_videos/.adb/`). Id = hash of the serial number (Fire OS hides its MAC from ADB). Real screen state
  from `dumpsys power`; on/off = KEYCODE_WAKEUP/SLEEP; remote keys mapped from the Samsung names (`KEYS`,
  `supports()` hides the rest in the remote); apps = `pm list packages -3` + known Amazon ones (`APP_NAMES`).
  A Fire TV running Fully Kiosk (the live view) gets "Show/Reload cameras" (`show_live`) and "put the cameras
  on the TV" / "reload the cameras". Commands name the TV ("on the Sentinel TV"); unnamed = the Samsung.
- **TV screen on the phone** (`sentinel/engine/tv_mirror.py`, `/api/tv/<id>/screen` MJPEG, `/api/tv/<id>/tap`):
  while a Fire TV's remote is open, the TV's hardware encoder records its screen (`screenrecord`, 960x540 H.264,
  started detached with `setsid nohup ... & sleep 1` or it dies with the shell) into `/data/local/tmp`; Sentinel
  reads the new bytes every 0.25 s with `tail -c +N` over the normal ADB connection (a live stream would hold the
  one connection the key presses need; a PNG screenshot takes ~10 s on that TV). The bundled ffmpeg decodes to
  JPEGs: one thread each way + `-fps_mode passthrough`, else it holds back ~6 frames; after a quiet poll an access
  unit delimiter (`FRAME_DONE`) releases the last frame of a still menu. No viewer for 15 s: recording stopped and
  the file removed. Tapping the picture = `input tap`. Copy-protected apps (Netflix...) show black.
- **Home screens off the tunnel** (`server._keep_home_screens_home`, `/handoff`, `auth.create_handoff`):
  `web.bind_host` may list several addresses (one werkzeug server each). When one is a home-LAN IPv4, a TV
  browser (`TV_BROWSER`) loading a page through the tunnel whose `Cf-Connecting-Ip` is in this PC's own public
  IPv6 /64 (`_from_this_home`, read from /proc/net/if_inet6) is redirected to `http://<lan>:<port>/handoff`
  with a one-time 60 s ticket that signs the same login in there. The camera TV had been pulling ~3 Mbit/s
  out through Cloudflare and back, all day. Settings edits: stop the service first (it saves on shutdown).
- **Severe weather warnings** (`sentinel/engine/weather_alerts.py`): api.weather.gov active alerts for the house
  (`home_coords`, rounded to 2 decimals) every 2 min. `level()`: Warnings/Emergencies with severity Severe/Extreme
  (minus slow ones like Flood Warning) = "loud": said out loud day or night + every chat (ignores pause) + the red
  `/api/weather/alerts` banner on the camera and Home pages; other warnings and watches = Telegram + banner;
  advisories/statements ignored. Announced ids (with their level) persist in `weather_alerts.json`; an Update
  referencing an announced id stays quiet unless it became loud. "Any weather alerts?" by voice.
- **Package alerts** (`sentinel/engine/packages.py`, `sentinel/detection/package_check.py`): every 10 s the door
  cameras' snapshot (names with door/porch/entry/step, or `package_cameras`) is compared with the last one without a
  person (`changes()`: brightness-matched diff, compact blobs, not the clock strip or edges). Only within
  VISIT_WINDOW (3 min) after the camera pipeline saw a person (`visit()`, every person detection ≥0.45) and once
  nobody has been seen for 20 s. A blob CLIP calls a package (PACKAGE_MIN 0.7; prompts in package_prompts.npz,
  built by tools/build_vehicle_prompts.py) on two snapshots in a row is reported with a photo, stored as a
  "package" event (visit headline, delivery questions, recap) and fires "seen: package" routines. Sunset reminder if
  still there; "gone" message only when nobody is home. Replay of 18 days of Front Door clips: see the commit.
- **Morning recap** (`sentinel/engine/recap.py`): at `recap_time` (07:00; skipped if Sentinel was down past 9:00)
  every unpaused chat gets the night since `recap_since` (21:00) from the events table (people, deliveries, animals,
  cars that stopped, a count of cars driving past, family cars, then parcels waiting, warnings and the weather) and
  an H.264 highlight video: up to 8 moments x 4 s cut from the motion clips with OpenCV, captioned, piped to the
  bundled ffmpeg. "Recap" in Telegram sends one for last night until now.
- **Tasmota plugs** (`sentinel/engine/tasmota.py`, fourth SmartSwitches driver): plugs running the open Tasmota
  firmware, over its local HTTP API (`/cm?cmnd=Status 0`, `Power<n> On|Off`, `FriendlyName<n>`). Tasmota doesn't
  announce itself, so the home /24 is swept (port 80, then Status 0) when none is known, when a known one stops
  answering at its address, and every 3 h. Several relays = one switch per relay. No web-password support.
- **Spotify on this PC is blocked by Spotify** (2026-11-03): it refuses audio keys to unofficial players
  ("Spotify refused the audio key"; spotifyd 0.4.2 and go-librespot 0.10.5 both tried). `spotify_fallback_device`
  (Settings > Spotify): music asked for on this PC is checked ~8 s after it starts (`_check_this_pc`) and, if not
  playing there, moved to that speaker; this PC is then skipped for a day (`_pc_broken_until`).
- **Sound cards** (`sentinel/audio/devices.py`, Settings > Audio): `voice_device` (mic, e.g.
  `plughw:CARD=USB,DEV=0` for a Jabra Speak 410: voice 29 dB above room noise vs 13 dB on the front mic jack,
  Whisper clearly better), `audio_device` (voice/chimes card) and `music_device` ("auto" = the PC's line-out when
  its jack-sense says something's plugged in, else the voice's card). Cards by ALSA id, not number. Non-default
  choices write a runtime ALSA conf (`use_cards`, its own dmix ipc_key per card) used via `alsa_env()`; volume
  follows the card ("Master" on PCH, "PCM" on the Jabra). The front-mic mixer setup runs only for the PC's jack.
- **Smart plugs and switches** (`sentinel/engine/switches.py`, known devices in `recorded_videos/switches.json`):
  Kasa plugs/power strips/wall switches over their local protocol (TCP/UDP 9999, XOR-scrambled JSON; no
  cloud, no new dependency). Found by UDP broadcast at start, every 15 min and on "Find new devices"; known
  ones are polled every 10 s (changes from the plug's button or the Kasa app are logged as
  `switch_changed source=outside`). Each outlet of a multi-outlet plug is its own switch (id
  `kasa-<mac>-<n>`, stable when the router hands out a new address). Names come from the plug (renaming
  writes back to it). Live page: 💡 Switches drawer (desktop) / Switches tab (phones), `/api/switches*`.
  Voice and Telegram: "patio lights on", "turn off the patio lights", "are the patio lights on?" (switch
  immediately; Claude's suggestions ask yes first / show a ✅ button). New makes = another driver class
  with `discover/status/set/rename` (Shelly and Tasmota have local HTTP APIs). Newer KLAP-only Kasa/Tapo
  devices don't answer on 9999 and aren't supported. No automations yet (e.g. lights on for a person at night).
- **Wi-Fi motion sensors** (`sentinel/engine/wifi_motion.py`, firmware in `firmware/`, page `/sensors`,
  `/api/wifi_motion*`, known boards in `recorded_videos/wifi_sensors.json`, every score in
  `recorded_videos/wifi_motion/YYYY-MM-DD.csv` for 14 days). An ESP32-S3 measures CSI (52 sub-carrier amplitudes,
  normalised per frame against AGC jumps) and serves a "score" (mean per-sub-carrier std over 1 s, x1000) 4x/s at
  `GET /status`; Sentinel finds boards by their UDP hello (port 47301, the packet's source address is used) and
  polls once a second. Frames to measure: every board (firmware 2.x, one firmware for the Feather, XIAO and
  generic S3 boards) joins the home Wi-Fi and broadcasts fixed-rate HT20 MCS0 ESP-NOW frames 50/s on the router's
  channel, and measures only those frames (HT MCS0 20 MHz) from the other boards; a board that hears no other board
  pings the router 50/s and measures its replies. The router alone is not enough: on a weak link it falls back to
  802.11b (no CSI), even when the board leaves 11b out of its rates. Each board heard is its own "line" (`links` in
  /status, ids `<board>-<link>`, e.g. `8d24-df1c`; the router line `rtr` only while no other board is heard) with its
  own quiet level and motion. Two boards hearing each other measure one path from both ends: the Sensors page and
  `view()` group them by `path_id` (the two board ids sorted, e.g. `2350-8d24`), one card per path, moving when
  either end is; names are stored per path. Each board hears ~32-45 of the 50/s (it can't listen while sending).
  `GET /identify` flashes a board's LED for 20 s. A board on USB takes the Wi-Fi as one serial line
  `wifi<TAB>ssid<TAB>password`. The send-only `firmware/wifi_motion_beacon` still works as a line source. Motion: the 1 s
  median above 1.4x the quiet level for 0.5 s, held 15 s. The quiet level = the median of the last 30 s whenever
  those were steady (p90/p10 <= 1.25; still 1.08-1.13, walking 1.49-1.68), so a steady jump (someone settled
  next to a board, a door) is learned within ~1 min instead of reading as motion; re-learned after a board restart
  or a switch between beacon and router. Tuned on one evening's recording (`MotionJudge` is pure: replay the CSVs).
  A board on the far side of a stone wall barely sees anything: same floor, open line. Setup: a new board makes an
  open AP "Sentinel-Sensor-<id>" with a page at 192.168.4.1 (Wi-Fi password stays on the board); OTA via espota
  (password in the git-ignored `firmware/wifi_motion/secrets.h`). Routines can start on them (see Routines); the Sensors page sets a movement line per path (1.1-8x; real walks reach 5-12x,
  `ratios` in wifi_sensors.json), relearns a path's quiet level, names boards and flashes a board's LED.
- **Routines** (`sentinel/engine/routines.py`, `recorded_videos/routines.db`, page `/routines`, `/api/routines*`):
  "when X, only if Y, then Z". Triggers: camera sees person/vehicle/animal/delivery (`_on_object`, deliveries also
  count as vehicles), motion (`_on_motion`, every frame; a motion routine fires at most every 30 s per camera and
  ignores motion for 20 s after a routine switched a light), sunset/sunrise/clock time (`tick` every 5 s in the
  command loop), phone arrives/leaves Home (OwnTracks changes), family car arrives/leaves (`_on_household_change`).
  Wi-Fi motion sensor (`sensor`, path ids from `WifiMotion.paths()`; `WifiMotion.on_motion` -> `RoutineRunner.sensor`
  on each moving reading, at most every 30 s per routine and path; "until quiet" uses `WifiMotion.last_motion`;
  no 20 s settling after a light switches, which only fools cameras). Sensor *rules* add groups to that trigger:
  `sensors` (any of these start it), `all` (must also move), `none` (must stay still), `within` seconds (default 5).
  A rule is decided `within` s after the movement that started it (`_decide`, a timer: the sensors react a few
  seconds apart as someone walks through), from each sensor's latest moving reading; one decision at a time per
  routine, at most every 30 s once it fired. The editor posts to `/api/routines/check` as you edit: the routine in
  words (or what's missing) and `RoutineRunner.replay()` over the last 24 h from `WifiMotion.moments()` (each
  single burst: path, start, end, peak x quiet level; table `moments` in wifi_motion.db, 7 days), counting a burst
  only if it reaches the path's movement line as it is now. It also polls `/api/wifi_motion` for live dots and a
  "would it go off now" line.
  Limits: dark/light (own sunrise-equation maths from Home place or the weather town; no location = 18:00-07:00),
  from/to hours (may cross midnight), days, nobody home (all location-sharing phones away). Steps: switch on/off
  (on "for N min" or "until_quiet N min" = until no motion on the routine's cameras; re-triggers extend it), Telegram
  text (+ the sighting's photo), say aloud; per routine, Telegram at most every 5 min and speech every 2 min
  (each routine on its own, so one routine per room names each room in turn). A light is only turned
  off by the routine that turned it on, never one switched on by hand; held lights are listed in
  `recorded_videos/routine_holds.json` and switched off at the next start. Stored by ids (cameras = host, switches,
  people, cars); `normalize()` accepts names. By voice/Telegram Claude emits `COMMAND: Routine {json}`; Sentinel
  describes it in its own words and saves it only after "yes" / the ✅ tap (a typed `Routine {json}` isn't saved).
  `routines.REQUEST` sends "new routine ..." phrases straight to Claude.
  Routines that only switch lights also fire on "maybe a person" (`CameraObjectTracker.maybe_people`): a moving
  (`Track.walked`, measured against the narrowest box) person track in the zone at >= 0.30 (`ROUTINE_PERSON_SCORE`; 0.33 caught the walk ~20 s late),
  logged as `maybe_person`. Alerts are unchanged. Why: a person walking through the back yard in IR scored 0.38
  (alert bar 0.45). Replayed 1,042 Back Yard night clips (Oct 27-Nov 2): 3 extra triggers (the real walk, a bug
  on the lens that also fooled the alert path, patio chairs at dusk once). Also: the weather card has a clock.
- **House page** (`sentinel/engine/house.py`, `templates/house.html`, `/house`, `/api/house*`, saved in
  `recorded_videos/house.json`: a floor plan is personal, so it never goes into git). A 3D drawing of the home in
  three.js r128 (MIT, served from `static/three/` so it stays local) with the real devices placed on it. The drawing =
  rooms (rectangles `parts` for hit-testing + an optional outline `poly`, a floor height `y` and ceiling `top` so a
  split-level can sit half a floor up or down, `level` 0/1, wall `gaps` = openings, `"door"` ones framed in yellow),
  features (steps, windows/sliding doors, doors, boxes for furniture/fireplace, rails) and text marks, all in the
  floor plan's own units (the page scales it to fit). It's replaced whole (`POST /api/house/drawing`); there is no
  wall editor yet: it's made from a floor plan or photos with Claude Code. Devices are placed by what they are
  (`switch:<id>`, `sensor:<board id>`, `camera:<camera address>`, never the web id, which changes every start, or
  the RTSP URL) with `POST /api/house/device`; ＋ Add device on the page lists every known device not yet on the map.
  `GET /api/house/live` (polled every 1.5 s) = paths (motion only, no score graphs), boards, switches and cameras
  (address + current web id for snapshots). A path's line lights up while it sees movement, and the room its
  midpoint is in lights up with it (a simple first rule; the routines' any/all/but-not rules could drive rooms
  later). Fire TV / smart-TV browsers start in the flat Top view with antialiasing off.
- **Telegram commands added:** `access` (remote-access guide) and `videos <when> [people|vehicles|animals] [camera]` (`sentinel/engine/queries.py`).
- **Web:** `/settings` covers status, cameras, detection, Telegram, recording, audio, remote access, password and help.
- **Home page** (`/`, `home.html`, 2026-11-04): weather, who's home, navigation tiles with live status (cameras, indoor
  movement, ...) and the switches. The camera grid moved to `/live` (`grid.html`; its header keeps only camera controls:
  Home, Timeline, Data saver, Zones, Switches, Reload). TV browsers (`TV_BROWSER` in `sentinel/web/context.py`: Fire TV "AFT..." in
  Fully Kiosk, Samsung Tizen) still get the grid at `/`, so kiosk start URLs didn't change. Other pages' back link = Home.
  Since 2026-11-07 the grid's script and styles are `static/grid.js` + `static/grid.css` (`grid.html` is only the
  markup). They load with `?v=<boot id>`, so a page that reloads itself after an update gets the new files; `BOOT_ID`
  and the first camera list stay inline in `grid.html` (they come from the server). `/static` needs no login.
- **Movement history** (`/sensors/history`, `/api/wifi_motion/events?day=`): stretches of indoor movement per path
  (gaps < 2 min merged) in `recorded_videos/wifi_motion.db`, 30 days. `WifiMotion.answer()` answers "any movement
  inside today?" / "anyone in the living room?" (a path named in the text) on Telegram and by voice; camera names or
  outdoor words keep a question with the cameras. The last day's movement goes into Claude's DATA.
- **Replay tool idea:** run the detector and tracker over a sample of a day's clips to check accuracy before changing thresholds.
- **Known issues for the user:**
  - The Rear Door camera (.11) looks at carport rafters, with an orange object against the lens.
  - The Garage camera clock is about 33 min slow (enable NTP on the cameras; needs admin).
  - The Cinnamon desktop in the xrdp session idles at about 300% CPU.
- **Next ideas:** delivery-vehicle recognition (CLIP zero-shot on vehicle crops), running detection on HD frames, a web events list with thumbnails, and an LLM/VLM chat ("did anyone come by?").

## Older status (Qt desktop era, kept for history)

- **Desktop app** (PySide6 + OpenCV) — multi-session: each camera has its own `MotionDetector` and profile in `settings.json` → `camera_profiles`.
- **RTSP** — discovery, sub-stream preference, per-host logins, reconnect timer when streams drop.
- **Motion** — MOG2, contours, night-bug filter, motion-triggered recording, Telegram clip request path respects **recording enabled**.
- **Zone / line gating (fixed logic, Apr 2026)**  
  - **No zones, no lines:** any qualifying motion can trigger.  
  - **Zones only:** centroid must be inside a polygon (**no longer bypassed** by empty entry-lines returning “true”).  
  - **Lines only:** line-crossing logic only (entry lines empty no longer makes `_line_crossed_for_track` return true).  
  - **Both:** zone **OR** line crossing.  
  - Line crossing uses **segment intersection** with the drawn segment.  
- **Vertical camera view** — `MotionDetector` applies `cam_orientation` **inside** `detect_motion()` so zones/lines match the UI; duplicate rotate removed from `MainWindow.update_feed`.
- **Embedded MultiView** — `QStackedWidget` so grid gets full feed area; grid tiles **letterbox** (`KeepAspectRatio`); **single-click** tile = active camera for controls; **double-click** = single view; overlay mode draws **each** camera’s zones on its own tile; `_ensure_grid_tiles` avoids rebuilding the grid every frame.
- **Camera dropdown** — connected sessions stay listed after discovery returns empty; `_ensure_camera_selectable` on connect; reconnect starts when all sessions drop.
- **Audio** — Piper preferred; **WAV amplitude scaled** from volume slider before `aplay`; pyttsx3 fallback.
- **Telegram** — `urllib` HTTPS to `api.telegram.org` (needs **internet egress** from the host, not the camera LAN).
- **Logging** — `sentinel/logging_utils.py` → append JSONL to `sentinel_events.log` (gitignored `*.log`): connect, read errors, stream drop, reconnect, recording toggle, volume, etc.
- **Web viewer (May 2026)** — Optional Flask server in `sentinel/web/`: `FrameBroker` receives the same display frames as the UI from `MainWindow.update_feed` (no second RTSP decode). Routes: `/login` (bcrypt + session cookie + IP lockout/backoff), `/`, `/api/cameras`, `/stream?id=…` MJPEG (query param — camera ids are often RTSP URLs with slashes), `/snapshot.jpg?id=…`. Defaults (merged): `web.bind_host=127.0.0.1`, `web.port=8088`; **`0.0.0.0`** common for LAN phones; refuse bind inside `web.camera_subnet_block`. `python -m sentinel.web.set_password` sets credentials. Logs: `web_login_*`, `web_session_revoked`, `web_server_start`. Deps: **Flask**, **bcrypt** in `requirements.txt`. Lazy package `__getattr__` in `sentinel/web/__init__.py`. Since 2026-11-07 the routes live in `sentinel/web/routes/`, one module per area, each with `register(app, ctx)` (`ctx` = `WebContext` in `context.py`: engine, broker, auth, session check, `config_guard`, `result`); `WebServer.start` only sets up and calls them. Deliberately not blueprints, so endpoint names stay the bare function names (`url_for("live_page")` in templates and code). New routes go in their area's module. **Cloudflare Tunnel:** see checklist section below + **`installation-guide.md`**.
- **GPU / VAAPI** — user chose to **hold off** on hardware decode (see plan `cpu_vs_gpu_decode` in Cursor plans if present); stay on OpenCV CPU decode until revisited.

### Runtime log review (Apr 29, 2026 sample)

From `sentinel_events.log` on disk (one file sample; **not** multi-day unless the app ran continuously and the file was not rotated):

- **Errors:** `camera_read_error` (frame read failures) and `camera_stream_dropped` clustered around transient RTSP/H.264 issues on **192.168.0.10** and **192.168.0.11**; **`camera_reconnect_success`** appeared (no `camera_reconnect_failed` in sample).
- **Risk:** each JSON line can include the **full RTSP URL with embedded credentials** — treat `sentinel_events.log` like a secret; consider **redacting URLs** in a future logging change.

---

## Docs (keep in sync when behavior changes)

- `readme.md` — user-facing overview + tables  
- `installation-guide.md` — venv, Piper (`requirements-piper.txt`, `models/`, `aplay`), Telegram network note, **web viewer + Cloudflare Tunnel / Tailscale**, logging, tests  
- `project-structure.md` — paths, new modules (`logging_utils.py`, `sentinel/web/`, `tests/`)  
- `handoff_summary.md` — this file  

---

## Known environment / safety

- **`settings.json`** — gitignored; may contain RTSP passwords and Telegram token — never commit.  
- **`sentinel_events.log`** — gitignored (`*.log`). **May contain RTSP passwords** in `camera_id` / URL fields — restrict file permissions and avoid sharing.  
- **GPU** — not used yet; decode + MOG2 are CPU-bound unless a future GStreamer/VAAPI or OpenCV-CUDA path is added (**deferred by user**).  
- **Git on agent host:** this environment often has **no `git` binary** — the assistant **cannot** run `git fetch`/`push` here. On your machine run:
  ```bash
  cd /path/to/sentinel-home
  git fetch origin && git status -sb
  git log origin/main..HEAD --oneline   # unpushed commits
  git log HEAD..origin/main --oneline   # commits on remote you lack
  ```
  Then `git add -A && git commit … && git push origin main` as needed.

---

## Piper quick reference

| Step | Action |
|------|--------|
| 1 | `pip install -r requirements-piper.txt` |
| 2 | Linux: `sudo apt install -y alsa-utils` → `aplay` |
| 3 | Add `models/<voice>.onnx` [+ `.json`] |
| 4 | Run app; console: `Piper TTS initialised with voice: …` |

Implementation: `sentinel/audio/alert_system.py` (`_apply_volume_to_wav`, `_speak_piper`).

---

## Cloudflare Tunnel (remote HTTPS) — operator checklist

**Full prose + example `config.yml`:** [`installation-guide.md`](installation-guide.md) → section **“Remote HTTPS with Cloudflare Tunnel (typical)”**.

Nothing in this repo installs or configures `cloudflared`; the user (or next chat) does that on the **Sentinel PC**. Summary:

| Step | Action |
|------|--------|
| 1 | **Web viewer OK locally:** Sentinel running, `web.enabled` true, password set (`python -m sentinel.web.set_password`), reachable at `http://127.0.0.1:8088` (or whatever `web.port` is). |
| 2 | **Install `cloudflared`** on the same PC (OS package or `.deb` from Cloudflare’s GitHub releases). |
| 3 | **`cloudflared tunnel login`** — browser; choose the Cloudflare **zone** that holds the domain (e.g. `example.com`). |
| 4 | **`cloudflared tunnel create <name>`** — creates tunnel; note UUID / credentials path under `~/.cloudflared/`. |
| 5 | **`cloudflared tunnel route dns <name> cams.example.com`** — creates DNS pointing at the tunnel (replace hostname). |
| 6 | **`~/.cloudflared/config.yml`** — map `hostname: cams.example.com` → **`service: http://127.0.0.1:8088`** (must match `settings.json` `web.port`; see install guide for full YAML including `http_status:404` fallback). |
| 7 | **Run as service:** `sudo cloudflared service install` then `sudo systemctl enable --now cloudflared` (or distro equivalent). |
| 8 | **Optional hardening:** Cloudflare **Zero Trust → Access** policy on `cams.example.com` (email OTP / Google) *before* Sentinel’s own login. |

**Bind host vs tunnel**

- **Tunnel-only (recommended for remote):** `web.bind_host` can stay **`127.0.0.1`** — `cloudflared` connects to localhost; cameras stay isolated; no extra LAN exposure.
- **Same Wi‑Fi phones without tunnel:** set `web.bind_host` to **`0.0.0.0`** or the PC’s LAN IP (not inside `web.camera_subnet_block`). Firewall may need **`8088/tcp`** allowed.

**Not done automatically**

- DNS hostname choice (`cams.*` vs other subdomain).
- Cloudflare Access policies.
- Troubleshooting: `journalctl -u cloudflared`, Tunnel **Metrics** in Zero Trust dashboard, verify ingress URL matches loopback + port.

---

## Suggested next-chat prompts

- “Read `handoff_summary.md`. Add GStreamer + VAAPI RTSP decode behind a feature flag and document deps in `installation-guide.md`.”  
- “Read `handoff_summary.md`. Add a UI button to tail the last N lines of `sentinel_events.log`.”  
- “Read `handoff_summary.md`. Redact RTSP credentials in `sentinel_events.log` (log host + path only).”  
- “Read `handoff_summary.md`. Add web viewer recording playback or CSRF tokens on logout POST.”  
- “Read `handoff_summary.md`. Help debug Cloudflare Tunnel → Sentinel web viewer (ingress, Access, MJPEG).”

---

## Next session checklist

- [ ] Read this file first.  
- [ ] Confirm user goal.  
- [ ] Run smoke checks before large refactors.  
- [ ] Update this file before ending: changes, validation, blockers, next steps.

---

## Session update template

### Date/Time  
### Goal worked on  
### Changes made  
### Validation performed  
### Open issues / risks  
### Next recommended step  

---

## Latest session update

### Date/Time

- Monday, May 4, 2026 (UTC-4) — optional HTTP web viewer + auth + docs

### Goal worked on

- Live multi-camera **web page** (MJPEG grid) with **login**, **bcrypt** password, **session cookies**, **IP lockout** with exponential backoff, **bind-host guard** so the server never listens on the camera-only subnet, **Cloudflare Tunnel** / Tailscale documentation. Isolated camera LAN unchanged.

### Changes made

- New package **`sentinel/web/`**: `frame_broker.py`, `auth.py`, `bind_util.py`, `server.py`, `set_password.py`, `templates/login.html`, `templates/grid.html`; lazy **`__getattr__`** in `sentinel/web/__init__.py` so lightweight imports skip NumPy.  
- **`sentinel/app.py`**: when `web.enabled` and `web.auth.password_hash` set, start `FrameBroker` + `WebServer` thread; pass broker into `MainWindow`.  
- **`sentinel/ui/main_window.py`**: `update_feed` publishes to broker (`web.published_frame` raw vs overlay path); `disconnect_camera` / stream-drop / empty sessions **unpublish** / **clear**.  
- **`sentinel/settings/manager.py`**: `WEB_VIEWER_DEFAULTS`, `ensure_web_defaults()`, `save_settings(..., force=)` for CLI. **`sentinel/__init__.py`** added (package marker).  
- **`requirements.txt`**: `Flask>=3.0.0`, `bcrypt>=4.0.0`.  
- **Tests:** `tests/test_frame_broker.py`, `tests/test_web_auth.py`, `tests/test_web_bind.py`.  
- **Docs:** `readme.md`, `installation-guide.md` (web + Tunnel + Tailscale), `project-structure.md`, this handoff.  
- **Handoff:** Added **“Cloudflare Tunnel — operator checklist”** section (table + bind-host guidance); install guide remains canonical for YAML/commands.

### Validation performed

- **`tests.test_web_bind`**: passes with system Python (no venv).  
- **Full suite** (`unittest discover`): not run in agent — image lacked `python3-venv` / preinstalled NumPy; user should run `pip install -r requirements.txt` in a venv and `python3 -m unittest discover -s tests -p 'test_*.py'`.

### Open issues / risks

- **Logout** is POST without CSRF token (home / tunnel use case; harden if exposed broadly).  
- **`web_login_*` events** may include `username_attempted` — low sensitivity; still local log.  
- Cloudflare free tier **video proxying** policy: keep `max_fps` / `jpeg_quality` modest or prefer Tailscale.

### Next recommended step

- **Cloudflare Tunnel:** follow the **“Cloudflare Tunnel (remote HTTPS) — operator checklist”** table above; paste-ready **`config.yml`** and commands live in **`installation-guide.md`** (Tunnel subsection). Prefer **`web.bind_host: "127.0.0.1"`** when using tunnel-only remote access.  
- Optional: run full unit tests locally; commit and push (agent host may lack `git`).
