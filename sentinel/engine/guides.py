"""
Local guides for the offline helper: free PDF field guides, regulations and manuals kept on the backup drive
(e.g. the state's wildlife field guides, hunting and fishing rules, extension fact sheets, Army survival manuals),
in folders such as "Wildlife ID" (tools/offline/get-guides.sh fetches a list of them). The offline library
(Kiwix) only reads its own .zim books, so Sentinel indexes these itself: each PDF page's text (pdftotext) goes
into an SQLite full-text index, so the offline AI can read the pages that match a question, and the family can
search and read them on /guides (with a link to the original PDF, pictures and all). The index is rebuilt for a
PDF only when it is added or changed, at low priority, and is a cache: deleting it just means it's built again.
"""

from __future__ import annotations

import getpass
import glob
import os
import re
import sqlite3
import subprocess
import threading
import time
from typing import Callable, Dict, List, Optional

from sentinel.logging_utils import app_logger

DB_FILE = "offline_guides.db"  # in the data folder; a cache, so the nightly backup leaves it out
AUTO_FOLDER = "/media/{user}/*/Offline Library/Local guides"
REFRESH_S = 30 * 60
EXTRACT_TIMEOUT_S = 600
STOP = set("""a an and are as at be but by can could do does did for from had has have how i if in into is it its
me my no not of on or our should so than that the their them then there these they this to up us was we what when
where which who why will with would you your about any some get go know tell""".split())


def words(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if w not in STOP and len(w) > 1]


def pdf_pages(path: str) -> List[str]:
    """Each page's text, in reading order (poppler's pdftotext, at low priority so the cameras come first)."""
    out = subprocess.run(["nice", "-n", "15", "pdftotext", "-enc", "UTF-8", "-q", path, "-"],
                         capture_output=True, timeout=EXTRACT_TIMEOUT_S)
    if out.returncode:
        raise OSError(f"pdftotext exit {out.returncode}")
    pages = out.stdout.decode("utf-8", "replace").split("\f")
    return pages[:-1] if pages and not pages[-1].strip() else pages


def paragraphs(page: str) -> List[str]:
    """A page's text as paragraphs: wrapped lines joined, words split by a hyphen at a line's end mended."""
    out = []
    for block in re.split(r"\n\s*\n", page or ""):
        text = re.sub(r"-\n(?=[a-z])", "", block.strip())
        text = re.sub(r"\s*\n\s*", " ", text)
        text = re.sub(r"[ \t]{2,}", " ", text).strip()
        if len(text) > 2:
            out.append(text)
    return out


def title_of(rel: str) -> str:
    """"Wildlife ID/Reptiles of Ohio (ODNR).pdf" -> "Reptiles of Ohio (ODNR)"; a book split into parts keeps its
    folder's name: "…/Where There Is No Doctor/03.pdf" -> "Where There Is No Doctor (03)"."""
    parts = rel.split("/")
    stem = re.sub(r"\.pdf$", "", parts[-1], flags=re.I)
    return f"{parts[-2]} ({stem})" if len(parts) > 2 else stem


class Guides:
    def __init__(self, settings: Dict, data_dir: str, extract: Callable[[str], List[str]] = pdf_pages) -> None:
        self.settings = settings
        self.db_path = os.path.join(data_dir, DB_FILE)
        self.extract = extract
        self._lock = threading.Lock()
        self.indexing = ""  # the PDF being read now ("" when idle)
        self._thread: Optional[threading.Thread] = None

    # ---- where they are ----
    def folder(self) -> str:
        """The guides' folder: offline_guides_folder in settings, else "Offline Library/Local guides" on a drive."""
        set_to = str(self.settings.get("offline_guides_folder") or "").strip()
        if set_to:
            return set_to if os.path.isdir(set_to) else ""
        try:
            user = getpass.getuser()
        except Exception:
            user = "*"
        found = sorted(glob.glob(AUTO_FOLDER.format(user=glob.escape(user))))
        return found[0] if found else ""

    def pdf_path(self, rel: str) -> Optional[str]:
        """The full path of one guide's PDF, only if it really is a PDF inside the guides' folder."""
        root = self.folder()
        if not root or not rel or not rel.lower().endswith(".pdf"):
            return None
        root = os.path.realpath(root)
        path = os.path.realpath(os.path.join(root, rel))
        return path if path.startswith(root + os.sep) and os.path.isfile(path) else None

    # ---- the index ----
    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.execute("CREATE TABLE IF NOT EXISTS files (rel TEXT PRIMARY KEY, size INTEGER, mtime REAL, pages INTEGER,"
                   " title TEXT, book TEXT)")
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(title, book, body, rel UNINDEXED,"
                   " page UNINDEXED, tokenize='porter unicode61')")
        return db

    def refresh(self) -> int:
        """Index the PDFs that are new or changed and forget the ones that are gone; the number of pages read."""
        root = self.folder()
        if not root:
            return 0
        with self._lock:
            found = {}
            for top, dirs, files in os.walk(root):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for name in files:
                    if name.lower().endswith(".pdf") and not name.startswith("."):
                        path = os.path.join(top, name)
                        try:
                            st = os.stat(path)
                        except OSError:
                            continue
                        if os.path.exists(path + ".ok") or not any(n.endswith(".ok") for n in files):
                            found[os.path.relpath(path, root).replace(os.sep, "/")] = (st.st_size, st.st_mtime)
            read = 0
            with self._db() as db:
                known = {r[0]: (r[1], r[2]) for r in db.execute("SELECT rel, size, mtime FROM files")}
                for rel in known.keys() - found.keys():
                    db.execute("DELETE FROM pages WHERE rel = ?", (rel,))
                    db.execute("DELETE FROM files WHERE rel = ?", (rel,))
                for rel, (size, mtime) in sorted(found.items()):
                    if known.get(rel) == (size, mtime):
                        continue
                    self.indexing = rel
                    try:
                        texts = self.extract(os.path.join(root, rel))
                    except (OSError, subprocess.SubprocessError) as e:
                        app_logger.log("warning", "guide_unreadable", file=rel, error_message=str(e)[:200],
                                       component="offline", action="guides")
                        texts = []
                    title, book = title_of(rel), rel.split("/")[0] if "/" in rel else "Local guides"
                    db.execute("DELETE FROM pages WHERE rel = ?", (rel,))
                    db.executemany("INSERT INTO pages (title, book, body, rel, page) VALUES (?, ?, ?, ?, ?)",
                                   [(title, book, "\n\n".join(paragraphs(t)), rel, i + 1)
                                    for i, t in enumerate(texts) if t.strip()])
                    db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?, ?)",
                               (rel, size, mtime, len(texts), title, book))
                    db.commit()
                    read += len(texts)
            self.indexing = ""
        if read:
            app_logger.log("info", "guides_indexed", pages=read, files=len(found), component="offline",
                           action="guides")
        return read

    def start(self) -> None:
        """Keep the index up to date in the background (now, then every REFRESH_S)."""
        if self._thread is not None:
            return

        def loop():
            while True:
                try:
                    self.refresh()
                except Exception as e:  # (a bad PDF or a drive pulled out mid-read: try again later)
                    self.indexing = ""
                    app_logger.log("warning", "guides_refresh_failed", error_message=str(e)[:200],
                                   component="offline", action="guides")
                time.sleep(REFRESH_S)

        self._thread = threading.Thread(target=loop, name="guides-index", daemon=True)
        self._thread.start()

    # ---- reading ----
    def search(self, question: str, n: int = 2) -> List[Dict]:
        """The best-matching pages, at most one per folder (two first-aid pages would crowd out the region's own
        field guide on "are there venomous snakes here, and what if one bites?"): title, book, link, snippet, and
        the page's paragraphs."""
        terms = words(question)
        if not terms or not os.path.exists(self.db_path):
            return []
        match = " OR ".join('"' + t.replace('"', "") + '"' for t in dict.fromkeys(terms))
        try:
            with self._db() as db:
                rows = db.execute("SELECT rowid, title, book, page, snippet(pages, 2, '', '', '…', 24), body, book"
                                  " FROM pages WHERE pages MATCH ? ORDER BY bm25(pages, 4.0, 1.0, 1.0) LIMIT ?",
                                  (match, n * 30)).fetchall()
        except sqlite3.Error:
            return []
        seen, picked = set(), []
        for row in rows:
            if row[6] not in seen and len(picked) < n:
                seen.add(row[6])
                picked.append(row[:6])
        rows = picked
        return [{"title": f"{title}, page {page}", "book": book, "link": f"/guides/page/{rowid}",
                 "snippet": re.sub(r"\s+", " ", snip)[:400], "paragraphs": body.split("\n\n")}
                for rowid, title, book, page, snip, body in rows]

    def page(self, rowid: int) -> Optional[Dict]:
        if not os.path.exists(self.db_path):
            return None
        with self._db() as db:
            row = db.execute("SELECT p.title, p.book, p.body, p.rel, p.page, f.pages FROM pages p"
                             " LEFT JOIN files f ON f.rel = p.rel WHERE p.rowid = ?", (rowid,)).fetchone()
            if row is None:
                return None
            near = dict(db.execute("SELECT page, rowid FROM pages WHERE rel = ? AND page IN (?, ?)",
                                   (row[3], row[4] - 1, row[4] + 1)).fetchall())
        return {"title": row[0], "book": row[1], "paragraphs": row[2].split("\n\n"), "rel": row[3], "page": row[4],
                "pages": row[5] or row[4], "prev": near.get(row[4] - 1), "next": near.get(row[4] + 1)}

    def first_pages(self) -> Dict[str, int]:
        """Each PDF's first indexed page (its link on the /guides list)."""
        if not os.path.exists(self.db_path):
            return {}
        with self._db() as db:
            return dict(db.execute("SELECT rel, MIN(rowid) FROM pages GROUP BY rel").fetchall())

    def listing(self) -> List[Dict]:
        """The guides by folder, for the /guides page: [{"book", "files": [{"rel", "title", "pages", "first"}]}]."""
        if not os.path.exists(self.db_path):
            return []
        first = self.first_pages()
        with self._db() as db:
            rows = db.execute("SELECT rel, title, book, pages FROM files ORDER BY book, title").fetchall()
        books: Dict[str, List[Dict]] = {}
        for rel, title, book, pages in rows:
            books.setdefault(book, []).append({"rel": rel, "title": title, "pages": pages, "first": first.get(rel)})
        return [{"book": b, "files": f} for b, f in books.items()]
