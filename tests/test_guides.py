"""Local PDF guides (sentinel/engine/guides.py): reading them into the index, searching, the offline helper reading
them first for wildlife questions, and the /guides pages."""

import _test_env  # noqa: F401  (isolates the event log)

import os
import shutil
import tempfile
import types
import unittest

from sentinel.engine import guides as G
from sentinel.engine.offline import OfflineHelper


def make_pdf(pages):
    """A tiny real PDF, one line of text per page."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {len(objs)} 0 R"
                    " /Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = "%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode("latin-1")


PAGES = {
    "Wildlife ID/Reptiles of Ohio (ODNR).pdf": ["Reptiles of the state", "The Northern Copperhead is venomous.\n"
                                                "Its bite is rarely fatal but needs a\ndoctor at once.\n\nIt lives "
                                                "on rocky wooded hillsides."],
    "Food and Homestead/Canning/Guide 1.pdf": ["Boil jars for ten minutes before filling them with tomatoes."],
}


class TestGuides(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.folder = os.path.join(self.tmp, "Local guides")
        for rel in PAGES:
            os.makedirs(os.path.dirname(os.path.join(self.folder, rel)), exist_ok=True)
            with open(os.path.join(self.folder, rel), "wb") as f:
                f.write(b"%PDF-1.4 fake")
        self.read = []

        def extract(path):
            rel = os.path.relpath(path, self.folder)
            self.read.append(rel)
            return PAGES[rel]

        self.settings = {"offline_guides_folder": self.folder}
        self.guides = G.Guides(self.settings, self.tmp, extract=extract)

    def test_pages_are_indexed_once_and_found(self):
        self.assertEqual(self.guides.refresh(), 3)
        self.assertEqual(self.guides.refresh(), 0)  # nothing changed: nothing read again
        hit, = self.guides.search("Is a copperhead bite dangerous?")
        self.assertEqual(hit["title"], "Reptiles of Ohio (ODNR), page 2")
        self.assertEqual(hit["book"], "Wildlife ID")
        self.assertRegex(hit["link"], r"^/guides/page/\d+$")
        self.assertIn("The Northern Copperhead is venomous. Its bite is rarely fatal but needs a doctor at once.",
                      hit["paragraphs"])
        self.assertIn("rocky wooded hillsides", hit["paragraphs"][1])

    def test_a_split_book_keeps_its_folders_name(self):
        self.guides.refresh()
        hit, = self.guides.search("canning jars")
        self.assertEqual(hit["title"], "Canning (Guide 1), page 1")
        self.assertEqual(hit["book"], "Food and Homestead")

    def test_changed_and_removed_files(self):
        self.guides.refresh()
        rel = "Food and Homestead/Canning/Guide 1.pdf"
        PAGES_BEFORE = PAGES[rel]
        self.addCleanup(PAGES.__setitem__, rel, PAGES_BEFORE)
        PAGES[rel] = ["Pickles need vinegar."]
        with open(os.path.join(self.folder, rel), "ab") as f:
            f.write(b" changed")
        self.guides.refresh()
        self.assertEqual(self.guides.search("tomatoes"), [])
        self.assertEqual(len(self.guides.search("pickles vinegar")), 1)
        os.remove(os.path.join(self.folder, "Wildlife ID/Reptiles of Ohio (ODNR).pdf"))
        self.guides.refresh()
        self.assertEqual(self.guides.search("copperhead"), [])
        self.assertEqual([b["book"] for b in self.guides.listing()], ["Food and Homestead"])

    def test_half_downloaded_files_wait_for_their_ok_marker(self):
        open(os.path.join(self.folder, "Wildlife ID", "Reptiles of Ohio (ODNR).pdf.ok"), "w").close()
        with open(os.path.join(self.folder, "Wildlife ID", "Spiders.pdf"), "wb") as f:
            f.write(b"%PDF half")  # (no .ok yet, while its neighbours have one: still downloading)
        self.guides.refresh()
        self.assertNotIn("Wildlife ID/Spiders.pdf", self.read)

    def test_pages_and_next_previous(self):
        self.guides.refresh()
        first = self.guides.listing()[1]["files"][0]
        page = self.guides.page(first["first"])
        self.assertEqual((page["page"], page["pages"], page["prev"]), (1, 2, None))
        nxt = self.guides.page(page["next"])
        self.assertEqual((nxt["page"], nxt["prev"]), (2, first["first"]))
        self.assertIsNone(self.guides.page(999))

    def test_only_pdfs_inside_the_folder(self):
        self.assertTrue(self.guides.pdf_path("Wildlife ID/Reptiles of Ohio (ODNR).pdf"))
        for bad in ("../x.pdf", "/etc/passwd", "Wildlife ID/../../x.pdf", "Wildlife ID", "", "a.txt"):
            self.assertIsNone(self.guides.pdf_path(bad), bad)

    def test_no_folder_no_guides(self):
        self.settings["offline_guides_folder"] = os.path.join(self.tmp, "nope")
        self.assertEqual(self.guides.refresh(), 0)
        self.assertEqual(self.guides.search("copperhead"), [])

    @unittest.skipUnless(shutil.which("pdftotext"), "needs poppler's pdftotext")
    def test_a_real_pdf(self):
        path = os.path.join(self.tmp, "real.pdf")
        with open(path, "wb") as f:
            f.write(make_pdf(["Morels grow under dead elms in spring.", "False morels are poisonous."]))
        pages = G.pdf_pages(path)
        self.assertEqual(len(pages), 2)
        self.assertIn("False morels are poisonous.", pages[1])


class TestHelperReadsGuides(unittest.TestCase):
    """The offline helper reads the local guides first for their kind of question."""

    def setUp(self):
        self.guides = types.SimpleNamespace(search=lambda q, n: [
            {"title": "Reptiles of Ohio (ODNR), page 2", "book": "Wildlife ID", "link": "/guides/page/2",
             "snippet": "copperhead", "paragraphs": ["The Northern Copperhead is venomous and lives on rocky wooded hillsides."]}][:n])
        self.helper = OfflineHelper({}, fetch=lambda *a, **k: (_ for _ in ()).throw(OSError("no library")),
                                    guides=self.guides)

    def test_wildlife_question_reads_the_guide(self):
        text, sources = self.helper.excerpts("Are there venomous snakes near here?", 2000)
        self.assertIn("[Wildlife ID] Reptiles of Ohio (ODNR), page 2\nThe Northern Copperhead is venomous", text)
        self.assertEqual(sources, [{"title": "Reptiles of Ohio (ODNR), page 2", "book": "Wildlife ID",
                                    "link": "/guides/page/2", "snippet": "copperhead"}])  # (no page text sent on)


class TestGuidePages(unittest.TestCase):
    def setUp(self):
        from flask import Flask

        from sentinel.web.routes import offline as routes

        self.t = TestGuides("test_pages_are_indexed_once_and_found")
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)
        self.t.guides.refresh()
        self.signed_in = True
        engine = types.SimpleNamespace(offline=types.SimpleNamespace(guides=self.t.guides))
        ctx = types.SimpleNamespace(engine=engine,
                                    require_session=lambda: None if self.signed_in else ("login", 302))
        app = Flask(__name__, template_folder=os.path.join(os.path.dirname(routes.__file__), "..", "templates"))
        routes.register(app, ctx)
        self.client = app.test_client()

    def test_list_search_page_and_pdf(self):
        r = self.client.get("/guides")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Reptiles of Ohio (ODNR)", r.data)
        r = self.client.get("/guides?q=copperhead")
        self.assertIn(b"Reptiles of Ohio (ODNR), page 2", r.data)
        link = self.t.guides.search("copperhead")[0]["link"]
        r = self.client.get(link)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Original page with pictures", r.data)
        self.assertIn(b"#page=2", r.data)
        r = self.client.get("/guides/file/Wildlife%20ID/Reptiles%20of%20Ohio%20(ODNR).pdf")
        self.assertEqual((r.status_code, r.mimetype), (200, "application/pdf"))
        r.close()
        self.assertEqual(self.client.get("/guides/file/../secret.pdf").status_code, 404)
        self.assertEqual(self.client.get("/guides/page/999").status_code, 404)

    def test_not_signed_in(self):
        self.signed_in = False
        for url in ("/guides", "/guides/page/1", "/guides/file/Wildlife%20ID/Reptiles%20of%20Ohio%20(ODNR).pdf"):
            self.assertEqual(self.client.get(url).status_code, 302, url)


if __name__ == "__main__":
    unittest.main()
