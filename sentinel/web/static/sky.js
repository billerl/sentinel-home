// A picture of the sky behind the weather cards (live grid / TV tile and the home page card): blue, overcast,
// rain, snow, fog or storm, and day, dawn, dusk or night from this page's own clock. sky.css draws it.
// Plain ES5: a 2018 Samsung TV's browser shows the live grid.
(function() {
  var LAND = '<svg class="sky-land" viewBox="0 0 400 40" preserveAspectRatio="xMidYMax slice">' +
    '<path class="far" d="M0 21C30 15 60 13 95 18S160 10 205 15 290 7 330 13 380 16 400 12V40H0Z"/>' +
    '<path class="near" d="M0 31C40 25 80 26 120 29S200 22 250 27 330 22 400 27V40H0Z' +
    'M38 27l4-11 4 11ZM46 26.5l3-8 3 8ZM301 25l4-12 4 12ZM310 24.5l3.5-9 3.5 9ZM360 24.5l3-8 3 8Z"/></svg>';

  function make() {
    var el = document.createElement("div");
    el.className = "wx-sky";
    el.setAttribute("aria-hidden", "true");
    el.innerHTML = '<div class="sky-stars"></div><div class="sky-sun"></div><div class="sky-moon"></div>' +
      '<div class="sky-cloud far"></div><div class="sky-cloud near"></div><div class="sky-mist"></div>' +
      '<div class="sky-fall a"></div><div class="sky-fall b"></div><div class="sky-flash"></div>' + LAND +
      '<div class="sky-shade"></div>';
    return el;
  }

  // "day", "dawn", "dusk" or "night": half an hour either side of sunrise/sunset is dawn/dusk
  function phase(w) {
    var now = new Date(), m = now.getHours() * 60 + now.getMinutes();
    var sun = w.sun_min || [], rise = sun[0] == null ? 420 : sun[0], set = sun[1] == null ? 1080 : sun[1];
    if (Math.abs(m - rise) <= 30) return "dawn";
    if (Math.abs(m - set) <= 30) return "dusk";
    return m > rise && m < set ? "day" : "night";
  }

  // Sets the card's classes (has-sky sky-<kind> ph-<phase>); none while there's no weather to show.
  function update(card, w) {
    var want = w && w.configured !== false && w.temp != null
      ? ["has-sky", "sky-" + (w.sky || "clear"), "ph-" + phase(w)] : [];
    var have = (card.getAttribute("data-sky") || "").split(" ").filter(Boolean);
    if (have.join(" ") === want.join(" ")) return;
    have.forEach(function(c) { card.classList.remove(c); });
    want.forEach(function(c) { card.classList.add(c); });
    card.setAttribute("data-sky", want.join(" "));
  }

  // Sun, moon and cloud sizes follow the card's height (--skyh = 1% of it)
  function size(sky) {
    var h = sky.parentNode ? sky.parentNode.clientHeight : 0;
    if (h) sky.style.setProperty("--skyh", h / 100 + "px");
  }

  window.WeatherSky = { make: make, update: update, size: size };
})();
