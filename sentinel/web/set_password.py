"""
Set web viewer username and bcrypt password in settings.json.

Usage (from repo root, venv active):
    python -m sentinel.web.set_password
"""

from __future__ import annotations

import getpass
import sys

import bcrypt

from sentinel.constants import SETTINGS_FILE
from sentinel.settings import SettingsManager


def main() -> int:
    sm = SettingsManager(SETTINGS_FILE)
    sm.ensure_web_defaults()

    print("Sentinel web viewer — set credentials (input hidden).")
    username = input("Username: ").strip()
    if not username:
        print("Username required.", file=sys.stderr)
        return 1
    pw1 = getpass.getpass("Password: ")
    pw2 = getpass.getpass("Password (again): ")
    if not pw1 or pw1 != pw2:
        print("Passwords empty or do not match.", file=sys.stderr)
        return 1

    h = bcrypt.hashpw(pw1.encode("utf-8"), bcrypt.gensalt(rounds=12))
    web = dict(sm.settings.get("web") or {})
    auth = dict(web.get("auth") or {})
    auth["username"] = username
    auth["password_hash"] = h.decode("ascii")
    web["auth"] = auth
    sm.settings["web"] = web
    if sm.save_settings(force=True):
        print("Saved. Set web.enabled to true and restart Sentinel to start the web server.")
        return 0
    print("Could not save settings.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
