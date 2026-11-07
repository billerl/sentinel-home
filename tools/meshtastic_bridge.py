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

Needs the `meshtastic` Python package. Exits when the radio connection drops (systemd starts it again).
Positions shared with reduced precision (public channels blur them to kilometres) are posted with that
uncertainty, so they never move a pet in or out of the yard.
"""

import base64
import json
import sys
import threading
import time
import urllib.request

M_PER_DEG = 111320.0
CHECK_IN_SECONDS = 5  # "recall Xena" shouldn't wait long
BELL = "\x07"  # a text with the bell character sounds the collar's buzzer (External Notification: alert bell)


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


def post(url: str, login: dict, msg: dict) -> list:
    """Send one message; Sentinel's reply (a list of OwnTracks commands, often empty)."""
    auth = base64.b64encode(f"{login['username']}:{login['token']}".encode()).decode()
    req = urllib.request.Request(url, json.dumps(msg).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Basic " + auth})
    with urllib.request.urlopen(req, timeout=15) as resp:
        try:
            reply = json.loads(resp.read() or b"[]")
        except ValueError:
            return []
    return reply if isinstance(reply, list) else []


def commands(reply: list) -> set:
    """The actions Sentinel asked for: "reportLocation", "beep"."""
    return {c.get("action") for c in reply if isinstance(c, dict) and c.get("_type") == "cmd"}


def main() -> None:
    import meshtastic.tcp_interface
    from pubsub import pub

    with open(sys.argv[1]) as f:
        config = json.load(f)
    trackers = {k.lower(): v for k, v in config["trackers"].items()}
    battery: dict = {}
    lost = threading.Event()
    radio = None

    def beep(node: str) -> None:
        try:
            radio.sendText(BELL, destinationId=node, wantAck=True, channelIndex=0)
            print(f"{node}: beeped", flush=True)
        except Exception as e:
            print(f"{node}: couldn't beep: {e}", flush=True)

    def ask(node: str) -> None:
        """Ask the collar for its position now (it answers in a few seconds; the answer comes in as a packet)."""
        try:
            radio.sendPosition(destinationId=node, wantResponse=True, channelIndex=0)
            print(f"{node}: asked for its position", flush=True)
        except Exception as e:  # the radio library raises its own kinds
            print(f"{node}: couldn't ask: {e}", flush=True)

    def on_receive(packet, interface):
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
    radio.close()
    sys.exit("lost the radio connection")


if __name__ == "__main__":
    main()
