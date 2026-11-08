"""Recipes: reading recipe pages and cooking videos, the family's own version, swaps and twists, what can I make,
and cooking mode with Jarvis following along."""

import _test_env  # noqa: F401  (isolates the event log)

import json
import os
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import cv2
import numpy as np

from sentinel.engine import recipes as R

PUBLIC = lambda host, _port: [(2, 1, 6, "", ("93.184.216.34", 0))]  # noqa: E731
HOME = lambda host, _port: [(2, 1, 6, "", ("192.168.1.20", 0))]  # noqa: E731

RECIPE_PAGE = """<html><head><script type="application/ld+json">{"@context": "https://schema.org", "@graph": [
  {"@type": "WebPage", "name": "Lemon chicken"},
  {"@type": ["Recipe"], "name": "Sheet-Pan Lemon Chicken &amp; Potatoes", "image": [{"url": "https://cdn.example.com/c.jpg"}],
   "recipeYield": ["4", "4 servings"], "prepTime": "PT15M", "cookTime": "PT45M", "recipeCategory": "Dinner",
   "recipeIngredient": ["1 1/2 lb baby potatoes, halved", "2 lb bone-in chicken thighs", "3 tbsp olive oil",
                        "4 cloves garlic, minced", "1 lemon, zested and juiced", "Fresh parsley, to serve"],
   "recipeInstructions": [{"@type": "HowToSection", "name": "Potatoes", "itemListElement": [
       {"@type": "HowToStep", "text": "Heat the oven to 425&deg;F."},
       {"@type": "HowToStep", "text": "Roast the potatoes for 10 minutes."}]},
     {"@type": "HowToStep", "text": "Add the chicken and roast for 20 minutes."},
     {"@type": "HowToStep", "text": "Rest for 5 minutes and serve with parsley."}]}]}
</script></head><body>A long story about lemons.</body></html>"""


def jpeg() -> bytes:
    img = np.zeros((40, 60, 3), np.uint8)
    img[:, :, 2] = 200
    return cv2.imencode(".jpg", img)[1].tobytes()


class FakeClaude:
    def __init__(self, replies):
        self.replies = list(replies)
        self.asked = []
        self.last_error = ""

    def enabled(self):
        return True

    def complete(self, system, prompt, what):
        self.asked.append((what, prompt))
        return json.dumps(self.replies.pop(0)) if self.replies else None


class TestReading(unittest.TestCase):
    def test_ingredient_lines(self):
        p = R.parse_ingredient
        self.assertEqual(p("1 1/2 cups flour, sifted"), {"q": 1.5, "u": "cup", "item": "flour, sifted"})
        self.assertEqual(p("1½ lb potatoes"), {"q": 1.5, "u": "lb", "item": "potatoes"})
        self.assertEqual(p("½ tsp salt"), {"q": 0.5, "u": "tsp", "item": "salt"})
        self.assertEqual(p("2 T butter")["u"], "tbsp")
        self.assertEqual(p("1-2 cloves garlic"), {"q": 1.0, "u": "clove", "item": "garlic", "q2": 2.0})
        self.assertEqual(p("1 (15 oz) can black beans, drained")["u"], "(15 oz) can")
        self.assertEqual(p("400g pasta"), {"q": 400.0, "u": "g", "item": "pasta"})
        self.assertEqual(p("1 tomato, diced"), {"q": 1.0, "u": "", "item": "tomato, diced"})  # "to" isn't a range
        self.assertEqual(p("Salt and pepper to taste"), {"q": None, "u": "", "item": "Salt and pepper to taste"})

    def test_durations_and_amounts(self):
        self.assertEqual((R.minutes("PT1H30M"), R.minutes("P0DT45M"), R.minutes("PT0S"), R.minutes("")), (90, 45, None, None))
        self.assertEqual([R.fmt_amount(x) for x in (0.5, 1.333, 0.125, 2, 3.75)], ["½", "1⅓", "⅛", "2", "3¾"])
        self.assertEqual(R.amount_text({"q": 1.5, "u": "cup", "item": "flour"}, 2, spoken=True), "3 cups flour")
        self.assertEqual(R.amount_text({"q": 0.5, "u": "tsp", "item": "salt"}, 1, spoken=True), "a half teaspoon salt")

    def test_schema_recipe_with_sections_and_a_graph(self):
        node = R.recipe_data(RECIPE_PAGE)
        recipe = R.from_recipe_data(node, "https://site.example/lemon")
        self.assertEqual(recipe["title"], "Sheet-Pan Lemon Chicken & Potatoes")
        self.assertEqual((recipe["servings"], recipe["prep_min"], recipe["cook_min"], recipe["total_min"]), (4, 15, 45, 60))
        self.assertEqual(recipe["category"], "Dinners")
        self.assertEqual(recipe["image"], "https://cdn.example.com/c.jpg")
        steps = [s["t"] for s in recipe["doc"]["steps"]]
        self.assertEqual(steps[0], "Potatoes: Heat the oven to 425°F.")
        self.assertEqual(len(steps), 4)
        self.assertEqual(recipe["doc"]["ingredients"][3], {"q": 4.0, "u": "clove", "item": "garlic, minced", "id": "i4"})

    def test_categories(self):
        self.assertEqual(R.guess_category(None, "Sparkling Cranberry Punch"), "Beverages")
        self.assertEqual(R.guess_category(["Dessert"], "Grandma's thing"), "Desserts")
        self.assertEqual(R.guess_category(None, "Overnight Oats"), "Breakfast")
        self.assertEqual(R.guess_category(None, "Mystery"), "Dinners")

    def test_only_public_websites(self):
        """A link must not reach this PC, the home network or a camera."""
        get = mock.Mock()
        with self.assertRaisesRegex(ValueError, "public website"):
            R.fetch("http://192.168.1.20/admin", 1000, get=get, resolve=HOME)
        with self.assertRaisesRegex(ValueError, "web link"):
            R.fetch("file:///etc/passwd", 1000, get=get, resolve=PUBLIC)
        redirect = SimpleNamespace(status_code=302, headers={"Location": "http://camera.local/"}, close=lambda: None)
        hosts = iter([PUBLIC, HOME])
        with self.assertRaisesRegex(ValueError, "public website"):
            R.fetch("https://site.example/r", 1000, get=lambda *a, **k: redirect,
                    resolve=lambda h, p: next(hosts)(h, p))
        get.assert_not_called()

    def test_telegram_and_voice_phrases(self):
        c = R.cooking_command
        self.assertEqual(c("Next step."), ("next", None))
        self.assertEqual(c("Okay, next step"), ("next", None))
        self.assertEqual(c("go back a step"), ("prev", None))
        self.assertEqual(c("How much garlic?"), ("how_much", ["garlic"]))
        self.assertEqual(c("Set a timer for 10 minutes"), ("timer", 10))
        self.assertEqual(c("set a five minute timer"), ("timer", 5))
        self.assertEqual(c("start the timer"), ("timer", None))
        self.assertEqual(c("how much longer"), ("timer_left", None))
        self.assertEqual(c("go to step 3"), ("goto", 3))
        self.assertIsNone(c("turn on the patio lights"))
        self.assertEqual(R.can_make_command("What can I make with chicken and rice?"), ["chicken", "rice"])
        self.assertEqual(R.save_link_command("https://site.example/r"), "https://site.example/r")
        self.assertEqual(R.save_link_command("save this recipe https://site.example/r"), "https://site.example/r")
        self.assertIsNone(R.save_link_command("look at this https://site.example/r"))
        self.assertTrue(R.is_video_link("https://www.instagram.com/reel/CnkNjXVsAJv"))
        self.assertTrue(R.is_video_link("https://www.tiktok.com/@cook/video/123"))
        self.assertFalse(R.is_video_link("https://www.allrecipes.com/recipe/1"))


class BoxTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sentinel-recipes-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.fetched = []
        self.claude = FakeClaude([])
        self.spoken = []
        self.box = R.RecipeBox(self.dir, self.claude, speak=self.spoken.append, fetcher=self.fetch)

    def fetch(self, url, max_bytes):
        self.fetched.append(url)
        if url.endswith(".jpg"):
            return url, "image/jpeg", jpeg()
        return url, "text/html", RECIPE_PAGE.encode()

    def saved(self):
        ok, message, rid = self.box.save_link("https://site.example/lemon", "Kate")
        self.assertTrue(ok, message)
        return rid


class TestBox(BoxTest):
    def test_save_a_link_keeps_the_recipe_and_its_photo(self):
        rid = self.saved()
        r = self.box.recipe(rid)
        self.assertEqual((r["title"], r["saved_by"], r["site"], r["category"]), ("Sheet-Pan Lemon Chicken & Potatoes", "Kate",
                                                                                 "site.example", "Dinners"))
        self.assertTrue(r["photo"].startswith(f"/recipes/photo/{rid}"))
        self.assertTrue(os.path.isfile(self.box.photo_file(rid)))
        self.assertFalse(r["video"])
        ok, message, again = self.box.save_link("https://site.example/lemon")
        self.assertEqual((ok, again), (True, rid))
        self.assertIn("already", message)

    def test_family_version_beside_the_original(self):
        rid = self.saved()
        doc = self.box.recipe(rid)["original"]
        doc["ingredients"][3]["item"] = "garlic powder"
        doc["ingredients"][3].update(q=0.5, u="tsp")
        doc["notes"] = "Kids liked it."
        self.assertTrue(self.box.save_version(rid, doc, "Kate")[0])
        r = self.box.recipe(rid)
        self.assertEqual(r["ours"]["ingredients"][3]["item"], "garlic powder")
        self.assertEqual(r["original"]["ingredients"][3]["item"], "garlic, minced")
        self.assertEqual((r["ours_by"], self.box.recipes()[0]["ours"]), ("Kate", True))
        self.assertTrue(self.box.revert(rid)[0])
        self.assertIsNone(self.box.recipe(rid)["ours"])
        self.assertFalse(self.box.save_version(rid, {"ingredients": "x"})[0])  # not a recipe

    def test_own_photo_category_and_delete(self):
        rid = self.saved()
        import base64
        self.assertTrue(self.box.set_photo(rid, "data:image/jpeg;base64," + base64.b64encode(jpeg()).decode())[0])
        self.assertTrue(self.box.photo_file(rid).endswith("-photo.jpg"))
        self.assertFalse(self.box.set_photo(rid, base64.b64encode(b"not a picture").decode())[0])
        self.assertTrue(self.box.set_category(rid, "Sides")[0])
        self.assertFalse(self.box.set_category(rid, "Nonsense")[0])
        self.assertTrue(self.box.delete(rid)[0])
        self.assertEqual((self.box.recipes(), os.listdir(os.path.join(self.dir, "recipes"))), ([], []))

    def test_what_can_i_make(self):
        self.saved()
        found = self.box.can_make(["chicken", "lemon"])
        self.assertEqual(found[0]["got"], ["bone-in chicken thighs", "lemon"])
        self.assertIn("Sheet-Pan Lemon Chicken", self.box.answer_can_make(["chicken", "lemon"]))
        self.assertIn("Nothing", self.box.answer_can_make(["tofu"]))

    def test_page_without_recipe_data_goes_to_claude(self):
        page = "<html><body>" + "Our family loves this. " * 20 + "Ingredients\n2 cups rice\nSteps: cook it.</body></html>"
        self.box._fetch = lambda url, n: (url, "text/html", page.encode())
        self.claude.replies = [{"title": "Rice", "servings": 2, "category": "Sides", "ingredients": ["2 cups rice"],
                                "steps": ["Cook it."]}]
        ok, message, rid = self.box.save_link("https://blog.example/rice")
        self.assertTrue(ok, message)
        self.assertEqual(self.box.recipe(rid)["original"]["ingredients"][0]["item"], "rice")
        self.assertEqual(self.claude.asked[0][0], "recipe_read")

    def test_swap_and_twist(self):
        rid = self.saved()
        doc = self.box.recipe(rid)["original"]
        self.claude.replies = [{"options": [{"q": 2, "u": "lb", "item": "boneless chicken thighs", "why": "Quicker.",
                                             "steps": [{"n": 3, "text": "Add the chicken and roast for 15 minutes."}]}]},
                               {"say": "Added chili flakes.", "ingredients": [dict(i) for i in doc["ingredients"]] +
                                [{"id": "new", "q": 0.5, "u": "tsp", "item": "chili flakes"}], "steps": [s["t"] for s in doc["steps"]]}]
        ok, options = self.box.swap(rid, doc, "i2")
        self.assertTrue(ok, options)
        self.assertEqual(options[0]["steps"], [{"n": 2, "text": "Add the chicken and roast for 15 minutes."}])
        self.assertIn("i2: 2 lb bone-in chicken thighs", self.claude.asked[0][1])
        ok, out = self.box.twist(rid, doc, "make it spicier")
        self.assertTrue(ok, out)
        self.assertEqual(out["doc"]["ingredients"][-1]["item"], "chili flakes")
        self.assertNotEqual(out["doc"]["ingredients"][-1]["id"], "new")
        self.assertEqual(out["doc"]["ingredients"][0]["id"], "i1")


class TestVideos(BoxTest):
    def test_a_reel_is_downloaded_listened_to_and_written_up(self):
        def download(url, folder):
            path = os.path.join(folder, "video.mp4")
            open(path, "wb").close()
            return {"description": "FAJITA BOMBS! Full recipe via the link in bio", "channel": "pinchofnom",
                    "uploader_id": "2514130654", "thumbnail": "https://cdn.example.com/cover.jpg", "duration": 60}, path
        self.claude.replies = [{"title": "Fajita Bombs", "servings": 4, "category": "Dinners",
                                "ingredients": ["1 lb chicken breast (est.)", "4 tortillas"], "steps": ["Bake for 20 minutes."]}]
        with mock.patch.object(R, "download_video", download), mock.patch.object(R, "listen", lambda p: "Pop the wraps in"), \
                mock.patch.object(R, "read_screen", lambda p: "650g CHICKEN BREAST"), \
                mock.patch.object(R, "_public", lambda h, *_a: True):
            ok, message, rid = self.box.save_link("https://www.instagram.com/reel/CnkNjXVsAJv", "Kate")
        self.assertTrue(ok, message)
        r = self.box.recipe(rid)
        self.assertTrue(r["video"])
        self.assertEqual(r["url"], "https://www.instagram.com/reel/CnkNjXVsAJv")  # the reel, to watch again
        self.assertIn("@pinchofnom", r["original"]["notes"])
        self.assertIn("(est.)", r["original"]["notes"])
        self.assertIn("Pop the wraps in", self.claude.asked[0][1])
        self.assertIn("650g CHICKEN BREAST", self.claude.asked[0][1])  # no amounts said: the screen was read too
        self.assertIn("cover.jpg", self.fetched[-1])
        self.assertEqual([d for d in os.listdir(os.path.join(self.dir, "recipes")) if d.startswith("video-")], [])

    def test_amounts_said_skip_reading_the_screen(self):
        self.assertEqual(R.amounts_in("2 cups flour, 1/2 tsp salt and 400g pasta"), 3)
        self.assertEqual(R.amounts_in("Full recipe in bio! Fry for 3 minutes"), 0)

    def test_no_recipe_in_the_video(self):
        self.claude.replies = [{"none": True, "why": "it only says comment RECIPE"}]
        with mock.patch.object(R, "download_video", lambda u, f: ({"description": "Comment RECIPE and I'll DM you the link!! #dinner #easyrecipes"}, None)), \
                mock.patch.object(R, "_public", lambda h, *_a: True):
            ok, message, _rid = self.box.save_link("https://www.instagram.com/reel/X1")
        self.assertFalse(ok)
        self.assertIn("comment RECIPE", message)

    def test_pasted_text_in_the_background(self):
        self.claude.replies = [{"title": "Punch", "category": "Beverages", "ingredients": ["2 cups cranberry juice"],
                                "steps": ["Stir."]}]
        done = []
        job = self.box.start_save("https://www.instagram.com/reel/X2", "Kate", "Punch: 2 cups cranberry juice, stir it all.",
                                  on_done=lambda *a: done.append(a))
        for _ in range(200):
            if done:
                break
            time.sleep(0.01)
        self.assertTrue(done and done[0][0], done)
        self.assertEqual(self.box.job(job)["state"], "done")
        r = self.box.recipe(done[0][2])
        self.assertEqual((r["category"], r["url"]), ("Beverages", "https://www.instagram.com/reel/X2"))


class TestCooking(BoxTest):
    def test_jarvis_follows_cooking_mode(self):
        rid = self.saved()
        self.assertIn("No recipe is open", self.box.answer_cooking(("next", None)))
        self.box.start_cooking(rid, "ours", servings=8)
        self.assertTrue(self.box.cooking())
        self.assertEqual(self.box.answer_cooking(("next", None)), "Step 2 of 4. Roast the potatoes for 10 minutes.")
        self.assertEqual(self.box.cooking_state()["step"], 1)  # the page follows
        self.assertTrue(self.box.answer_cooking(("prev", None)).startswith("Step 1 of 4"))
        self.assertEqual(self.box.answer_cooking(("how_much", ["garlic"])), "8 cloves garlic, minced.")  # doubled
        self.assertIn("3 pounds baby potatoes", self.box.answer_cooking(("ingredients", None)))
        self.box.answer_cooking(("goto", 2))
        self.assertEqual(self.box.answer_cooking(("timer", None)), "Okay, 10 minutes, starting now.")
        self.assertGreater(self.box.cooking_state()["timer_left"], 590)
        self.assertEqual(self.box.answer_cooking(("timer_stop", None)), "Timer cancelled.")
        self.box._timer_done(10)
        self.assertEqual(self.spoken, ["Your 10 minute timer is done."])
        self.box.stop_cooking()
        self.assertFalse(self.box.cooking())


class TestRoutes(unittest.TestCase):
    def test_signed_out_gets_nothing(self):
        from flask import Flask

        from sentinel.web.context import WebContext
        from sentinel.web.routes import recipes

        server = SimpleNamespace(_settings=None, _broker=None, _recordings=None, _engine=SimpleNamespace(), _auth=None,
                                 _limiter=None)
        app = Flask(__name__)
        app.add_url_rule("/login", "login", lambda: "sign in")
        recipes.register(app, WebContext(server, {}))
        client = app.test_client()
        self.assertEqual(client.get("/api/recipes").status_code, 401)
        self.assertEqual(client.post("/api/recipes/save", json={"url": "https://x.example"},
                                     headers={"X-Sentinel-Request": "1"}).status_code, 401)
        self.assertEqual(client.get("/recipes/photo/1").status_code, 302)


if __name__ == "__main__":
    unittest.main()
