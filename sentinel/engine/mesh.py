"""
Mesh chat: text messages over the Meshtastic radio mesh, through the LoRa base station that hears the pets'
collars (tools/meshtastic_bridge.py). The Mesh page lists who the base hears, holds the conversations (one
per person messaged directly, one per channel) and makes the invites: a QR that adds the base station to
someone's Meshtastic app as a contact (direct messages, end-to-end encrypted once both radios know each
other's key), and one that adds the private "Family" group channel. New messages to the house come to
Telegram, Jarvis says who sent one (not at night), and the Live page shows a 📡 count until they're read;
chatter on the public channel does none of that. "Read my mesh messages" (Jarvis or Telegram) reads them out.

The base station can't be reached from here (it sits behind the home network's Wi-Fi and its radio takes one
connection), so it checks in every few seconds instead: it brings what it heard, its node list and invite
links, and takes back what to send. It signs in with a pet's tracker login (Family page), the only login it
has. Messages are kept in mesh.db; the node list lives in memory (the base sends it again within seconds).
"""

import os
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from sentinel.logging_utils import app_logger

DB_NAME = "mesh.db"
TEXT_MAX_BYTES = 200  # a Meshtastic text packet holds ~230 bytes; leave room for the radio's own framing
BASE_LONG_MAX = 39  # the radio's own limits for its names (bytes)
BASE_SHORT_MAX = 4
CONNECTED_S = 30  # the base checks in every 5 s; this long without one = not connected
NODES_FRESH_S = 60  # how often the base sends its (possibly long) node list
SEND_EXPIRE_S = 10 * 60  # a message the base never picked up gives up after this
FAMILY_CHANNEL = "Family"
BROADCAST = "^all"
QUIET_HOURS = (22, 7)  # Jarvis doesn't announce messages from 10 pm to 7 am (Telegram still gets them)
ANNOUNCE_GAP_S = 120  # one announcement per sender this often (a burst of short messages is one heads-up)
READ_MAX = 5  # messages Jarvis reads in one go

# "read my mesh messages", "any new mesh messages?", "read me my messages", "check mesh" (Whisper: mash, mess)
_MESH = r"(?:mesh|mash|mess)"
READ_COMMAND = re.compile(
    r"^(?:(?:can you |could you |please )?read (?:me )?(?:my |the |any )?(?:new |unread |latest )?(?:" + _MESH +
    r" )?messages?|(?:do i have |have i got |are there |did i get |i have )?(?:any )?(?:new |unread )?" + _MESH +
    r" messages?|check (?:my |the )?" + _MESH + r"(?: messages?)?)(?: please)?[?.!]*$", re.IGNORECASE)


def read_command(text: str) -> Optional[bool]:
    """True when the text asks for the mesh messages (the command registry's match: None = not this)."""
    return True if READ_COMMAND.match(" ".join(str(text or "").strip().split())) else None


def _clip(text: Any, limit: int) -> str:
    """A string at most limit bytes long in UTF-8, cut on a character boundary."""
    raw = str(text if isinstance(text, str) else "").strip().encode("utf-8")[:limit]
    return raw.decode("utf-8", "ignore")


def _node_id(value: Any) -> str:
    """A Meshtastic node id ("!3248f6ed") or "" for anything else."""
    s = str(value or "").strip().lower()
    return s if len(s) == 9 and s[0] == "!" and all(c in "0123456789abcdef" for c in s[1:]) else ""


class MeshChat:
    def __init__(self, root: str, settings: Dict[str, Any], settings_manager,
                 authenticate: Callable[[str, str], Any], telegram,
                 trackers: Callable[[], List[str]] = lambda: [], speak: Callable[[str], Any] = lambda _t: None) -> None:
        self.db_path = os.path.join(os.path.abspath(root), DB_NAME)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.settings = settings
        self.settings_manager = settings_manager
        self._authenticate = authenticate
        self.telegram = telegram
        self._trackers = trackers  # the pets' collars: radios too, but not ones to chat with
        self._speak = speak  # Jarvis, out loud in the house
        self._announced: Dict[str, float] = {}  # sender -> last time Jarvis said they wrote
        self._lock = threading.RLock()
        self._base: Dict[str, Any] = {}  # the base's last check-in: id, name, short, channels, invites
        self._checked_in = 0.0
        self._nodes: Dict[str, Dict[str, Any]] = {}
        self._nodes_at = 0.0
        self._make_channel = False  # the page asked for the Family channel: the next check-in says so
        self._rename: Optional[Dict[str, str]] = None
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            # peer = who the conversation is with: a node id (direct) or "#<channel index>" (a channel).
            conn.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,"
                         " peer TEXT NOT NULL, inbound INTEGER NOT NULL, sender TEXT NOT NULL, sender_name TEXT NOT NULL,"
                         " text TEXT NOT NULL, status TEXT NOT NULL, packet_id INTEGER, read INTEGER NOT NULL DEFAULT 0)")
            conn.execute("CREATE INDEX IF NOT EXISTS messages_peer ON messages (peer, ts)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS messages_heard ON messages (sender, packet_id)"
                         " WHERE inbound = 1")  # the base sends a message again if Sentinel didn't answer
            # What the family calls a radio here ("KD1ABC" -> "Uncle Ben"): shown instead of the radio's own name.
            conn.execute("CREATE TABLE IF NOT EXISTS nicknames (node TEXT PRIMARY KEY, name TEXT NOT NULL)")
        try:
            os.chmod(self.db_path, 0o600)  # family conversations
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------ the base station's check-in
    def bridge_checkin(self, username: str, password: str, payload: Any) -> Optional[Dict[str, Any]]:
        """What to tell the base station, or None if its login is wrong."""
        person = self._authenticate(username, password)
        if person is None or not getattr(person, "tracker", ""):
            return None
        body = payload if isinstance(payload, dict) else {}
        now = time.time()
        with self._lock:
            self._checked_in = now
            base = body.get("base")
            if isinstance(base, dict):
                self._base = base
            if isinstance(body.get("nodes"), list):
                self._nodes = {n["id"]: n for n in body["nodes"] if isinstance(n, dict) and _node_id(n.get("id"))}
                self._nodes_at = now
        for item in body.get("received") or []:
            if isinstance(item, dict):
                self._heard(item)
        for item in body.get("results") or []:
            if isinstance(item, dict):
                self._result(item)
        reply: Dict[str, Any] = {"send": self._outbox(now), "want_nodes": now - self._nodes_at > NODES_FRESH_S}
        with self._lock:
            if self._make_channel:
                reply["make_channel"] = FAMILY_CHANNEL
                self._make_channel = False
            if self._rename:
                reply["rename"], self._rename = self._rename, None
        return reply

    def _channel(self, index: Any) -> Dict[str, Any]:
        return next((c for c in self._base.get("channels") or [] if c.get("index") == index), {})

    def _heard(self, item: Dict[str, Any]) -> None:
        """A text the base heard: keep it, and tell Telegram unless it's the public channel's chatter."""
        sender, text = _node_id(item.get("from")), _clip(item.get("text"), 1000)
        if not sender or not text or not isinstance(item.get("packet"), int):
            return
        me = self._base.get("id")
        to = str(item.get("to") or "")
        if to == BROADCAST:
            channel = item.get("channel") if isinstance(item.get("channel"), int) else 0
            peer = f"#{channel}"
        elif me and to == me:
            channel, peer = None, sender
        else:
            return  # (a direct message between two other radios isn't readable anyway)
        name = _clip(item.get("name"), 60) or (self._nodes.get(sender) or {}).get("name") or sender
        try:
            ts = min(float(item.get("ts")), time.time())
        except (TypeError, ValueError):
            ts = time.time()
        try:
            with self._lock, self._connect() as conn:
                conn.execute("INSERT INTO messages (ts, peer, inbound, sender, sender_name, text, status, packet_id)"
                             " VALUES (?, ?, 1, ?, ?, ?, 'heard', ?)", (ts, peer, sender, name, text, item["packet"]))
        except sqlite3.IntegrityError:
            return  # already have it
        app_logger.log("info", "mesh_message_heard", direct=channel is None, channel=channel,
                       component="engine", action="mesh")
        info = self._channel(channel) if channel is not None else {}
        if info.get("public"):
            return
        who = self.nicknames().get(sender) or name
        where = "" if channel is None else f" in {info.get('name') or 'a channel'}"
        if self.settings.get("mesh_alerts_enabled", True):
            text = f"📡 {who}{where} (mesh): {text}"
            for chat_id in self.telegram.unpaused_chats():
                threading.Thread(target=self.telegram.reply_to_chat, args=(chat_id, text),
                                 name="mesh-alert", daemon=True).start()
        self._announce(sender, who, "" if channel is None else f" on the {info.get('name') or 'group'} channel")

    def _announce(self, sender: str, who: str, where: str, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        hour = time.localtime(now).tm_hour
        if (not self.settings.get("mesh_speak_enabled", True) or hour >= QUIET_HOURS[0] or hour < QUIET_HOURS[1]
                or now - self._announced.get(sender, 0) < ANNOUNCE_GAP_S):
            return
        self._announced[sender] = now
        threading.Thread(target=self._speak, args=(f"You have a mesh message from {who}{where}.",),
                         name="mesh-announce", daemon=True).start()

    def _result(self, item: Dict[str, Any]) -> None:
        status = item.get("status")
        if not isinstance(item.get("id"), int) or status not in ("sent", "relayed", "delivered", "failed"):
            return
        if status == "failed":
            status = "failed: " + _clip(item.get("error") or "no answer", 60)
        with self._lock, self._connect() as conn:
            # (a late "relayed" never overwrites "delivered")
            conn.execute("UPDATE messages SET status = ? WHERE id = ? AND inbound = 0 AND status != 'delivered'",
                         (status, item["id"]))

    def _outbox(self, now: float) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE messages SET status = 'failed: the base station never picked it up'"
                         " WHERE inbound = 0 AND status = 'queued' AND ts < ?", (now - SEND_EXPIRE_S,))
            rows = conn.execute("SELECT id, peer, text FROM messages WHERE inbound = 0 AND status = 'queued'"
                                " ORDER BY id").fetchall()
        out = []
        for r in rows:
            if r["peer"].startswith("#"):
                out.append({"id": r["id"], "to": BROADCAST, "channel": int(r["peer"][1:]), "text": r["text"]})
            else:
                out.append({"id": r["id"], "to": r["peer"], "text": r["text"]})
        return out

    # ------------------------------------------------------------ the Mesh page
    def connected(self) -> bool:
        return time.time() - self._checked_in < CONNECTED_S

    def view(self) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            base, nodes = dict(self._base), dict(self._nodes)
        me = base.get("id")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT peer, MAX(ts) AS ts, SUM(inbound = 1 AND read = 0) AS unread, COUNT(*) AS n FROM messages"
                " GROUP BY peer").fetchall()
            last = {r["peer"]: r for r in conn.execute(
                "SELECT m.peer, m.text, m.sender, m.sender_name, m.inbound FROM messages m JOIN"
                " (SELECT peer, MAX(id) AS id FROM messages GROUP BY peer) l ON m.id = l.id")}
            names = {r["sender"]: r["sender_name"] for r in conn.execute(  # (the newest name wins)
                "SELECT sender, sender_name FROM messages WHERE inbound = 1 ORDER BY id")}
        nicks = self.nicknames()
        chats = []
        for ch in base.get("channels") or []:  # every channel has a conversation, even before anyone writes
            if ch.get("index") == 0 and not ch.get("public"):
                continue  # the primary private channel is the collars' (it's never shared)
            chats.append({"peer": f"#{ch['index']}", "name": ch.get("name") or f"Channel {ch['index']}",
                          "channel": True, "public": bool(ch.get("public"))})
        for r in rows:
            if r["peer"].startswith("#") and not any(c["peer"] == r["peer"] for c in chats):
                chats.append({"peer": r["peer"], "name": f"Channel {r['peer'][1:]}", "channel": True, "public": False})
            elif not r["peer"].startswith("#"):
                node = nodes.get(r["peer"]) or {}
                radio = node.get("name") or names.get(r["peer"]) or r["peer"]
                chats.append({"peer": r["peer"], "name": nicks.get(r["peer"]) or radio,
                              "radio": radio if r["peer"] in nicks else "", "channel": False,
                              "private": bool(node.get("pki"))})
        for c in chats:
            r = next((x for x in rows if x["peer"] == c["peer"]), None)
            c["ts"], c["unread"] = (r["ts"], int(r["unread"] or 0)) if r else (0, 0)
            if c["peer"] in last:
                m = last[c["peer"]]
                who = nicks.get(m["sender"]) or m["sender_name"]
                c["last"] = ("" if not m["inbound"] else who + ": " if c["channel"] else "") + m["text"]
        # channels first (Family, then the public one), then direct chats, newest first
        chats.sort(key=lambda c: (not c["channel"], c.get("public", False), -c["ts"]))
        collars = set(self._trackers())
        heard = sorted((dict(n, nick=nicks.get(n["id"], "")) for n in nodes.values()
                        if n["id"] != me and n["id"] not in collars), key=lambda n: -(n.get("heard") or 0))
        family = next((c for c in base.get("channels") or [] if c.get("name") == FAMILY_CHANNEL), None)
        return {
            "connected": self.connected(), "checked_in": self._checked_in or None, "now": now,
            "base": {"id": me, "name": base.get("name"), "short": base.get("short")},
            "chats": chats, "nodes": heard[:300],
            "invites": {"contact": (base.get("invites") or {}).get("contact"),
                        "family": (base.get("invites") or {}).get(str(family["index"])) if family else None},
            "family_channel": bool(family), "making_channel": self._make_channel,
            "alerts": bool(self.settings.get("mesh_alerts_enabled", True)), "text_max": TEXT_MAX_BYTES,
            "speak": bool(self.settings.get("mesh_speak_enabled", True)),
        }

    def thread(self, peer: str, limit: int = 200) -> List[Dict[str, Any]]:
        """A conversation, oldest first; opening it marks it read."""
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM messages WHERE peer = ? ORDER BY id DESC LIMIT ?",
                                (peer, limit)).fetchall()
            conn.execute("UPDATE messages SET read = 1 WHERE peer = ? AND read = 0", (peer,))
        nicks = self.nicknames()
        return [{"id": r["id"], "ts": r["ts"], "inbound": bool(r["inbound"]), "sender": r["sender"],
                 "name": nicks.get(r["sender"]) or r["sender_name"], "text": r["text"], "status": r["status"]}
                for r in reversed(rows)]

    def send(self, peer: Any, text: Any) -> Tuple[bool, str]:
        peer = str(peer or "")
        if peer.startswith("#"):
            if not peer[1:].isdigit() or not 0 <= int(peer[1:]) <= 7:
                return False, "That isn't a channel."
        elif not _node_id(peer):
            return False, "Pick who to send it to."
        body = text.strip() if isinstance(text, str) else ""
        if not body:
            return False, "Type a message first."
        if len(body.encode("utf-8")) > TEXT_MAX_BYTES:
            return False, f"Too long for one radio message ({TEXT_MAX_BYTES} characters at most)."
        if not self._checked_in:
            return False, "The base station hasn't checked in yet."
        with self._lock:
            name = self._base.get("name") or "Sentinel"
        with self._lock, self._connect() as conn:
            conn.execute("INSERT INTO messages (ts, peer, inbound, sender, sender_name, text, status, read)"
                         " VALUES (?, ?, 0, ?, ?, ?, 'queued', 1)",
                         (time.time(), _node_id(peer) or peer, self._base.get("id") or "", name, body))
        app_logger.log("info", "mesh_message_queued", direct=not peer.startswith("#"), component="engine", action="mesh")
        return True, "Sending…" if self.connected() else "Queued: the base station isn't connected right now."

    def make_family_channel(self) -> Tuple[bool, str]:
        with self._lock:
            if any(c.get("name") == FAMILY_CHANNEL for c in self._base.get("channels") or []):
                return True, "The Family channel is already there."
            if not self.connected():
                return False, "The base station isn't connected."
            self._make_channel = True
        return True, "Adding the Family channel to the base station…"

    def rename_base(self, long_name: Any, short_name: Any) -> Tuple[bool, str]:
        long_name, short_name = _clip(long_name, BASE_LONG_MAX), _clip(short_name, BASE_SHORT_MAX)
        if not long_name or not short_name:
            return False, "Give the base station a name and a short name (up to 4 letters)."
        if not self.connected():
            return False, "The base station isn't connected."
        with self._lock:
            self._rename = {"long": long_name, "short": short_name}
        return True, "Renaming the base station… everyone on the mesh will see the new name."

    def _unread_rows(self) -> List[sqlite3.Row]:
        """Unread messages to the house: direct ones and private channels' (not the public chatter), oldest first."""
        public = {f"#{c['index']}" for c in self._base.get("channels") or [] if c.get("public")}
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM messages WHERE inbound = 1 AND read = 0 ORDER BY id").fetchall()
        return [r for r in rows if r["peer"] not in public]

    def unread(self) -> Dict[str, Any]:
        """For the Live page's 📡: how many, and from whom."""
        rows, nicks = self._unread_rows(), self.nicknames()
        senders = list(dict.fromkeys(nicks.get(r["sender"]) or r["sender_name"] for r in reversed(rows)))
        return {"unread": len(rows), "from": senders[:3]}

    def read_unread(self) -> str:
        """Jarvis / Telegram: read the unread messages (oldest first) and mark them read."""
        rows, nicks = self._unread_rows(), self.nicknames()
        if not rows:
            return "No new mesh messages."
        names = {f"#{c['index']}": c.get("name") for c in self._base.get("channels") or []}
        shown = rows[:READ_MAX]
        lines = []
        for r in shown:
            who = nicks.get(r["sender"]) or r["sender_name"]
            where = f" on the {names.get(r['peer']) or 'group'} channel" if r["peer"].startswith("#") else ""
            lines.append(f"{who}{where} says: {' '.join(r['text'].split()).rstrip('.!? ')}.")
        with self._lock, self._connect() as conn:
            conn.executemany("UPDATE messages SET read = 1 WHERE id = ?", [(r["id"],) for r in shown])
        head = "One new mesh message." if len(rows) == 1 else f"{len(rows)} new mesh messages."
        more = len(rows) - len(shown)
        tail = f" {more} more on the Mesh page." if more else ""
        return " ".join([head] + lines) + tail

    def nicknames(self) -> Dict[str, str]:
        with self._connect() as conn:
            return {r["node"]: r["name"] for r in conn.execute("SELECT node, name FROM nicknames")}

    def set_nickname(self, node: Any, name: Any) -> Tuple[bool, str]:
        """Blank removes it (back to the radio's own name)."""
        node, name = _node_id(node), " ".join(_clip(name, 60).split())[:30]
        if not node:
            return False, "That isn't a radio."
        with self._lock, self._connect() as conn:
            if name:
                conn.execute("INSERT OR REPLACE INTO nicknames (node, name) VALUES (?, ?)", (node, name))
            else:
                conn.execute("DELETE FROM nicknames WHERE node = ?", (node,))
        return True, f"Nickname saved: {name}." if name else "Nickname removed."

    def set_speak(self, on: Any) -> Tuple[bool, str]:
        if not isinstance(on, bool):
            return False, "expected true or false"
        self.settings["mesh_speak_enabled"] = on
        self.settings_manager.save_settings(force=True)
        return True, "Jarvis will " + ("say when a mesh message comes in." if on else "stay quiet about mesh messages.")

    def set_alerts(self, on: Any) -> Tuple[bool, str]:
        if not isinstance(on, bool):
            return False, "expected true or false"
        self.settings["mesh_alerts_enabled"] = on
        self.settings_manager.save_settings(force=True)
        return True, "Telegram messages for new mesh chats " + ("on." if on else "off.")
