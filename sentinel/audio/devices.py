"""
Which sound card Sentinel listens and speaks on: the PC's own (front mic and line-out jacks) or a USB
speakerphone such as a Jabra Speak, chosen under Settings > Audio.

Measured 2026-11-03, same 30 s of speech on both at once: a Jabra Speak 410 had the voice 29 dB above the
room noise against 13 dB on the PC's front mic jack, and Whisper heard "is the garage door open?" (75% sure)
where the jack gave "garage floor" (67%); both woke on "Hey Jarvis". The Jabra also cancels its own echo.

Cards are named by their ALSA id ("PCH", "USB"), which stays put when USB devices are plugged in a different
order, unlike card numbers. The shared speaker (alsa-sentinel.conf: voice, chimes and Spotify mixed onto one
card) is written for the chosen cards at runtime. Music can have its own card: by default ("auto") the speakers
in the PC's line-out jack when something is plugged in there (the sound card reports it), else the voice's.
"""

import os
import re
import subprocess
import tempfile
import zlib
from typing import Dict, List, Optional, Tuple

ASOUND = "/proc/asound"
DEFAULT_CARD = "PCH"  # the PC's own sound card
BUNDLED_CONF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alsa-sentinel.conf")
CARD_ID = re.compile(r"^\w{1,32}$")
MIC_DEVICE = re.compile(r"^plughw:CARD=(\w{1,32}),DEV=(\d)$")

_speaker_card = DEFAULT_CARD
_music_card = DEFAULT_CARD
_conf_path = BUNDLED_CONF
MIX = """pcm.{name} {{
    type dmix
    ipc_key {key}
    ipc_perm 0660
    slave {{
        pcm "hw:{card},0"
        rate 48000
        channels 2
        format S16_LE
        period_size 1024
        buffer_size 8192
    }}
}}
"""


def sound_cards(root: str = ASOUND) -> List[Dict[str, object]]:
    """[{id, name, number, mic, speaker}] for each sound card ALSA knows (HDMI-only outputs left out)."""
    try:
        with open(os.path.join(root, "cards")) as f:
            text = f.read()
    except OSError:
        return []
    out = []
    for m in re.finditer(r"^\s*(\d+) \[(\w+)\s*\]: [^\n]*\n\s+(.+)$", text, re.M):
        number, card_id, name = int(m.group(1)), m.group(2), m.group(3).strip()
        name = re.sub(r" at \S+.*$", "", name)  # "Jabra SPEAK 410 USB at usb-0000:00:14.0-1, full speed"
        try:
            pcms = os.listdir(os.path.join(root, f"card{number}"))
        except OSError:
            pcms = []
        out.append({"id": card_id, "name": name, "number": number,
                    "mic": any(re.fullmatch(r"pcm\d+c", p) for p in pcms),
                    "speaker": "pcm0p" in pcms})  # device 0: the analog/USB output, not HDMI
    return out


def mic_device(card_id: str) -> str:
    return f"plughw:CARD={card_id},DEV=0"


def speaker_card() -> str:
    return _speaker_card


def alsa_conf_path() -> str:
    return _conf_path


def line_out_plugged(card_id: str = DEFAULT_CARD, run=subprocess.run) -> bool:
    """Is something plugged into the card's line-out/headphone jack? (Its jack-sense switch.)"""
    try:
        controls = run(["amixer", "-c", card_id, "controls"], capture_output=True, text=True, timeout=3).stdout
        for name in re.findall(r"name='([^']*(?:Line Out|Headphone) Jack)'", controls):
            if "values=on" in run(["amixer", "-c", card_id, "cget", f"iface=CARD,name={name}"],
                                  capture_output=True, text=True, timeout=3).stdout:
                return True
    except (OSError, subprocess.TimeoutExpired):
        pass
    return False


def music_card_for(setting: Optional[str], speaker: str, plugged=line_out_plugged) -> str:
    """The music's card for the "music_device" setting: a card id, or "auto"/"": the PC's line-out when
    something's plugged into it, else the voice's speaker."""
    if setting and setting != "auto" and CARD_ID.match(setting):
        return setting
    return DEFAULT_CARD if speaker == DEFAULT_CARD or plugged(DEFAULT_CARD) else speaker


def use_cards(speaker: Optional[str], music: Optional[str] = "auto", directory: Optional[str] = None,
              plugged=line_out_plugged) -> Tuple[str, str]:
    """Point the shared speaker (voice, chimes) at card ``speaker`` (None / "": the PC's own) and the music at
    ``music`` (see music_card_for). Returns (speaker card, music card)."""
    global _speaker_card, _music_card, _conf_path
    speaker = speaker if speaker and CARD_ID.match(speaker) else DEFAULT_CARD
    music_card = music_card_for(music, speaker, plugged)
    if speaker == music_card == DEFAULT_CARD:
        _speaker_card, _music_card, _conf_path = speaker, music_card, BUNDLED_CONF
        return speaker, music_card

    def key(card: str) -> int:  # each card its own shared-memory key: a mixer running for another must not be joined
        return 51515 if card == DEFAULT_CARD else 51515 + zlib.crc32(card.encode()) % 1000

    conf = ["# Written by Sentinel (sentinel/audio/devices.py) from Settings > Audio; see alsa-sentinel.conf",
            MIX.format(name="sentinel_mix", key=key(speaker), card=speaker),
            'pcm.sentinel_speaker {\n    type plug\n    slave.pcm "sentinel_mix"\n}\n']
    music_mix = "sentinel_mix"
    if music_card != speaker:
        music_mix = "sentinel_music_mix"
        conf.append(MIX.format(name=music_mix, key=key(music_card), card=music_card))
    conf.append('pcm.sentinel_music {\n    type plug\n    slave.pcm {\n        type softvol\n'
                f'        slave.pcm "{music_mix}"\n        control {{\n            name "Sentinel Music"\n'
                f'            card {music_card}\n        }}\n    }}\n}}\n')
    directory = directory or tempfile.gettempdir()
    path = os.path.join(directory, f"sentinel-alsa-{speaker}-{music_card}.conf")
    try:
        with open(path + ".tmp", "w") as f:
            f.write("\n".join(conf))
        os.replace(path + ".tmp", path)
    except OSError:
        return _speaker_card, _music_card
    _speaker_card, _music_card, _conf_path = speaker, music_card, path
    return speaker, music_card


def use_speaker_card(card_id: Optional[str], directory: Optional[str] = None) -> str:
    """Voice and music on one card (tests, and callers without a music setting)."""
    return use_cards(card_id, card_id or DEFAULT_CARD, directory)[0]


def volume_card() -> str:
    """amixer's -c argument for the voice's speaker."""
    return _speaker_card


def music_card() -> str:
    """The card the music plays on (its "Sentinel Music" volume lives there)."""
    return _music_card
