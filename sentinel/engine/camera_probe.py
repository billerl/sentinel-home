"""
"Test connection" for the Add-a-camera form: is the camera reachable, does it accept
the login, is there a stream at that path, and does the video decode?

OpenCV/ffmpeg only say "failed to open", so the login/path check speaks a single RTSP
DESCRIBE itself (with digest or basic auth), which returns a clear status code:
200 = fine, 401 = wrong username/password, 404 and friends = no stream at that path.
"""

import base64
import hashlib
import re
import socket
from typing import Dict, Optional, Tuple

import cv2

from sentinel.recordings.hd_recorder import main_stream_url

# Paths for the live/detection stream (the camera's low-res "substream") by brand.
BRAND_PATHS = {
    "anpviz": "/h264/ch1/sub/av_stream",
    "hikvision": "/Streaming/Channels/102",
    "reolink": "/h264Preview_01_sub",
    "dahua": "/cam/realmonitor?channel=1&subtype=1",
    "uniview": "/media/video2",
    "tapo": "/stream2",
    "foscam": "/videoSub",
}


def _rtsp_request(sock: socket.socket, url: str, cseq: int, auth: str = "") -> Tuple[int, Dict[str, str]]:
    lines = [f"DESCRIBE {url} RTSP/1.0", f"CSeq: {cseq}", "Accept: application/sdp", "User-Agent: Sentinel"]
    if auth:
        lines.append(f"Authorization: {auth}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    data = b""
    while b"\r\n\r\n" not in data and len(data) < 65536:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1", "replace").split("\r\n")
    m = re.match(r"RTSP/\d\.\d\s+(\d{3})", head[0] if head else "")
    headers: Dict[str, str] = {}
    for line in head[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers.setdefault(k.strip().lower(), v.strip())
    return (int(m.group(1)) if m else 0), headers


def _digest(user: str, password: str, challenge: str, url: str) -> str:
    params = dict(re.findall(r'(\w+)="?([^",]*)"?', challenge))
    ha1 = hashlib.md5(f"{user}:{params.get('realm', '')}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"DESCRIBE:{url}".encode()).hexdigest()
    response = hashlib.md5(f"{ha1}:{params.get('nonce', '')}:{ha2}".encode()).hexdigest()
    return (f'Digest username="{user}", realm="{params.get("realm", "")}", nonce="{params.get("nonce", "")}", '
            f'uri="{url}", response="{response}"')


def rtsp_status(host: str, port: int, path: str, user: str, password: str, timeout: float = 5.0) -> Optional[int]:
    """RTSP status for DESCRIBE on host:port/path (logging in if asked); None if unreachable."""
    url = f"rtsp://{host}:{port}{path}"
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            status, headers = _rtsp_request(sock, url, 1)
            if status == 401 and user:
                challenge = headers.get("www-authenticate", "")
                if challenge.lower().startswith("digest"):
                    auth = _digest(user, password, challenge, url)
                else:
                    auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
                status, _ = _rtsp_request(sock, url, 2, auth)
            return status
    except OSError:
        return None


def grab_frame(url: str, timeout_ms: int = 12000):
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms,
                                                 cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms])
    try:
        ok, frame = cap.read() if cap.isOpened() else (False, None)
    finally:
        cap.release()
    return frame if ok else None


def probe_camera(host: str, port: int, path: str, user: str, password: str, source: str,
                 main_source: Optional[str]) -> Dict:
    """Plain-English result for the form: {ok, message, width, height, hd, hd_message, jpeg}."""
    status = rtsp_status(host, port, path, user, password)
    if status is None:
        return {"ok": False, "message": f"Can't reach {host} on port {port}. Check the address, that the camera is "
                                        "powered and on this network, and that RTSP is turned on in its settings."}
    if status == 401:
        return {"ok": False, "message": "The camera is there but rejected the username or password."}
    if status != 200:
        return {"ok": False, "message": f"The camera answered, but there's no video at that stream path (code {status}). "
                                        "Pick the camera's brand above, or check its manual for the RTSP path."}
    frame = grab_frame(source)
    if frame is None:
        return {"ok": False, "message": "Connected and logged in, but the video couldn't be decoded. If the camera "
                                        "is set to H.265 for this stream, switch it to H.264 in the camera's settings."}
    height, width = frame.shape[:2]
    result = {"ok": True, "width": width, "height": height,
              "message": f"Connected — {width}×{height} video."}
    if width > 1280:
        # Some cameras answer any unknown path with their main stream instead of an error.
        result["message"] += (" That looks like the camera's high-res main stream, not its low-res substream: "
                              "live view and detection would use a lot more CPU. Check the brand / stream path.")
        result["heavy"] = True
    if main_source:
        from urllib.parse import urlparse

        parts = urlparse(main_source)
        main_path = parts.path + (f"?{parts.query}" if parts.query else "")
        hd = rtsp_status(host, parts.port or port, main_path, user, password) == 200
        result["hd"] = hd
        result["hd_message"] = ("High-res stream found: clips will be saved in HD." if hd else
                                "High-res stream not found: clips will be saved at normal quality.")
    else:
        result["hd"] = False
        result["hd_message"] = ("No high-res stream known for this path: clips will be saved at normal quality "
                                "(enter it under “High-res stream path” if you know it).")
    small = cv2.resize(frame, (480, int(480 * height / width)), interpolation=cv2.INTER_AREA)
    ok, jpeg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])
    result["jpeg"] = jpeg.tobytes() if ok else None
    return result


def guess_main_source(source: str) -> Optional[str]:
    return main_stream_url(source)
