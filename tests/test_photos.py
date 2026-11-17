import _test_env  # noqa: F401  (isolates the event log)

import io
import os
import shutil
import tempfile
import unittest

from PIL import Image

from sentinel.engine.photos import ALBUMS_DIR, Photos, clean_rel, upload_kind

BEN, LIZ = 2, 3  # people's ids; None = the household login


def jpeg(w=800, h=600, colour=(200, 40, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(buf, "JPEG", quality=95)
    return buf.getvalue() + b"\x00" * 40000  # (past MIN_PHOTO_BYTES: a real photo, not an icon)


def put(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


class TestPhotos(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sentinel-photos-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.drive = os.path.join(self.tmp, "drive")
        data = os.path.join(self.tmp, "data")
        os.makedirs(data)
        put(os.path.join(self.drive, "Backup of Photos", "2015", "beach.jpg"), jpeg())
        put(os.path.join(self.drive, "Backup of Photos", "2015", "notes.txt"), b"not a photo")
        put(os.path.join(self.drive, "Backup of Photos", "clip.mp4"), b"\x00\x00\x00\x18ftypmp42" + b"0" * 100)
        put(os.path.join(self.drive, "Lenovo_Laptop", "Users", "Ben", "Pictures", "dog.jpg"), jpeg())
        put(os.path.join(self.drive, "Lenovo_Laptop", "Users", "Ben", "AppData", "logo.jpg"), jpeg())  # program stuff
        put(os.path.join(self.drive, "Lenovo_Laptop", "Users", "Ben", "Pictures", "icon.png"), b"\x89PNG\r\n\x1a\n" + b"0" * 100)
        put(os.path.join(self.drive, "Sentinel Backup", "snapshots", "x.jpg"), jpeg())
        self.settings = {"photos_shared_folders": "Backup of Photos"}
        self.p = Photos(data, self.settings, drive=lambda: self.drive)
        self.p.rescan(wait=True)

    def paths(self, folder, person=BEN, household=False):
        out = self.p.listing(folder, person, household)
        return None if out is None else sorted(i["name"] for i in out["items"])

    def test_only_shared_folders_and_only_photos(self):
        home = self.p.overview(BEN, False)
        self.assertEqual([f["name"] for f in home["folders"]], ["Backup of Photos"])
        self.assertEqual(home["folders"][0]["count"], 2)
        self.assertEqual(self.paths("Backup of Photos/2015"), ["beach.jpg"])
        self.assertEqual(self.paths("Backup of Photos"), ["clip.mp4"])
        self.assertIsNone(self.p.listing("Lenovo_Laptop", BEN, False))  # not shared yet
        self.assertIsNone(self.p.listing("Sentinel Backup", BEN, False))
        self.assertNotIn("Sentinel Backup", self.p.folder_choices())
        # an admin shares the laptop: its pictures show, not its programs' or its icons
        self.settings["photos_shared_folders"] = "Backup of Photos,Lenovo_Laptop"
        self.p.rescan(wait=True)
        self.assertEqual(self.paths("Lenovo_Laptop/Users/Ben/Pictures"), ["dog.jpg"])
        self.assertEqual(self.paths("Lenovo_Laptop/Users/Ben/AppData"), [])  # (not looked through)
        ben = self.p.listing("Lenovo_Laptop/Users/Ben", BEN, False)
        self.assertEqual([f["name"] for f in ben["folders"]], ["Pictures"])  # (AppData isn't offered)

    def test_no_way_out_of_the_drive(self):
        self.assertIsNone(clean_rel("../etc/passwd"))
        self.assertIsNone(clean_rel("Backup of Photos/../../x"))
        self.assertIsNone(self.p.listing("../", BEN, False))
        self.assertIsNone(self.p.file_for("Backup of Photos/2015/notes.txt", BEN, False))  # (not a photo: unknown)
        self.assertIsNone(self.p.file_for("Sentinel Backup/snapshots/x.jpg", BEN, False))
        self.assertIsNotNone(self.p.file_for("Backup of Photos/2015/beach.jpg", BEN, False))

    def test_a_private_album_is_its_owners_alone(self):
        ok, aid = self.p.create_album("Ben's trip", False, BEN, "Ben")
        self.assertTrue(ok, aid)
        aid = int(aid)
        self.assertTrue(self.p.upload(aid, io.BytesIO(jpeg()), "IMG_1.jpg", BEN, False)[0])
        folder = f"{ALBUMS_DIR}/Ben/Ben's trip"
        self.assertEqual(self.paths(folder), ["IMG_1.jpg"])
        for person, household in ((LIZ, False), (None, True)):  # another person, and the household login
            self.assertEqual(self.p.overview(person, household)["albums"], [])
            self.assertIsNone(self.p.listing(folder, person, household))
            self.assertIsNone(self.p.file_for(folder + "/IMG_1.jpg", person, household))
        self.assertIsNone(self.p.listing(ALBUMS_DIR, BEN, False))  # (everyone's albums aren't one folder to browse)
        self.assertFalse(self.p.set_shared(aid, True, LIZ, False)[0])  # only its owner
        self.assertTrue(self.p.set_shared(aid, True, BEN, False)[0])
        self.assertEqual([a["name"] for a in self.p.overview(LIZ, False)["albums"]], ["Ben's trip"])
        self.assertFalse(self.p.overview(LIZ, False)["albums"][0]["mine"])
        self.assertEqual(self.paths(folder, LIZ), ["IMG_1.jpg"])
        # Liz can look, but not add or delete
        self.assertFalse(self.p.upload(aid, io.BytesIO(jpeg()), "x.jpg", LIZ, False)[0])
        self.assertFalse(self.p.delete_photo(folder + "/IMG_1.jpg", LIZ, False)[0])
        self.assertFalse(self.p.delete_album(aid, LIZ, False)[0])

    def test_uploads_never_overwrite_or_reuse_a_name(self):
        aid = int(self.p.create_album("Mine", True, None, "Household")[1])
        self.assertEqual(self.p.upload(aid, io.BytesIO(jpeg()), "IMG.jpg", None, True), (True, "IMG.jpg"))
        self.assertEqual(self.p.upload(aid, io.BytesIO(jpeg()), "IMG.jpg", None, True), (True, "IMG (2).jpg"))
        folder = f"{ALBUMS_DIR}/Household/Mine"
        self.assertTrue(self.p.delete_photo(folder + "/IMG.jpg", None, True)[0])
        # (the nightly copy never replaces a file on the old PC: a reused name would never reach it)
        self.assertEqual(self.p.upload(aid, io.BytesIO(jpeg()), "IMG.jpg", None, True), (True, "IMG (3).jpg"))
        self.assertEqual(self.paths(folder, None, True), ["IMG (2).jpg", "IMG (3).jpg"])
        # by its first bytes, not its name; small photos are kept in albums
        self.assertEqual(self.p.upload(aid, io.BytesIO(b"PK\x03\x04 a zip"), "x.jpg", None, True)[1],
                         "That isn't a photo or video.")
        small = io.BytesIO()
        Image.new("RGB", (20, 20)).save(small, "PNG")
        self.assertEqual(self.p.upload(aid, io.BytesIO(small.getvalue()), "tiny.jpg", None, True), (True, "tiny.png"))
        self.assertIn("tiny.png", self.paths(folder, None, True))
        self.assertFalse(self.p.create_album("mine", False, None, "Household")[0])  # (the drive ignores case)
        self.assertFalse(self.p.create_album("../x", False, None, "Household")[0])

    def test_the_shared_folders_are_never_changed(self):
        self.assertFalse(self.p.delete_photo("Backup of Photos/2015/beach.jpg", None, True)[0])
        self.assertTrue(os.path.exists(os.path.join(self.drive, "Backup of Photos", "2015", "beach.jpg")))

    def test_deleting_an_album(self):
        aid = int(self.p.create_album("Gone", False, BEN, "Ben")[1])
        self.p.upload(aid, io.BytesIO(jpeg()), "a.jpg", BEN, False)
        self.assertTrue(self.p.delete_album(aid, BEN, False)[0])
        self.assertFalse(os.path.exists(os.path.join(self.drive, ALBUMS_DIR, "Ben", "Gone")))
        self.assertEqual(self.p.overview(BEN, False)["albums"], [])

    def test_thumbnails_and_screen_copies_are_made_once(self):
        found = self.p.file_for("Backup of Photos/2015/beach.jpg", BEN, False)
        thumb = self.p.thumbnail(*found)
        with Image.open(thumb) as im:
            self.assertLessEqual(max(im.size), 320)
        self.assertEqual(self.p.thumbnail(*found), thumb)
        with Image.open(self.p.display(*found)) as im:
            self.assertEqual(im.size, (800, 600))  # (already smaller than a screen: not made bigger)
        self.assertTrue(thumb.startswith(self.p.cache))

    def test_a_relative_data_folder_still_gives_full_paths(self):
        # (the engine passes "recorded_videos"; Flask's send_file reads relative paths from its own package folder)
        here = os.getcwd()
        os.chdir(self.tmp)
        try:
            p = Photos("data", self.settings, drive=lambda: self.drive)
            p.rescan(wait=True)
            thumb = p.thumbnail(*p.file_for("Backup of Photos/2015/beach.jpg", BEN, False))
        finally:
            os.chdir(here)
        self.assertTrue(os.path.isabs(thumb) and os.path.exists(thumb), thumb)

    def test_no_drive(self):
        self.p._drive = lambda: None
        self.p._drive_at = (0.0, None)
        self.assertFalse(self.p.overview(BEN, False)["drive"])
        self.assertFalse(self.p.create_album("x", False, BEN, "Ben")[0])
        self.assertFalse(self.p.rescan())

    def test_upload_kinds(self):
        self.assertEqual(upload_kind(b"\xff\xd8\xff\xe0"), ".jpg")
        self.assertEqual(upload_kind(b"\x00\x00\x00\x18ftypheic"), ".heic")
        self.assertEqual(upload_kind(b"\x00\x00\x00\x14ftypqt  "), ".mov")
        self.assertEqual(upload_kind(b"RIFF\x00\x00\x00\x00AVI LIST"), ".avi")
        self.assertIsNone(upload_kind(b"%PDF-1.7"))


if __name__ == "__main__":
    unittest.main()
