"""
Printing: the household printer through this computer's own printing system (CUPS), for the Print page.

A PDF or photo uploaded on the page goes to CUPS with ``lp``; CUPS turns it into what the printer takes
(driverless IPP Everywhere, so no printer drivers to install or keep up to date). The printer used is this
computer's default one (``lpstat -d``) unless settings name another (``printing.queue``).

The page also says whether the printer can print right now: it asks the printer itself (IPP, with ``ipptool``)
for paper, ink and its state, because a printer that's switched off or out of paper was what made it look
broken (2026-11-17: "I was getting ready to throw it away" - it had no paper and an alignment page waiting).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from sentinel.logging_utils import app_logger

MAX_BYTES = 30 * 1024 * 1024  # a long PDF or a phone photo at full size
MAX_COPIES = 20
RECENT = 8  # jobs listed on the page
ASK_SECONDS = 5  # the printer itself; it answers in well under a second when it's on

# What can be sent, by the file's first bytes (not its name): CUPS prints these without anything extra
KINDS = (
    (b"%PDF-", "pdf", "PDF"),
    (b"\xff\xd8\xff", "jpg", "photo"),
    (b"\x89PNG\r\n\x1a\n", "png", "picture"),
)
# Black & white goes around CUPS: this computer's CUPS (cups-filters 2.0.0, its "universal" filter) printed in
# colour whatever it was told (print-color-mode=monochrome, ColorModel=Gray, cupsColorSpace=18: the raster it made
# was always sRGB; 2026-11-17, a draft "black and white" form came out in colour). So each page is made a grey
# JPEG here and sent to the printer itself (IPP Print-Job) with print-color-mode=monochrome: the printer also
# offers process-monochrome (grey mixed from the colour inks), so its "monochrome" is the black cartridge alone.
# The printer takes one document per job (multiple-document-jobs-supported = false): one job per page.
GRAY_PAGES = ["gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=jpeggray", "-r300", "-dJPEGQ=85"]
GRAY_PDF = ["gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite", "-sColorConversionStrategy=Gray",
            "-dProcessColorModel=/DeviceGray", "-dCompatibilityLevel=1.4"]  # (a printer CUPS reaches some other way)
MAX_PAGES = 60
_IPP_HEAD = """{
	OPERATION %s
	GROUP operation-attributes-tag
	ATTR charset attributes-charset utf-8
	ATTR naturalLanguage attributes-natural-language en
	ATTR uri printer-uri $uri
	ATTR name requesting-user-name sentinel
"""
IPP_PRINT = _IPP_HEAD % "Print-Job" + """	ATTR name job-name "$title"
	ATTR mimeMediaType document-format image/jpeg
	GROUP job-attributes-tag
	ATTR integer copies $copies
	ATTR keyword print-color-mode monochrome
	ATTR enum print-quality $quality
	ATTR keyword print-scaling fit
	FILE $filename
	STATUS successful-ok
	DISPLAY job-id
}
"""
IPP_STATE = _IPP_HEAD % "Get-Job-Attributes" + """	ATTR integer job-id $job
	ATTR keyword requested-attributes job-state
	STATUS successful-ok
	DISPLAY job-state
}
"""
IPP_CANCEL = _IPP_HEAD % "Cancel-Job" + """	ATTR integer job-id $job
	STATUS successful-ok
}
"""
FINISHED = {"completed", "canceled", "aborted"}

# IPP print-quality: draft prints at 300 dpi and lays down least ink (a form, a shopping list); best is for photos
QUALITY = {"draft": 3, "normal": 4, "best": 5}
PAGES = re.compile(r"^\d{1,4}(-\d{1,4})?(,\d{1,4}(-\d{1,4})?)*$")
JOB_ID = re.compile(r"^[A-Za-z0-9_.@-]{1,120}-\d{1,9}$")

# The printer's own words for what's wrong (IPP printer-state-reasons), in plain English; "-report" etc. are dropped
REASONS = {
    "media-empty": "It's out of paper: put some in the tray.",
    "media-needed": "It's out of paper: put some in the tray.",
    "media-jam": "Paper is jammed: open it up and gently pull the paper out.",
    "media-low": "Paper is running low.",
    "door-open": "A door or cover is open.",
    "cover-open": "A door or cover is open.",
    "marker-supply-empty": "An ink cartridge is empty.",
    "marker-supply-low": "Ink is running low.",
    "toner-empty": "It's out of ink.",
    "toner-low": "Ink is running low.",
    "marker-waste-full": "Its ink waste pad is full.",
    "input-tray-missing": "The paper tray is out.",
    "output-tray-missing": "The output tray is out.",
    "output-area-full": "The output tray is full: take the printed pages out.",
    "paused": "Printing is paused on this computer.",
    "offline": "The printer isn't answering. It may be switched off: press its power button.",
    "shutdown": "The printer is switching off.",
    "spool-area-full": "The printer is too busy for more right now.",
    "cartridge-missing": "An ink cartridge is missing.",
}
QUIET = {"none", "cups-waiting-for-job-completed", "other", "moving-to-paused"}

Runner = Callable[..., subprocess.CompletedProcess]


def file_kind(data: bytes) -> Optional[Tuple[str, str]]:
    """(file extension, what to call it) when Sentinel can print it, else None."""
    for magic, ext, said in KINDS:
        if data.startswith(magic):
            return ext, said
    return None


def clean_title(name: str) -> str:
    """The name shown in the printer's list of jobs (the uploaded file's name, without anything odd in it)."""
    name = os.path.basename(str(name or "").replace("\\", "/"))
    name = re.sub(r"[^\w .,()&+'-]", "", name).strip(" .")
    return name[:80] or "Sentinel print"


def _ipp_attributes(text: str) -> Dict[str, str]:
    """ipptool -tv output -> {attribute: value}."""
    out: Dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"\s+([\w-]+) \([^)]*\) = (.*)$", line)
        if m and m.group(1) not in out:
            out[m.group(1)] = m.group(2).strip()
    return out


class Printer:
    def __init__(self, queue: Optional[str] = None, run: Runner = subprocess.run, clock: Callable[[], float] = time.time,
                 spool_dir: Optional[str] = None) -> None:
        self._queue = (queue or "").strip() or None
        self._run = run
        self._clock = clock
        self._spool_dir = spool_dir
        self._lock = threading.Lock()
        self._sent: List[Dict[str, Any]] = []  # newest first: {id, title, who, at, copies}

    # ------------------------------------------------------------------ asking CUPS and the printer
    def _cmd(self, args: List[str], timeout: float = 15.0) -> Tuple[int, str]:
        try:
            done = self._run(args, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as e:
            return 1, str(e)
        return done.returncode, (done.stdout or "") + (done.stderr or "")

    def queue(self) -> Optional[str]:
        """Which printer: the one settings name, else this computer's default; None when there's none."""
        if self._queue:
            return self._queue
        code, text = self._cmd(["lpstat", "-d"], timeout=5)
        m = re.search(r"destination:\s*(\S+)", text) if code == 0 else None
        return m.group(1) if m else None

    def _device(self, queue: str) -> Optional[str]:
        code, text = self._cmd(["lpstat", "-v", queue], timeout=5)
        m = re.search(r":\s*((?:ipps?)://\S+)", text) if code == 0 else None
        return m.group(1) if m else None

    def _ask_printer(self, uri: str) -> Optional[Dict[str, str]]:
        code, text = self._cmd(["ipptool", "-T", str(ASK_SECONDS), "-tv", uri, "get-printer-attributes.test"],
                               timeout=ASK_SECONDS + 5)
        attrs = _ipp_attributes(text)
        return attrs if attrs.get("printer-state") else None

    def _waiting(self, queue: str) -> List[str]:
        """Job ids CUPS still has (waiting or printing)."""
        code, text = self._cmd(["lpstat", "-o", queue], timeout=5)
        return [line.split()[0] for line in text.splitlines() if code == 0 and line.strip()]

    def status(self) -> Dict[str, Any]:
        """For the page: which printer, can it print now (and if not, why, in plain words), ink, recent jobs."""
        queue = self.queue()
        if queue is None:
            return {"printer": None, "ready": False,
                    "problems": ["No printer is set up on the Sentinel computer yet."], "ink": [], "jobs": []}
        uri = self._device(queue)
        attrs = self._ask_printer(uri) if uri else None
        problems: List[str] = []
        ink: List[Dict[str, Any]] = []
        name = queue
        state = "unknown"
        if attrs is None:
            problems.append(REASONS["offline"] if uri else "")
        else:
            name = attrs.get("printer-make-and-model") or queue
            state = attrs.get("printer-state", "unknown")
            for reason in attrs.get("printer-state-reasons", "none").split(","):
                reason = reason.strip()
                base = re.sub(r"-(report|warning|error)$", "", reason)
                # "-report" is only for information, except an empty tray (this printer says "media-empty-report")
                if reason.endswith("-report") and base not in ("media-empty", "media-needed"):
                    continue
                said = REASONS.get(base)
                if said and said not in problems:
                    problems.append(said)
            names = [n.strip() for n in attrs.get("marker-names", "").split(",") if n.strip()]
            levels = [lv.strip() for lv in attrs.get("marker-levels", "").split(",")]
            for i, n in enumerate(names):
                try:
                    level = int(levels[i])
                except (IndexError, ValueError):
                    continue
                ink.append({"name": n.replace(" ink", "").strip().capitalize(), "level": level if level >= 0 else None})
        problems = [p for p in problems if p]
        waiting = set(self._waiting(queue))
        with self._lock:
            recent = [dict(j) for j in self._sent[:RECENT]]
        jobs = []
        for j in recent:
            if "ids" in j:  # black & white: sent to the printer itself, asked there
                j["done"] = j.get("finished") or self._direct_done(j)
            else:
                j["done"] = j["id"] not in waiting
            j.pop("ids", None), j.pop("uri", None), j.pop("finished", None)
            jobs.append(j)
        return {"printer": name, "queue": queue, "ready": attrs is not None and not problems and state != "stopped",
                "state": state, "problems": problems, "ink": ink, "jobs": jobs}

    # ------------------------------------------------------------------ black & white, to the printer itself
    def _ipp(self, uri: str, test: str, timeout: float = 60, **values: Any) -> Tuple[bool, str]:
        fd, path = tempfile.mkstemp(prefix="sentinel-ipp-", suffix=".test", dir=self._spool_dir)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(test)
            args = ["ipptool", "-tv", "-T", str(int(timeout))]
            for key, value in values.items():
                args += ["-d", f"{key}={value}"]
            code, text = self._cmd(args + [uri, path], timeout=timeout + 10)
        finally:
            os.unlink(path)
        return code == 0 and "[PASS]" in text, text

    def _gray_pages(self, path: str, ext: str, pages: str, folder: str) -> List[str]:
        """Each page (the chosen ones) as a grey JPEG in folder, in order; [] when they couldn't be made."""
        if ext == "pdf":
            args = GRAY_PAGES + ([f"-sPageList={pages}"] if pages else [])
            code, _ = self._cmd(args + ["-sOutputFile=" + os.path.join(folder, "page-%03d.jpg"), path], timeout=180)
            made = sorted(f for f in os.listdir(folder) if f.startswith("page-"))
            return [os.path.join(folder, f) for f in made][:MAX_PAGES] if code == 0 else []
        try:
            from PIL import Image, ImageOps

            out = os.path.join(folder, "page-001.jpg")
            with Image.open(path) as im:
                ImageOps.exif_transpose(im).convert("L").save(out, quality=90)  # (a phone photo the right way up)
            return [out]
        except Exception:
            return []

    def _send_gray(self, uri: str, path: str, ext: str, folder: str, title: str, copies: int, quality: str,
                   pages: str) -> Tuple[List[int], str]:
        """(the printer's job ids, what went wrong if any)."""
        files = self._gray_pages(path, ext, pages, folder)
        if not files:
            return [], "It couldn't be made black and white, so it wasn't printed (to save the colour ink)."
        ids: List[int] = []
        for page in files:
            ok, text = self._ipp(uri, IPP_PRINT, filename=page, title=title.replace('"', "'"), copies=copies,
                                 quality=QUALITY.get(quality, QUALITY["normal"]))
            m = re.search(r"job-id \(integer\) = (\d+)", text)
            if not ok or not m:
                app_logger.log("warning", "print_failed", error_message=text.strip()[-200:], component="printing",
                               action="send")
                return ids, "The printer didn't take it" + (f" (after {len(ids)} pages)" if ids else "") + "."
            ids.append(int(m.group(1)))
        return ids, ""

    def _direct_done(self, job: Dict[str, Any]) -> bool:
        """Every page's job finished at the printer (or the printer no longer knows it)."""
        for job_id in job["ids"]:
            ok, text = self._ipp(job["uri"], IPP_STATE, timeout=8, job=job_id)
            m = re.search(r"job-state \(enum\) = ([\w-]+)", text)
            if ok and m and m.group(1) not in FINISHED:
                return False
        with self._lock:
            for j in self._sent:
                if j["id"] == job["id"]:
                    j["finished"] = True  # (not asked again)
        return True

    # ------------------------------------------------------------------ printing
    def _gray(self, path: str, ext: str) -> Optional[str]:
        """A grey copy of the file beside it (removed with it), or None when it couldn't be made."""
        out = path + ".gray." + ("pdf" if ext == "pdf" else "png")
        if ext == "pdf":
            code, text = self._cmd(GRAY_PDF + ["-sOutputFile=" + out, path], timeout=120)
            return out if code == 0 and os.path.exists(out) and os.path.getsize(out) > 0 else None
        try:
            from PIL import Image, ImageOps

            with Image.open(path) as im:
                ImageOps.exif_transpose(im).convert("L").save(out)  # (a phone photo the right way up, too)
            return out
        except Exception:
            return None

    def send(self, data: bytes, filename: str, copies: Any = 1, color: bool = True, pages: str = "",
             who: str = "", quality: str = "normal") -> Tuple[bool, str]:
        """Print an uploaded PDF or photo. (ok, what to tell the person)."""
        if not data:
            return False, "That file is empty."
        if len(data) > MAX_BYTES:
            return False, f"That file is too big to print from here (over {MAX_BYTES // (1024 * 1024)} MB)."
        kind = file_kind(data)
        if kind is None:
            return False, "Sentinel can print PDFs and photos (JPG or PNG). Save it as one of those and try again."
        try:
            copies = max(1, min(MAX_COPIES, int(copies)))
        except (TypeError, ValueError):
            copies = 1
        pages = re.sub(r"\s+", "", str(pages or ""))
        if pages and (kind[0] != "pdf" or not PAGES.match(pages)):
            return False, "Pages should look like 1-3 or 2,5 (and only for PDFs)."
        queue = self.queue()
        if queue is None:
            return False, "No printer is set up on the Sentinel computer yet."
        title = clean_title(filename)
        args = ["lp", "-d", queue, "-n", str(copies), "-t", title,
                "-o", "print-color-mode=" + ("color" if color else "monochrome"),  # (black cartridge only)
                "-o", f"print-quality={QUALITY.get(quality, QUALITY['normal'])}"]
        if pages:
            args += ["-o", "page-ranges=" + pages]
        if kind[0] != "pdf":
            args += ["-o", "print-scaling=fit"]  # a photo fills the page, whole (not cut off, not tiny)
        uri = None if color else self._device(queue)
        if uri:  # black & white: the printer itself, black ink only
            folder = tempfile.mkdtemp(prefix="sentinel-print-", dir=self._spool_dir)
            try:
                path = os.path.join(folder, "upload." + kind[0])
                with open(path, "wb") as f:
                    f.write(data)
                ids, problem = self._send_gray(uri, path, kind[0], folder, title, copies, quality, pages)
            finally:
                shutil.rmtree(folder, ignore_errors=True)
            if not ids:
                return False, problem
            job = f"printer-{ids[0]}"
            with self._lock:
                self._sent.insert(0, {"id": job, "title": title, "who": who, "at": self._clock(), "copies": copies,
                                      "ids": ids, "uri": uri})
                del self._sent[RECENT * 2:]
            app_logger.log("info", "print_sent", job=job, kind=kind[0], copies=copies, color=False, quality=quality,
                           pages=pages or "all", sheets=len(ids), who=who, direct=True, component="printing",
                           action="send")
            n = len(ids)
            return (problem == "", ("Sent to the printer in black and white: your " + kind[1] +
                                    (f" ({n} pages)" if n > 1 else "") + (f", {copies} copies" if copies > 1 else "") +
                                    "." + (" " + problem if problem else "")))
        fd, path = tempfile.mkstemp(prefix="sentinel-print-", suffix="." + kind[0], dir=self._spool_dir)
        gray = None
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            if not color:
                gray = self._gray(path, kind[0])
                if gray is None:
                    return False, "It couldn't be made black and white, so it wasn't printed (to save the colour ink)."
            code, text = self._cmd(args + ["--", gray or path], timeout=60)
        finally:
            for leftover in (path, gray):
                try:
                    if leftover:
                        os.unlink(leftover)  # (lp has handed CUPS its own copy by the time it returns)
                except OSError:
                    pass
        m = re.search(r"request id is (\S+)", text)
        if code != 0 or not m:
            app_logger.log("warning", "print_failed", error_message=text.strip()[:200], component="printing",
                           action="send")
            return False, "The printer didn't take it: " + (text.strip().splitlines() or ["no reason given"])[-1][:160]
        job = m.group(1)
        with self._lock:
            self._sent.insert(0, {"id": job, "title": title, "who": who, "at": self._clock(), "copies": copies})
            del self._sent[RECENT * 2:]
        app_logger.log("info", "print_sent", job=job, kind=kind[0], copies=copies, color=bool(color), quality=quality,
                       pages=pages or "all", who=who, component="printing", action="send")
        return True, f"Sent to the printer: your {kind[1]}" + (f", {copies} copies" if copies > 1 else "") + "."

    def cancel(self, job: str) -> Tuple[bool, str]:
        """Stop a job that was sent from here and hasn't finished."""
        job = str(job or "")
        with self._lock:
            ours = any(j["id"] == job for j in self._sent)
        if not JOB_ID.match(job) or not ours:
            return False, "I don't know that print job."
        with self._lock:
            direct = next((dict(j) for j in self._sent if j["id"] == job and "ids" in j), None)
        if direct is not None:  # black & white: each page's job at the printer
            stopped = [self._ipp(direct["uri"], IPP_CANCEL, timeout=8, job=i)[0] for i in direct["ids"]]
            if not any(stopped):
                return False, "It couldn't be stopped (it may have already printed)."
        else:
            code, text = self._cmd(["cancel", job], timeout=10)
            if code != 0:
                return False, "It couldn't be stopped (it may have already printed)."
        with self._lock:
            for j in self._sent:
                if j["id"] == job:
                    j["stopped"] = True
        app_logger.log("info", "print_cancelled", job=job, component="printing", action="cancel")
        return True, "Stopped."
