"""
Telegram messages Sentinel's own rules don't understand go to Claude, through the
official ``claude`` command signed in with the household's own Claude subscription.

Opt-in (setting ``assistant_enabled``) because it is the one thing that leaves the
house: the message and a short text summary of recent events (times, cameras, family
car names; never pictures) are sent to Anthropic. Everything Sentinel can answer
itself still is answered locally, first.

Locked down: Claude gets no tools (it can't run commands, read files or change
anything), a fixed system prompt, one turn, and runs from an empty folder so no
project instructions or memory are loaded. Calls are capped per day.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from sentinel.logging_utils import app_logger

TIMEOUT_SECONDS = 90
MAX_EVENT_LINES = 150
EVENT_HOURS = 48
WORK_DIR = os.path.join(os.path.expanduser("~"), ".cache", "sentinel-assistant")  # no CLAUDE.md above it

SYSTEM_PROMPT = """You are the Telegram helper of Sentinel, a home security camera system run by a family at home.
A family member sent a message that Sentinel's built-in commands didn't recognise. Answer it using ONLY
the facts in the DATA section (recent camera events, which family cars are home, cameras, weather).

Rules:
- Reply in plain, friendly language, 1-4 short sentences (a short list is fine when they ask for several things).
- Never invent sightings, times or people. If the data can't answer the question, say so honestly.
- Event times are local. "Today" means the date given as NOW.
- You can't do anything yourself. If one of Sentinel's COMMANDS does what they asked (e.g. "turn alerts back on"
  -> Enable messages), say so in your reply and put that exact command alone on the LAST line as
  "COMMAND: <command>". Sentinel shows it as a button; it only runs if they tap it. At most one command,
  only from the COMMANDS list, never invented ones.
- Reminders: Sentinel can send a Telegram message at a set time. For "remind me ..." work out the exact date and
  24-hour time from NOW (e.g. "Wednesday at 9" = the next Wednesday, 09:00) and use the command
  "COMMAND: Remind YYYY-MM-DD HH:MM <what to remind them of>". Say the day, date and time back in words so they can
  check it. If they didn't say a time or it's unclear (morning or evening?), ask instead of guessing (no command).
- Routines ("new routine ...", "every time ... do ...", "when ... turn on ..."): Sentinel runs "when this happens,
  then do that" routines. Build one and put it alone on the LAST line as
  COMMAND: Routine {"name": "...", "when": {...}, "only": {...}, "then": [...]}
  (JSON on one line). "when" is one of:
  {"type": "seen", "what": ["person"|"vehicle"|"animal"|"delivery"|"package", ...], "cameras": [camera names]}  ("something" = all of them; [] = any camera; "delivery" = a delivery van, "package" = a parcel left at the door)
  {"type": "motion", "cameras": [...]}  (any movement, even a shadow; prefer "seen" for people/cars/animals)
  {"type": "sensor", "sensors": [Wi-Fi motion sensor names]}  (movement indoors, e.g. "the living room sensor"; [] = any)
    optional: "all": [sensors that must move too], "none": [sensors that must stay still], "within": seconds (default 5)
    ("someone in the living room but not the kitchen" = sensors [living room], none [kitchen])
  {"type": "time", "at": "sunset"|"sunrise"|"HH:MM", "offset": minutes before (-) or after (+) sunset/sunrise}
  {"type": "arrive"|"leave", "people": [names]}  (their phone gets home / leaves home; [] = anyone)
  {"type": "car_arrive"|"car_leave", "cars": [family car names]}  ([] = any family car)
  "only" (optional): "dark": true (after dark / at night), "light": true (daytime), "from"/"to": "HH:MM",
  "days": ["mon", ...], "nobody_home": true.
  "then", up to 5 steps: {"do": "switch", "switch": name, "on": true|false, "for": minutes} or with
  "until_quiet": minutes instead of "for" (stays on until there's been no motion on the routine's cameras or Wi-Fi
  sensors that long; camera and sensor routines only); {"do": "telegram", "text": "...", "photo": true (camera routines)}; {"do": "say", "text": "..."};
  {"do": "tv_camera", "tv": TV name, "camera": camera name ("" = the camera that saw it), "for": minutes (1-30,
  then back to what was on; only if that TV is on)}.
  Use only camera, switch, people, car and Wi-Fi sensor names from DATA. If the switch or what should happen is unclear, ask
  instead (no command). For "until there's no motion" without a time, use 2 minutes. Keep your sentence short:
  Sentinel reads the routine back in its own words before saving. To pause or delete one use
  "Routine N off" / "Routine N on" / "Delete routine N".
- Family cars stand for their owners: "Kate's Edge got home" means Kate (Katherine, Mom) got home.
- Small talk is fine; keep it brief and steer back to what Sentinel can do.
- No markdown formatting (Telegram shows it raw); emoji are fine."""


# Same rules, but the answer is spoken aloud by the PC's speakers ("Hey Jarvis").
VOICE_SYSTEM_PROMPT = (SYSTEM_PROMPT
    .replace("You are the Telegram helper of Sentinel", "You are Jarvis, the voice of Sentinel")
    .replace("A family member sent a message", "A family member said something out loud")
    # By voice Jarvis is the house's general assistant too (the family asked, 2026-10-27).
    .replace("Answer it using ONLY\nthe facts in the DATA section (recent camera events, which family cars are home, cameras, weather).",
             "Questions about the house (cameras, visitors, deliveries, family cars,\nwho's home, weather) are answered "
             "using ONLY the facts in the DATA section. Anything else (general knowledge, cooking, maths, conversions,\n"
             "spelling, advice, chat) you answer from your own knowledge like a helpful home voice assistant.")
    .replace("- Small talk is fine; keep it brief and steer back to what Sentinel can do.",
             "- You can't look anything up: for live information (news, scores, prices, opening hours, anything after "
             "your training) say you can't check that, rather than guess.")
    .replace("1-4 short sentences (a short list is fine when they ask for several things)",
             "1-3 short sentences (up to 5 when they ask how to do something) that sound natural spoken aloud: "
             "no lists, no emoji, say times like \"3:40 pm\"")
    .replace("Sentinel shows it as a button; it only runs if they tap it.", "Sentinel asks them out loud and only runs it if they say yes.")
    .replace("- No markdown formatting (Telegram shows it raw); emoji are fine.",
             "- Plain words only: your reply is read out by a text-to-speech voice. Whatever they said was "
             "transcribed by speech recognition, so allow for misheard words (e.g. \"Jon\" = John).\n"
             # The family found the long "I didn't catch a question there, you can ask me about..." tedious (2026-11-03).
             "- If it isn't a clear question or request (a fragment, half a sentence, talk not meant for you), reply "
             "with just two or three words like \"Huh?\", \"Come again?\" or \"What?\" and nothing else.\n"
             # The family's choice (2026-11-03): "being formal isn't necessary and sarcasm and sassy is ok".
             "- Talk like a laid-back friend, not a butler: casual, a bit of sarcasm and sass is welcome. Never at the "
             "cost of the facts, and still short."))


def find_claude(configured: str = "") -> Optional[str]:
    for path in (configured, shutil.which("claude") or "", os.path.expanduser("~/.local/bin/claude")):
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _env() -> Dict[str, str]:
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # would bill the API instead of the subscription
    env.setdefault("HOME", os.path.expanduser("~"))
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    return env


def build_command(claude: str, prompt: str, model: str, system_prompt: str = SYSTEM_PROMPT) -> List[str]:
    return [claude, "-p", prompt, "--model", model, "--tools", "", "--system-prompt", system_prompt,
            "--max-turns", "1", "--output-format", "json", "--no-session-persistence",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "user"]


class Assistant:
    """``ask(text, context)`` -> reply text, or None when Claude can't be used (the caller falls back)."""

    def __init__(self, settings: Dict, run: Callable = subprocess.run) -> None:
        self.settings = settings
        self._run = run
        self._lock = threading.Lock()
        self._day = ""
        self._used = 0
        self.last_error = ""

    def enabled(self) -> bool:
        return bool(self.settings.get("assistant_enabled"))

    def _limit(self) -> int:
        return int(self.settings.get("assistant_daily_limit", 30))

    def _take_slot(self) -> bool:
        with self._lock:
            today = time.strftime("%Y-%m-%d")
            if today != self._day:
                self._day, self._used = today, 0
            if self._used >= self._limit():
                return False
            self._used += 1
            return True

    def remaining_today(self) -> int:
        with self._lock:
            used = self._used if self._day == time.strftime("%Y-%m-%d") else 0
        return max(0, self._limit() - used)

    def check_login(self) -> Tuple[bool, str]:
        """Is the claude command installed and signed in? (startup health check, no model call)"""
        claude = find_claude(str(self.settings.get("assistant_claude_path") or ""))
        if not claude:
            return False, "the claude command isn't installed for this user"
        try:
            r = self._run([claude, "auth", "status"], capture_output=True, text=True, timeout=30, env=_env(),
                          cwd=self._work_dir())
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"couldn't run claude ({e})"
        return (r.returncode == 0), ("signed in" if r.returncode == 0 else "not signed in: run `claude` on the PC and /login")

    @staticmethod
    def _work_dir() -> str:
        os.makedirs(WORK_DIR, exist_ok=True)
        return WORK_DIR

    def ask(self, text: str, context: str, voice: bool = False, name: str = "") -> Optional[str]:
        if not self.enabled():
            return None
        claude = find_claude(str(self.settings.get("assistant_claude_path") or ""))
        if not claude:
            self.last_error = "the claude command isn't installed"
            return None
        if not self._take_slot():
            self.last_error = f"today's limit of {self._limit()} Claude questions is used up"
            return None
        prompt = f"{context}\n\n{'SAID OUT LOUD' if voice else 'MESSAGE'} FROM THE FAMILY:\n{text.strip()[:1000]}"
        name = re.sub(r"[^A-Za-z' -]", "", str(name or self.settings.get("voice_name") or "Jarvis"))[:20] or "Jarvis"
        system = VOICE_SYSTEM_PROMPT.replace("You are Jarvis,", f"You are {name},") if voice else SYSTEM_PROMPT
        return self._call(claude, prompt, system, voice=voice)

    def complete(self, system: str, prompt: str, what: str) -> Optional[str]:
        """A one-off job with its own instructions (a recipe swap, say): Claude's reply, or None. Same rules as
        ask(): opt-in, no tools, counted against the daily limit; what = the job, for the log."""
        if not self.enabled():
            self.last_error = "Claude is turned off (Settings → Assistant)"
            return None
        claude = find_claude(str(self.settings.get("assistant_claude_path") or ""))
        if not claude:
            self.last_error = "the claude command isn't installed"
            return None
        if not self._take_slot():
            self.last_error = f"today's limit of {self._limit()} Claude questions is used up"
            return None
        return self._call(claude, prompt, system, what=what)

    def _call(self, claude: str, prompt: str, system: str, voice: bool = False, what: str = "") -> Optional[str]:
        model = str(self.settings.get("assistant_model") or "sonnet")
        started = time.time()
        try:
            r = self._run(build_command(claude, prompt, model, system),
                          capture_output=True, text=True,
                          timeout=TIMEOUT_SECONDS, env=_env(), cwd=self._work_dir())
            out = r.stdout or ""
            data = json.loads(out[out.find("{"):out.rfind("}") + 1]) if "{" in out else {}
        except subprocess.TimeoutExpired:
            data, r = {}, None
            self.last_error = "Claude took too long to answer"
        except (OSError, ValueError) as e:
            data, r = {}, None
            self.last_error = f"couldn't run Claude ({e})"
        reply = str(data.get("result") or "").strip()
        ok = r is not None and r.returncode == 0 and not data.get("is_error") and bool(reply)
        if not ok and r is not None:
            detail = reply or (r.stderr or "").strip()[-200:]
            self.last_error = ("Claude needs signing in again on the PC (run `claude` and /login)"
                               if "login" in detail.lower() or "auth" in detail.lower() else
                               f"Claude didn't answer ({detail[:120] or 'exit ' + str(r.returncode)})")
        usage = data.get("usage") or {}
        app_logger.log("info" if ok else "warning", "assistant_answered" if ok else "assistant_failed",
                       seconds=round(time.time() - started, 1), model=model, voice=voice, job=what or None,
                       input_tokens=sum(int(usage.get(k) or 0) for k in
                                        ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
                       output_tokens=usage.get("output_tokens"), error_message=None if ok else self.last_error,
                       component="assistant", action="ask")
        return reply if ok else None


def split_command(reply: str) -> Tuple[str, Optional[str]]:
    """Claude's reply without its last "COMMAND: ..." line, and that command (unchecked)."""
    lines = reply.rstrip().split("\n")
    if lines and lines[-1].strip().upper().startswith("COMMAND:"):
        command = lines[-1].strip()[len("COMMAND:"):].strip().strip('"\'` ')
        return "\n".join(lines[:-1]).rstrip(), command or None
    return reply, None


def _clock(ts: float) -> str:
    t = time.localtime(ts)
    return f"{t.tm_hour % 12 or 12}:{t.tm_min:02d} {'am' if t.tm_hour < 12 else 'pm'}"


def build_context(now: float, events: List[Dict], cameras: List[Tuple[str, bool]], home_text: str,
                  vehicles: List, weather: Optional[Dict], commands: str, reminders: str = "",
                  switches: str = "", routines: str = "", indoor: str = "") -> str:
    """The DATA Claude answers from: plain text, newest events last, trimmed to the most recent."""
    lines = [f"NOW: {time.strftime('%A %B %d %Y', time.localtime(now))}, {_clock(now)}"]
    lines.append("CAMERAS: " + ", ".join(f"{name} ({'online' if up else 'OFFLINE'})" for name, up in cameras))
    if vehicles:
        lines.append("FAMILY CARS (name: owner / other names): " + "; ".join(
            f"{v.name}: {v.owner or '?'}" + (f" / {', '.join(v.aliases)}" if v.aliases else "") for v in vehicles))
    if home_text:
        lines.append("CARS HOME RIGHT NOW (from the cameras):\n" + home_text)
    if weather and weather.get("temp") is not None:
        lines.append(f"WEATHER in {weather.get('place')}: {weather.get('temp')}{weather.get('unit', '')}, "
                     f"{weather.get('words')}, high {weather.get('high')} low {weather.get('low')}, "
                     f"wind {weather.get('wind')} mph, {weather.get('rain_today')}% chance of rain today")
    shown = events[-MAX_EVENT_LINES:]
    head = f"CAMERA EVENTS, last {EVENT_HOURS} hours, oldest first"
    if len(events) > len(shown):
        head += f" (only the latest {len(shown)} of {len(events)} shown)"
    day = None
    body = []
    for e in shown:
        d = time.strftime("%A %b %d", time.localtime(e["ts"]))
        if d != day:
            body.append(f"-- {d} --")
            day = d
        body.append(f"{_clock(e['ts'])} {e.get('description') or e.get('label') or e.get('grp')}")
    lines.append(head + ":\n" + ("\n".join(body) if body else "(none)"))
    if reminders:
        lines.append("REMINDERS ALREADY SET:\n" + reminders)
    if switches:
        lines.append("SMART PLUGS AND SWITCHES (name: state; switch one with \"COMMAND: Turn on <name>\" or "
                     "\"COMMAND: Turn off <name>\"):\n" + switches)
    if routines:
        lines.append("ROUTINES (and the names routines can use):\n" + routines)
    if indoor:
        lines.append("MOVEMENT INSIDE THE HOUSE, last 24 hours (Wi-Fi motion sensors; pets set them off "
                     "too, so this is movement, not necessarily people):\n" + indoor)
    lines.append("COMMANDS the family can send to Sentinel:\n" + commands)
    return "DATA\n" + "\n\n".join(lines)
