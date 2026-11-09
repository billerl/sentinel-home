"""
Collar trackers -> Sentinel's family locations. Runs on the LoRa base station (a Raspberry Pi with meshtasticd),
next to the radio, and posts each tracker's position to Sentinel's /owntracks address the way a family phone
does, with that pet's own login (Family page; the pet is a person with a tracker id). Sentinel then treats it
like any family member, plus the yard ("Xena left the yard").

    python meshtastic_bridge.py bridge.json

bridge.json (keep it private: chmod 600):
    {"sentinel": "http://192.168.1.10:8088/owntracks",
     "trackers": {"!a1cacb2a": {"username": "grace", "token": "<the pet's phone password from Sentinel>"}}}

It also checks in with Sentinel every CHECK_IN_SECONDS. While a pet is out ("the dogs are going out" by voice or
Telegram) Sentinel answers "reportLocation" (the OwnTracks command it sends phones), and the base asks that
collar for its position at once, instead of waiting for the tracker's own schedule. "Recall Xena" answers
"beep": the base sends the collar a bell message, which sounds its buzzer (set the tracker's External
Notification to beep for the alert bell only, so nothing else makes it beep).

Mesh chat (Sentinel's Mesh page): the base passes on every text message it hears (direct messages to it and
channel messages), with its node list and invite links, and sends what Sentinel hands back, reporting whether
each one was delivered. It signs in with the first tracker's login. "mesh": false in bridge.json turns that off.

Needs the `meshtastic` Python package. Exits when the radio connection drops (systemd starts it again).
Positions shared with reduced precision (public channels blur them to kilometres) are posted with that
uncertainty, so they never move a pet in or out of the yard.
"""

import base64
import json
import os
import sys
import threading
import time
import urllib.request

M_PER_DEG = 111320.0
CHECK_IN_SECONDS = 5  # "recall Xena" shouldn't wait long
BELL = "\x07"  # a text with the bell character sounds the collar's buzzer (External Notification: alert bell)
TEXT_PORT = 1  # TEXT_MESSAGE_APP
NODES_MAX = 300
DEFAULT_KEYS = {b"", b"\x01"} | {bytes([i]) for i in range(2, 11)}  # no key, or one everybody knows ("AQ==")


def accuracy_m(position: dict) -> float:
    """How far off the fix may be, in metres: the blur of a reduced-precision position, else from HDOP."""
    bits = int(position.get("precisionBits") or 32)
    if bits < 32:
        return 2 ** (32 - bits) * 1e-7 * M_PER_DEG / 2
    hdop = position.get("HDOP") or position.get("PDOP")
    return max(5.0, hdop / 100 * 5) if hdop else 10.0


def owntracks(packet: dict, battery: dict, now: float) -> dict:
    """An OwnTracks location message from a Meshtastic position packet (as the meshtastic package decodes it)."""
    pos = packet["decoded"]["position"]
    lat = pos.get("latitude", pos.get("latitudeI", 0) * 1e-7)
    lon = pos.get("longitude", pos.get("longitudeI", 0) * 1e-7)
    msg = {"_type": "location", "lat": round(lat, 7), "lon": round(lon, 7), "acc": round(accuracy_m(pos), 1),
           "tst": int(pos.get("time") or packet.get("rxTime") or now), "t": "p"}
    if pos.get("groundSpeed") is not None:
        msg["vel"] = round(float(pos["groundSpeed"]) * 3.6)  # m/s -> km/h, as phones send it
    if battery.get(packet.get("fromId")) is not None:
        msg["batt"] = battery[packet["fromId"]]
    return msg


def post_json(url: str, login: dict, msg: dict):
    """Send one message; Sentinel's reply, decoded (None if it isn't JSON)."""
    auth = base64.b64encode(f"{login['username']}:{login['token']}".encode()).decode()
    req = urllib.request.Request(url, json.dumps(msg).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Basic " + auth})
    with urllib.request.urlopen(req, timeout=15) as resp:
        try:
            return json.loads(resp.read() or b"null")
        except ValueError:
            return None


def post(url: str, login: dict, msg: dict) -> list:
    """Send one OwnTracks message; Sentinel's reply (a list of OwnTracks commands, often empty)."""
    reply = post_json(url, login, msg)
    return reply if isinstance(reply, list) else []


def commands(reply: list) -> set:
    """The actions Sentinel asked for: "reportLocation", "beep"."""
    return {c.get("action") for c in reply if isinstance(c, dict) and c.get("_type") == "cmd"}


def mesh_text(packet: dict, nodes: dict) -> dict:
    """A text message the radio heard, the way Sentinel's mesh chat takes it ({} = not a chat message)."""
    decoded = packet.get("decoded") or {}
    text = decoded.get("text")
    if decoded.get("portnum") != "TEXT_MESSAGE_APP" or not isinstance(text, str) or not text.strip() or BELL in text:
        return {}
    sender = str(packet.get("fromId") or "")
    user = (nodes.get(sender) or {}).get("user") or {}
    return {"packet": packet.get("id"), "from": sender, "to": str(packet.get("toId") or ""),
            "channel": int(packet.get("channel") or 0), "name": user.get("longName") or sender, "text": text,
            "ts": int(packet.get("rxTime") or time.time())}


def node_list(nodes: dict, me: str) -> list:
    """Who the radio has heard, newest first (as the meshtastic package keeps them)."""
    out = []
    for n in nodes.values():
        user = n.get("user") or {}
        if not user.get("id") or user["id"] == me:
            continue
        out.append({"id": user["id"], "name": user.get("longName") or "", "short": user.get("shortName") or "",
                    "heard": n.get("lastHeard"), "hops": n.get("hopsAway"), "snr": n.get("snr"),
                    "pki": bool(user.get("publicKey")) and not user.get("isLicensed"), "channel": n.get("channel") or 0})
    out.sort(key=lambda n: -(n["heard"] or 0))
    return out[:NODES_MAX]


def channel_list(channels) -> list:
    """The radio's channels in use (role 1 = primary, 2 = secondary). Public = a key everybody has."""
    out = []
    for c in channels or []:
        if c.role in (1, 2):
            public = bytes(c.settings.psk) in DEFAULT_KEYS
            out.append({"index": c.index, "name": c.settings.name or ("LongFast" if public else "Primary"),
                        "public": public, "primary": c.role == 1})
    return out


def dm_channel(node: dict, channels: list) -> int:
    """Which channel a direct message goes out on: one the other radio has. With swapped keys it's encrypted to
    that radio either way; without, it can only be read with the channel's key."""
    heard_on = node.get("channel") or 0
    if heard_on and any(c["index"] == heard_on for c in channels):
        return heard_on  # e.g. the Family channel
    return next((c["index"] for c in channels if c["public"]), 0)


def send_status(response: dict, to: str) -> dict:
    """What the radio's answer to a sent message means: delivered (the other radio said so), relayed (a radio
    nearby passed it on: all a channel message gets) or failed."""
    routing = (response.get("decoded") or {}).get("routing") or {}
    error = routing.get("errorReason") or "NONE"
    if error != "NONE":
        reasons = {"MAX_RETRANSMIT": "no radio heard it" if to == "^all" else "no answer from their radio",
                   "NO_CHANNEL": "no channel in common", "PKI_FAILED": "the radios' keys didn't match"}
        return {"status": "failed", "error": reasons.get(error, error.lower().replace("_", " "))}
    return {"status": "delivered" if to != "^all" and response.get("fromId") == to else "relayed"}


def channel_url(node, index: int) -> str:
    """An invite that adds one channel to someone's Meshtastic app, keeping the channels they have."""
    from meshtastic.protobuf import apponly_pb2

    channel_set = apponly_pb2.ChannelSet()
    channel_set.settings.append(node.channels[index].settings)
    channel_set.lora_config.CopyFrom(node.localConfig.lora)
    return "https://meshtastic.org/e/?add=true#" + base64.urlsafe_b64encode(channel_set.SerializeToString()).decode().rstrip("=")


def make_channel(node, name: str) -> None:
    """A new private group channel (a random key), in the first free slot. The primary channel (the collars')
    and the public one stay as they are; the radio's frequency comes from the primary, so it doesn't move."""
    if any(c.role and c.settings.name == name for c in node.channels):
        return
    channel = node.getDisabledChannel()
    if channel is None:
        print(f"no free channel slot for {name}", flush=True)
        return
    channel.settings.name = name
    channel.settings.psk = os.urandom(32)
    channel.settings.module_settings.position_precision = 0  # the base doesn't share a position there
    channel.role = 2  # secondary
    node.writeChannel(channel.index)
    print(f"added channel {channel.index}: {name}", flush=True)


def main() -> None:
    import meshtastic.tcp_interface
    from pubsub import pub

    with open(sys.argv[1]) as f:
        config = json.load(f)
    trackers = {k.lower(): v for k, v in config["trackers"].items()}
    battery: dict = {}
    lost = threading.Event()
    radio = None
    mesh_url = config.get("mesh", config["sentinel"].rsplit("/", 1)[0] + "/mesh/bridge")
    mesh_login = next(iter(trackers.values()))
    lock = threading.Lock()
    heard: list = []  # texts waiting to reach Sentinel
    results: list = []  # how sent messages went, likewise
    sent: set = set()  # Sentinel's ids already on the air (it hands a message out until it hears back)
    want_nodes = True

    def beep(node: str) -> None:
        try:
            radio.sendText(BELL, destinationId=node, wantAck=True, channelIndex=0)
            print(f"{node}: beeped", flush=True)
        except Exception as e:
            print(f"{node}: couldn't beep: {e}", flush=True)

    def ask(node: str) -> None:
        """Ask the collar for its position now (it answers in a few seconds; the answer comes in as a packet).
        Not radio.sendPosition: with wantResponse it waits up to 5 min for the answer, and the check-ins (a "recall
        Xena" beep among them) waited with it (2026-11-09)."""
        from meshtastic.protobuf import mesh_pb2, portnums_pb2

        try:
            radio.sendData(mesh_pb2.Position(), destinationId=node, portNum=portnums_pb2.PortNum.POSITION_APP,
                           wantResponse=True, channelIndex=0)
            print(f"{node}: asked for its position", flush=True)
        except Exception as e:  # the radio library raises its own kinds
            print(f"{node}: couldn't ask: {e}", flush=True)

    def on_receive(packet, interface):
        if mesh_url:
            text = mesh_text(packet, interface.nodes or {})
            if text:
                with lock:
                    if len(heard) < 500:  # (Sentinel down for a long while: keep the first ones)
                        heard.append(text)
                print(f"{text['from']}: text for the mesh chat", flush=True)
                return
        sender = str(packet.get("fromId") or "").lower()
        if sender not in trackers:
            return
        decoded = packet.get("decoded") or {}
        metrics = (decoded.get("telemetry") or {}).get("deviceMetrics") or {}
        if metrics.get("batteryLevel") is not None:
            battery[packet.get("fromId")] = min(100, int(metrics["batteryLevel"]))  # 101 = on the charger
        if "position" not in decoded or not (decoded["position"].get("latitudeI") or decoded["position"].get("latitude")):
            return
        msg = owntracks(packet, battery, time.time())
        try:
            post(config["sentinel"], trackers[sender], msg)
            print(f"{sender}: posted (acc {msg['acc']} m)", flush=True)
        except OSError as e:
            print(f"{sender}: Sentinel didn't take it: {e}", flush=True)

    def send(item: dict, channels: list) -> None:
        to = str(item.get("to") or "")
        channel = int(item.get("channel") or 0) if to == "^all" else dm_channel((radio.nodes or {}).get(to) or {}, channels)
        packet_id = []

        def on_response(response):
            result = dict(send_status(response, to), id=item["id"])
            with lock:
                results.append(result)
            if result["status"] == "relayed" and to != "^all" and packet_id:
                radio._addResponseHandler(packet_id[0], on_response, ackPermitted=True)  # wait for "delivered"

        try:
            packet = radio.sendData(item["text"].encode(), destinationId=to, portNum=TEXT_PORT, wantAck=True,
                                    onResponse=on_response, onResponseAckPermitted=True, channelIndex=channel)
            packet_id.append(packet.id)
            result = {"id": item["id"], "status": "sent"}
            print(f"{to}: sent a chat message (channel {channel})", flush=True)
        except Exception as e:  # the radio library raises its own kinds
            result = {"id": item["id"], "status": "failed", "error": str(e)[:60]}
        sent.add(item["id"])
        with lock:
            results.insert(0, result)  # ("sent" before any answer that already came)

    def mesh_checkin() -> None:
        nonlocal want_nodes
        me = radio.getMyUser() or {}
        node = radio.localNode
        channels = channel_list(node.channels)
        invites = {}
        try:
            invites["contact"] = node.getContactURL(me["id"])
        except (Exception, SystemExit):  # (it exits the program when the radio doesn't know itself yet)
            pass
        for c in channels:
            if not c["primary"] and not c["public"]:
                invites[str(c["index"])] = channel_url(node, c["index"])
        with lock:
            got, done = list(heard), list(results)
        body = {"base": {"id": me.get("id"), "name": me.get("longName"), "short": me.get("shortName"),
                         "channels": channels, "invites": invites}, "received": got, "results": done}
        if want_nodes:
            body["nodes"] = node_list(radio.nodes or {}, me.get("id"))
        reply = post_json(mesh_url, mesh_login, body)
        with lock:
            del heard[:len(got)]
            for r in done:
                results.remove(r)
        if not isinstance(reply, dict):
            return
        want_nodes = bool(reply.get("want_nodes"))
        if reply.get("make_channel"):
            make_channel(node, str(reply["make_channel"]))
        rename = reply.get("rename")
        if isinstance(rename, dict) and rename.get("long") and rename.get("short"):
            node.setOwner(long_name=rename["long"], short_name=rename["short"])
            print(f"renamed the base station: {rename['long']}", flush=True)
        for item in reply.get("send") or []:
            if isinstance(item, dict) and isinstance(item.get("id"), int) and item["id"] not in sent:
                send(item, channels)

    pub.subscribe(on_receive, "meshtastic.receive")
    pub.subscribe(lambda interface, topic=pub.AUTO_TOPIC: lost.set(), "meshtastic.connection.lost")
    radio = meshtastic.tcp_interface.TCPInterface(config.get("radio", "localhost"))
    print(f"listening for {', '.join(trackers)}", flush=True)
    while not lost.wait(CHECK_IN_SECONDS):
        for node, login in trackers.items():
            try:
                wanted = commands(post(config["sentinel"], login, {"_type": "status"}))
                if "beep" in wanted:
                    beep(node)
                if "reportLocation" in wanted:
                    ask(node)
            except OSError:
                pass  # Sentinel restarting: next time
        if mesh_url:
            try:
                mesh_checkin()
            except OSError:
                pass
            except Exception as e:  # the chat must never stop the collars' tracking
                print(f"mesh chat: {e!r}", flush=True)
    radio.close()
    sys.exit("lost the radio connection")


if __name__ == "__main__":
    main()
