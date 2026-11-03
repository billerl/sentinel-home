"""Choosing the sound card: the PC's own jacks or a USB speakerphone (Settings > Audio)."""

import _test_env  # noqa: F401  (isolates the event log)

import os
import shutil
import tempfile
import unittest

from sentinel.audio import devices
from sentinel.engine import admin

CARDS = """ 0 [PCH            ]: HDA-Intel - HDA Intel PCH
                      HDA Intel PCH at 0xd1310000 irq 143
 1 [USB            ]: USB-Audio - Jabra SPEAK 410 USB
                      Jabra SPEAK 410 USB at usb-0000:00:14.0-1, full speed
 2 [HDMI           ]: HDA-Intel - HDA ATI HDMI
                      HDA ATI HDMI at 0xf7e60000 irq 34
"""


class TestDevices(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-asound-")
        with open(os.path.join(self.dir, "cards"), "w") as f:
            f.write(CARDS)
        for card, pcms in ((0, ["pcm0p", "pcm0c", "pcm3p"]), (1, ["pcm0p", "pcm0c"]), (2, ["pcm3p", "pcm7p"])):
            for p in pcms:
                os.makedirs(os.path.join(self.dir, f"card{card}", p))
        self.addCleanup(devices.use_speaker_card, None)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_cards(self):
        self.assertEqual(devices.sound_cards(self.dir), [
            {"id": "PCH", "name": "HDA Intel PCH", "number": 0, "mic": True, "speaker": True},
            {"id": "USB", "name": "Jabra SPEAK 410 USB", "number": 1, "mic": True, "speaker": True},
            {"id": "HDMI", "name": "HDA ATI HDMI", "number": 2, "mic": False, "speaker": False}])  # HDMI only: no speaker
        self.assertEqual(devices.sound_cards(os.path.join(self.dir, "nope")), [])

    def test_the_shared_speaker_moves_to_the_chosen_card(self):
        self.assertEqual(devices.use_speaker_card("USB", self.dir), "USB")
        with open(devices.alsa_conf_path()) as f:
            conf = f.read()
        self.assertIn('pcm "hw:USB,0"', conf)
        self.assertIn("card USB", conf)
        self.assertNotIn("ipc_key 51515", conf)  # not the PC card's mixer
        self.assertEqual(devices.use_speaker_card("", self.dir), "PCH")
        self.assertEqual(devices.alsa_conf_path(), devices.BUNDLED_CONF)
        self.assertEqual(devices.use_speaker_card("../../etc", self.dir), "PCH")  # not a card id

    def test_music_on_the_pc_speakers_when_plugged_in(self):
        speaker, music = devices.use_cards("USB", "auto", self.dir, plugged=lambda card: True)
        self.assertEqual((speaker, music), ("USB", "PCH"))
        with open(devices.alsa_conf_path()) as f:
            conf = f.read()
        self.assertIn('pcm "hw:USB,0"', conf)  # voice and chimes: the Jabra
        self.assertIn('pcm.sentinel_music_mix', conf)
        self.assertIn('pcm "hw:PCH,0"', conf)  # music: the speakers in the PC's jack
        self.assertIn("card PCH", conf)  # its volume (turned down while the assistant talks) is on that card
        self.assertEqual(devices.music_card(), "PCH")
        self.assertEqual(devices.use_cards("USB", "auto", self.dir, plugged=lambda card: False), ("USB", "USB"))
        self.assertEqual(devices.use_cards("USB", "USB", self.dir, plugged=lambda card: True), ("USB", "USB"))  # chosen
        self.assertEqual(devices.use_cards("", "auto", self.dir, plugged=lambda card: False), ("PCH", "PCH"))
        self.assertEqual(devices.alsa_conf_path(), devices.BUNDLED_CONF)

    def test_jack_sense(self):
        answers = {"controls": "numid=26,iface=CARD,name='Front Line Out Jack'\nnumid=27,iface=CARD,name='Speaker Phantom Jack'\n",
                   "cget": "  ; type=BOOLEAN,access=r-------,values=1\n  : values={}\n"}
        for state, expected in (("on", True), ("off", False)):
            run = lambda cmd, **kw: type("R", (), {"stdout": answers[cmd[3]].format(state)})()
            self.assertIs(devices.line_out_plugged("PCH", run=run), expected)

    def test_settings(self):
        rules = admin.SETTINGS_SCHEMA
        self.assertEqual(rules["voice_device"]("plughw:CARD=USB,DEV=0"), "plughw:CARD=USB,DEV=0")
        self.assertEqual(rules["voice_device"]("hw:0; rm -rf /"), "")
        self.assertEqual(rules["voice_device"](None), "")
        self.assertEqual(rules["audio_device"]("USB"), "USB")
        self.assertEqual(rules["audio_device"]("USB,0 x"), "")
        self.assertEqual(rules["music_device"](None), "auto")
        self.assertEqual(rules["music_device"]("PCH"), "PCH")


if __name__ == "__main__":
    unittest.main()
