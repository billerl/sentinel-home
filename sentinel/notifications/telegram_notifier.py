"""
Telegram notification sender for Sentinel Home.
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from sentinel.logging_utils import app_logger


TELEGRAM_MAX_UPLOAD_BYTES = 49 * 1024 * 1024  # Bot API upload limit is 50 MB


class TelegramNotifier:
    """Sends Telegram Bot API alerts with cooldown and recipient pause controls."""

    def __init__(self, settings: Dict[str, Any]):
        self.enabled = False
        self.bot_token = ""
        self.chat_ids: List[str] = []
        self.cooldown_seconds = 10.0
        self.include_video = False
        self.last_alert_time = 0.0
        self.paused_until_by_chat: Dict[str, float] = {}
        self._send_lock = threading.Lock()
        self._listener_thread: Optional[threading.Thread] = None
        self._listener_running = False
        self._update_offset: Optional[int] = None
        self._pending_clip_chat_ids: List[str] = []
        self._pending_remote_commands: List[Tuple[str, Dict[str, bool]]] = []
        self._pending_detailed_status_chat_ids: List[str] = []
        self._pending_access_chat_ids: List[str] = []
        self._pending_video_queries: List[Tuple[str, str]] = []
        self._pending_questions: List[Tuple[str, str]] = []
        # Button taps: (chat_id, message_id, data, callback_query_id)
        self._pending_buttons: List[Tuple[str, int, str, str]] = []
        # Extra question check supplied by the engine (knows the family's car/owner names).
        self.question_filter: Optional[Callable[[str], bool]] = None
        # When set and returning True, messages nothing else understood go to the engine (Claude)
        # instead of getting the help text.
        self.assistant_available: Optional[Callable[[], bool]] = None
        self._pending_free_text: List[Tuple[str, str]] = []
        self.update_settings(settings)

    @staticmethod
    def _commands_help_text() -> str:
        return (
            "Sentinel bot commands (case-insensitive):\n\n"
            "Pause / resume:\n"
            "- Pause <minutes> — pause Telegram alerts for this chat\n"
            "- Resume — unpause\n\n"
            "Information:\n"
            "- Status — cameras, recording and Telegram options summary\n"
            "- Access — how to reach Sentinel remotely (web link, Tailscale, Remote Desktop)\n"
            "- Videos <when> — send recorded clips, e.g. Videos last 2 hours, Videos 2pm-4pm people\n"
            "- Ask in plain words — Did anyone come by? Any deliveries today? What happened last night?\n"
            "- Help — this message\n\n"
            "Clip:\n"
            "- Clip — record ~10s per connected camera and upload\n\n"
            "Enable / disable (combine with \"and\" or commas):\n"
            "- Recording — motion-triggered clip capture to disk\n"
            "- Messages — Telegram motion alerts (text / clips per settings)\n"
            "- Clips — attach motion recordings to Telegram (checkbox)\n"
            "- Night bug / Night bug filter — scheduled small-motion filter\n"
            "- Reconnect / Auto reconnect — stream reconnect when dropped\n\n"
            "Examples:\n"
            "- Enable recording and messages\n"
            "- Disable clips\n"
            "- Enable night bug filter"
        )

    # Aliases ordered longest-first so compound phrases match first.
    _FEATURE_ALIAS_TO_KEY: Tuple[Tuple[str, str], ...] = (
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

    @classmethod
    def _parse_feature_toggle_command(cls, text: str) -> Tuple[Optional[Dict[str, bool]], Optional[str]]:
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
            for alias, key in cls._FEATURE_ALIAS_TO_KEY:
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

    @staticmethod
    def _parse_chat_ids(value: str) -> List[str]:
        return [item.strip() for item in value.split(",") if item.strip()]

    def _extract_chat_ids_from_settings(self, settings: Dict[str, Any]) -> List[str]:
        chat_ids_raw = str(settings.get("telegram_chat_ids", "")).strip()
        if chat_ids_raw:
            return self._parse_chat_ids(chat_ids_raw)
        legacy = str(settings.get("telegram_chat_id", "")).strip()
        return [legacy] if legacy else []

    def update_settings(self, settings: Dict[str, Any]) -> None:
        """Update notifier settings at runtime."""
        if "telegram_enabled" in settings:
            self.enabled = bool(settings["telegram_enabled"])
        if "telegram_bot_token" in settings:
            self.bot_token = str(settings["telegram_bot_token"]).strip()
        if (
            "telegram_chat_ids" in settings
            or "telegram_chat_id" in settings
            or not self.chat_ids
        ):
            self.chat_ids = self._extract_chat_ids_from_settings(settings)
        if "telegram_cooldown" in settings:
            self.cooldown_seconds = max(0.0, float(settings["telegram_cooldown"]))
        if "telegram_include_video" in settings:
            self.include_video = bool(settings["telegram_include_video"])
        self._refresh_listener_state()

    def is_configured(self) -> bool:
        """Return True when token and at least one chat ID are present."""
        return bool(self.bot_token and self.chat_ids)

    def _api_url(self, method: str) -> str:
        encoded_token = urllib.parse.quote(self.bot_token, safe="")
        return f"https://api.telegram.org/bot{encoded_token}/{method}"

    @staticmethod
    def _post_json(url: str, payload: Dict[str, Any], timeout: int = 8) -> bool:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return 200 <= int(response.status) < 300
        except (urllib.error.URLError, TimeoutError, ValueError):
            return False

    def _send_message_to_chat(self, chat_id: str, message: str) -> bool:
        return self._post_json(
            self._api_url("sendMessage"),
            {
                "chat_id": chat_id,
                "text": message,
                "disable_notification": False,
            },
        )

    def _send_video_to_chat(self, chat_id: str, video_path: str, caption: str) -> bool:
        if not os.path.exists(video_path) or os.path.getsize(video_path) > TELEGRAM_MAX_UPLOAD_BYTES:
            return False
        boundary = f"sentinel-{int(time.time() * 1000)}"
        with open(video_path, "rb") as video_file:
            video_bytes = video_file.read()

        parts: List[bytes] = []
        for name, value in [("chat_id", chat_id), ("caption", caption)]:
            parts.append(f"--{boundary}\r\n".encode("utf-8"))
            parts.append(
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
            )
            parts.append(f"{value}\r\n".encode("utf-8"))

        filename = os.path.basename(video_path)
        content_type = "video/mp4" if filename.lower().endswith(".mp4") else "video/x-msvideo"
        parts.append(f"--{boundary}\r\n".encode("utf-8"))
        parts.append(
            (
                f'Content-Disposition: form-data; name="video"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        parts.append(video_bytes)
        parts.append(b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(parts)

        request = urllib.request.Request(
            self._api_url("sendVideo"),
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return 200 <= int(response.status) < 300
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return False

    def _send_photo_to_chat(self, chat_id: str, jpeg: bytes, caption: str) -> bool:
        boundary = f"sentinel-{int(time.time() * 1000)}"
        parts: List[bytes] = []
        for name, value in [("chat_id", chat_id), ("caption", caption)]:
            parts.append(f"--{boundary}\r\n".encode("utf-8"))
            parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
            parts.append(f"{value}\r\n".encode("utf-8"))
        parts.append(f"--{boundary}\r\n".encode("utf-8"))
        parts.append(
            b'Content-Disposition: form-data; name="photo"; filename="sentinel.jpg"\r\n'
            b"Content-Type: image/jpeg\r\n\r\n"
        )
        parts.append(jpeg)
        parts.append(b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        request = urllib.request.Request(
            self._api_url("sendPhoto"),
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return 200 <= int(response.status) < 300
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return False

    def send_photo_with_buttons(self, chat_id: str, jpeg: bytes, caption: str,
                                buttons: List[List[Tuple[str, str]]]) -> Optional[int]:
        """Send a photo with inline buttons (rows of (label, callback data)); returns its message id."""
        if not self.is_configured():
            return None
        markup = json.dumps({"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]})
        boundary = f"sentinel-{int(time.time() * 1000)}"
        parts: List[bytes] = []
        for name, value in [("chat_id", chat_id), ("caption", caption), ("reply_markup", markup)]:
            parts.append(f"--{boundary}\r\n".encode("utf-8"))
            parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
            parts.append(f"{value}\r\n".encode("utf-8"))
        parts.append(f"--{boundary}\r\n".encode("utf-8"))
        parts.append(b'Content-Disposition: form-data; name="photo"; filename="sentinel.jpg"\r\n'
                     b"Content-Type: image/jpeg\r\n\r\n")
        parts.append(jpeg)
        parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        request = urllib.request.Request(
            self._api_url("sendPhoto"), data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return int(payload["result"]["message_id"]) if payload.get("ok") else None
        except (urllib.error.URLError, TimeoutError, ValueError, OSError, KeyError, TypeError):
            return None

    def consume_button_presses(self) -> List[Tuple[str, int, str, str]]:
        """Return and clear (chat_id, message_id, data, callback_id) taps on inline buttons."""
        pending, self._pending_buttons = self._pending_buttons, []
        return pending

    def answer_button(self, callback_id: str, text: str = "") -> bool:
        """Acknowledge a tap (stops the button's loading spinner; optional toast)."""
        return self._post_json(self._api_url("answerCallbackQuery"), {"callback_query_id": callback_id, "text": text})

    def edit_caption(self, chat_id: str, message_id: int, caption: str) -> bool:
        """Replace a photo's caption and remove its buttons."""
        return self._post_json(self._api_url("editMessageCaption"),
                               {"chat_id": chat_id, "message_id": message_id, "caption": caption})

    def edit_buttons(self, chat_id: str, message_id: int, buttons: List[List[Tuple[str, str]]]) -> bool:
        """Swap a message's inline buttons (rows of (label, callback data)); [] removes them."""
        return self._post_json(self._api_url("editMessageReplyMarkup"), {
            "chat_id": chat_id, "message_id": message_id,
            "reply_markup": {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]},
        })

    def trigger_photo_alert(self, caption: str, jpeg: bytes,
                            buttons: Optional[List[List[Tuple[str, str]]]] = None) -> bool:
        """Send a snapshot with a caption (and optional inline buttons) to every unpaused chat
        (async). The caller applies its own cooldown; the global text-alert cooldown is not used."""
        if not self.enabled or not self.is_configured():
            return False

        def _send() -> None:
            now_ts = time.time()
            for chat_id in self.chat_ids:
                if self._is_paused(chat_id, now_ts):
                    continue
                if buttons and self.send_photo_with_buttons(chat_id, jpeg, caption, buttons):
                    continue
                if not self._send_photo_to_chat(chat_id, jpeg, caption):
                    self._send_message_to_chat(chat_id, caption)

        threading.Thread(target=_send, daemon=True).start()
        return True

    def _is_paused(self, chat_id: str, now_ts: float) -> bool:
        paused_until = self.paused_until_by_chat.get(chat_id, 0.0)
        if paused_until <= now_ts:
            self.paused_until_by_chat.pop(chat_id, None)
            return False
        return True

    def _remaining_pause_minutes(self, chat_id: str) -> int:
        now_ts = time.time()
        paused_until = self.paused_until_by_chat.get(chat_id, 0.0)
        if paused_until <= now_ts:
            self.paused_until_by_chat.pop(chat_id, None)
            return 0
        remaining_seconds = int(paused_until - now_ts)
        return max(1, (remaining_seconds + 59) // 60)

    def _send_alert(
        self,
        message: str,
        video_path: Optional[str] = None,
        target_chat_ids: Optional[List[str]] = None,
        force_video: bool = False,
    ) -> bool:
        if not self.enabled or not self.is_configured():
            return False
        sent_any = False
        now_ts = time.time()
        recipients = target_chat_ids if target_chat_ids else self.chat_ids
        for chat_id in recipients:
            if self._is_paused(chat_id, now_ts):
                continue
            use_video = (self.include_video or force_video) and video_path and os.path.exists(video_path)
            if use_video:
                # Single-message flow: send only one video message with caption.
                sent = self._send_video_to_chat(chat_id, video_path, message)
                if not sent:
                    sent = self._send_message_to_chat(chat_id, message)
            else:
                sent = self._send_message_to_chat(chat_id, message)
            sent_any = sent_any or sent
        return sent_any

    def trigger_alert(
        self,
        message: str = "Sentinel: motion detected.",
        video_path: Optional[str] = None,
        target_chat_ids: Optional[List[str]] = None,
        force_video: bool = False,
        bypass_cooldown: bool = False,
    ) -> bool:
        """Send an alert asynchronously if enabled and cooldown elapsed."""
        if not self.enabled or not self.is_configured():
            return False
        if not bypass_cooldown:
            with self._send_lock:
                now = time.time()
                if (now - self.last_alert_time) < self.cooldown_seconds:
                    return False
                self.last_alert_time = now
        thread = threading.Thread(
            target=self._send_alert,
            args=(message, video_path, target_chat_ids, force_video),
            daemon=True,
        )
        thread.start()
        return True

    def send_test_message(self) -> bool:
        """Send a synchronous test message from the UI."""
        if not self.enabled or not self.is_configured():
            return False
        return self._send_alert("Sentinel Telegram test message.")

    def consume_clip_requests(self) -> List[str]:
        """Return and clear chat IDs that requested an on-demand clip."""
        requests = list(dict.fromkeys(self._pending_clip_chat_ids))
        self._pending_clip_chat_ids = []
        return requests

    def consume_remote_feature_commands(self) -> List[Tuple[str, Dict[str, bool]]]:
        """Return and clear (chat_id, feature_changes) from Telegram toggle commands."""
        pending = list(self._pending_remote_commands)
        self._pending_remote_commands = []
        return pending

    # Phrases that ask "how do I get to Sentinel from elsewhere?"
    _ACCESS_PHRASES = {
        "access", "remote", "remote access", "connect", "how do i connect",
        "web", "website", "link", "url", "tailscale", "remote desktop", "rdp",
    }

    @classmethod
    def _is_access_request(cls, text: str) -> bool:
        normalized = re.sub(r"[^a-z ]+", "", text.lower()).strip()
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized in cls._ACCESS_PHRASES

    def consume_video_queries(self) -> List[Tuple[str, str]]:
        """Return and clear (chat_id, text) requests for recorded videos."""
        pending, self._pending_video_queries = self._pending_video_queries, []
        return pending

    def consume_questions(self) -> List[Tuple[str, str]]:
        """Return and clear (chat_id, text) plain-language questions ("did anyone come by?")."""
        pending, self._pending_questions = self._pending_questions, []
        return pending

    def consume_free_text(self) -> List[Tuple[str, str]]:
        """Return and clear (chat_id, text) messages none of the built-in commands understood."""
        pending, self._pending_free_text = self._pending_free_text, []
        return pending

    def send_photo(self, chat_id: str, jpeg: bytes, caption: str) -> bool:
        """Synchronously send one photo (used for question replies)."""
        return self._send_photo_to_chat(chat_id, jpeg, caption)

    def send_video(self, chat_id: str, video_path: str, caption: str) -> bool:
        """Synchronously send one video (used for query replies)."""
        return self._send_video_to_chat(chat_id, video_path, caption)

    def consume_access_requests(self) -> List[str]:
        """Return and clear chat IDs that asked how to access Sentinel remotely."""
        chats = list(dict.fromkeys(self._pending_access_chat_ids))
        self._pending_access_chat_ids = []
        return chats

    def consume_detailed_status_requests(self) -> List[str]:
        """Return and clear chat IDs waiting for a full Status reply."""
        chats = list(dict.fromkeys(self._pending_detailed_status_chat_ids))
        self._pending_detailed_status_chat_ids = []
        return chats

    def reply_to_chat(self, chat_id: str, message: str) -> bool:
        """Send a plain text reply (main thread / worker thread safe enough for Bot API)."""
        if not self.is_configured():
            return False
        return self._send_message_to_chat(chat_id, message)

    def remaining_pause_minutes(self, chat_id: str) -> int:
        """Minutes remaining for Pause for this chat, or 0 if active."""
        return self._remaining_pause_minutes(chat_id)

    def _refresh_listener_state(self) -> None:
        if self.enabled and self.is_configured():
            self._start_listener()
        else:
            self._listener_running = False

    def _start_listener(self) -> None:
        if self._listener_thread and self._listener_thread.is_alive():
            return
        self._listener_running = True
        self._listener_thread = threading.Thread(
            target=self._listener_loop,
            daemon=True,
        )
        self._listener_thread.start()

    def _listener_loop(self) -> None:
        while self._listener_running and self.enabled and self.is_configured():
            try:
                self._process_pause_commands()
            except Exception as e:  # keep listening, but say what went wrong
                app_logger.log("error", "telegram_listen_failed", error_message=str(e)[:300], component="telegram",
                               action="listen")
            time.sleep(2)

    def _process_pause_commands(self) -> None:
        params = {"timeout": 20}
        if self._update_offset is not None:
            params["offset"] = self._update_offset
        url = (
            f"{self._api_url('getUpdates')}?{urllib.parse.urlencode(params)}"
        )
        try:
            with urllib.request.urlopen(url, timeout=25) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
            return

        if not payload.get("ok"):
            return
        for item in payload.get("result", []):
            update_id = int(item.get("update_id", 0))
            self._update_offset = update_id + 1
            press = item.get("callback_query")
            if press:
                chat_id = str(((press.get("message") or {}).get("chat") or {}).get("id", "")).strip()
                if chat_id and chat_id in self.chat_ids:  # only our own chats may press our buttons
                    self._pending_buttons.append((chat_id, int(press["message"].get("message_id", 0)),
                                                  str(press.get("data", ""))[:64], str(press.get("id", ""))))
                continue
            message = item.get("message", {})
            chat = message.get("chat", {})
            chat_id = str(chat.get("id", "")).strip()
            text = str(message.get("text", "")).strip()
            if not chat_id:
                continue
            if chat_id not in self.chat_ids:
                app_logger.log("warning", "telegram_message_ignored", chat_id=chat_id, chat_type=chat.get("type"),
                               reason="chat not in Telegram chat IDs", component="telegram", action="listen")
                continue
            if not text:
                app_logger.log("info", "telegram_message_ignored", chat_id=chat_id,
                               reason="no text (photo, voice or sticker)", component="telegram", action="listen")
                self._send_message_to_chat(chat_id, "I can only read typed messages. Send Help for what I can do.")
                continue
            app_logger.log("info", "telegram_message_received", chat_id=chat_id, length=len(text),
                           component="telegram", action="listen")

            self.route_text(chat_id, text)

    @classmethod
    def is_command(cls, text: str) -> bool:
        """Is this exactly one of Sentinel's own commands (what a "Do it" button may run)?"""
        from sentinel.engine.queries import is_video_query

        t = text.strip()
        if re.fullmatch(r"(?i)(resume|status|help|clip|pause\s+\d{1,4})", t) or cls._is_access_request(t):
            return True
        changes, err = cls._parse_feature_toggle_command(t)
        return bool(changes) and err is None or is_video_query(t)

    def route_text(self, chat_id: str, text: str, from_button: bool = False) -> None:
        """Handle one typed message (or a command from a "Do it" button)."""
        if re.match(r"^resume$", text, flags=re.IGNORECASE):
            self.paused_until_by_chat.pop(chat_id, None)
            self._send_message_to_chat(chat_id, "Sentinel notifications resumed.")
            return

        if re.match(r"^status$", text, flags=re.IGNORECASE):
            self._pending_detailed_status_chat_ids.append(chat_id)
            return

        if self._is_access_request(text):
            self._pending_access_chat_ids.append(chat_id)
            return

        from sentinel.engine.queries import is_video_query

        if is_video_query(text):
            self._pending_video_queries.append((chat_id, text))
            return

        from sentinel.engine.questions import QUESTION_HELP, is_question

        if is_question(text) or (self.question_filter is not None and self.question_filter(text)):
            self._pending_questions.append((chat_id, text))
            return

        if re.match(r"^help$", text, flags=re.IGNORECASE):
            self._send_message_to_chat(
                chat_id,
                self._commands_help_text(),
            )
            return

        if re.match(r"^clip$", text, flags=re.IGNORECASE):
            self._pending_clip_chat_ids.append(chat_id)
            self._send_message_to_chat(
                chat_id,
                "Sentinel is recording a 10-second clip now.",
            )
            return

        changes, parse_err = self._parse_feature_toggle_command(text)
        if parse_err is not None:
            if not from_button and self.assistant_available is not None and self.assistant_available():
                self._pending_free_text.append((chat_id, text))  # "enable alerts": let Claude work it out
            else:
                self._send_message_to_chat(chat_id, parse_err)
            return
        if changes is not None:
            if changes:
                self._pending_remote_commands.append((chat_id, changes))
            return

        match = re.match(r"^pause\s+(\d{1,4})$", text, flags=re.IGNORECASE)
        if not match:
            if not from_button and self.assistant_available is not None and self.assistant_available():
                self._pending_free_text.append((chat_id, text))
            elif text.endswith("?"):  # a question we didn't understand: show the kinds we do
                self._send_message_to_chat(chat_id, "Sorry, I didn't catch that. " + QUESTION_HELP
                                           + "\n\nSend Help for all commands.")
            else:
                self._send_message_to_chat(chat_id, self._commands_help_text())
            return
        minutes = max(1, int(match.group(1)))
        self.paused_until_by_chat[chat_id] = time.time() + (minutes * 60)
        self._send_message_to_chat(
            chat_id,
            f"Sentinel notifications paused for {minutes} minutes.",
        )

    def send_text_with_buttons(self, chat_id: str, text: str, buttons: List[List[Tuple[str, str]]]) -> bool:
        """A text message with inline buttons (rows of (label, callback data))."""
        if not self.is_configured():
            return False
        return self._post_json(self._api_url("sendMessage"), {
            "chat_id": chat_id, "text": text,
            "reply_markup": {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]},
        })

    def edit_text(self, chat_id: str, message_id: int, text: str) -> bool:
        """Replace a text message (and remove its buttons)."""
        return self._post_json(self._api_url("editMessageText"),
                               {"chat_id": chat_id, "message_id": message_id, "text": text})

    def cleanup(self) -> None:
        """Stop background listener when app exits."""
        self._listener_running = False
