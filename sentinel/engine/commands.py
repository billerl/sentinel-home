"""Requests both Telegram and voice ("Hey Jarvis") understand, each registered once.

A command says how to recognise it and what the answer is. Telegram (Engine.answer_question) and voice
(Engine.answer_voice) ask the registry, instead of each keeping its own copy of every check. Telegram-only
commands (Pause, Status, Clip, ...) stay in TelegramNotifier.route_text, voice-only ones in answer_voice.
"""

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from sentinel.logging_utils import app_logger


@dataclass(frozen=True)
class Command:
    name: str
    match: Callable[[str], Any]  # what it understood, or None when the text isn't this
    answer: Callable[[Any, str], str]  # (what it understood, the text) -> the reply; does the thing first
    speak: Optional[Callable[[str], str]] = None  # the reply as Jarvis says it; None = Telegram only
    voice_stage: str = "early"  # where answer_voice checks it: "early" (before switches) or "late" (after routines)
    voice_if: Callable[[str], bool] = lambda _text: True  # voice takes it only when this holds too
    telegram: Optional[Callable[[str, Any, str], None]] = None  # (chat, understood, text): sends its own reply
    prefix: str = ""  # before the Telegram reply
    log_kind: Optional[Callable[[Any], str]] = None  # Telegram logs question_answered with this kind
    claim_if: Callable[[], bool] = lambda: True  # Telegram takes a message for this only while this holds


class Commands:
    """The registry, in the order Telegram checks it (voice keeps that order within each stage)."""

    def __init__(self, reply: Callable[[str, str], Any]):
        self._reply = reply  # (chat, text): a Telegram reply
        self._commands: List[Command] = []

    def add(self, command: Command) -> None:
        self._commands.append(command)

    def claims(self, text: str) -> bool:
        """Telegram: is this message one of these (so it goes to answer_question)?"""
        return any(c.match(text) is not None and c.claim_if() for c in self._commands)

    def telegram_reply(self, chat_id: str, text: str) -> bool:
        """Answer the first command that understands the text, on Telegram; False when none does."""
        for c in self._commands:
            understood = c.match(text)
            if understood is None:
                continue
            if c.telegram is not None:
                c.telegram(chat_id, understood, text)
            else:
                self._reply(chat_id, c.prefix + c.answer(understood, text))
            if c.log_kind is not None:
                app_logger.log("info", "question_answered", kind=c.log_kind(understood), component="engine",
                               action="answer_question")
            return True
        return False

    def voice_reply(self, text: str, stage: str) -> Optional[str]:
        """What Jarvis says for the first command of this stage that understands the text, else None."""
        for c in self._commands:
            if c.speak is None or c.voice_stage != stage or not c.voice_if(text):
                continue
            understood = c.match(text)
            if understood is not None:
                return c.speak(c.answer(understood, text))
        return None


# Enable / disable: aliases ordered longest-first so compound phrases match first.
FEATURE_ALIASES: Tuple[Tuple[str, str], ...] = (
    ("night bug filter", "night_bug"),
    ("night bug", "night_bug"),
    ("auto reconnect", "reconnect"),
    ("telegram clips", "clips"),
    ("video clips", "clips"),
    ("recording", "recording"),
    ("messages", "messages"),
    ("alerts", "messages"),  # what people actually say
    ("alert", "messages"),
    ("notifications", "messages"),
    ("motion alerts", "messages"),
    ("photo alerts", "messages"),
    ("telegram alerts", "messages"),
    ("clips", "clips"),
    ("reconnect", "reconnect"),
)


def parse_feature_toggle(text: str) -> Tuple[Optional[Dict[str, bool]], Optional[str]]:
    """Parse enable/disable feature phrases; returns (changes dict or None, error or None)."""
    raw = text.strip()
    m = re.match(r"^(enable|disable)\s+(.+)$", raw, flags=re.IGNORECASE)
    if not m:
        return None, None
    want_on = m.group(1).lower() == "enable"
    rest = m.group(2).strip().lower().replace(",", " and ")
    parts = [p.strip() for p in re.split(r"\s+and\s+", rest) if p.strip()]
    if not parts:
        return None, "Say what to enable or disable, e.g. recording, messages, or clips."

    changes: Dict[str, bool] = {}
    unknown: List[str] = []
    for part in parts:
        matched_key = None
        for alias, key in FEATURE_ALIASES:
            if part == alias:
                matched_key = key
                break
        if matched_key is not None:
            changes[matched_key] = want_on
        else:
            unknown.append(part)
    if unknown:
        return None, (
            f"Unknown: {', '.join(unknown)}. You can enable or disable: alerts (messages), clips, "
            "recording, night bug, reconnect."
        )
    return changes, None
