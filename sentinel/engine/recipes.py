"""
Recipes: the family recipe box (the Recipes page). Someone pastes a link (or shares it to Sentinel from an Android
phone, or sends it to the Telegram bot) and Sentinel keeps only the recipe: most recipe sites put a tidy copy in the
page for Google (schema.org Recipe in JSON-LD), which is read here; for the few without one, Claude reads the page.
The site's photo is downloaded, so a saved recipe survives the site changing or disappearing.

A cooking video (an Instagram reel, TikTok, YouTube Short, Facebook reel) has no recipe card: Sentinel downloads it
(yt-dlp), listens to it with the same speech recogniser Jarvis uses (on this PC), and when the caption and the talking
still give hardly any amounts, reads the text shown on screen too (RapidOCR, on this PC; Instagram's own captions
aren't downloadable). Claude writes the recipe up from all of it, marking any amount it had to estimate. The video's cover becomes the photo and the link
is kept, so the reel is one tap away. When a site won't let Sentinel in, the recipe text can be pasted instead.
Saving runs in the background (a video takes a minute or two); the page and Telegram are told when it's done.

Each recipe has the original and, once someone changes it, the family's own version (swaps Claude suggested, twists,
edits, notes), saved next to it so the original is always one tap away. A photo of their own can replace the site's.

Cooking mode on any screen tells Sentinel which recipe and step is open, so Jarvis can follow along: "next step",
"how much garlic?", "read the ingredients", "start the timer". "What can I make with chicken and rice?" (Jarvis,
Telegram or the page) ranks the box by what's on hand.

Recipes are shared by the whole family; nothing here is private. Everything is kept in recipes.db and the recipes
folder (photos) next to the recordings, which the recordings' clean-up never touches.
"""

import base64
import glob
import html
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from sentinel.logging_utils import app_logger

DB_NAME = "recipes.db"
PHOTO_DIR = "recipes"
CATEGORIES = ("Breakfast", "Dinners", "Sides", "Snacks", "Desserts", "Beverages", "Other")
PAGE_MAX_BYTES = 4 * 1024 * 1024
IMAGE_MAX_BYTES = 8 * 1024 * 1024
PHOTO_MAX_PX = 1280
FETCH_TIMEOUT = (6, 20)  # connect, read
MAX_REDIRECTS = 5
PAGE_TEXT_FOR_CLAUDE = 15000  # characters of a page without recipe data that Claude reads
COOKING_IDLE_S = 4 * 3600  # cooking mode forgotten after this long without a step
VIDEO_MAX_BYTES = 200 * 1024 * 1024
VIDEO_LISTEN_S = 20 * 60  # longer videos: only the caption is read
WHISPER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                           "models", "whisper", "base.en")  # Jarvis's speech recogniser
JOB_KEEP_S = 3600
OCR_EVERY_S = 1.5  # how often a video's frames are looked at for on-screen text...
OCR_MAX_FRAMES = 40  # ...at most this many (about 1.5 s each on this PC), skipping ones that hardly changed
OCR_WHEN_AMOUNTS_UNDER = 3  # read the screen only when the caption and speech give fewer amounts than this
VIDEO_LINK = re.compile(r"^https?://(?:[\w-]+\.)?(?:instagram\.com/(?:[\w.]+/)?(?:reels?|p|tv)/|tiktok\.com/|"
                        r"youtube\.com/shorts/|youtu\.be/|facebook\.com/(?:reel/|.+/videos/|watch)|fb\.watch/)", re.I)
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36")

# Document limits (a version sent back from the page is checked against these).
MAX_INGREDIENTS, MAX_STEPS = 80, 60
ITEM_MAX, STEP_MAX, NOTES_MAX, TITLE_MAX, UNIT_MAX = 200, 2000, 4000, 150, 24

UNITS = {
    "cup": "cup", "cups": "cup", "c": "cup", "c.": "cup",
    "tablespoon": "tbsp", "tablespoons": "tbsp", "tbsp": "tbsp", "tbsp.": "tbsp", "tbs": "tbsp", "tbl": "tbsp", "T": "tbsp",
    "teaspoon": "tsp", "teaspoons": "tsp", "tsp": "tsp", "tsp.": "tsp", "t": "tsp",
    "pound": "lb", "pounds": "lb", "lb": "lb", "lbs": "lb", "lb.": "lb", "lbs.": "lb",
    "ounce": "oz", "ounces": "oz", "oz": "oz", "oz.": "oz",
    "gram": "g", "grams": "g", "g": "g", "kilogram": "kg", "kilograms": "kg", "kg": "kg",
    "milliliter": "ml", "milliliters": "ml", "ml": "ml", "liter": "l", "liters": "l", "litre": "l", "litres": "l",
    "quart": "qt", "quarts": "qt", "qt": "qt", "pint": "pt", "pints": "pt", "pt": "pt", "gallon": "gal", "gallons": "gal",
    "clove": "clove", "cloves": "clove", "can": "can", "cans": "can", "package": "package", "packages": "package",
    "pkg": "package", "pinch": "pinch", "pinches": "pinch", "dash": "dash", "dashes": "dash", "slice": "slice",
    "slices": "slice", "stick": "stick", "sticks": "stick", "bunch": "bunch", "bunches": "bunch", "sprig": "sprig",
    "sprigs": "sprig", "jar": "jar", "jars": "jar", "bag": "bag", "bags": "bag", "head": "head", "heads": "head",
}
FRACTIONS = {"¼": 0.25, "½": 0.5, "¾": 0.75, "⅓": 1 / 3, "⅔": 2 / 3, "⅛": 0.125, "⅜": 0.375, "⅝": 0.625, "⅞": 0.875,
             "⅕": 0.2, "⅙": 1 / 6}
_NUM = r"(?:\d+\s+\d+/\d+|\d+/\d+|\d*\.\d+|\d+\s*[¼½¾⅓⅔⅛⅜⅝⅞⅕⅙]|[¼½¾⅓⅔⅛⅜⅝⅞⅕⅙]|\d+)"
_INGREDIENT = re.compile(r"^(?P<q>" + _NUM + r")(?:\s*(?:-|–|to)\s*(?P<q2>" + _NUM + r"))?\s*(?P<size>\([^)]{1,30}\))?\s*(?P<rest>.*)$")

CATEGORY_WORDS = (  # checked in order: the site's own category first, then the title
    ("Beverages", r"drink|beverage|cocktail|mocktail|smoothie|punch|lemonade|latte|coffee|\btea\b|cocoa|hot chocolate|"
                  r"cider|shake\b|milkshake|sangria|margarita|spritz"),
    ("Desserts", r"dessert|cookie|cake|\bpie\b|brownie|\bbars?\b|pudding|cheesecake|cupcake|fudge|ice cream|candy|"
                 r"cobbler|crisp\b|tart\b|frosting|truffle|sweet"),
    ("Breakfast", r"breakfast|brunch|pancake|waffle|oatmeal|\boats\b|granola|french toast|muffin|scone|frittata|omelet"),
    ("Snacks", r"snack|appetizer|\bdip\b|starter|finger food|party food|salsa|guacamole|hummus"),
    ("Sides", r"side dish|\bsides?\b|salad|slaw|mashed|roasted vegetables|rice pilaf|\bbread\b|rolls\b"),
    ("Dinners", r"dinner|main|entree|entrée|lunch|supper|soup|stew|chili|casserole|pasta|chicken|beef|pork|fish|"
                r"salmon|shrimp|taco|burger|pizza"),
)

# Words that say how an ingredient is cut or kept, not what it is ("2 cups chopped fresh basil" -> basil).
PREP_WORDS = {"chopped", "diced", "minced", "sliced", "fresh", "freshly", "large", "small", "medium", "finely",
              "roughly", "thinly", "ground", "grated", "shredded", "peeled", "trimmed", "halved", "cubed", "softened",
              "melted", "packed", "divided", "optional", "taste", "to", "for", "of", "and", "or", "the", "a", "into",
              "cut", "pieces", "piece", "plus", "more", "about", "room", "temperature", "cold", "warm", "hot", "whole",
              "boneless", "skinless", "dried", "lightly", "beaten", "crushed", "serving", "serve", "garnish", "extra"}


# ------------------------------------------------------------------ reading a recipe page
def _text(value: Any) -> str:
    """Plain text: HTML tags and entities gone, whitespace collapsed."""
    s = html.unescape(str(value or ""))
    s = re.sub(r"<[^>]+>", " ", s)
    return " ".join(html.unescape(s).split())


def _number(s: str) -> Optional[float]:
    s = s.strip()
    if not s:
        return None
    total = 0.0
    for ch, val in FRACTIONS.items():
        if ch in s:
            s = s.replace(ch, "")
            total += val
    s = s.strip()
    if " " in s:  # "1 1/2"
        whole, s = s.split(None, 1)
        total += float(whole)
    if "/" in s:
        a, b = s.split("/", 1)
        return total + (float(a) / float(b) if float(b) else 0.0)
    return total + (float(s) if s else 0.0)


def parse_ingredient(line: str) -> Dict[str, Any]:
    """'1 1/2 cups flour, sifted' -> {q: 1.5, u: 'cup', item: 'flour, sifted'}; no amount -> q None."""
    text = _text(line)
    out: Dict[str, Any] = {"q": None, "u": "", "item": text[:ITEM_MAX]}
    m = _INGREDIENT.match(text)
    if not m:
        return out
    try:
        q = _number(m.group("q"))
        q2 = _number(m.group("q2")) if m.group("q2") else None
    except (ValueError, ZeroDivisionError):
        return out
    rest = m.group("rest").strip()
    unit = ""
    word = re.match(r"^([A-Za-z]+\.?)(?:\s+|$)", rest)
    if word and (word.group(1) in UNITS or word.group(1).lower() in UNITS) and not (word.group(1) in ("t", "T", "c") and
                                                                                     len(rest) == len(word.group(0))):
        key = word.group(1) if word.group(1) in ("T", "t") else word.group(1).lower()
        unit = UNITS[key]
        rest = rest[len(word.group(0)):].strip()
        if rest.lower().startswith("of "):
            rest = rest[3:]
    if m.group("size"):  # "1 (15 oz) can black beans"
        unit = (m.group("size") + " " + unit).strip()
    if not rest:
        return out
    out.update(q=round(q, 4) if q is not None else None, u=unit[:UNIT_MAX], item=rest[:ITEM_MAX])
    if q2 is not None and q2 > (q or 0):
        out["q2"] = round(q2, 4)
    return out


def minutes(value: Any) -> Optional[int]:
    """ISO 8601 duration (PT1H30M, P0DT45M) -> minutes; None when there's none."""
    m = re.match(r"^P(?:(\d+)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?$",
                 str(value or "").strip().upper())
    if not m or not any(m.groups()):
        return None
    d, h, mi, s = (float(g) if g else 0.0 for g in m.groups())
    total = round(d * 1440 + h * 60 + mi + s / 60)
    return total or None


def _servings(value: Any) -> Optional[int]:
    for v in (value if isinstance(value, list) else [value]):
        m = re.search(r"\d+", str(v or ""))
        if m and 0 < int(m.group()) <= 500:
            return int(m.group())
    return None


def _image(value: Any) -> str:
    if isinstance(value, list):
        for v in value:
            found = _image(v)
            if found:
                return found
        return ""
    if isinstance(value, dict):
        return _image(value.get("url") or value.get("contentUrl"))
    s = str(value or "").strip()
    return s if s.startswith(("http://", "https://", "//")) else ""


def _steps(value: Any, out: List[str]) -> None:
    """recipeInstructions in any of its shapes -> step texts."""
    if isinstance(value, str):
        text = html.unescape(value)
        parts = [p for p in re.split(r"\n+|<br\s*/?>|</p>|</li>", text) if _text(p)]
        if len(parts) == 1:
            parts = [p for p in re.split(r"(?:^|\s)\d{1,2}[.)]\s+", parts[0]) if _text(p)]
        out.extend(_text(p) for p in parts)
    elif isinstance(value, list):
        for v in value:
            _steps(v, out)
    elif isinstance(value, dict):
        kind = value.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if "HowToSection" in kinds:
            before = len(out)
            _steps(value.get("itemListElement") or [], out)
            name = _text(value.get("name"))
            if name and len(out) > before:
                out[before] = f"{name}: {out[before]}"
        elif value.get("itemListElement"):
            _steps(value["itemListElement"], out)
        else:
            text = _text(value.get("text") or value.get("name") or value.get("description"))
            if text:
                out.append(text)


def _find_recipe(node: Any) -> Optional[Dict[str, Any]]:
    if isinstance(node, list):
        for n in node:
            found = _find_recipe(n)
            if found:
                return found
    elif isinstance(node, dict):
        kind = node.get("@type")
        if "Recipe" in (kind if isinstance(kind, list) else [kind]):
            return node
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in node:
                found = _find_recipe(node[key])
                if found:
                    return found
    return None


def recipe_data(page: str) -> Optional[Dict[str, Any]]:
    """The schema.org Recipe a page carries for search engines, or None."""
    for block in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", page, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            try:  # some sites leave raw newlines inside strings
                data = json.loads(re.sub(r"[\r\n\t]+", " ", block.strip()))
            except ValueError:
                continue
        found = _find_recipe(data)
        if found:
            return found
    return None


def guess_category(*texts: Any) -> str:
    for text in texts:
        flat = " ".join(str(t) for t in (text if isinstance(text, list) else [text]) if t).lower()
        if not flat:
            continue
        for cat, words in CATEGORY_WORDS:
            if re.search(words, flat):
                return cat
    return "Dinners"


def from_recipe_data(node: Dict[str, Any], url: str) -> Dict[str, Any]:
    """A saved recipe from schema.org data."""
    steps: List[str] = []
    _steps(node.get("recipeInstructions"), steps)
    ingredients = [parse_ingredient(x) for x in (node.get("recipeIngredient") or node.get("ingredients") or [])
                   if _text(x)]
    prep, cook, total = minutes(node.get("prepTime")), minutes(node.get("cookTime")), minutes(node.get("totalTime"))
    image = _image(node.get("image"))
    title = re.split(r"\s+\|\s+", _text(node.get("name")))[0][:TITLE_MAX]  # "Fajita Bombs | Slimming Friendly"
    return {
        "title": title or "Recipe",
        "servings": _servings(node.get("recipeYield")),
        "prep_min": prep, "cook_min": cook, "total_min": total or ((prep or 0) + (cook or 0) or None),
        "category": guess_category(node.get("recipeCategory"), title, node.get("keywords")),
        "image": urljoin(url, image) if image else "",
        "doc": make_doc(ingredients, steps),
    }


def make_doc(ingredients: List[Dict[str, Any]], steps: List[str], notes: str = "", title: str = "") -> Dict[str, Any]:
    doc = {"ingredients": [dict(i, id=f"i{n + 1}") for n, i in enumerate(ingredients[:MAX_INGREDIENTS])],
           "steps": [{"t": s[:STEP_MAX]} for s in steps[:MAX_STEPS] if s], "notes": notes[:NOTES_MAX]}
    if title:
        doc["title"] = title[:TITLE_MAX]
    return doc


def page_text(page: str) -> str:
    """What a reader sees of a page, for Claude: no scripts, styles or tags; around the recipe when it's long."""
    s = re.sub(r"<(script|style|noscript|svg|iframe|template)\b.*?</\1>", " ", page, flags=re.S | re.I)
    s = re.sub(r"<(?:br|p|div|li|h\d|tr|section|article)\b[^>]*>", "\n", s, flags=re.I)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    s = "\n".join(" ".join(line.split()) for line in s.splitlines())
    s = re.sub(r"\n{2,}", "\n", s).strip()
    if len(s) <= PAGE_TEXT_FOR_CLAUDE:
        return s
    # the recipe card: an "Ingredients" heading followed soon by amounts
    best = 0
    for m in re.finditer(r"ingredients", s, re.I):
        if re.search(r"\n\s*" + _NUM + r"\s", s[m.end():m.end() + 600]):
            best = m.start()
    start = max(0, best - 1500)
    return s[start:start + PAGE_TEXT_FOR_CLAUDE]


def og_image(page: str) -> str:
    m = re.search(r"<meta[^>]+property=[\"']og:image[\"'][^>]+content=[\"']([^\"']+)", page, re.I) or \
        re.search(r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+property=[\"']og:image", page, re.I)
    return html.unescape(m.group(1)) if m else ""


def json_reply(reply: Optional[str]) -> Optional[Any]:
    """The JSON object in Claude's reply (it sometimes wraps it in ``` fences or a sentence)."""
    if not reply:
        return None
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        return json.loads(reply[start:end + 1])
    except ValueError:
        return None


# ------------------------------------------------------------------ fetching safely
def _public(host: str, resolve: Callable = socket.getaddrinfo) -> bool:
    """Every address the name points at is on the public internet (not this PC, the home network or a camera)."""
    try:
        addrs = {info[4][0] for info in resolve(host, None)}
    except (OSError, UnicodeError):
        return False
    try:
        return bool(addrs) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs)
    except ValueError:
        return False


def fetch(url: str, max_bytes: int, get: Optional[Callable] = None,
          resolve: Callable = socket.getaddrinfo) -> Tuple[str, str, bytes]:
    """(final url, content type, body) of a public web page. Raises ValueError with a reason people can read."""
    if get is None:
        import requests
        get = requests.get
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlparse(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("that isn't a web link")
        if not _public(parts.hostname, resolve):
            raise ValueError("that link doesn't go to a public website")
        try:
            r = get(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,image/*;q=0.9,*/*;q=0.8",
                                  "Accept-Language": "en-US,en;q=0.9"},
                    timeout=FETCH_TIMEOUT, stream=True, allow_redirects=False)
        except Exception as e:
            raise ValueError(f"couldn't open the page ({type(e).__name__})")
        try:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                url = urljoin(url, r.headers["Location"])
                continue
            if r.status_code >= 400:
                raise ValueError(f"the site answered {r.status_code}" +
                                 (" (it blocks automatic readers; paste the recipe text instead)" if r.status_code in (401, 403) else ""))
            body = b""
            for chunk in r.iter_content(65536):
                body += chunk
                if len(body) > max_bytes:
                    raise ValueError("the page is too big")
            return url, str(r.headers.get("Content-Type") or ""), body
        finally:
            try:
                r.close()
            except Exception:
                pass
    raise ValueError("the link redirects too many times")


def shrink_jpeg(data: bytes, max_px: int = PHOTO_MAX_PX) -> Optional[bytes]:
    """Any picture -> a JPEG at most max_px on its long side; None when it isn't a picture."""
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    scale = min(1.0, max_px / float(max(h, w)))
    if scale < 1.0:
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes() if ok else None


# ------------------------------------------------------------------ checking a version from the page
def clean_doc(doc: Any) -> Optional[Dict[str, Any]]:
    """A recipe version as the page sent it, checked and trimmed; None when it isn't one."""
    if not isinstance(doc, dict) or not isinstance(doc.get("ingredients"), list) or not isinstance(doc.get("steps"), list):
        return None
    ingredients, seen = [], set()
    for i in doc["ingredients"][:MAX_INGREDIENTS]:
        if not isinstance(i, dict):
            continue
        item = _text(i.get("item"))[:ITEM_MAX]
        if not item:
            continue
        iid = re.sub(r"[^\w-]", "", str(i.get("id") or ""))[:24] or f"n{len(ingredients) + 1}"
        while iid in seen:
            iid += "x"
        seen.add(iid)
        row: Dict[str, Any] = {"id": iid, "q": _amount(i.get("q")), "u": _text(i.get("u"))[:UNIT_MAX], "item": item}
        q2 = _amount(i.get("q2"))
        if q2 is not None and row["q"] is not None and q2 > row["q"]:
            row["q2"] = q2
        ingredients.append(row)
    steps = []
    for s in doc["steps"][:MAX_STEPS]:
        t = _text(s.get("t") if isinstance(s, dict) else s)[:STEP_MAX]
        if t:
            steps.append({"t": t})
    out: Dict[str, Any] = {"ingredients": ingredients, "steps": steps,
                           "notes": str(doc.get("notes") or "").strip()[:NOTES_MAX]}
    title = _text(doc.get("title"))[:TITLE_MAX]
    if title:
        out["title"] = title
    return out


def _amount(value: Any) -> Optional[float]:
    try:
        q = float(value)
    except (TypeError, ValueError):
        return None
    return round(q, 4) if 0 < q <= 100000 else None


def fmt_amount(q: Optional[float]) -> str:
    """0.5 -> '½', 1.333 -> '1⅓', 0.125 -> '⅛' (for Jarvis and Telegram)."""
    if q is None:
        return ""
    whole = int(q + 1e-9)
    rest = q - whole
    _gap, sym = min((abs(rest - v), s) for v, s in ((0.0, ""), (0.125, "⅛"), (0.25, "¼"), (1 / 3, "⅓"), (0.5, "½"),
                                                     (2 / 3, "⅔"), (0.75, "¾"), (1.0, "+")))
    if sym == "+":
        whole, sym = whole + 1, ""
    return (str(whole) if whole else "") + sym or f"{q:g}"


def amount_text(i: Dict[str, Any], factor: float = 1.0, spoken: bool = False) -> str:
    if i.get("q") is None:
        return i.get("item", "")
    q = fmt_amount(i["q"] * factor)
    if i.get("q2"):
        q += (" to " if spoken else "–") + fmt_amount(i["q2"] * factor)
    unit = i.get("u") or ""
    many = i["q"] * factor > 1
    if spoken:
        unit = {"tbsp": "tablespoon", "tsp": "teaspoon", "lb": "pound", "oz": "ounce", "g": "gram", "kg": "kilogram",
                "ml": "milliliter", "l": "liter", "qt": "quart", "pt": "pint", "gal": "gallon"}.get(unit, unit)
        q = q.replace("¼", " and a quarter").replace("½", " and a half").replace("¾", " and three quarters") \
             .replace("⅓", " and a third").replace("⅔", " and two thirds").strip()
        q = re.sub(r"^and a ", "a ", re.sub(r"^and ", "", q))
    if unit and many and unit in ("cup", "clove", "can", "package", "slice", "stick", "sprig", "jar", "bag", "head",
                                  "tablespoon", "teaspoon", "pound", "ounce", "gram", "kilogram", "milliliter", "liter",
                                  "quart", "pint", "gallon"):
        unit += "s"
    return " ".join(p for p in (q, unit, i.get("item", "")) if p)


# ------------------------------------------------------------------ Jarvis and Telegram
_STEP = r"(?:step|stop)"  # Whisper hears "next stop"
COOK_NEXT = re.compile(r"^(?:ok(?:ay)?,? |alright,? )?(?:what'?s |what is )?(?:the |go to the |on to the |move on to the )?next " + _STEP + r"(?: please)?$")
COOK_PREV = re.compile(r"^(?:(?:go )?back(?: one| a)? " + _STEP + r"|(?:the )?previous " + _STEP + r"|(?:go to the )?last " + _STEP + r"|" + _STEP + r" back)(?: please)?$")
COOK_REPEAT = re.compile(r"^(?:repeat(?: that| the " + _STEP + r"| this " + _STEP + r")?|say that again|what'?s (?:this|the current|the) " + _STEP + r"|(?:read|what was) (?:the|this|that) " + _STEP + r"(?: again)?|where (?:am i|are we))(?: please)?$")
COOK_GOTO = re.compile(r"^(?:go to|skip to|jump to) " + _STEP + r" (\w+)$")
COOK_INGREDIENTS = re.compile(r"^(?:read (?:me )?(?:the |all the )?ingredients|what are the ingredients|what (?:do i|do we) need|ingredients)(?: please)?$")
COOK_HOW_MUCH = re.compile(r"^how (?:much|many) (.+?)(?: do (?:i|we) (?:need|use|put in|add)| does it (?:need|take|call for|use)| (?:is|goes|go) in(?: it| there)?| in (?:it|this|the recipe)| again)?$")
COOK_TIMER = re.compile(r"^(?:start|set)(?: the| a| an)?(?: (\w+)(?:-| )minute)? timer(?: for (\w+) (minutes?|hours?))?(?: please)?$")
COOK_TIMER_LEFT = re.compile(r"^(?:how (?:much time|long) (?:is )?(?:left|to go)(?: on the timer)?|how much longer|time left|how'?s the timer|check the timer)$")
COOK_TIMER_STOP = re.compile(r"^(?:cancel|stop|clear) the timer$")
CAN_MAKE = re.compile(r"^(?:what can (?:i|we) (?:make|cook|bake)|what should (?:i|we) (?:make|cook)|give me (?:an idea|ideas) for)"
                      r"(?: for (?:dinner|lunch|breakfast|dessert))?(?: tonight| today)?(?: with| using| from) (.+)$")
SAVE_LINK = re.compile(r"https?://\S+", re.I)
NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
                "forty": 40, "forty-five": 45, "sixty": 60, "ninety": 90, "first": 1, "second": 2, "third": 3,
                "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "last": -1}


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[?.!,]+$", "", str(text or "").strip().lower()).replace("’", "'").split())


def _num(word: Optional[str]) -> Optional[int]:
    if not word:
        return None
    return int(word) if word.isdigit() else NUMBER_WORDS.get(word)


def cooking_command(text: str) -> Optional[Tuple[str, Any]]:
    """('next'|'prev'|'repeat'|'goto'|'ingredients'|'how_much'|'timer'|'timer_left'|'timer_stop', detail) or None."""
    t = _norm(text)
    for kind, rx in (("next", COOK_NEXT), ("prev", COOK_PREV), ("repeat", COOK_REPEAT), ("ingredients", COOK_INGREDIENTS),
                     ("timer_left", COOK_TIMER_LEFT), ("timer_stop", COOK_TIMER_STOP)):
        if rx.match(t):
            return kind, None
    m = COOK_GOTO.match(t)
    if m and _num(m.group(1)) is not None:
        return "goto", _num(m.group(1))
    m = COOK_TIMER.match(t)
    if m:
        n = _num(m.group(1)) or _num(m.group(2))
        if m.group(1) and n is None:
            return None
        if n is not None and m.group(3) and m.group(3).startswith("hour"):
            n *= 60
        return "timer", n
    m = COOK_HOW_MUCH.match(t)
    if m:
        words = [w for w in re.findall(r"[a-z]+", m.group(1)) if w not in PREP_WORDS and w not in ("do", "i", "we", "need")]
        if words:
            return "how_much", words
    return None


def can_make_command(text: str) -> Optional[List[str]]:
    m = CAN_MAKE.match(_norm(text))
    return split_have(m.group(1)) if m else None


def split_have(text: str) -> List[str]:
    """'chicken, broccoli and some rice' -> ['chicken', 'broccoli', 'rice']."""
    parts = re.split(r",|\band\b|\bplus\b|&|\+|/|\bwith\b", str(text or "").lower())
    out = []
    for p in parts:
        words = [w for w in re.findall(r"[a-z]+", p) if w not in PREP_WORDS and w not in ("some", "leftover", "what", "we",
                                                                                             "have", "i", "got", "our")]
        if words:
            w = " ".join(words)
            out.append(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)
    return out[:12]


def save_link_command(text: str) -> Optional[str]:
    """Telegram: a recipe link to save (the message is just a link, or says "recipe" with one)."""
    t = str(text or "").strip()
    m = SAVE_LINK.search(t)
    if not m:
        return None
    rest = (t[:m.start()] + t[m.end():]).strip().lower()
    if rest and "recipe" not in rest:
        return None
    return m.group(0).rstrip(").,>]")


# ------------------------------------------------------------------ Claude's jobs
CLAUDE_SYSTEM = """You help a family with their recipes on their home system. Reply with ONE JSON object and nothing
else: no prose before or after it, no markdown fences. Amounts are numbers (0.5, not "1/2"), or null when the recipe
gives none. Units are short (cup, tbsp, tsp, lb, oz, g, clove, can) or "" for whole things ("1 lemon"). Write plainly
for a home cook. US measures unless the recipe uses metric. Never invent a recipe's facts that aren't there."""

EXTRACT_PROMPT = """Below is the text of a web page someone wants to save as a recipe. Pull out the recipe.
Reply: {{"title": "...", "servings": number or null, "prep_min": number or null, "cook_min": number or null,
"category": one of {cats}, "ingredients": ["1 1/2 cups flour", ...exactly as written...], "steps": ["...", ...]}}
If the page has no recipe, reply {{"none": true}}.

PAGE ({url}):
{text}"""

VIDEO_PROMPT = """Below is a cooking video someone wants to save as a recipe: {where}. You get its caption and what's
said in it (written down by a computer, so expect misheard words; fix obvious ones like "teaspoon" heard as "tea spoon").
Write up the recipe from both. Use the amounts the caption or the speech give. When the video never says how much of
something, estimate a sensible amount and end that ingredient's line with " (est.)". Don't add ingredients the video
doesn't use, apart from obvious basics it clearly uses (salt, oil for the pan).
Reply: {{"title": "...", "servings": number or null, "prep_min": number or null, "cook_min": number or null,
"category": one of {cats}, "ingredients": ["1 1/2 cups flour", "2 tbsp butter (est.)", ...], "steps": ["...", ...]}}
If there's no recipe in it (say the caption only says "comment RECIPE"), reply {{"none": true, "why": "a short reason"}}.

CAPTION:
{caption}

SAID IN THE VIDEO:
{speech}

TEXT SHOWN ON SCREEN (read by a computer from video frames, in order; spaces are sometimes missing):
{screen}"""

PASTE_PROMPT = """Someone pasted this text to save as a recipe (copied from a caption, a message or a web page).
Write the recipe up exactly as given; don't invent amounts that aren't there.
Reply: {{"title": "...", "servings": number or null, "prep_min": number or null, "cook_min": number or null,
"category": one of {cats}, "ingredients": ["1 1/2 cups flour", ...], "steps": ["...", ...]}}
If it isn't a recipe, reply {{"none": true}}.

TEXT:
{text}"""

SWAP_PROMPT = """Recipe: {title} (makes {servings}).
Ingredients (id: amount unit item):
{ingredients}
Steps:
{steps}

The family doesn't have, or wants to replace: {target}.{extra}
Suggest 2 or 3 things they might have at home instead, best first, with the amount for this recipe as written.
If a swap changes how it's cooked (time, temperature, when it goes in), rewrite those steps in full.
Reply: {{"options": [{{"q": number or null, "u": "unit", "item": "what to use", "why": "one short sentence",
"steps": [{{"n": step number, "text": "the whole new step"}}]}}]}}  ("steps": [] when nothing else changes)"""

TWIST_PROMPT = """Recipe: {title} (makes {servings}).
Ingredients (id: amount unit item):
{ingredients}
Steps:
{steps}

The family asks: {request}
Rewrite the recipe to do that, changing only what you need to. Keep each ingredient's id when you keep or change it;
use "new" for one you add; leave out ones you remove. Amounts are for the recipe as written.
Reply: {{"say": "one or two sentences on what you changed", "ingredients": [{{"id": "...", "q": number or null,
"u": "unit", "item": "..."}}], "steps": ["the whole text of each step", ...]}}
If it can't sensibly be done, reply {{"say": "why not", "ingredients": null}}."""


def is_video_link(url: str) -> bool:
    return bool(VIDEO_LINK.match(str(url or "").strip()))


_whisper = None
_whisper_lock = threading.Lock()


def listen(path: str) -> str:
    """What's said in a video, written down by Jarvis's speech recogniser (on this PC)."""
    global _whisper
    import subprocess

    import numpy as np
    from faster_whisper import WhisperModel

    from sentinel.recordings.store import find_ffmpeg
    # the sound as 16 kHz mono, by Sentinel's own ffmpeg (faster-whisper's own decoder trips over this PyAV)
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise ValueError("no ffmpeg")
    pcm = subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-i", path, "-t", str(VIDEO_LISTEN_S), "-vn", "-ac", "1",
                          "-ar", "16000", "-f", "s16le", "-"], capture_output=True, timeout=300).stdout
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
    if len(audio) < 16000:
        return ""
    with _whisper_lock:
        if _whisper is None:
            _whisper = WhisperModel(WHISPER_DIR, device="cpu", compute_type="int8", cpu_threads=3)
        segments, _info = _whisper.transcribe(audio, beam_size=1, language="en", vad_filter=True,
                                              condition_on_previous_text=False,
                                              initial_prompt="A cooking video: cups, tablespoons, teaspoons, grams, ounces.")
        return " ".join(seg.text.strip() for seg in segments if seg.end <= VIDEO_LISTEN_S)[:20000]


def download_video(url: str, folder: str) -> Tuple[Dict[str, Any], Optional[str]]:
    """(its details, the downloaded file or None) of a video post. Raises ValueError with a reason people can read."""
    import yt_dlp
    from sentinel.recordings.store import find_ffmpeg
    opts = {"outtmpl": os.path.join(folder, "video.%(ext)s"), "quiet": True, "no_warnings": True, "noprogress": True,
            "noplaylist": True, "socket_timeout": 20, "retries": 2, "max_filesize": VIDEO_MAX_BYTES,
            "format": "b[height<=720][acodec!=none]/bv*[height<=720]+ba/b/bv*+ba", "ffmpeg_location": find_ffmpeg()}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True) or {}
    except Exception as e:  # yt-dlp's own errors say "login required", "private", "unavailable"...
        msg = str(e).replace("ERROR: ", "")
        low = msg.lower()
        if "login" in low or "cookies" in low or "rate" in low:
            raise ValueError("the site wants a login before it shows that video. Copy the caption and paste it "
                             "under “Paste a recipe” instead")
        if "private" in low:
            raise ValueError("that post is private")
        raise ValueError("couldn't download the video (" + re.sub(r"\s+", " ", msg)[:120] + ")")
    if info.get("entries"):  # a post with several videos: the first
        info = next((e for e in info["entries"] if e), info)
    files = [f for f in glob.glob(os.path.join(folder, "video.*")) if not f.endswith((".part", ".ytdl"))]
    return info, (files[0] if files else None)


_AMOUNT = re.compile(_NUM + r"\s*(?:cups?|tbsp|tablespoons?|tsp|teaspoons?|lbs?|pounds?|oz|ounces?|g|grams?|kg|ml|"
                     r"cloves?|cans?)\b", re.I)


def amounts_in(text: str) -> int:
    """How many amounts ('2 cups', '1/2 tsp', '400g') a text mentions."""
    return len(_AMOUNT.findall(text or ""))


_ocr = None
_ocr_lock = threading.Lock()


def read_screen(path: str) -> str:
    """The text shown in a video (ingredient lists, amounts), read from frames every OCR_EVERY_S seconds."""
    global _ocr
    import cv2
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR
    with _ocr_lock:
        if _ocr is None:
            _ocr = RapidOCR()
        cap = cv2.VideoCapture(path)
        try:
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            seconds = min(VIDEO_LISTEN_S, (cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) / fps)
            lines: List[str] = []
            last, looked = None, 0
            at = 0.5
            while at < seconds and looked < OCR_MAX_FRAMES:
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(at * fps))
                at += OCR_EVERY_S
                ok, frame = cap.read()
                if not ok:
                    break
                h, w = frame.shape[:2]
                scale = 640.0 / max(h, w)
                small = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
                gray = cv2.cvtColor(cv2.resize(small, (64, 64)), cv2.COLOR_BGR2GRAY).astype(np.float32)
                if last is not None and float(np.mean(np.abs(gray - last))) < 4.0:
                    continue  # the same picture as last time
                last = gray
                looked += 1
                found, _elapsed = _ocr(small)
                text = " ".join(r[1] for r in found or [] if r[2] >= 0.75)
                if text and (not lines or text != lines[-1]):
                    lines.append(text)
            return "\n".join(dict.fromkeys(lines))[:6000]
        finally:
            cap.release()


def video_frame(path: str) -> Optional[bytes]:
    """A sharp frame from the video (for its photo when it has no cover picture)."""
    import cv2
    cap = cv2.VideoCapture(path)
    try:
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        best, best_score = None, -1.0
        for at in (0.1, 0.3, 0.6, 0.85, 0.95):
            if count:
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(count * at))
            ok, frame = cap.read()
            if not ok:
                continue
            score = float(cv2.Laplacian(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
            if score > best_score:
                best, best_score = frame, score
        if best is None:
            return None
        ok, buf = cv2.imencode(".jpg", best, [cv2.IMWRITE_JPEG_QUALITY, 88])
        return buf.tobytes() if ok else None
    finally:
        cap.release()


def _doc_lines(doc: Dict[str, Any]) -> Tuple[str, str]:
    ing = "\n".join(f"{i['id']}: {' '.join(p for p in (fmt_amount(i.get('q')), i.get('u', ''), i['item']) if p)}"
                    for i in doc["ingredients"])
    steps = "\n".join(f"{n + 1}. {s['t']}" for n, s in enumerate(doc["steps"]))
    return ing, steps


class RecipeBox:
    def __init__(self, root: str, assistant=None, speak: Callable[[str], Any] = lambda _t: None,
                 fetcher: Callable[..., Tuple[str, str, bytes]] = fetch, clock: Callable[[], float] = time.time) -> None:
        self.root = os.path.abspath(root)
        self.db_path = os.path.join(self.root, DB_NAME)
        self.photo_dir = os.path.join(self.root, PHOTO_DIR)
        os.makedirs(self.photo_dir, exist_ok=True)
        self.assistant = assistant  # Claude (sentinel/engine/assistant.py): swaps, twists, pages without recipe data
        self._speak = speak  # Jarvis: the timer
        self._fetch = fetcher
        self._clock = clock
        self._lock = threading.RLock()
        self._cooking: Dict[str, Any] = {}  # {id, version, step, servings, at, timer_end, timer_min}
        self._timer: Optional[threading.Timer] = None
        self._jobs: Dict[str, Dict[str, Any]] = {}  # saving in the background: job id -> {state, stage, message, id}
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            # original / ours: {"ingredients": [{id, q, q2?, u, item}], "steps": [{t}], "notes", "title"?}
            conn.execute("CREATE TABLE IF NOT EXISTS recipes (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,"
                         " url TEXT NOT NULL DEFAULT '', site TEXT NOT NULL DEFAULT '', saved_by TEXT NOT NULL DEFAULT '',"
                         " saved_at REAL NOT NULL, category TEXT NOT NULL, servings INTEGER, prep_min INTEGER,"
                         " cook_min INTEGER, total_min INTEGER, original TEXT NOT NULL, ours TEXT, ours_by TEXT,"
                         " ours_at REAL, site_image INTEGER NOT NULL DEFAULT 0, photo REAL)")
            conn.execute("CREATE INDEX IF NOT EXISTS recipes_url ON recipes (url)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _row(self, rid: Any) -> Optional[sqlite3.Row]:
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            return None
        with self._connect() as conn:
            return conn.execute("SELECT * FROM recipes WHERE id = ?", (rid,)).fetchone()

    # ------------------------------------------------------------ saving a link
    def start_save(self, url: Any = "", by: str = "", text: Any = "",
                   on_done: Optional[Callable[[bool, str, Optional[int]], Any]] = None) -> str:
        """Save a link (or pasted text) in the background: the job's id, for job()."""
        job_id = secrets.token_hex(8)
        now = self._clock()
        with self._lock:
            self._jobs = {k: v for k, v in self._jobs.items() if now - v["at"] < JOB_KEEP_S}
            self._jobs[job_id] = {"state": "working", "stage": "Reading the page", "message": "", "id": None, "at": now,
                                  "video": is_video_link(str(url or ""))}

        def progress(stage: str) -> None:
            with self._lock:
                self._jobs[job_id]["stage"] = stage

        def run() -> None:
            try:
                if str(text or "").strip():
                    ok, message, rid = self.save_text(text, url, by, progress)
                else:
                    ok, message, rid = self.save_link(url, by, progress)
            except Exception as e:  # never leave the page waiting
                app_logger.log("error", "recipe_save_failed", error_message=str(e), component="recipes", action="save")
                ok, message, rid = False, f"Couldn't save it ({type(e).__name__}).", None
            with self._lock:
                self._jobs[job_id].update(state="done" if ok else "failed", message=message, id=rid)
            if on_done is not None:
                try:
                    on_done(ok, message, rid)
                except Exception:
                    pass
        threading.Thread(target=run, daemon=True, name="recipe-save").start()
        return job_id

    def job(self, job_id: Any) -> Optional[Dict[str, Any]]:
        with self._lock:
            j = self._jobs.get(str(job_id or ""))
            return {k: v for k, v in j.items() if k != "at"} if j else None

    def save_link(self, url: Any, by: str = "",
                  progress: Callable[[str], Any] = lambda _s: None) -> Tuple[bool, str, Optional[int]]:
        """(ok, message, recipe id) for a recipe link."""
        url = str(url or "").strip()
        m = SAVE_LINK.search(url)
        if not m:
            return False, "That doesn't look like a link. Copy the recipe page's address and paste it here.", None
        url = m.group(0).rstrip(").,>]")
        with self._connect() as conn:
            known = conn.execute("SELECT id, title FROM recipes WHERE url = ?", (url,)).fetchone()
        if known:
            return True, f"“{known['title']}” is already in the recipe box.", known["id"]
        if is_video_link(url):
            return self._save_video(url, by, progress)
        try:
            final, kind, body = self._fetch(url, PAGE_MAX_BYTES)
        except ValueError as e:
            return False, f"Couldn't save it: {e}.", None
        page = body.decode("utf-8", "replace")
        node = recipe_data(page)
        how = "recipe data"
        if node is not None:
            recipe = from_recipe_data(node, final)
        else:
            progress("Claude is reading the page")
            recipe = self._claude_extract(final, page)
            how = "Claude"
            if recipe is None:
                return False, (self._claude_error() or "That page doesn't seem to have a recipe on it") + ".", None
        if not recipe["doc"]["ingredients"] and not recipe["doc"]["steps"]:
            return False, "Couldn't find the ingredients or steps on that page.", None
        if not recipe.get("image"):
            image = og_image(page)
            recipe["image"] = urljoin(final, image) if image else ""
        rid = self._insert(recipe, url, by)
        self._save_site_image(rid, recipe.get("image") or "")
        app_logger.log("info", "recipe_saved", recipe_id=rid, site=urlparse(url).hostname, how=how,
                       ingredients=len(recipe["doc"]["ingredients"]), steps=len(recipe["doc"]["steps"]),
                       component="recipes", action="save")
        return True, f"Saved “{recipe['title']}” to the recipe box under {recipe['category']}.", rid

    def _save_video(self, url: str, by: str, progress: Callable[[str], Any]) -> Tuple[bool, str, Optional[int]]:
        """A cooking video: its caption and what's said in it, written up by Claude."""
        parts = urlparse(url)
        if not _public(parts.hostname or ""):
            return False, "Couldn't save it: that link doesn't go to a public website.", None
        if self.assistant is None or not self.assistant.enabled():
            return False, "Saving recipes from videos needs Claude, which is turned off (Settings → Assistant).", None
        folder = tempfile.mkdtemp(prefix="video-", dir=self.photo_dir)
        try:
            progress("Downloading the video")
            try:
                info, path = download_video(url, folder)
            except ValueError as e:
                return False, f"Couldn't save it: {e}.", None
            caption = _text(info.get("description") or "")[:6000]
            speech = ""
            if path and (info.get("duration") or 0) <= VIDEO_LISTEN_S * 3:
                progress("Listening to the video")
                try:
                    speech = listen(path)
                except Exception as e:
                    app_logger.log("warning", "recipe_listen_failed", error_message=str(e), component="recipes",
                                   action="save")
                    if len(caption) < 200:  # the caption alone won't be the recipe
                        return False, f"Couldn't listen to the video ({type(e).__name__}).", None
            screen = ""
            if path and amounts_in(caption + " " + speech) < OCR_WHEN_AMOUNTS_UNDER:
                progress("Reading the text on screen")  # the amounts may only be shown, not said
                try:
                    screen = read_screen(path)
                except Exception as e:
                    app_logger.log("warning", "recipe_ocr_failed", error_message=str(e), component="recipes",
                                   action="save")
            if len(caption) + len(speech) + len(screen) < 40:
                return False, "That video has no caption, talking or text on screen to take a recipe from.", None
            progress("Claude is writing up the recipe")
            handle = next((str(h) for h in (info.get("channel"), info.get("uploader_id"))
                           if h and not str(h).isdigit() and " " not in str(h)), "")  # Instagram's ids are numbers
            who = f"@{handle.lstrip('@')}" if handle else _text(info.get("uploader"))[:60]
            site = (parts.hostname or "").removeprefix("www.").removeprefix("m.")
            where = f"a {site} video" + (f" by {who}" if who else "")
            reply = json_reply(self.assistant.complete(CLAUDE_SYSTEM, VIDEO_PROMPT.format(
                where=where, cats=", ".join(CATEGORIES[:-1]), caption=caption or "(none)",
                speech=speech or "(nothing said, or no sound)", screen=screen or "(not read)"), "recipe_video"))
            recipe = self._recipe_from_reply(reply)
            if recipe is None:
                why = _text(reply.get("why")) if isinstance(reply, dict) else ""
                return False, (f"Claude didn't find a recipe in that video ({why})." if why else
                               (self._claude_error() or "Claude didn't find a recipe in that video") + "."), None
            estimated = any(i["item"].endswith("(est.)") for i in recipe["doc"]["ingredients"])
            credit = f"From {who}'s video on {site}." if who else f"From a video on {site}."
            recipe["doc"]["notes"] = credit + (" Amounts marked (est.) weren't said in the video; Claude estimated them."
                                               if estimated else "")
            rid = self._insert(recipe, url, by)
            progress("Saving the photo")
            thumb = info.get("thumbnail") or ""
            if thumb:
                self._save_site_image(rid, thumb)
            if path and self._row(rid)["site_image"] == 0:
                frame = video_frame(path)
                jpeg = shrink_jpeg(frame) if frame else None
                if jpeg:
                    self._write(self._photo_path(rid, "site"), jpeg)
                    with self._connect() as conn:
                        conn.execute("UPDATE recipes SET site_image = 1 WHERE id = ?", (rid,))
            app_logger.log("info", "recipe_saved", recipe_id=rid, site=site, how="video", heard=len(speech), seen=len(screen),
                           caption=len(caption), estimated=estimated, ingredients=len(recipe["doc"]["ingredients"]),
                           steps=len(recipe["doc"]["steps"]), component="recipes", action="save")
            return True, f"Saved “{recipe['title']}” to the recipe box under {recipe['category']}.", rid
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def save_text(self, text: Any, url: Any = "", by: str = "",
                  progress: Callable[[str], Any] = lambda _s: None) -> Tuple[bool, str, Optional[int]]:
        """A recipe pasted as text (a caption, a message), written up by Claude; url = where it came from, if known."""
        text = str(text or "").strip()[:PAGE_TEXT_FOR_CLAUDE]
        if len(text) < 30:
            return False, "Paste the whole recipe: the ingredients and the steps.", None
        if self.assistant is None or not self.assistant.enabled():
            return False, "Reading pasted recipes needs Claude, which is turned off (Settings → Assistant).", None
        progress("Claude is writing up the recipe")
        reply = json_reply(self.assistant.complete(CLAUDE_SYSTEM, PASTE_PROMPT.format(
            cats=", ".join(CATEGORIES[:-1]), text=text), "recipe_paste"))
        recipe = self._recipe_from_reply(reply)
        if recipe is None:
            return False, (self._claude_error() or "Claude didn't find a recipe in that text") + ".", None
        link = SAVE_LINK.search(str(url or ""))
        rid = self._insert(recipe, link.group(0) if link else "", by)
        app_logger.log("info", "recipe_saved", recipe_id=rid, how="pasted", component="recipes", action="save")
        return True, f"Saved “{recipe['title']}” to the recipe box under {recipe['category']}.", rid

    def _recipe_from_reply(self, reply: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(reply, dict) or reply.get("none") or not isinstance(reply.get("ingredients"), list):
            return None
        steps = [_text(s) for s in reply.get("steps") or [] if _text(s)]
        ingredients = [parse_ingredient(x) for x in reply["ingredients"] if _text(x)]
        if not ingredients and not steps:
            return None
        prep, cook = _amount(reply.get("prep_min")), _amount(reply.get("cook_min"))
        cat = reply.get("category") if reply.get("category") in CATEGORIES else guess_category(reply.get("title"))
        return {"title": _text(reply.get("title"))[:TITLE_MAX] or "Recipe", "servings": _servings(reply.get("servings")),
                "prep_min": int(prep) if prep else None, "cook_min": int(cook) if cook else None,
                "total_min": int((prep or 0) + (cook or 0)) or None, "category": cat, "image": "",
                "doc": make_doc(ingredients, steps)}

    def _claude_error(self) -> str:
        err = getattr(self.assistant, "last_error", "") if self.assistant is not None else ""
        return f"That page has no recipe data, and Claude couldn't read it ({err})" if err else ""

    def _claude_extract(self, url: str, page: str) -> Optional[Dict[str, Any]]:
        if self.assistant is None:
            return None
        text = page_text(page)
        if len(text) < 200:
            return None
        return self._recipe_from_reply(json_reply(self.assistant.complete(
            CLAUDE_SYSTEM, EXTRACT_PROMPT.format(cats=", ".join(CATEGORIES[:-1]), url=url, text=text), "recipe_read")))

    def _insert(self, recipe: Dict[str, Any], url: str, by: str) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO recipes (title, url, site, saved_by, saved_at, category, servings, prep_min, cook_min,"
                " total_min, original) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (recipe["title"], url, (urlparse(url).hostname or "").removeprefix("www."), by[:40], self._clock(),
                 recipe["category"], recipe.get("servings"), recipe.get("prep_min"), recipe.get("cook_min"),
                 recipe.get("total_min"), json.dumps(recipe["doc"])))
            return int(cur.lastrowid)

    def _save_site_image(self, rid: int, url: str) -> None:
        if not url:
            return
        try:
            _final, kind, body = self._fetch(url.replace("//", "https://", 1) if url.startswith("//") else url,
                                             IMAGE_MAX_BYTES)
            jpeg = shrink_jpeg(body)
        except (ValueError, ImportError) as e:
            app_logger.log("info", "recipe_image_failed", recipe_id=rid, error_message=str(e), component="recipes",
                           action="save")
            return
        if jpeg is None:
            return
        self._write(self._photo_path(rid, "site"), jpeg)
        with self._connect() as conn:
            conn.execute("UPDATE recipes SET site_image = 1 WHERE id = ?", (rid,))

    def _photo_path(self, rid: int, kind: str) -> str:
        return os.path.join(self.photo_dir, f"{int(rid)}-{kind}.jpg")

    @staticmethod
    def _write(path: str, data: bytes) -> None:
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)

    # ------------------------------------------------------------ the page
    def _summary(self, r: sqlite3.Row) -> Dict[str, Any]:
        doc = json.loads(r["ours"] or r["original"])
        return {"id": r["id"], "title": doc.get("title") or r["title"], "category": r["category"],
                "total_min": r["total_min"], "saved_by": r["saved_by"], "saved_at": r["saved_at"], "site": r["site"],
                "ours": r["ours"] is not None, "photo": self._photo_url(r),
                "ingredients": [i["item"] for i in doc["ingredients"]]}

    def _photo_url(self, r: sqlite3.Row) -> str:
        if r["photo"]:
            return f"/recipes/photo/{r['id']}?v={int(r['photo'])}"
        return f"/recipes/photo/{r['id']}?v=site" if r["site_image"] else ""

    def recipes(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM recipes ORDER BY saved_at DESC").fetchall()
        return [self._summary(r) for r in rows]

    def recipe(self, rid: Any) -> Optional[Dict[str, Any]]:
        r = self._row(rid)
        if r is None:
            return None
        out = self._summary(r)
        out.update(url=r["url"], title=r["title"], servings=r["servings"], prep_min=r["prep_min"], cook_min=r["cook_min"],
                   original=json.loads(r["original"]), ours=json.loads(r["ours"]) if r["ours"] else None,
                   ours_by=r["ours_by"], ours_at=r["ours_at"], own_photo=bool(r["photo"]), video=is_video_link(r["url"]))
        return out

    def photo_file(self, rid: Any) -> Optional[str]:
        r = self._row(rid)
        if r is None:
            return None
        for kind, has in (("photo", r["photo"]), ("site", r["site_image"])):
            path = self._photo_path(r["id"], kind)
            if has and os.path.isfile(path):
                return path
        return None

    def save_version(self, rid: Any, doc: Any, by: str = "") -> Tuple[bool, str]:
        r = self._row(rid)
        if r is None:
            return False, "That recipe isn't in the box any more."
        clean = clean_doc(doc)
        if clean is None or not clean["ingredients"] and not clean["steps"]:
            return False, "The recipe needs at least one ingredient or step."
        original = json.loads(r["original"])
        same = clean == original or (clean.get("title") == r["title"] and dict(clean, title=None) == dict(original, title=None))
        with self._lock, self._connect() as conn:
            if same:
                conn.execute("UPDATE recipes SET ours = NULL, ours_by = NULL, ours_at = NULL WHERE id = ?", (r["id"],))
            else:
                conn.execute("UPDATE recipes SET ours = ?, ours_by = ?, ours_at = ? WHERE id = ?",
                             (json.dumps(clean), by[:40], self._clock(), r["id"]))
        app_logger.log("info", "recipe_changed", recipe_id=r["id"], back_to_original=same, component="recipes",
                       action="save_version")
        return True, ("That's the same as the original, so the original is used." if same else
                      "Saved as your family's version. The original is still one tap away.")

    def revert(self, rid: Any) -> Tuple[bool, str]:
        r = self._row(rid)
        if r is None:
            return False, "That recipe isn't in the box any more."
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE recipes SET ours = NULL, ours_by = NULL, ours_at = NULL WHERE id = ?", (r["id"],))
        return True, "Back to the original recipe."

    def set_category(self, rid: Any, category: Any) -> Tuple[bool, str]:
        r = self._row(rid)
        if r is None or category not in CATEGORIES:
            return False, "Pick one of the categories."
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE recipes SET category = ? WHERE id = ?", (category, r["id"]))
        return True, f"Moved to {category}."

    def set_photo(self, rid: Any, data_url: Any) -> Tuple[bool, str]:
        r = self._row(rid)
        if r is None:
            return False, "That recipe isn't in the box any more."
        raw = str(data_url or "")
        if raw.startswith("data:"):
            raw = raw.split(",", 1)[-1]
        try:
            jpeg = shrink_jpeg(base64.b64decode(raw, validate=False))
        except (ValueError, ImportError):
            jpeg = None
        if jpeg is None:
            return False, "That doesn't look like a photo."
        self._write(self._photo_path(r["id"], "photo"), jpeg)
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE recipes SET photo = ? WHERE id = ?", (self._clock(), r["id"]))
        return True, "Photo added."

    def remove_photo(self, rid: Any) -> Tuple[bool, str]:
        r = self._row(rid)
        if r is None:
            return False, "That recipe isn't in the box any more."
        try:
            os.remove(self._photo_path(r["id"], "photo"))
        except OSError:
            pass
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE recipes SET photo = NULL WHERE id = ?", (r["id"],))
        return True, "Photo removed." + (" The site's photo is back." if r["site_image"] else "")

    def delete(self, rid: Any) -> Tuple[bool, str]:
        r = self._row(rid)
        if r is None:
            return False, "That recipe isn't in the box any more."
        for kind in ("photo", "site"):
            try:
                os.remove(self._photo_path(r["id"], kind))
            except OSError:
                pass
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM recipes WHERE id = ?", (r["id"],))
        with self._lock:
            if self._cooking.get("id") == r["id"]:
                self._cooking = {}
        app_logger.log("info", "recipe_deleted", recipe_id=r["id"], component="recipes", action="delete")
        return True, f"Deleted “{r['title']}”."

    # ------------------------------------------------------------ Claude: swaps and twists
    def _ask(self, prompt: str, what: str) -> Tuple[Optional[Any], str]:
        if self.assistant is None:
            return None, "Claude isn't set up on Sentinel."
        reply = json_reply(self.assistant.complete(CLAUDE_SYSTEM, prompt, what))
        if reply is None:
            return None, "Claude couldn't answer" + (f": {self.assistant.last_error}" if self.assistant.last_error else "") + "."
        return reply, ""

    def swap(self, rid: Any, doc: Any, ingredient_id: Any, note: Any = "") -> Tuple[bool, Any]:
        """(True, [options]) or (False, message): what to use instead of one ingredient."""
        r = self._row(rid)
        doc = clean_doc(doc)
        if r is None or doc is None:
            return False, "That recipe isn't in the box any more."
        target = next((i for i in doc["ingredients"] if i["id"] == str(ingredient_id)), None)
        if target is None:
            return False, "That ingredient isn't in the recipe."
        ing, steps = _doc_lines(doc)
        extra = f"\nThey said: {_text(note)[:200]}" if _text(note) else ""
        reply, err = self._ask(SWAP_PROMPT.format(title=doc.get("title") or r["title"],
                                                  servings=f"{r['servings']} servings" if r["servings"] else "the amount written",
                                                  ingredients=ing, steps=steps, target=f"{target['id']}: {amount_text(target)}",
                                                  extra=extra), "recipe_swap")
        if reply is None:
            return False, err
        options = []
        for o in (reply.get("options") or [])[:3] if isinstance(reply, dict) else []:
            if not isinstance(o, dict) or not _text(o.get("item")):
                continue
            changes = []
            for s in o.get("steps") or []:
                try:
                    n = int(s.get("n")) - 1
                except (TypeError, ValueError, AttributeError):
                    continue
                if 0 <= n < len(doc["steps"]) and _text(s.get("text")):
                    changes.append({"n": n, "text": _text(s["text"])[:STEP_MAX]})
            options.append({"q": _amount(o.get("q")), "u": _text(o.get("u"))[:UNIT_MAX], "item": _text(o["item"])[:ITEM_MAX],
                            "why": _text(o.get("why"))[:300], "steps": changes})
        if not options:
            return False, "Claude didn't have a good swap for that."
        return True, options

    def twist(self, rid: Any, doc: Any, request: Any) -> Tuple[bool, Any]:
        """(True, {say, doc}) or (False, message): the recipe rewritten the way they asked."""
        r = self._row(rid)
        doc = clean_doc(doc)
        ask = _text(request)[:300]
        if r is None or doc is None:
            return False, "That recipe isn't in the box any more."
        if not ask:
            return False, "Say how you'd like it changed."
        ing, steps = _doc_lines(doc)
        reply, err = self._ask(TWIST_PROMPT.format(title=doc.get("title") or r["title"],
                                                   servings=f"{r['servings']} servings" if r["servings"] else "the amount written",
                                                   ingredients=ing, steps=steps, request=ask), "recipe_twist")
        if reply is None:
            return False, err
        say = _text(reply.get("say"))[:400] if isinstance(reply, dict) else ""
        if not isinstance(reply, dict) or not isinstance(reply.get("ingredients"), list):
            return False, say or "Claude couldn't change it that way."
        known = {i["id"] for i in doc["ingredients"]}
        rows = []
        for n, i in enumerate(reply["ingredients"]):
            if isinstance(i, dict):
                iid = str(i.get("id") or "")
                rows.append(dict(i, id=iid if iid in known else f"c{int(self._clock()) % 100000}{n}"))
        new = clean_doc({"ingredients": rows, "steps": reply.get("steps") or [], "notes": doc.get("notes", ""),
                         "title": doc.get("title", "")})
        if new is None or not new["ingredients"]:
            return False, say or "Claude couldn't change it that way."
        return True, {"say": say or "Done.", "doc": new}

    # ------------------------------------------------------------ what can I make
    def can_make(self, have: List[str], limit: int = 5) -> List[Dict[str, Any]]:
        """Recipes ranked by how many of their ingredients are on hand (then by how few others they need)."""
        have = [h for h in have if h]
        if not have:
            return []
        out = []
        for r in self.recipes():
            got, need = [], []
            for item in r["ingredients"]:
                low = item.lower()
                (got if any(re.search(r"\b" + re.escape(h), low) for h in have) else need).append(
                    re.split(r",| \(", item)[0])
            if got:
                out.append({"id": r["id"], "title": r["title"], "category": r["category"], "got": got, "need": need,
                            "photo": r["photo"]})
        out.sort(key=lambda x: (-len(x["got"]), len(x["need"])))
        return out[:limit]

    def answer_can_make(self, have: List[str]) -> str:
        """Sentences (Jarvis says them as they are; Telegram shows them)."""
        found = self.can_make(have, 3)
        what = ", ".join(have[:-1]) + (" and " if len(have) > 1 else "") + have[-1] if have else "that"
        if not found:
            return f"Nothing in the recipe box uses {what} yet."

        def one(f: Dict[str, Any]) -> str:
            if not f["need"]:
                return f"{f['title']}, and you have everything"
            need = f["need"][:3]
            more = len(f["need"]) - len(need)
            listed = ", ".join(need[:-1]) + (" and " if len(need) > 1 else "") + need[-1]
            return f"{f['title']}, which also needs {listed}" + (f" plus {more} more" if more else "")
        return f"With {what}, from the recipe box: " + "; or ".join(one(f) for f in found) + "."

    # ------------------------------------------------------------ cooking mode (and Jarvis following along)
    def start_cooking(self, rid: Any, version: Any = "ours", servings: Any = None, step: Any = 0) -> Tuple[bool, str]:
        r = self._row(rid)
        if r is None:
            return False, "That recipe isn't in the box any more."
        try:
            servings = float(servings) if servings else None
            step = max(0, int(step or 0))
        except (TypeError, ValueError):
            servings, step = None, 0
        with self._lock:
            same = self._cooking.get("id") == r["id"]
            self._cooking = {"id": r["id"], "version": "original" if version == "original" else "ours",
                             "servings": servings if servings and 0 < servings <= 500 else r["servings"],
                             "step": step, "at": self._clock(),
                             "timer_end": self._cooking.get("timer_end") if same else None,
                             "timer_min": self._cooking.get("timer_min") if same else None}
        return True, "Cooking."

    def stop_cooking(self) -> None:
        with self._lock:
            self._cooking = {}
        self.stop_timer()

    def _cook_doc(self) -> Optional[Tuple[Dict[str, Any], sqlite3.Row, Dict[str, Any]]]:
        with self._lock:
            c = dict(self._cooking)
        if not c or self._clock() - c["at"] > COOKING_IDLE_S:
            return None
        r = self._row(c["id"])
        if r is None:
            return None
        doc = json.loads(r["ours"]) if c["version"] == "ours" and r["ours"] else json.loads(r["original"])
        return c, r, doc

    def cooking(self) -> bool:
        return self._cook_doc() is not None

    def cooking_state(self) -> Dict[str, Any]:
        found = self._cook_doc()
        if found is None:
            return {"cooking": False}
        c, _r, _doc = found
        left = max(0, round(c["timer_end"] - self._clock())) if c.get("timer_end") else None
        return {"cooking": True, "id": c["id"], "version": c["version"], "step": c["step"],
                "servings": c["servings"], "timer_left": left, "timer_min": c.get("timer_min")}

    def _factor(self, c: Dict[str, Any], r: sqlite3.Row) -> float:
        return (c["servings"] / r["servings"]) if c.get("servings") and r["servings"] else 1.0

    def _step_say(self, doc: Dict[str, Any], n: int) -> str:
        return f"Step {n + 1} of {len(doc['steps'])}. {doc['steps'][n]['t']}"

    def answer_cooking(self, command: Tuple[str, Any], spoken: bool = True) -> str:
        found = self._cook_doc()
        if found is None:
            return "No recipe is open in cooking mode. Open one on the Recipes page and tap Start cooking."
        c, r, doc = found
        kind, detail = command
        steps = doc["steps"]
        if kind in ("next", "prev", "goto", "repeat"):
            if not steps:
                return "This recipe has no steps."
            n = c["step"]
            if kind == "next":
                if n >= len(steps) - 1:
                    return "That was the last step. Enjoy!"
                n += 1
            elif kind == "prev":
                if n == 0:
                    return "You're on the first step. " + self._step_say(doc, 0)
                n -= 1
            elif kind == "goto":
                n = len(steps) - 1 if detail == -1 else detail - 1
                if not 0 <= n < len(steps):
                    return f"This recipe has {len(steps)} steps."
            with self._lock:
                if self._cooking.get("id") == c["id"]:
                    self._cooking.update(step=n, at=self._clock())
            return self._step_say(doc, n)
        factor = self._factor(c, r)
        if kind == "ingredients":
            items = [amount_text(i, factor, spoken) for i in doc["ingredients"]]
            return "You need: " + "; ".join(items) + "." if items else "This recipe has no ingredients listed."
        if kind == "how_much":
            hits = [i for i in doc["ingredients"] if any(re.search(r"\b" + re.escape(w.rstrip("s")), i["item"].lower())
                                                         for w in detail)]
            if not hits:
                return f"I don't see {' '.join(detail)} in this recipe."
            return "; ".join(amount_text(i, factor, spoken) for i in hits[:3]) + "."
        if kind == "timer":
            mins = detail
            if mins is None and steps:
                m = re.search(r"(\d+)(?:\s*(?:to|-|–)\s*\d+)?\s*min", steps[c["step"]]["t"], re.I)
                mins = int(m.group(1)) if m else None
            if not mins:
                return "How long? Say, for example, set a timer for 10 minutes."
            return self.start_timer(mins)
        if kind == "timer_left":
            state = self.cooking_state()
            left = state.get("timer_left")
            if not left:
                return "There's no timer running."
            m, s = divmod(left, 60)
            return f"{m} minute{'s' if m != 1 else ''} and {s} seconds left." if m else f"{s} seconds left."
        if kind == "timer_stop":
            return "Timer cancelled." if self.stop_timer() else "There's no timer running."
        return ""

    def start_timer(self, mins: int) -> str:
        mins = max(1, min(int(mins), 600))
        self.stop_timer()
        with self._lock:
            if not self._cooking:
                return "Open a recipe in cooking mode first."
            self._cooking.update(timer_end=self._clock() + mins * 60, timer_min=mins, at=self._clock())
            self._timer = threading.Timer(mins * 60, self._timer_done, args=(mins,))
            self._timer.daemon = True
            self._timer.start()
        return f"Okay, {mins} minute{'s' if mins != 1 else ''}, starting now."

    def stop_timer(self) -> bool:
        with self._lock:
            had = self._timer is not None
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self._cooking:
                self._cooking.update(timer_end=None, timer_min=None)
        return had

    def _timer_done(self, mins: int) -> None:
        with self._lock:
            self._timer = None
        try:
            self._speak(f"Your {mins} minute timer is done.")
        except Exception:
            pass
