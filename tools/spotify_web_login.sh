#!/bin/sh
# One-time Spotify sign-in for the web-player speaker (sentinel/audio/spotify_web.py).
# Run it on the PC's own screen (or over Remote Desktop), from the Sentinel folder: a Firefox window opens on
# Spotify's sign-in page. Sign in, wait for the player to appear, then close the window.
cd "$(dirname "$0")/.." || exit 1
venv/bin/python -c "from sentinel.audio.spotify_web import write_prefs; write_prefs()" || exit 1
firefox --no-remote --profile "$PWD/.spotify-web/profile" \
  "https://accounts.spotify.com/login?continue=https%3A%2F%2Fopen.spotify.com%2F"
if [ -f .spotify-web/profile/cookies.sqlite ]; then
  touch .spotify-web/signed_in
  echo "Saved. Sentinel starts the speaker within half a minute (Settings > Spotify shows it)."
else
  echo "No sign-in was saved."
fi
