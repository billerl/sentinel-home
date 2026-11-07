"""
Signing in: the login page, QR sign-in for TVs, and moving house screens to the home-network address.
"""

from __future__ import annotations

import ipaddress
import time
from typing import Any, List, Optional
from urllib.parse import quote

import segno
from flask import Flask, Response, jsonify, redirect, render_template, request, url_for

from sentinel.logging_utils import app_logger
from sentinel.web.auth import _client_ip
from sentinel.web.context import PERSON_PREFIX, TV_BROWSER, WebContext


_home_nets: List[Any] = [0.0, []]  # (read at, this PC's public IPv6 /64s)


def _from_this_home(client: str, if_inet6: str = "/proc/net/if_inet6") -> bool:
    """Whether a visitor the tunnel reports (Cf-Connecting-Ip) is in this house: its IPv6 address in one of this
    PC's own public /64s, which is how devices here reach Cloudflare. (Over IPv4 they'd share the router's
    address, which this PC doesn't know without asking the internet; those simply aren't recognised.)"""
    try:
        ip = ipaddress.ip_address((client or "").strip())
    except ValueError:
        return False
    if ip.version != 6:
        return False
    if time.time() - _home_nets[0] > 300:
        nets = []
        try:
            with open(if_inet6) as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 4 and parts[3] == "00":  # global scope
                        addr = ipaddress.ip_address(":".join(parts[0][i:i + 4] for i in range(0, 32, 4)))
                        if not addr.is_private:
                            nets.append(ipaddress.ip_network(f"{addr}/64", strict=False))
        except (OSError, ValueError):
            pass
        _home_nets[:] = [time.time(), nets]
    return any(ip in net for net in _home_nets[1])


def _device_name(user_agent: str) -> str:
    """Rough, readable device type for the QR sign-in approval page."""
    ua = user_agent or ""
    for needle, name in (("AFT", "Fire TV"), ("Silk", "Fire TV / Fire tablet"), ("SMART-TV", "Smart TV"),
                         ("Tizen", "Samsung TV"), ("Web0S", "LG TV"), ("CrKey", "Chromecast"),
                         ("Android", "Android device"), ("iPhone", "iPhone"), ("iPad", "iPad"),
                         ("Macintosh", "Mac"), ("Windows", "Windows PC"), ("Linux", "Linux PC")):
        if needle in ua:
            return name
    return "Unknown device"


def register(app: Flask, ctx: WebContext, home_base: Optional[str]) -> None:
    server, engine, auth, _session_token = ctx.server, ctx.engine, ctx.auth, ctx.session_token
    _require_session, _set_session_cookie = ctx.require_session, ctx.set_session_cookie
    _write_request_ok = ctx.write_request_ok

    def _loggable_username(typed: str) -> str:
        # People sometimes type their password into the username box; only
        # log the attempted name when it is the real one.
        configured = str(server._auth_cfg().get("username", ""))
        return typed if typed and typed == configured else "(other)"

    @app.before_request
    def _keep_home_screens_home():
        """A TV here that opens the public address sends its video out to Cloudflare and back all day (and
        goes dark with the internet): move it, still signed in, to this PC's home-network address."""
        if (home_base is None or auth is None or request.method != "GET"
                or not request.headers.get("Accept", "").startswith("text/html")  # pages, not images/streams
                or request.path.startswith(("/api/", "/static/", "/login", "/handoff", "/pair"))
                or request.remote_addr not in ("127.0.0.1", "::1")
                or not TV_BROWSER.search(request.headers.get("User-Agent", ""))
                or not _from_this_home(request.headers.get("Cf-Connecting-Ip", ""))):
            return None
        rec = auth.validate_session(_session_token(), _client_ip(request.remote_addr or "", request.headers))
        ticket = auth.create_handoff(rec.username) if rec else None
        if ticket is None:
            return None
        app_logger.log("info", "web_home_handoff", user_agent=request.headers.get("User-Agent", "")[:200],
                       component="web_server", action="handoff")
        return redirect(f"{home_base}/handoff?t={ticket}&next={quote(request.full_path.rstrip('?'), safe='')}")

    @app.route("/handoff")
    def handoff():
        ip = request.remote_addr or ""
        username = (auth.claim_handoff(request.args.get("t", ""))
                    if auth is not None and ip not in ("127.0.0.1", "::1") else None)
        if username is None:
            return redirect(url_for("login"))
        nxt = request.args.get("next", "/")
        resp = redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
        _set_session_cookie(resp, auth.create_session(username, ip, remember=True), True)
        return resp

    # ---- QR sign-in: a TV shows a code, a signed-in phone scans it and approves ----
    @app.route("/login/pair", methods=["POST"])
    def login_pair_start():
        if auth is None or not _write_request_ok():
            return jsonify({"error": "bad request"}), 400
        ip = _client_ip(request.remote_addr or "", request.headers)
        made = auth.create_pairing(ip, request.headers.get("User-Agent", ""))
        if made is None:
            return jsonify({"error": "too many sign-in codes; wait a few minutes"}), 429
        code, pairing = made
        proto = (request.headers.get("X-Forwarded-Proto") or request.scheme).split(",")[0].strip()
        url = f"{proto}://{request.host}/pair/{code}"
        svg = segno.make(url, error="m").svg_inline(scale=6, dark="#000", light="#fff", border=2)
        return jsonify({"code": code, "short": pairing.short, "qr": svg})

    @app.route("/login/pair/<code>")
    def login_pair_poll(code: str):
        if auth is None:
            return jsonify({"state": "expired"})
        ip = _client_ip(request.remote_addr or "", request.headers)
        state, token = auth.claim_pairing(code, ip)
        resp = jsonify({"state": state})
        if token:
            _set_session_cookie(resp, token, True)
            app_logger.log("info", "web_login_success", username=str(server._auth_cfg().get("username", "")),
                           ip=ip, method="qr", user_agent=request.headers.get("User-Agent", ""),
                           component="web_server", action="login_pair")
        return resp

    @app.route("/pair/<code>", methods=["GET", "POST"])
    def pair_approve(code: str):
        redir = _require_session()
        if redir:
            return redirect(url_for("login", next=f"/pair/{code}"))
        pairing = auth.pairing(code)
        if request.method == "POST":
            if not _write_request_ok():
                return jsonify({"error": "bad request"}), 400
            rec = auth.validate_session(_session_token(), _client_ip(request.remote_addr or "", request.headers))
            if pairing is None or not auth.approve_pairing(code, rec.username):
                return jsonify({"error": "That code has expired. Show a new one on the TV."}), 400
            app_logger.log("info", "web_pair_approved", ip=pairing.client_ip, user_agent=pairing.user_agent,
                           component="web_server", action="pair")
            return jsonify({"ok": True})
        here = _client_ip(request.remote_addr or "", request.headers)
        return render_template("pair.html", pairing=pairing, code=code,
                               same_network=pairing is not None and pairing.client_ip == here,
                               device=_device_name(pairing.user_agent) if pairing else "")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if auth is None:
            return "Web auth not configured", 503
        ip = _client_ip(request.remote_addr or "", request.headers)
        auth.reload_config(server._auth_cfg())

        if request.method == "GET":
            locked, retry_after = auth.is_locked(ip)
            return render_template(
                "login.html",
                locked=locked,
                retry_after=int(retry_after) + 1 if locked else 0,
            )

        locked, retry_after = auth.is_locked(ip)
        if locked:
            resp = Response("Too many attempts", 429)
            resp.headers["Retry-After"] = str(int(retry_after) + 1)
            app_logger.log(
                "warning",
                "web_login_locked",
                ip=ip,
                username_attempted=_loggable_username(request.form.get("username", "")),
                retry_after_seconds=int(retry_after) + 1,
                component="web_server",
                action="login",
            )
            return resp

        username = request.form.get("username", "")
        password = request.form.get("password", "")

        # The shared household login first, then each family member's own (their username is the
        # one on the Family page; "alex" is both, and the password says which one is meant).
        session_name = username if auth.verify_password(username, password) else None
        if session_name is None and engine is not None:
            person = engine.family.person_login(username, password)
            session_name = PERSON_PREFIX + person.username if person else None
        if session_name is not None:
            auth.record_success(ip)
            remember = request.form.get("remember") == "1"
            token = auth.create_session(session_name, ip, remember)
            app_logger.log(
                "info",
                "web_login_success",
                username=session_name,
                ip=ip,
                user_agent=request.headers.get("User-Agent", ""),
                component="web_server",
                action="login",
            )
            nxt = request.args.get("next") or url_for("index")
            resp = redirect(nxt)
            _set_session_cookie(resp, token, remember)
            return resp

        now_locked, ra = auth.record_failure(ip)
        app_logger.log(
            "warning",
            "web_login_failed",
            username_attempted=_loggable_username(username),
            ip=ip,
            component="web_server",
            action="login",
        )
        if now_locked:
            app_logger.log(
                "warning",
                "web_login_locked",
                ip=ip,
                username_attempted=_loggable_username(username),
                retry_after_seconds=int(ra) + 1,
                component="web_server",
                action="login",
            )
            resp = Response("Too many attempts", 429)
            resp.headers["Retry-After"] = str(int(ra) + 1)
            return resp
        return render_template("login.html", locked=False, error="Invalid username or password"), 401

    @app.route("/logout", methods=["POST"])
    def logout():
        if auth is None:
            return redirect(url_for("login"))
        tok = _session_token()
        if tok:
            rec = auth.revoke_session(tok, "logout")
            if rec:
                app_logger.log(
                    "info",
                    "web_session_revoked",
                    username=rec.username,
                    ip=_client_ip(request.remote_addr or "", request.headers),
                    reason="logout",
                    component="web_server",
                    action="logout",
                )
        resp = redirect(url_for("login"))
        resp.delete_cookie("sentinel_session", path="/")
        return resp
