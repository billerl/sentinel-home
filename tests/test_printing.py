import _test_env  # noqa: F401  (isolates the event log)

import os
import subprocess
import tempfile
import unittest

from sentinel.engine.printing import MAX_BYTES, Printer, clean_title, file_kind

PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n"
JPG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 50
GRAY_PDF_OUT = b"%PDF-1.4 grey"
IPP_OK = """
        printer-make-and-model (textWithoutLanguage) = HP DeskJet 2700 series
        printer-state (enum) = idle
        printer-state-reasons (keyword) = none
        marker-names (1setOf nameWithoutLanguage) = tri-color ink,black ink
        marker-levels (1setOf integer) = 100,8
"""


class FakeCups:
    """lpstat / lp / cancel / ipptool, as this computer's CUPS answers them."""

    def __init__(self):
        self.calls = []
        self.files = []  # what lp was given (read while it still exists)
        self.ipp = IPP_OK
        self.waiting = []
        self.next_job = 7
        self.printer_jobs = {}  # black & white jobs sent to the printer itself: id -> {state, values, page}
        self.pdf_pages = 2  # how many pages Ghostscript finds in a PDF

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        out, code = "", 0
        if args[:2] == ["lpstat", "-d"]:
            out = "system default destination: DeskJet\n"
        elif args[:2] == ["lpstat", "-v"]:
            out = "device for DeskJet: ipp://192.0.2.81/ipp/print\n"
        elif args[:2] == ["lpstat", "-o"]:
            out = "".join(f"{j} someone 1024 Tue 17 Nov 2026\n" for j in self.waiting)
        elif args[0] == "ipptool":
            test = open(args[-1]).read() if args[-1].endswith(".test") and os.path.exists(args[-1]) else ""
            values = dict(args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "-d")
            if self.ipp is None:
                code, out = 1, "ipptool: Unable to connect to 192.0.2.81: No route to host"
            elif "OPERATION Print-Job" in test:
                job = 100 + len(self.printer_jobs)
                with open(values["filename"], "rb") as f:
                    page = f.read()
                self.printer_jobs[job] = {"state": "processing", "values": values, "page": page, "test": test}
                out = f"    Sentinel print  [PASS]\n        job-id (integer) = {job}\n"
            elif "OPERATION Get-Job-Attributes" in test:
                out = f"  [PASS]\n        job-state (enum) = {self.printer_jobs[int(values['job'])]['state']}\n"
            elif "OPERATION Cancel-Job" in test:
                self.printer_jobs[int(values["job"])]["state"] = "canceled"
                out = "  [PASS]\n"
            else:
                out = self.ipp
        elif args[0] == "gs":
            target = next(a for a in args if a.startswith("-sOutputFile="))[len("-sOutputFile="):]
            if "%03d" in target:  # each page a grey JPEG
                for n in range(1, self.pdf_pages + 1):
                    with open(target % n, "wb") as f:
                        f.write(b"\xff\xd8\xff grey page %d" % n)
            else:  # a grey copy of the PDF
                with open(target, "wb") as f:
                    f.write(GRAY_PDF_OUT)
        elif args[0] == "lp":
            path = args[-1]
            with open(path, "rb") as f:
                self.files.append(f.read())
            job = f"DeskJet-{self.next_job}"
            self.next_job += 1
            self.waiting.append(job)
            out = f"request id is {job} (1 file(s))\n"
        elif args[0] == "cancel":
            self.waiting.remove(args[1])
        return subprocess.CompletedProcess(args, code, out, "")


class TestPrinting(unittest.TestCase):
    def setUp(self):
        self.cups = FakeCups()
        self.spool = tempfile.mkdtemp(prefix="sentinel-print-test-")
        self.p = Printer(run=self.cups, spool_dir=self.spool, clock=lambda: 1_800_000_000.0)

    def lp_args(self):
        return next(c for c in reversed(self.cups.calls) if c[0] == "lp")

    def test_what_it_takes(self):
        self.assertEqual(file_kind(PDF)[0], "pdf")
        self.assertEqual(file_kind(JPG)[0], "jpg")
        self.assertEqual(file_kind(b"\x89PNG\r\n\x1a\n...")[0], "png")
        self.assertIsNone(file_kind(b"PK\x03\x04 a Word file"))
        self.assertEqual(clean_title("../../etc/My Report (final).pdf"), "My Report (final).pdf")
        self.assertEqual(clean_title(""), "Sentinel print")

    def test_a_colour_pdf_goes_through_cups(self):
        ok, said = self.p.send(PDF, "Report.pdf", copies="2", pages="1-3, 5", who="Alex")
        self.assertTrue(ok, said)
        self.assertIn("2 copies", said)
        args = self.lp_args()
        self.assertEqual(args[:6], ["lp", "-d", "DeskJet", "-n", "2", "-t"])
        self.assertIn("print-color-mode=color", args)
        self.assertIn("page-ranges=1-3,5", args)
        self.assertIn("print-quality=4", args)  # (normal unless asked)
        self.assertNotIn("print-scaling=fit", args)
        self.assertEqual(self.cups.files, [PDF])
        self.assertEqual(os.listdir(self.spool), [])  # (CUPS has its own copy)

    def test_black_and_white_goes_to_the_printer_itself_black_ink_only(self):
        # (CUPS printed colour whatever it was told: each page goes to the printer as a grey JPEG, "monochrome")
        ok, said = self.p.send(PDF, "Form.pdf", copies="2", color=False, pages="1-2", quality="draft", who="Alex")
        self.assertTrue(ok, said)
        self.assertIn("black and white", said)
        self.assertIn("2 pages", said)
        self.assertFalse(any(c[0] == "lp" for c in self.cups.calls))
        gs = next(c for c in self.cups.calls if c[0] == "gs")
        self.assertIn("-sDEVICE=jpeggray", gs)
        self.assertIn("-sPageList=1-2", gs)
        jobs = self.cups.printer_jobs
        self.assertEqual(len(jobs), 2)  # (one job per page: it takes one document a job)
        for job in jobs.values():
            self.assertIn("print-color-mode monochrome", job["test"])
            self.assertEqual((job["values"]["copies"], job["values"]["quality"]), ("2", "3"))
            self.assertTrue(job["page"].startswith(b"\xff\xd8\xff"))
        ipp = next(c for c in self.cups.calls if c[0] == "ipptool" and "-d" in c)
        self.assertEqual(ipp[-2], "ipp://192.0.2.81/ipp/print")
        self.assertEqual(os.listdir(self.spool), [])  # (the pages and the .test files are gone)
        # shown until the printer has finished every page; Stop cancels them there
        job = self.p.status()["jobs"][0]
        self.assertEqual((job["id"], job["done"]), ("printer-100", False))
        self.assertNotIn("ids", job)
        self.assertTrue(self.p.cancel("printer-100")[0])
        self.assertEqual({j["state"] for j in jobs.values()}, {"canceled"})
        self.assertTrue(self.p.status()["jobs"][0]["done"])

    def test_a_photo_is_fitted_to_the_page(self):
        ok, said = self.p.send(JPG, "IMG_2041.jpg")
        self.assertTrue(ok, said)
        self.assertIn("photo", said)
        self.assertIn("print-scaling=fit", self.lp_args())
        self.assertIn("print-color-mode=color", self.lp_args())

    def test_draft_saves_ink(self):
        self.p.send(PDF, "form.pdf", quality="draft")
        self.assertIn("print-quality=3", self.lp_args())
        self.p.send(JPG, "photo.jpg", quality="best")
        self.assertIn("print-quality=5", self.lp_args())
        self.p.send(PDF, "x.pdf", quality="ultra; lp")
        self.assertIn("print-quality=4", self.lp_args())

    def test_a_black_and_white_photo_arrives_grey(self):
        import io

        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (40, 30), (220, 30, 30)).save(buf, "JPEG")
        ok, said = self.p.send(buf.getvalue(), "red.jpg", color=False)
        self.assertTrue(ok, said)
        page = list(self.cups.printer_jobs.values())[0]["page"]
        with Image.open(io.BytesIO(page)) as im:
            self.assertEqual((im.format, im.mode), ("JPEG", "L"))
        self.assertEqual(os.listdir(self.spool), [])

    def test_no_grey_copy_no_print(self):
        self.assertFalse(self.p.send(JPG, "broken.jpg", color=False)[0])  # (not a real picture: can't be greyed)
        self.assertFalse(any(c[0] == "lp" for c in self.cups.calls))
        self.assertEqual(self.cups.printer_jobs, {})

    def test_what_it_refuses(self):
        self.assertFalse(self.p.send(b"", "x.pdf")[0])
        self.assertFalse(self.p.send(b"PK\x03\x04", "letter.docx")[0])
        self.assertFalse(self.p.send(b"%PDF-" + b"0" * MAX_BYTES, "huge.pdf")[0])
        self.assertFalse(self.p.send(PDF, "x.pdf", pages="1-3; rm -rf")[0])
        self.assertFalse(self.p.send(JPG, "x.jpg", pages="2")[0])  # (pages are for PDFs)
        self.assertFalse(any(c[0] == "lp" for c in self.cups.calls))
        self.p.send(PDF, "x.pdf", copies="999")
        self.assertEqual(self.lp_args()[4], "20")

    def test_status_says_what_is_wrong_in_plain_words(self):
        st = self.p.status()
        self.assertTrue(st["ready"])
        self.assertEqual(st["printer"], "HP DeskJet 2700 series")
        self.assertEqual(st["ink"], [{"name": "Tri-color", "level": 100}, {"name": "Black", "level": 8}])
        # this printer says an empty tray as "media-empty-report"; other "-report"s are only for information
        self.cups.ipp = IPP_OK.replace("= none", "= media-empty-report,marker-supply-low-report")
        st = self.p.status()
        self.assertFalse(st["ready"])
        self.assertEqual(st["problems"], ["It's out of paper: put some in the tray."])
        self.cups.ipp = None
        st = self.p.status()
        self.assertFalse(st["ready"])
        self.assertIn("switched off", st["problems"][0])

    def test_jobs_show_until_done_and_can_be_stopped(self):
        self.p.send(PDF, "a.pdf", who="Alex")
        self.p.send(PDF, "b.pdf", who="Kate")
        jobs = self.p.status()["jobs"]
        self.assertEqual([(j["title"], j["done"]) for j in jobs], [("b.pdf", False), ("a.pdf", False)])
        self.cups.waiting.remove("DeskJet-7")  # a.pdf printed
        self.assertTrue(self.p.status()["jobs"][1]["done"])
        self.assertTrue(self.p.cancel("DeskJet-8")[0])
        b = self.p.status()["jobs"][0]
        self.assertTrue(b["done"] and b.get("stopped"))
        self.assertFalse(self.p.cancel("Other-99")[0])  # only jobs sent from here
        self.assertFalse(self.p.cancel("DeskJet-8; reboot")[0])

    def test_no_printer_set_up(self):
        def none(args, **kw):
            return subprocess.CompletedProcess(args, 1, "lpstat: No system default destination", "")
        p = Printer(run=none)
        self.assertIsNone(p.status()["printer"])
        self.assertFalse(p.send(PDF, "x.pdf")[0])


if __name__ == "__main__":
    unittest.main()
