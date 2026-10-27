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
- Family cars stand for their owners: "Stephanie's Edge got home" means Stephanie (Steph, Mom) got home.
- Small talk is fine; keep it brief and steer back to what Sentinel can do.
- No markdown formatting (Telegram shows it raw); emoji are fine."""


# Same rules, but the answer is spoken aloud by the PC's speakers ("Hey Jarvis").
VOICE_SYSTEM_PROMPT = (SYSTEM_PROMPT
    .replace("You are the Telegram helper of Sentinel", "You are Jarvis, the voice of Sentinel")
    .replace("A family member sent a message", "A family member said something out loud")
    .replace("1-4 short sentences (a short list is fine when they ask for several things)",
             "1-3 short sentences that sound natural spoken aloud: no lists, no emoji, say times like \"3:40 pm\"")
    .replace("Sentinel shows it as a button; it only runs if they tap it.", "Sentinel asks them out loud and only runs it if they say yes.")
    .replace("- No markdown formatting (Telegram shows it raw); emoji are fine.",
             "- Plain words only: your reply is read out by a text-to-speech voice. Whatever they said was "
             "transcribed by speech recognition, so allow for misheard words (e.g. \"Abby\" = Abi)."))


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

    def ask(self, text: str, context: str, voice: bool = False) -> Optional[str]:
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
        model = str(self.settings.get("assistant_model") or "sonnet")
        started = time.time()
        try:
            r = self._run(build_command(claude, prompt, model, VOICE_SYSTEM_PROMPT if voice else SYSTEM_PROMPT),
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
                       seconds=round(time.time() - started, 1), model=model, voice=voice,
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
                  vehicles: List, weather: Optional[Dict], commands: str) -> str:
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
    lines.append("COMMANDS the family can send to Sentinel:\n" + commands)
    return "DATA\n" + "\n\n".join(lines)
