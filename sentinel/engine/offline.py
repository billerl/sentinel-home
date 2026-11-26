"""
The offline helper: a small AI that runs on this PC (Ollama) reading from the offline library (Kiwix: Wikipedia,
a medical encyclopedia, survival, first-aid and repair guides, kept on the backup drive). Nothing leaves the house,
so it keeps answering with the internet down; Claude stays the first choice while the internet is up.

Both run as their own services, outside Sentinel (installation-guide.md, "Offline helper"): this module only talks
to them on 127.0.0.1, and Sentinel works the same without them. On this PC's processor the AI reads slowly (about
35 words a second, then writes about 8), so it is given the few library paragraphs that match the question, never
whole articles. While it thinks it is kept to a few threads at low priority: the cameras and the voice come first.
"""

from __future__ import annotations

import base64
import itertools
import json
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import Callable, Dict, List, Optional, Tuple

from sentinel.logging_utils import app_logger

AI_URL = "http://127.0.0.1:11434"
LIBRARY_URL = "http://127.0.0.1:8097/library"
MODEL = "gemma3:4b"  # (reads photos too: "what plant is this?")
THREADS = 3
KEEP_ALIVE = "5m"  # then the AI leaves memory until the next question
ANSWER_TIMEOUT_S = 240
READY_CACHE_S = 30
SOURCES = 4  # search results whose snippets it reads
ARTICLES = 2  # of those, the articles it opens for their best paragraphs
BUDGET_CHARS = {"page": 2000, "voice": 1100}  # library text per question (~4 characters a word-piece)
MAX_TOKENS = {"page": 420, "voice": 140}
JOB_KEEP_S = 30 * 60
FORAGING = re.compile(r"\b(eat|eats|eating|edible|poison\w*|toxic|forag\w*|berr(y|ies)|mushrooms?|fung(i|us)|"
                      r"wild (plant|green|onion|garlic|carrot|parsnip)s?|safe to (eat|chew|cook))\b", re.I)
FIELD_GUIDE = "Never eat a wild plant, berry or mushroom on this answer alone: check a field guide for your area or ask an expert first."
HEALTH = re.compile(r"\b(hurt\w*|pain\w*|ache|burn(s|ed|t)?|bleed\w*|blood|sting\w*|bit(e|ten)|fever|sick\w*|"
                    r"illness|wound\w*|injur\w*|poison\w*|rash|itch\w*|medicine|medication|dose|cough\w*|"
                    r"vomit\w*|diarrh\w*|infect\w*|swollen|swelling|sprain\w*|fracture\w*|faint\w*|chok\w*|"
                    r"allerg\w*|asthma|heart attack|stroke|breathing|flu|remed(y|ies)|first aid|symptom\w*|tick)\b", re.I)
MEDICAL_BOOKS = ("mdwiki_", "zimgit-medicine", "zimgit-post-disaster")  # read first for health questions
REFERENCE_BOOKS = ("wikipedia_", "mdwiki_", "zimgit-")  # and for everything else, before the Q&A sites
BOOKS_CACHE_S = 600
SKIP_TITLE = re.compile(r"^(Highest Voted|Newest|Active|Unanswered)\b", re.I)
STOP = set("""a an and are as at be but by can could do does did for from had has have how i if in into is it its
me my of on or our should so than that the their them then there these they this to us was we what when where which
who why will with would you your yours""".split())

SYSTEM = """You are the offline helper of a family's home system. The internet may be down, so you can't look
anything up: you have only the LIBRARY EXCERPTS below (from offline Wikipedia, a medical encyclopedia, and survival,
first-aid, cooking, gardening and repair guides) and what you already know.

Rules:
- Plain, practical, friendly language for someone who isn't an expert. Short: a few sentences or a short list.
- The excerpts are other people's articles and Q&A posts, not the family's own situation: don't say "you've already
  tried ..." because someone in a post did.
- Use the excerpts when they answer the question, and name the source in brackets, e.g. [The Great Outdoors Q&A].
  If they don't help, answer from what you know and say it's from memory, not the library.
- Never make up facts, numbers or doses. If you're not sure, say so.
- Health: for anything serious (trouble breathing, heavy bleeding, chest pain, poisoning, a bad allergic
  reaction, a head injury), start with "Call 911 now if you can." and then give first aid.
- Wild plants, mushrooms and berries: NEVER tell them something is safe to eat. Say what it might be, name any
  poisonous look-alikes, and tell them to confirm with a field guide or an expert before eating any of it.
- You can't do anything in the house (lights, TV, music, reminders, alerts, routines). If they ask for that, say
  the offline helper can only answer questions, and to try saying the command a simpler way."""

VOICE_EXTRA = """
- Your answer is spoken aloud: 1-3 short sentences, no lists, no brackets, no symbols or formatting. Say "the
  library says" instead of naming sources."""

PHOTO_NAME = """Look at the photo. Reply with only the most likely name of the main plant, mushroom, animal or
thing in it (common name and, for a living thing, the scientific name), nothing else. If you can't tell, reply: unknown"""


class _Text(HTMLParser):
    """An article's readable paragraphs (headings, paragraphs, list items), without menus, tables or scripts."""

    KEEP = {"p", "li", "dd", "h1", "h2", "h3", "h4", "blockquote"}
    SKIP = {"script", "style", "table", "nav", "footer", "header", "sup", "figure", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._skip = 0
        self._keep = 0
        self._buf: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.KEEP:
            self._keep += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self.KEEP:
            self._keep = max(0, self._keep - 1)
            if self._keep == 0:
                text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
                if len(text) > 30:
                    self.parts.append(text)
                self._buf = []

    def handle_data(self, data):
        if self._keep and not self._skip:
            self._buf.append(data)


def words(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in STOP and len(w) > 1]


def best_paragraphs(paragraphs: List[str], question: str, budget: int) -> str:
    """The paragraphs that share most words with the question, in the article's order, up to budget characters."""
    want = set(words(question))
    scored = sorted(((len(want & set(words(p))), -i, p) for i, p in enumerate(paragraphs)), reverse=True)
    picked, used = [], 0
    for score, neg_i, p in scored:
        if score == 0 and picked:
            break
        p = p[:budget - used] if used + len(p) > budget else p
        if len(p) < 60:
            break
        picked.append((-neg_i, p))
        used += len(p)
        if used >= budget:
            break
    return "\n".join(p for _, p in sorted(picked))


def library_path(link: str) -> Optional[str]:
    """A search result's link, if it really is a page in the library (it is shown to phones as a link)."""
    return link if re.fullmatch(r"/library/content/[\w.-]+/[^\s\"'<>]*", link or "") else None


def clean_reply(text: str, voice: bool = False) -> str:
    """Plain text for the page, Telegram and the speaker: no markdown stars, and no [source] brackets when spoken."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text or "")
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"\1", text)
    text = re.sub(r"(?m)^(\s*)[*-]\s+", r"\1• ", text)
    if voice:
        text = re.sub(r"\s*\[[^\]\n]{1,80}\]", "", text)
    return re.sub(r"\n{3,}", "\n\n", text.strip())


class OfflineHelper:
    def __init__(self, settings: Dict, fetch: Optional[Callable[..., bytes]] = None) -> None:
        self.settings = settings
        self._fetch = fetch or self._http
        self._ready: Dict[str, Tuple[float, bool]] = {}
        self._books: Tuple[float, List[str]] = (0.0, [])
        self._jobs: Dict[str, Dict] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._busy = threading.Lock()  # one question at a time (the AI only thinks about one anyway)
        self.last_error = ""

    # ---- the two services ----
    def enabled(self) -> bool:
        return bool(self.settings.get("offline_helper_enabled", True))

    def _ai(self) -> str:
        return str(self.settings.get("offline_ai_url") or AI_URL).rstrip("/")

    def _library(self) -> str:
        return str(self.settings.get("offline_library_url") or LIBRARY_URL).rstrip("/")

    def library_base(self) -> str:
        """The library server's address for /library/... pages (Sentinel's /library proxies to it)."""
        return self._library()

    def _model(self) -> str:
        return str(self.settings.get("offline_ai_model") or MODEL)

    @staticmethod
    def _http(url: str, body: Optional[bytes] = None, timeout: float = 10) -> bytes:
        req = urllib.request.Request(url, body, {"Content-Type": "application/json"} if body else {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    def _up(self, what: str, url: str) -> bool:
        now = time.time()
        seen = self._ready.get(what)
        if seen and now - seen[0] < READY_CACHE_S:
            return seen[1]
        try:
            self._fetch(url, timeout=2)
            ok = True
        except Exception:
            ok = False
        self._ready[what] = (now, ok)
        return ok

    def ai_ready(self) -> bool:
        return self.enabled() and self._up("ai", self._ai() + "/api/version")

    def library_ready(self) -> bool:
        return self.enabled() and self._up("library", self._library() + "/catalog/v2/root.xml")

    def status(self) -> Dict:
        return {"ai": self.ai_ready(), "library": self.library_ready(), "busy": self._busy.locked()}

    # ---- the library ----
    def books(self) -> List[str]:
        """The library's books by their names in its links (wikipedia_en_all_maxi_2026-08, ...), looked up now and then."""
        now = time.time()
        if now - self._books[0] < BOOKS_CACHE_S and self._books[1]:
            return self._books[1]
        try:
            names = sorted(set(re.findall(r'href="/library/content/([\w.-]+)"',
                                          self._fetch(f"{self._library()}/catalog/v2/entries?count=500",
                                                      timeout=10).decode("utf-8", "replace"))))
        except Exception:
            names = []
        self._books = (now, names)
        return names

    def search(self, question: str, n: int = SOURCES, books: Optional[List[str]] = None) -> List[Dict]:
        """The library's best matches (in the given books, else all): title, book, link and the matching snippet."""
        if not self.library_ready():
            return []
        pattern = " ".join(words(question)) or question  # ("how do I" and "?" only crowd the search)
        q = urllib.parse.urlencode([("pattern", pattern[:200]), ("format", "xml"), ("pageLength", n + 16)] +
                                   ([("books.name", b) for b in books] if books else [("books.filter.lang", "eng")]))
        try:
            root = ET.fromstring(self._fetch(f"{self._library()}/search?{q}", timeout=10))
        except Exception as e:
            app_logger.log("warning", "offline_search_failed", error_message=str(e)[:200], component="offline",
                           action="search")
            return []
        found = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = library_path(item.findtext("link") or "")
            if not title or not link or SKIP_TITLE.match(title) or "/tagged/" in link or title.startswith("questions/"):
                continue
            desc = item.find("description")
            snippet = re.sub(r"\s+", " ", "".join(desc.itertext()) if desc is not None else "").strip(" .")
            book = (item.findtext("book/title") or "").strip()
            found.append({"title": title, "book": book, "link": link, "snippet": snippet[:400]})
            if len(found) >= n:
                break
        return found

    def article(self, link: str) -> List[str]:
        path = library_path(link)
        if not path:
            return []
        base = self._library()
        url = base[:-len("/library")] + path if base.endswith("/library") else base + path[len("/library"):]
        try:
            parser = _Text()
            parser.feed(self._fetch(url, timeout=10).decode("utf-8", "replace"))
            return parser.parts
        except Exception:
            return []

    def excerpts(self, question: str, budget: int) -> Tuple[str, List[Dict]]:
        """The library text the AI reads for this question, and the sources it came from: the encyclopedias and
        guides first (the medical ones for a health question), then the best of everything (the Q&A sites too)."""
        first = MEDICAL_BOOKS if HEALTH.search(question) else REFERENCE_BOOKS
        picked = [b for b in self.books() if b.startswith(first)]
        sources = self.search(question, ARTICLES, picked) if picked else []
        for s in self.search(question):
            if len(sources) >= SOURCES:
                break
            if all(s["link"] != t["link"] for t in sources):
                sources.append(s)
        if not sources:
            return "", []
        per = budget // (ARTICLES + 1)
        blocks = []
        for i, s in enumerate(sources):
            text = best_paragraphs(self.article(s["link"]), question, per) if i < ARTICLES else ""
            text = text or s["snippet"]
            if text:
                blocks.append(f"[{s['book'] or 'Library'}] {s['title']}\n{text}")
        return "\n\n".join(blocks)[:budget + 400], sources

    # ---- the AI ----
    def _chat(self, messages: List[Dict], max_tokens: int, on_text: Optional[Callable[[str], None]] = None) -> str:
        body = json.dumps({"model": self._model(), "messages": messages, "stream": True, "keep_alive": KEEP_ALIVE,
                           "options": {"num_thread": int(self.settings.get("offline_ai_threads") or THREADS),
                                       "num_predict": max_tokens, "temperature": 0.3}}).encode()
        req = urllib.request.Request(self._ai() + "/api/chat", body, {"Content-Type": "application/json"})
        out = []
        with urllib.request.urlopen(req, timeout=ANSWER_TIMEOUT_S) as r:
            for line in r:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("error"):
                    raise RuntimeError(str(row["error"])[:200])
                piece = (row.get("message") or {}).get("content") or ""
                if piece:
                    out.append(piece)
                    if on_text is not None:
                        on_text("".join(out))
                if row.get("done"):
                    break
        return clean_reply("".join(out))

    def answer(self, question: str, voice: bool = False, history: Optional[List[Tuple[str, str]]] = None,
               jpeg: Optional[bytes] = None, on_step: Optional[Callable[[str], None]] = None,
               on_text: Optional[Callable[[str], None]] = None) -> Tuple[Optional[str], List[Dict]]:
        """The offline answer and the library pages it used; (None, []) when the AI isn't there or failed."""
        question = (question or "").strip()[:1000] or ("What is this?" if jpeg else "")
        if not question or not self.ai_ready():
            self.last_error = "the offline AI isn't running" if question else "nothing was asked"
            return None, []
        step = on_step or (lambda s: None)
        kind = "voice" if voice else "page"
        started = time.time()
        with self._busy:
            try:
                image = [base64.b64encode(jpeg).decode()] if jpeg else None
                looked_up = question
                if image:  # first the name of what's in the photo, so the library can be searched for it
                    step("Looking at the photo…")
                    name = self._chat([{"role": "user", "content": PHOTO_NAME, "images": image}], 30)
                    name = re.sub(r"[*_`]", "", name.splitlines()[0] if name else "").strip()[:120]
                    if name and "unknown" not in name.lower():
                        looked_up = name
                step("Searching the offline library…")
                text, sources = self.excerpts(looked_up, BUDGET_CHARS[kind])
                prompt = (f"LIBRARY EXCERPTS:\n{text}\n\n" if text else "LIBRARY EXCERPTS: (nothing found)\n\n")
                if image:  # (looking at a photo takes this PC most of a minute: once is enough, the name is passed on)
                    prompt += (f"THE PHOTO: a first look at it says it shows {looked_up}. It may be wrong: say how sure "
                               "that is and what else it could be.\n\n" if looked_up != question else
                               "THE PHOTO: a first look couldn't tell what it shows. Say so, and what would help.\n\n")
                prompt += f"QUESTION:\n{question}"
                messages = [{"role": "system", "content": SYSTEM + (VOICE_EXTRA if voice else "")}]
                for asked, said in (history or [])[-3:]:
                    messages += [{"role": "user", "content": asked[:500]}, {"role": "assistant", "content": said[:800]}]
                messages.append({"role": "user", "content": prompt})
                step("Reading the library and writing the answer (this PC is slow at it: about a minute)…")
                reply = clean_reply(self._chat(messages, MAX_TOKENS[kind], on_text), voice)
            except Exception as e:
                self.last_error = f"the offline AI didn't answer ({str(e)[:120]})"
                app_logger.log("warning", "offline_answer_failed", error_message=self.last_error, voice=voice,
                               seconds=round(time.time() - started, 1), component="offline", action="answer")
                return None, []
        app_logger.log("info", "offline_answered", seconds=round(time.time() - started, 1), voice=voice,
                       photo=jpeg is not None, sources=len(sources), component="offline", action="answer")
        if not reply:
            self.last_error = "the offline AI gave an empty answer"
            return None, sources
        books = {s["book"] for s in sources if s["book"]}
        reply = re.sub(r"\s*\[([^\]\n]{1,80})\]", lambda m: m.group(0) if m.group(1) in books else "", reply)
        if not sources and not voice:  # (a small AI happily names a source it never read: those were taken out)
            reply += "\n\n(Nothing in the library matched, so this is from the AI's own memory.)"
        if (jpeg is not None or FORAGING.search(question)) and "field guide" not in reply.lower():
            reply += ("\n\n⚠️ " if not voice else " ") + FIELD_GUIDE  # (said every time, whatever the small AI wrote)
        return reply, sources

    # ---- the web page's questions: started, then asked after until done ----
    def start(self, question: str, history: Optional[List[Tuple[str, str]]] = None,
              jpeg: Optional[bytes] = None) -> Tuple[bool, str]:
        if not self.enabled():
            return False, "The offline helper is turned off."
        if not (question or "").strip() and not jpeg:
            return False, "Type a question first."
        if self._busy.locked():
            return False, "The offline helper is answering another question. Try again in a moment."
        if not self.ai_ready():
            return False, "The offline AI isn't running on the PC."
        job_id = f"{next(self._ids)}-{int(time.time())}"
        job = {"state": "working", "step": "Starting…", "text": "", "sources": [], "error": "", "at": time.time()}
        with self._lock:
            now = time.time()
            for old in [k for k, v in self._jobs.items() if now - v["at"] > JOB_KEEP_S]:
                del self._jobs[old]
            self._jobs[job_id] = job

        def run():
            reply, sources = self.answer(question, history=history, jpeg=jpeg,
                                         on_step=lambda s: job.update(step=s),
                                         on_text=lambda t: job.update(text=t, step="Writing…"))
            job.update(sources=[{k: s[k] for k in ("title", "book", "link")} for s in sources])
            if reply:
                job.update(state="done", text=reply, step="")
            else:
                job.update(state="failed", error=self.last_error or "no answer", step="")

        threading.Thread(target=run, name="offline-answer", daemon=True).start()
        return True, job_id

    def job(self, job_id: str) -> Optional[Dict]:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job is not None else None
