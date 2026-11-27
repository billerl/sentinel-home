"""The offline helper (sentinel/engine/offline.py): library search, what the AI is given, and the fallbacks."""

import _test_env  # noqa: F401  (isolates the event log)

import threading
import time
import types
import unittest

from sentinel.engine.offline import OfflineHelper, best_paragraphs, library_path

SEARCH = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Search</title>
<item><title>Highest Voted 'water' Questions</title><link>/library/content/outdoors_en/questions/tagged/water</link>
  <description>a list</description><book><title>The Great Outdoors Q&amp;A</title></book></item>
<item><title>How do you make an emergency water filter?</title>
  <link>/library/content/outdoors_en/questions/1/emergency-filter</link>
  <description>...<b>boil</b> the <b>water</b> for a minute...</description>
  <book><title>The Great Outdoors Q&amp;A</title></book></item>
<item><title>Water purification</title><link>/library/content/wikipedia_en/A/Water_purification</link>
  <description>Water purification is the process of removing...</description>
  <book><title>Wikipedia</title></book></item>
<item><title>questions/tagged/water_page=2</title><link>/library/content/outdoors_en/questions/tagged/water_page%3D2</link>
  <description>x</description></item>
<item><title>Evil</title><link>javascript:alert(1)</link><description>x</description></item>
</channel></rss>"""

ARTICLE = b"""<html><head><script>var x = 1;</script></head><body><nav><p>Menu menu menu menu menu menu menu</p></nav>
<p>Boiling is the oldest way to make water safe: bring it to a rolling boil for one minute, three at altitude.</p>
<table><tr><td>a table that should not be read at all, ever</td></tr></table>
<p>Cats are small furry animals that have nothing at all to do with this question.</p>
<p>Chlorine tablets also make water safe to drink; follow the packet for how long to wait.</p></body></html>"""


def fake_fetch(asked):
    def fetch(url, body=None, timeout=10):
        asked.append(url)
        if "/api/version" in url or "root.xml" in url:
            return b"{}"
        if "/catalog/v2/entries" in url:
            return (b'<feed><entry><link type="text/html" href="/library/content/wikipedia_en_all_maxi_2026-08"/></entry>'
                    b'<entry><link type="text/html" href="/library/content/mdwiki_en_all_maxi_2025-11"/></entry>'
                    b'<entry><link type="text/html" href="/library/content/outdoors_en"/></entry>'
                    b'<entry><link type="text/html" href="/library/content/lowtech_mul_all"/></entry></feed>')
        if "/search?" in url and "books.name=mdwiki" in url and "outdoors_en" not in url:
            return SEARCH.replace(b"Water purification</title>", b"Burn</title>")
        if "/search?" in url:
            return SEARCH
        if "/content/" in url:
            return ARTICLE
        raise OSError("not here")
    return fetch


class TestLibrary(unittest.TestCase):
    def setUp(self):
        self.asked = []
        self.helper = OfflineHelper({}, fetch=fake_fetch(self.asked))

    def test_only_real_library_pages_are_kept(self):
        self.assertEqual(library_path("/library/content/wikipedia_en/A/Water"), "/library/content/wikipedia_en/A/Water")
        self.assertIsNone(library_path("javascript:alert(1)"))
        self.assertIsNone(library_path("https://example.com/library/content/x/y"))
        self.assertIsNone(library_path('/library/content/x/"><script>'))

    def test_search_skips_lists_and_strange_links(self):
        found = self.helper.search("how do I make water safe")
        self.assertEqual([f["title"] for f in found], ["How do you make an emergency water filter?", "Water purification"])
        self.assertEqual(found[0]["book"], "The Great Outdoors Q&A")
        self.assertIn("boil the water", found[0]["snippet"])

    def test_the_paragraphs_that_match_the_question_and_no_menus_tables_or_scripts(self):
        text = best_paragraphs(self.helper.article("/library/content/outdoors_en/questions/1/emergency-filter"),
                               "how do I make water safe to drink", 400)
        self.assertIn("rolling boil", text)
        self.assertIn("Chlorine", text)
        self.assertNotIn("Cats", text)
        self.assertNotIn("should not be read", text)
        self.assertNotIn("Menu", text)
        self.assertNotIn("var x", text)
        self.assertLess(text.index("rolling boil"), text.index("Chlorine"))  # (in the article's order)

    def test_plain_text(self):
        from sentinel.engine.offline import clean_reply

        self.assertEqual(clean_reply("**Wash it.**\n* Ice [Wikipedia]\n\n\n\nDone"), "Wash it.\n• Ice [Wikipedia]\n\nDone")
        self.assertEqual(clean_reply("a dandelion (*Taraxacum officinale*), 2*3"), "a dandelion (Taraxacum officinale), 2*3")
        self.assertEqual(clean_reply("The library says boil it [The Great Outdoors Q&A].", voice=True),
                         "The library says boil it.")

    def test_encyclopedias_and_guides_first_and_the_medical_ones_for_health(self):
        self.helper.excerpts("how do I treat a minor burn", 300)
        searches = [u for u in self.asked if "/search?" in u]
        self.assertIn("books.name=mdwiki_en_all_maxi_2025-11", searches[0])
        self.assertNotIn("wikipedia", searches[0])
        self.assertIn("books.name=outdoors_en", searches[1])  # then everything...
        self.assertNotIn("_mul_", searches[1])  # ...in English only (kiwix refuses a search across languages)
        self.asked.clear()
        _, sources = self.helper.excerpts("how do I tie a bowline", 300)
        searches = [u for u in self.asked if "/search?" in u]
        self.assertIn("books.name=wikipedia_en_all_maxi_2026-08", searches[0])
        self.assertIn("books.name=mdwiki", searches[0])
        self.assertNotIn("outdoors_en", searches[0])
        self.assertEqual(len({s["link"] for s in sources}), len(sources))  # (no page twice)

    def test_the_budget_is_kept(self):
        text, sources = self.helper.excerpts("water safe", 300)
        self.assertLessEqual(len(text), 700)
        self.assertEqual(len(sources), 2)


class TestAnswers(unittest.TestCase):
    def setUp(self):
        self.asked, self.chats = [], []
        self.helper = OfflineHelper({}, fetch=fake_fetch(self.asked))

        def chat(messages, max_tokens, on_text=None):
            self.chats.append((messages, max_tokens))
            if "only the most likely name" in messages[-1]["content"]:
                return "Wild garlic (Allium ursinum)"
            if on_text:
                on_text("Boil it")
            return "Boil it for one minute [The Great Outdoors Q&A]."
        self.helper._chat = chat

    def test_the_ai_reads_the_library_then_answers(self):
        said, sources = self.helper.answer("How do I make water safe?")
        self.assertEqual(said, "Boil it for one minute [The Great Outdoors Q&A].")
        self.assertEqual(len(sources), 2)
        messages, tokens = self.chats[-1]
        self.assertIn("NEVER tell them something is safe to eat", messages[0]["content"])
        self.assertIn("rolling boil", messages[-1]["content"])
        self.assertIn("QUESTION:\nHow do I make water safe?", messages[-1]["content"])
        self.assertEqual(tokens, 420)

    def test_only_sources_it_really_read(self):
        self.helper._chat = lambda messages, max_tokens, on_text=None: "Boil it [Wikipedia] [Household Repairs Guide]."
        said, _ = self.helper.answer("How do I make water safe?")
        self.assertEqual(said, "Boil it [Wikipedia].")
        self.helper._chat = lambda messages, max_tokens, on_text=None: "Boil it [How do you make an emergency water filter], page 3."
        said, _ = self.helper.answer("How do I make water safe?")
        self.assertEqual(said, "Boil it [How do you make an emergency water filter], page 3.")  # (a page it read, named by its title)
        self.helper._chat = lambda messages, max_tokens, on_text=None: "Boil it [Wikipedia] [Household Repairs Guide]."
        self.helper.search = lambda *a, **k: []
        said, _ = self.helper.answer("How do I make water safe?")
        self.assertEqual(said, "Boil it.\n\n(Nothing in the library matched, so this is from the AI's own memory.)")

    def test_spoken_answers_are_short_and_plain(self):
        self.helper.answer("How do I make water safe?", voice=True, history=[("hi", "hello")])
        messages, tokens = self.chats[-1]
        self.assertIn("spoken aloud", messages[0]["content"])
        self.assertEqual(tokens, 140)
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "user"])

    def test_a_photo_is_named_first_and_the_library_searched_for_that_name(self):
        said, _ = self.helper.answer("", jpeg=b"\xff\xd8photo")
        self.assertTrue(said)
        self.assertIn("images", self.chats[0][0][-1])
        self.assertTrue(any("wild+garlic+allium+ursinum" in u for u in self.asked if "/search?" in u))
        self.assertIn("a first look at it says it shows Wild garlic", self.chats[-1][0][-1]["content"])
        self.assertNotIn("images", self.chats[-1][0][-1])  # (looked at once: the second pass reads the name)

    def test_eating_wild_things_always_gets_the_field_guide_warning(self):
        said, _ = self.helper.answer("Are these berries edible?")
        self.assertTrue(said.endswith("check a field guide for your area or ask an expert first."))
        said, _ = self.helper.answer("", jpeg=b"\xff\xd8photo")
        self.assertIn("field guide", said)
        said, _ = self.helper.answer("How do I make water safe?")
        self.assertNotIn("field guide", said)

    def test_no_ai_no_answer(self):
        helper = OfflineHelper({}, fetch=lambda url, body=None, timeout=10: (_ for _ in ()).throw(OSError("down")))
        self.assertEqual(helper.answer("anything"), (None, []))
        self.assertIn("isn't running", helper.last_error)
        self.assertFalse(helper.start("anything")[0])
        self.assertEqual(OfflineHelper({"offline_helper_enabled": False}, fetch=fake_fetch([])).answer("x"), (None, []))

    def test_the_page_starts_a_question_and_asks_after_it(self):
        ok, job_id = self.helper.start("How do I make water safe?")
        self.assertTrue(ok)
        for _ in range(100):
            job = self.helper.job(job_id)
            if job["state"] != "working":
                break
            time.sleep(0.02)
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["text"], "Boil it for one minute [The Great Outdoors Q&A].")
        self.assertEqual(job["sources"][0], {"title": "How do you make an emergency water filter?",
                                             "book": "The Great Outdoors Q&A",
                                             "link": "/library/content/outdoors_en/questions/1/emergency-filter"})
        self.assertIsNone(self.helper.job("nope"))

    def test_one_question_at_a_time(self):
        release = threading.Event()
        self.helper._chat = lambda messages, max_tokens, on_text=None: release.wait(5) and "done"
        self.assertTrue(self.helper.start("first")[0])
        time.sleep(0.1)
        ok, said = self.helper.start("second")
        self.assertFalse(ok)
        self.assertIn("another question", said)
        release.set()


class TestFallbacks(unittest.TestCase):
    """Claude couldn't answer: Telegram and the voice get the offline helper's answer instead."""

    def setUp(self):
        from sentinel.engine.engine import Engine

        self.sent = []
        self.engine = types.SimpleNamespace(
            assistant=types.SimpleNamespace(enabled=lambda: True, remaining_today=lambda: 5, last_error="",
                                            ask=lambda *a, **k: None),
            offline=types.SimpleNamespace(ai_ready=lambda: True, answer=lambda text, **k: (
                "Boil it for a minute.", [{"title": "Water purification", "book": "Wikipedia", "link": "/x"}])),
            telegram=types.SimpleNamespace(reply_to_chat=lambda chat, text: self.sent.append(text),
                                           commands_help_text=lambda: "HELP"),
            assistant_context=lambda: "")
        self.engine.assistant.ask = lambda *a, **k: setattr(self.engine.assistant, "last_error",
                                                            "the internet seems to be down")
        self.telegram = lambda text: Engine.answer_with_assistant(self.engine, "1", text)
        self.voice = lambda text: Engine._offline_voice(self.engine, text, lambda s: None, said_first=True)

    def test_telegram(self):
        self.telegram("how do I make water safe to drink?")
        self.assertIn("offline helper", self.sent[-2])
        self.assertEqual(self.sent[-1], "🧭 Offline: Boil it for a minute.\n\n📖 Water purification (Wikipedia)")

    def test_telegram_without_the_offline_ai_gets_the_old_answer(self):
        self.engine.offline.ai_ready = lambda: False
        self.telegram("how do I make water safe to drink?")
        self.assertIn("Claude couldn't help: the internet seems to be down", self.sent[-1])

    def test_voice(self):
        self.assertEqual(self.voice("how do I make water safe").text, "Boil it for a minute.")
        self.engine.offline.ai_ready = lambda: False
        self.assertIsNone(self.voice("how do I make water safe"))


if __name__ == "__main__":
    unittest.main()


class TestLibraryPages(unittest.TestCase):
    """/library/... is the library server's page, for signed-in family only."""

    def setUp(self):
        import http.server

        from flask import Flask

        from sentinel.web.routes import offline as routes

        class Library(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/library/content/wiki/Water"):
                    body = b"<p>Water is wet.</p>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Set-Cookie", "x=1")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Library)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.signed_in = True
        base = f"http://127.0.0.1:{self.server.server_port}/library"
        engine = types.SimpleNamespace(offline=types.SimpleNamespace(library_base=lambda: base))
        ctx = types.SimpleNamespace(engine=engine,
                                    require_session=lambda: None if self.signed_in else ("login", 302))
        app = Flask(__name__)
        routes.register(app, ctx)
        self.client = app.test_client()

    def test_a_page_comes_through_without_the_library_servers_cookies(self):
        r = self.client.get("/library/content/wiki/Water?x=1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, b"<p>Water is wet.</p>")
        self.assertNotIn("Set-Cookie", r.headers)
        self.assertEqual(self.client.get("/library/content/wiki/Nope").status_code, 404)

    def test_not_signed_in(self):
        self.signed_in = False
        self.assertEqual(self.client.get("/library/content/wiki/Water").status_code, 302)
