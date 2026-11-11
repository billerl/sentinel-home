// Live camera page (templates/grid.html). BOOT_ID and the camera list come from the page.
// Browsers before Chrome 71 (a 2018 Samsung TV has 56) leave cookies off fetch() unless asked: the TV's sign-in
// cookie was dropped and every request after it came back "signed out" (2026-11-10).
(function() {
  var f = window.fetch;
  if (!f) return;
  window.fetch = function(url, opts) {
    opts = opts || {};
    if (!opts.credentials) opts.credentials = "same-origin";
    return f.call(this, url, opts);
  };
})();
if (!String.prototype.padStart) {  // Chrome 57+; without it the weather tile's clock threw and the tile stayed empty
  String.prototype.padStart = function(len, fill) {
    var s = String(this);
    while (s.length < len) s = (fill || " ") + s;
    return s;
  };
}
var liveCams = [];
var focusedCamId = null;
var gridTimers = {};

// ---------------- Data saver (per device) ----------------
// Grid: 480 px pictures every ~5 s instead of tile-sized ones every ~1.75 s (about 0.1 GB/hour
// instead of 0.4-1.3). Full screen: one sharp picture per second instead of live video.
var dataSaver = false;
try { dataSaver = localStorage.getItem("sentinel.dataSaver") === "1"; } catch (e) {}
var focusTimer = null;

function showDataSaver() {
  var b = document.getElementById("saver-toggle");
  b.textContent = dataSaver ? "Data saver: on" : "Data saver: off";
  b.classList.toggle("active", dataSaver);
}
function toggleDataSaver() {
  dataSaver = !dataSaver;
  try { localStorage.setItem("sentinel.dataSaver", dataSaver ? "1" : "0"); } catch (e) {}
  showDataSaver();
  if (focusedCamId) openFocus(focusedCamId, document.getElementById("focus-title").textContent);
  else applyCameras(liveCams);  // restart the grid refresh at the new pace
}

function openFocus(camId, displayName) {
  focusedCamId = camId;
  var overlay = document.getElementById('focus-overlay');
  var img = document.getElementById('focus-img');
  document.getElementById('focus-title').textContent = displayName;
  clearInterval(focusTimer);
  focusTimer = null;
  overlay.style.display = 'block';
  // A TV remote's OK goes to the focused element: on Back, OK again returns to the grid (Fire TV, 2026-11-03)
  try { document.getElementById('focus-back').focus({ preventScroll: true }); } catch (e) {}
  Object.values(gridTimers).forEach(clearInterval);  // stop grid updates
  gridTimers = {};

  if (dataSaver || TV_PAGE) {  // (TV_PAGE: full-size live video made the 2018 Samsung close the show behind it)
    // One picture a second, at most 960 px wide.
    img.onerror = null;
    var w = Math.min(960, Math.ceil(window.innerWidth * Math.min(window.devicePixelRatio || 1, 1.5)));
    var refresh = function() {
      if (focusedCamId !== camId) return;
      var url = "/snapshot.jpg?id=" + encodeURIComponent(camId) + "&w=" + w + "&t=" + Date.now();
      var loader = new Image();
      loader.onload = function() { if (focusedCamId === camId) img.src = url; };
      loader.src = url;
    };
    refresh();
    focusTimer = setInterval(refresh, 1000);
    return;
  }

  // High-quality MJPEG for focus
  img.src = "/stream?id=" + encodeURIComponent(camId);
  img.onerror = function() {
    // Stream ended (camera dropped or server restarted): retry while focused.
    setTimeout(function() {
      if (focusedCamId === camId) img.src = "/stream?id=" + encodeURIComponent(camId) + "&r=" + Date.now();
    }, 3000);
  };
}

// "/#cam=<id>": straight to that camera full screen (a routine showing the front door on a TV)
function focusFromHash() {
  var m = /^#cam=([\w-]+)$/.exec(location.hash);
  var c = m && liveCams.filter(function(x) { return x.id === m[1]; })[0];
  if (c && focusedCamId !== c.id) openFocus(c.id, c.display || c.id);
}
window.addEventListener("hashchange", focusFromHash);

// "/#map=3" (or "#map=all"): where someone is, full screen over the cameras ("show me Xena's location" on a TV)
var mapFrame = null;
function mapFromHash() {
  var m = /^#map=([\w,]+)$/.exec(location.hash);
  if (!m) { closeMap(); return; }
  if (focusedCamId) closeFocus();  // (its applyCameras comes back here and opens the map)
  if (mapFrame && mapFrame.getAttribute("data-p") === m[1]) return;
  closeMap();
  mapFrame = document.createElement("iframe");
  mapFrame.setAttribute("data-p", m[1]);
  mapFrame.src = m[1] === "planes" ? "/planes" : "/where?p=" + m[1];  // (the planes near the house: planes.html)
  mapFrame.style.cssText = "position:fixed;top:0;left:0;width:100%;height:100%;border:0;z-index:5000;background:#000";
  document.body.appendChild(mapFrame);
}
function closeMap() {
  if (!mapFrame) return;
  mapFrame.parentNode.removeChild(mapFrame);
  mapFrame = null;
}
window.addEventListener("hashchange", mapFromHash);

// A TV's browser (a Samsung's own, a Fire TV's kiosk): Sentinel says what to show here (instead of opening its address
// in a new tab each time)
var TV_PAGE = /SMART-TV|Tizen|\bAFT[A-Z0-9]+\b/.test(navigator.userAgent);  // (a Fire TV's kiosk too: maps, one camera)
function tvPagePoll() {
  fetch("/api/tv_page").then(function(r) { return r.ok ? r.json() : null; }).then(function(c) {
    if (!c || !c.seq) return;
    var done = 0;
    try { done = Number(sessionStorage.getItem("sentinel.tvSeq") || 0); sessionStorage.setItem("sentinel.tvSeq", c.seq); } catch (e) {}
    if (c.seq <= done) return;
    if (c.reload) {
      history.replaceState(null, "", location.pathname + (c.path || ""));
      location.reload();
    } else if (c.path && c.path.indexOf("#cam=") === 0) {
      if (location.hash !== c.path) location.hash = c.path; else focusFromHash();
    } else if (c.path && c.path.indexOf("#map=") === 0) {
      if (location.hash !== c.path) location.hash = c.path; else mapFromHash();
    } else {
      if (/^#map=/.test(location.hash)) history.replaceState(null, "", location.pathname);
      closeMap();
      if (focusedCamId) closeFocus();
    }
  }).catch(function() {});
}
if (TV_PAGE) { tvPagePoll(); setInterval(tvPagePoll, 2000); }

function closeFocus() {
  if (/^#cam=/.test(location.hash)) history.replaceState(null, "", location.pathname);  // Back stays on the grid
  clearInterval(focusTimer);
  focusTimer = null;
  document.getElementById('focus-img').onerror = null;
  document.getElementById('focus-img').src = "";
  document.getElementById('focus-overlay').style.display = 'none';
  focusedCamId = null;
  applyCameras(liveCams);
}

// Ask for a picture about as wide as the tile is on screen (the server rounds it to a few sizes).
function tileWidthParam(imgElement) {
  // Capped at 1.5x: phones are ~3x and would otherwise pull full-size pictures every refresh
  // (about 2 GB an hour on mobile data). Full screen uses the full-resolution stream.
  var w = Math.ceil((imgElement.clientWidth || 640) * Math.min(window.devicePixelRatio || 1, 1.5));
  if (dataSaver) w = Math.min(w, 480);
  return "&w=" + w;
}

function updateTile(camId, imgElement) {
  var url = "/snapshot.jpg?id=" + encodeURIComponent(camId) + tileWidthParam(imgElement) + "&t=" + Date.now();
  var loader = new Image();
  loader.onload = function() { imgElement.src = url; };
  loader.src = url;
}

// ---------------- Weather tile ----------------
var weatherTile = document.createElement("div");
weatherTile.className = "tile weather";
weatherTile.hidden = true;
var weather = null;  // last /api/weather answer

function esc(t) { var d = document.createElement("div"); d.textContent = t == null ? "" : String(t); return d.innerHTML; }

var family = [];  // last /api/family answer: [{name, state: home|out|off, label, lat, lon}]
var mesh = { unread: 0, from: [] };  // last /api/mesh/unread answer: messages over the radio mesh not read yet
function meshHtml() {
  if (!mesh.unread) return "";
  var who = mesh.from.join(", ");
  return '<a class="mesh" href="/mesh" title="' + esc(mesh.unread + " unread mesh message" + (mesh.unread === 1 ? "" : "s") +
    (who ? " from " + who : "")) + '">📡 ' + mesh.unread + (who ? ' <span class="who">' + esc(mesh.from[0]) + "</span>" : "") + "</a>";
}
function familyHtml() {
  if (!family.length && !mesh.unread) return "";
  return '<div class="wx-family">' + family.map(function(p) {
    var inner = '<span class="dot ' + esc(p.state) + '"></span>' + esc(p.name);
    var title = esc(p.name + " — " + p.label);
    if (p.lat == null || p.id == null) return '<span class="p" title="' + title + '">' + inner + '</span>';
    // Sentinel's own map (the Family timeline), in this window: a new tab is a pop-up to a TV's kiosk browser
    return '<a href="/timeline?person=' + encodeURIComponent(p.id) + '" title="' + title + '">' + inner + '</a>';
  }).join("") + meshHtml() + '</div>';
}

function renderWeather() {
  var w = weather;
  if (!w) { weatherTile.innerHTML = ""; return; }
  renderWeatherOnly(w);
  weatherTile.classList.toggle("fam", family.length > 0 || mesh.unread > 0);
  weatherTile.insertAdjacentHTML("beforeend", familyHtml());
}

function renderWeatherOnly(w) {
  if (!w.configured) {
    weatherTile.innerHTML = '<h2>Weather</h2><div class="wx-msg">Show the weather here: enter your town or ZIP code in ' +
      '<a href="/settings#weather">Settings → Weather</a>.</div>';
    return;
  }
  if (w.temp == null) {
    weatherTile.innerHTML = '<h2>Weather</h2><div class="wx-msg">' + (w.error ? esc(w.error) : "Loading the weather…") + '</div>';
    return;
  }
  var deg = "°";
  var hours = (w.hours || []).map(function(h) {
    return '<div><span>' + esc(h.label) + '</span><span>' + esc(h.icon) + '</span><span>' + esc(h.temp) + deg +
      '</span><span class="r">' + (h.rain >= 20 ? "☔ " + esc(h.rain) + "%" : "") + '</span></div>';
  }).join("");
  weatherTile.innerHTML =
    '<h2>Weather — ' + esc(w.place) + '</h2>' +
    '<div class="wx-top"><div class="wx-icon">' + esc(w.icon) + '</div><div class="wx-temp">' + esc(w.temp) + deg + '</div>' +
    '<div class="wx-now"><b>' + esc(w.words) + '</b><span>Feels like ' + esc(w.feels) + deg + '</span>' +
    '<span>High ' + esc(w.high) + deg + ' · Low ' + esc(w.low) + deg + '</span></div>' +
    '<div class="wx-clock">' + clockHtml() + '</div></div>' +
    '<div class="wx-meta"><span>💨 ' + esc(w.wind) + ' mph' + (w.gusts > w.wind + 4 ? ', gusts ' + esc(w.gusts) : '') + '</span>' +
    '<span>💧 ' + esc(w.humidity) + '%</span><span>☔ ' + esc(w.rain_today) + '% today</span>' +
    '<span>🌅 ' + esc(w.sunrise) + '</span><span>🌇 ' + esc(w.sunset) + '</span></div>' +
    '<div class="wx-hours">' + hours + '</div>' +
    '<div class="wx-foot">' + (w.windy ? '<span class="wx-windy">Windy: expect motion clips from trees. </span>' : '') +
    'Updated ' + esc(w.updated) + '</div>';
}

function clockHtml() {
  var now = new Date(), h = now.getHours();
  return '<span class="t">' + (h % 12 || 12) + ":" + String(now.getMinutes()).padStart(2, "0") + '</span><span class="ap">' +
    (h < 12 ? "am" : "pm") + '</span><span class="d">' +
    esc(now.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })) + "</span>";
}
setInterval(function() {
  var html = clockHtml();
  document.querySelectorAll(".wx-clock, .wxf-clock").forEach(function(c) { if (c.innerHTML !== html) c.innerHTML = html; });
}, 5000);

// ---------------- Full-screen weather ----------------
var WXF_IDLE_MS = 2 * 60 * 1000;  // a TV showing the cameras goes back to them by itself
var wxfTimer = null;
weatherTile.tabIndex = 0;
weatherTile.setAttribute("role", "button");
weatherTile.setAttribute("aria-label", "Open the full weather forecast");
weatherTile.addEventListener("click", function(e) {
  if (e.target.closest("a")) return;  // a family member's name opens their map instead
  openWeatherFull();
});
weatherTile.addEventListener("keydown", function(e) {
  if ((e.key === "Enter" || e.key === " ") && !e.target.closest("a")) { e.preventDefault(); openWeatherFull(); }
});
function weatherFullOpen() { return document.getElementById("wxf").classList.contains("open"); }
function wxfPoke() {
  clearTimeout(wxfTimer);
  wxfTimer = setTimeout(function() { if (weatherFullOpen()) closeWeatherFull(); }, WXF_IDLE_MS);
}
function openWeatherFull() {
  if (!weather || !weather.full) return;
  var box = document.getElementById("wxf");
  box.classList.add("open");
  box.scrollTop = 0;
  renderWeatherFull();
  if (location.hash !== "#weather") history.pushState({ wxf: 1 }, "", "#weather");  // Back (phone, Fire TV) closes it
  document.getElementById("wxf-close").focus();
  wxfPoke();
}
function closeWeatherFull(fromHistory) {
  if (!weatherFullOpen()) return;
  document.getElementById("wxf").classList.remove("open");
  clearTimeout(wxfTimer);
  if (!fromHistory && location.hash === "#weather") history.back();
  weatherTile.focus({ preventScroll: true });
}
window.addEventListener("popstate", function() { if (location.hash !== "#weather") closeWeatherFull(true); });
["pointerdown", "keydown", "wheel", "touchmove"].forEach(function(t) {
  document.getElementById("wxf").addEventListener(t, wxfPoke, { passive: true });
});
document.addEventListener("keydown", function(e) {
  if (e.key === "Escape" && weatherFullOpen()) { closeWeatherFull(); e.stopImmediatePropagation(); }
}, true);
window.addEventListener("resize", function() { if (weatherFullOpen()) renderWeatherChart(); });

function renderWeatherFull() {
  var w = weather, f = w.full, n = f.now, deg = "°";
  document.getElementById("wxf-title").innerHTML = "Weather — " + esc(w.place) + "<small>Updated " + esc(w.updated) + "</small>";
  var today = f.days[0] || {};
  document.getElementById("wxf-now").innerHTML =
    '<div class="ic">' + esc(w.icon) + '</div><div class="t">' + esc(w.temp) + deg + '</div>' +
    '<div class="w"><b>' + esc(w.words) + '</b><span>Feels like ' + esc(w.feels) + deg + '</span>' +
    '<span>High ' + esc(w.high) + deg + ' · Low ' + esc(w.low) + deg + (w.rain_today ? ' · ☔ ' + esc(w.rain_today) + '% today' : '') + '</span>' +
    '<span class="sun">🌅 ' + esc(today.sunrise) + ' · 🌇 ' + esc(today.sunset) + '</span>' +
    (w.windy ? '<span class="wx-windy">Windy: expect motion clips from trees.</span>' : '') + '</div>' +
    '<div class="wxf-clock">' + clockHtml() + '</div>';
  function tile(k, v, sub) {
    return '<div class="wxf-tile"><div class="k">' + k + '</div><div class="v">' + v + '</div>' + (sub ? '<div class="s">' + sub + '</div>' : '') + '</div>';
  }
  var arrow = n.dir_deg == null ? "" : '<span class="wxf-arrow" style="transform: rotate(' + (n.dir_deg + 180) + 'deg)" aria-hidden="true">↑</span>';
  var vis = n.visibility == null ? "–" : (n.visibility >= 10 && n.visibility_unit === "mi" ? "10+" : esc(n.visibility));
  var html = tile("Wind", arrow + esc(n.wind) + "<small>mph</small>", "from " + esc(n.dir || "–") + (n.gusts > n.wind + 4 ? " · gusts " + esc(n.gusts) : "")) +
    tile("Humidity", esc(n.humidity) + "<small>%</small>", "dew point " + esc(n.dew) + deg) +
    tile("UV index", esc(n.uv == null ? "–" : n.uv), esc(n.uv_words) + (today.uv != null ? " · top " + esc(today.uv) : "")) +
    tile("Pressure", esc(n.pressure == null ? "–" : n.pressure) + "<small>" + esc(n.pressure_unit) + "</small>", "") +
    tile("Visibility", vis + "<small>" + esc(n.visibility_unit) + "</small>", "") +
    tile("Clouds", esc(n.clouds) + "<small>%</small>", "");
  document.getElementById("wxf-tiles").innerHTML = html;
  var lows = f.days.map(function(d) { return d.low; }), highs = f.days.map(function(d) { return d.high; });
  var min = Math.min.apply(null, lows), max = Math.max.apply(null, highs), span = Math.max(1, max - min);
  document.getElementById("wxf-days").innerHTML = f.days.map(function(d, i) {
    var left = (d.low - min) / span * 100, width = Math.max(2, (d.high - d.low) / span * 100);
    var dot = i === 0 && w.temp != null ? '<b style="left:' + Math.min(100, Math.max(0, (w.temp - min) / span * 100)) + '%" title="Now ' + esc(w.temp) + deg + '"></b>' : "";
    var extra = [];
    if (d.precip > 0) extra.push(esc(d.precip) + " " + esc(n.precip_unit) + " rain");
    if (d.snow > 0) extra.push(esc(d.snow) + " " + esc(n.snow_unit) + " snow");
    extra.push("wind " + esc(d.wind) + (d.gusts > d.wind + 4 ? "–" + esc(d.gusts) : "") + " mph " + esc(d.dir));
    if (d.uv != null) extra.push("UV " + esc(d.uv));
    return '<div class="wxf-day" title="' + esc(d.words) + '"><span class="n">' + esc(d.name) + '</span><span class="ic">' + esc(d.icon) + '</span>' +
      '<span class="r">' + (d.rain >= 10 ? "☔ " + esc(d.rain) + "%" : "") + '</span><span class="lo">' + esc(d.low) + deg + '</span>' +
      '<span class="wxf-range"><i style="left:' + left + '%;width:' + width + '%"></i>' + dot + '</span><span class="hi">' + esc(d.high) + deg + '</span>' +
      '<span class="x">' + esc(d.words) + " · " + extra.join(" · ") + '</span></div>';
  }).join("");
  renderWeatherChart();
}

// The next 48 hours: temperature as a line, rain chance as bars underneath (two charts on one time axis,
// never two scales on one chart); hover/tap for that hour.
function renderWeatherChart() {
  var box = document.getElementById("wxf-chart"), f = weather && weather.full;
  if (!f || !f.hours.length) { box.innerHTML = ""; return; }
  var tv = window.innerWidth >= 760 && window.innerHeight <= 640;  // the compact one-screen layout
  var hrs = f.hours, W = Math.max(320, box.clientWidth), padL = 34, padR = 10, top = 22,
      H1 = tv ? 58 : 170, gap = tv ? 24 : 34, H2 = tv ? 20 : 54;
  var H = top + H1 + gap + H2 + 22, step = (W - padL - padR) / (hrs.length - 1);
  var temps = hrs.map(function(h) { return h.temp; }), tmin = Math.min.apply(null, temps), tmax = Math.max.apply(null, temps);
  var lo = Math.floor((tmin - 2) / 5) * 5, hi = Math.ceil((tmax + 2) / 5) * 5;
  function x(i) { return padL + i * step; }
  function y(t) { return top + (hi - t) / (hi - lo) * H1; }
  var svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" height="' + H + '" role="img" aria-label="Temperature and rain chance, next 48 hours">';
  [lo, (lo + hi) / 2, hi].forEach(function(t) {  // recessive grid: three lines
    svg += '<line x1="' + padL + '" x2="' + (W - padR) + '" y1="' + y(t) + '" y2="' + y(t) + '" stroke="rgba(255,255,255,0.07)"/>' +
           '<text x="' + (padL - 6) + '" y="' + (y(t) + 4) + '" text-anchor="end">' + Math.round(t) + '°</text>';
  });
  hrs.forEach(function(h, i) {
    if (i > 0 && h.t.slice(11, 13) === "00") {  // midnight: the day's name
      var dn = new Date(h.day + "T12:00").toLocaleDateString([], { weekday: "short" });
      svg += '<line x1="' + x(i) + '" x2="' + x(i) + '" y1="' + (top - 14) + '" y2="' + (H - 18) + '" stroke="rgba(127,232,255,0.22)" stroke-dasharray="3 4"/>' +
             '<text x="' + (x(i) + 5) + '" y="' + (top - 6) + '" style="fill:#a9d3e2">' + esc(dn) + '</text>';
    }
    if (i % (step * 3 < 36 ? 6 : 3) === 0) svg += '<text x="' + x(i) + '" y="' + (H - 4) + '" text-anchor="middle">' + esc(h.label) + '</text>';
  });
  var path = hrs.map(function(h, i) { return (i ? "L" : "M") + x(i).toFixed(1) + " " + y(h.temp).toFixed(1); }).join(" ");
  svg += '<defs><linearGradient id="wxf-g" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#7fe8ff" stop-opacity="0.22"/>' +
         '<stop offset="1" stop-color="#7fe8ff" stop-opacity="0"/></linearGradient></defs>' +
         '<path d="' + path + ' L' + x(hrs.length - 1) + ' ' + (top + H1) + ' L' + x(0) + ' ' + (top + H1) + ' Z" fill="url(#wxf-g)"/>' +
         '<path d="' + path + '" fill="none" stroke="#7fe8ff" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>';
  // direct labels: only the warmest and coldest hours
  [temps.indexOf(tmax), temps.indexOf(tmin)].forEach(function(i, k) {
    svg += '<circle cx="' + x(i) + '" cy="' + y(temps[i]) + '" r="4" fill="#7fe8ff" stroke="#06121b" stroke-width="2"/>' +
           '<text class="lbl" x="' + Math.min(W - padR - 14, Math.max(padL + 16, x(i))) + '" y="' + (y(temps[i]) + (k ? 18 : -9)) +
           '" text-anchor="middle">' + temps[i] + '°</text>';
  });
  var by = top + H1 + gap;  // rain chance, 0-100%
  svg += '<text x="' + padL + '" y="' + (by - 8) + '" style="fill:#7cc4ff">Rain chance</text>' +
         '<line x1="' + padL + '" x2="' + (W - padR) + '" y1="' + (by + H2) + '" y2="' + (by + H2) + '" stroke="rgba(255,255,255,0.1)"/>' +
         '<text x="' + (padL - 6) + '" y="' + (by + 4) + '" text-anchor="end">100%</text>';
  var bw = Math.max(2, step - 2);
  hrs.forEach(function(h, i) {
    if (!h.rain) return;
    var bh = h.rain / 100 * H2;
    svg += '<rect x="' + (x(i) - bw / 2) + '" y="' + (by + H2 - bh) + '" width="' + bw + '" height="' + bh + '" rx="' + Math.min(2, bw / 2) + '" fill="#7cc4ff" opacity="0.85"/>';
  });
  svg += '<line id="wxf-cross" x1="0" x2="0" y1="' + (top - 4) + '" y2="' + (by + H2) + '" stroke="rgba(255,255,255,0.5)" visibility="hidden"/>' +
         '<circle id="wxf-dot" r="5" fill="#fff" stroke="#06121b" stroke-width="2" visibility="hidden"/>' +
         '<rect x="0" y="0" width="' + W + '" height="' + H + '" fill="transparent" id="wxf-hit"/></svg>';
  box.innerHTML = svg + '<div class="wxf-tip" id="wxf-tip"></div>';
  var hit = document.getElementById("wxf-hit"), tip = document.getElementById("wxf-tip");
  function show(ev) {
    var rect = box.getBoundingClientRect(), px = (ev.clientX - rect.left) * W / rect.width;
    var i = Math.max(0, Math.min(hrs.length - 1, Math.round((px - padL) / step))), h = hrs[i];
    document.getElementById("wxf-cross").setAttribute("x1", x(i));
    document.getElementById("wxf-cross").setAttribute("x2", x(i));
    document.getElementById("wxf-cross").setAttribute("visibility", "visible");
    var dot = document.getElementById("wxf-dot");
    dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(h.temp)); dot.setAttribute("visibility", "visible");
    var day = new Date(h.day + "T12:00").toLocaleDateString([], { weekday: "short" });
    tip.innerHTML = '<b>' + esc(day) + ' ' + esc(h.label) + '</b> ' + esc(h.icon) + ' ' + esc(h.words) + '<br>' +
      '<b>' + esc(h.temp) + '°</b> · feels ' + esc(h.feels) + '°<br>☔ ' + esc(h.rain) + '%' +
      (h.precip > 0 ? ' · ' + esc(h.precip) + ' ' + esc(f.now.precip_unit) : '') + '<br>💨 ' + esc(h.wind) + ' mph ' + esc(h.dir) +
      (h.gusts > h.wind + 4 ? ', gusts ' + esc(h.gusts) : '') + ' · 💧 ' + esc(h.humidity) + '%';
    tip.style.display = "block";
    var left = x(i) * rect.width / W + 12;
    if (left + tip.offsetWidth > rect.width) left = x(i) * rect.width / W - tip.offsetWidth - 12;
    tip.style.left = Math.max(0, left) + "px";
  }
  function hide() {
    tip.style.display = "none";
    document.getElementById("wxf-cross").setAttribute("visibility", "hidden");
    document.getElementById("wxf-dot").setAttribute("visibility", "hidden");
  }
  hit.addEventListener("pointermove", show);
  hit.addEventListener("pointerdown", show);
  hit.addEventListener("pointerleave", function(e) { if (e.pointerType === "mouse") hide(); });
}

async function loadWeather() {
  var next = 10 * 60 * 1000;
  try {
    var r = await fetch("/api/weather");
    if (r.ok) {
      weather = await r.json();
      if (weather.loading) next = 3000;
      renderWeather();
      if (weatherFullOpen()) renderWeatherFull();
      layoutGrid();
    }
  } catch (e) { next = 60 * 1000; }
  setTimeout(loadWeather, next);
}
loadWeather();
// Severe weather warnings: a banner across the top while one is in effect (checked every minute).
async function loadWeatherAlerts() {
  try {
    var r = await fetch("/api/weather/alerts");
    if (r.ok) {
      var alerts = (await r.json()).alerts || [], el = document.getElementById("wx-alert");
      el.hidden = !alerts.length;
      el.className = "wx-alert " + (alerts.length && alerts[0].level === "loud" ? "loud" : "notice");
      el.textContent = alerts.map(function(a) {
        return (/Tornado/.test(a.event) ? "🌪️ " : "⚠️ ") + a.event.toUpperCase() + (a.until ? " until " + a.until : "");
      }).join("  ·  ");
    }
  } catch (e) {}
}
loadWeatherAlerts(); setInterval(loadWeatherAlerts, 60 * 1000);

async function loadFamily() {
  try {
    var r = await fetch("/api/family");
    if (r.ok) { family = (await r.json()).people || []; renderWeather(); }
  } catch (e) {}
  setTimeout(loadFamily, 60 * 1000);
}
loadFamily();

async function loadMesh() {
  try {
    var r = await fetch("/api/mesh/unread");
    if (r.ok) {
      var m = await r.json();
      if (m.unread !== mesh.unread || String(m.from) !== String(mesh.from)) { mesh = m; renderWeather(); }
    }
  } catch (e) {}
  setTimeout(loadMesh, 20 * 1000);
}
loadMesh();

function applyCameras(cams) {
  var root = document.getElementById("grid");
  liveCams = cams;
  root.innerHTML = "";
  Object.values(gridTimers).forEach(clearInterval);
  gridTimers = {};

  cams.forEach(function(c) {
    var tile = document.createElement('div');
    tile.className = 'tile';
    tile.onclick = function() { openFocus(c.id, c.display || c.id); };
    
    var h2 = document.createElement('h2');
    h2.textContent = c.display;
    
    var img = document.createElement('img');
    img.onload = function() {
      var aspect = img.naturalWidth / img.naturalHeight;
      if (aspect > 0.5 && aspect < 3 && Math.abs(aspect - camAspect) > 0.01) { camAspect = aspect; layoutGrid(); }
    };
    img.src = "/snapshot.jpg?id=" + encodeURIComponent(c.id) + (dataSaver ? "&w=480" : "");
    
    tile.appendChild(h2);
    tile.appendChild(img);
    root.appendChild(tile);

    // Staggered snapshot updates for the grid (1.5s interval)
    // This is MUCH more stable for mobile than multiple concurrent MJPEG streams.
    gridTimers[c.id] = setInterval(function() {
      if (!focusedCamId && !mapFrame && !playerOpen && !(isPhone() && currentTab === "events")) updateTile(c.id, img);
    }, (dataSaver ? 5000 : 1500) + (Math.random() * 500));
  });
  root.appendChild(weatherTile);
  setTimeout(layoutGrid, 0);  // after the page script has finished setting up
  focusFromHash();
  mapFromHash();
}

(function boot() {
  try {
    var raw = document.getElementById("initial-cameras").textContent;
    applyCameras(JSON.parse(raw || "[]"));
  } catch (e) {}
})();

async function refreshList() {
  if (focusedCamId) return;
  try {
    var r = await fetch("/api/cameras");
    if (r.status === 401) { location.href = "/login"; return; }  // signed out (e.g. a TV left on)
    var boot = r.headers.get("X-Sentinel-Boot");
    if (boot && boot !== BOOT_ID) { if (!playerOpen) location.reload(); return; }  // Sentinel was updated: load the new page
    var cams = await r.json();
    var cur = liveCams.map(c => c.id).sort().join(",");
    var nxt = cams.map(c => c.id).sort().join(",");
    if (cur !== nxt) applyCameras(cams);
  } catch (e) {}
}
setInterval(refreshList, 10000);

// ---------------- Motion timeline ----------------
var tl = null;          // last /api/recordings response
var tlZoom = 1;         // 1 = 24h visible, 4 = 6h, 24 = 1h
var playerOpen = false;
var tlFilter = "all";
var GROUPS = { person: "person", bicycle: "vehicle", car: "vehicle", motorcycle: "vehicle", bus: "vehicle",
               truck: "vehicle", dog: "animal", cat: "animal", horse: "animal", sheep: "animal", cow: "animal",
               bear: "animal", bird: "animal", deer: "animal", delivery: "delivery" };

function clipGroup(clip) {
  var groups = (clip.labels || []).map(function(l) { return GROUPS[l]; });
  if (groups.indexOf("delivery") >= 0) return "delivery";  // usually has a driver in it too
  if (groups.indexOf("person") >= 0) return "person";
  if (groups.indexOf("vehicle") >= 0) return "vehicle";
  if (groups.indexOf("animal") >= 0) return "animal";
  return "motion";
}

function visibleClips() {
  if (!tl) return [];
  return tl.clips.filter(function(c) { return tlFilter === "all" || clipGroup(c) === tlFilter; });
}

function setFilter(f) {
  tlFilter = f;
  document.querySelectorAll("#tl-legend button").forEach(function(b) { b.classList.toggle("active", b.dataset.filter === f); });
  if (tl) renderTimeline(false);
}
var playingClip = null;

function store(key, value) {
  try { localStorage.setItem(key, value); } catch (e) {}
}
function recall(key) {
  try { return localStorage.getItem(key); } catch (e) { return null; }
}

function fmtOffset(sec) {
  sec = Math.max(0, Math.floor(sec));
  var h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
}

function cameraName(key) {
  if (!tl) return key;
  var row = tl.cameras.find(function(c) { return c.key === key; });
  return row ? row.name : key;
}

// Zone outlines are drawn on the server, so this switches them for every viewer.
var zonesShown = true;
function showZonesState(on) {
  zonesShown = on;
  var b = document.getElementById("zones-toggle");
  b.textContent = on ? "Zones: on" : "Zones: off";
  b.classList.toggle("active", on);
  b.hidden = false;
}
async function loadZonesState() {
  try {
    var r = await fetch("/api/settings");
    if (r.ok) showZonesState((await r.json()).values["web.show_zones"] !== false);
  } catch (e) {}
}
async function toggleZones() {
  var want = !zonesShown;
  showZonesState(want);
  try {
    var r = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Sentinel-Request": "1" },
      body: JSON.stringify({ "web.show_zones": want })
    });
    if (!r.ok) throw new Error();
  } catch (e) { showZonesState(!want); }
}
loadZonesState();
showDataSaver();

// ---------------- Phone layout ----------------
var PHONE_QUERY = window.matchMedia("(max-width: 760px), (max-height: 500px) and (pointer: coarse)");
var currentTab = "live";
function isPhone() { return PHONE_QUERY.matches; }

// ---------------- Spotify now-playing bar ----------------
var musicNow = null, musicTimer = null;
function showMusic(now) {
  musicNow = now;
  var bar = document.getElementById("music");
  var show = !!(now && now.connected && now.active);
  if (bar.hidden === show) {
    bar.hidden = !show;
    document.body.classList.toggle("has-music", show);
    layoutGrid();  // the cameras get a little less height
  }
  if (!show) return;
  var art = document.getElementById("mu-art");
  if (now.art) { if (art.getAttribute("src") !== now.art) art.src = now.art; art.hidden = false; } else art.hidden = true;
  var line = document.getElementById("mu-line");
  line.innerHTML = (now.playing ? "♪ " : "⏸ ") + '<span class="mu-title">' + esc(now.track) + "</span> — " + esc(now.artist) +
    (now.device ? ' <span class="mu-where">· ' + esc(now.device) + "</span>" : "");
  var box = line.parentNode;
  box.classList.remove("long");
  if (line.offsetWidth > box.clientWidth) box.classList.add("long");  // scroll like a ticker when it doesn't fit
  document.getElementById("mu-play").textContent = now.playing ? "⏸" : "▶";
  document.getElementById("mu-vol").textContent = now.volume == null ? "" : now.volume + "%";
}
async function pollMusic() {
  clearTimeout(musicTimer);
  var next = 60000;  // not connected: check now and then
  try {
    var r = await fetch("/api/spotify");
    if (r.ok) {
      var now = await r.json();
      showMusic(now);
      if (now.connected) next = now.active ? (dataSaver ? 15000 : 5000) : 20000;
    }
  } catch (e) {}
  musicTimer = setTimeout(pollMusic, document.hidden ? Math.max(next, 30000) : next);
}
async function music(action, value) {
  try {
    var r = await fetch("/api/spotify/control", {
      method: "POST", headers: { "Content-Type": "application/json", "X-Sentinel-Request": "1" },
      body: JSON.stringify({ action: action, value: value || "" })
    });
    var data = await r.json();
    if (data.now) showMusic(data.now);
  } catch (e) {}
  setTimeout(pollMusic, 1500);  // Spotify takes a moment to report the change
}


// ---------------- Smart plugs and switches ----------------
var switchList = [], switchTimer = null, switchBusy = {};
var SW_ICONS = { light: "💡", tree: "🎄", fan: "🌀", water: "💧", plug: "🔌", tv: "📺" };
function switchesShown() {
  return isPhone() ? currentTab === "switches" : document.getElementById("switches").classList.contains("open");
}
function toggleSwitches(open) {
  var panel = document.getElementById("switches");
  if (open === undefined) open = !panel.classList.contains("open");
  panel.classList.toggle("open", open);
  document.getElementById("sw-open").classList.toggle("active", open);
  if (open) pollSwitches();
}
function onFor(since) {
  var s = Math.max(0, Date.now() / 1000 - since);
  if (s < 60) return "On just now";
  if (s < 3600) return "On for " + Math.floor(s / 60) + " min";
  if (s < 86400) return "On for " + Math.floor(s / 3600) + " h" + (s % 3600 >= 60 ? " " + Math.floor(s % 3600 / 60) + " min" : "");
  var d = Math.floor(s / 86400);
  return "On for " + d + (d === 1 ? " day" : " days");
}
function niceName(n) { return n ? n.charAt(0).toUpperCase() + n.slice(1) : n; }
function renderSwitches(data) {
  switchList = data.switches || [];
  var on = switchList.filter(function(s) { return s.on && s.online; }).length;
  document.querySelector("#sw-open .sw-count").textContent = on ? " · " + on + " on" : "";
  document.getElementById("sw-sub").textContent = data.scanning && !switchList.length ? "Looking for devices…" :
    switchList.length ? switchList.length + (switchList.length === 1 ? " switch" : " switches") + " · " + (on ? on + " on" : "all off") : "";
  document.getElementById("sw-scan").textContent = data.scanning ? "Looking…" : "Find new devices";
  document.getElementById("sw-scan").disabled = !!data.scanning;
  var body = document.getElementById("sw-body");
  if (!switchList.length) {
    body.innerHTML = '<div class="sw-empty">' + (data.scanning ? "Looking for smart plugs on your Wi-Fi…" :
      "No smart plugs or switches found yet. Set a new one up in its own app first (Kasa), then tap " +
      "<b>Find new devices</b>.") + "</div>";
    return;
  }
  var groups = [];
  switchList.forEach(function(s) {
    var g = groups.find(function(x) { return x.device === s.device && x.key === s.id.split("-").slice(0, 2).join("-"); });
    if (!g) groups.push(g = { device: s.device, key: s.id.split("-").slice(0, 2).join("-"), items: [] });
    g.items.push(s);
  });
  var focused = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.id : null;
  body.innerHTML = "";
  groups.forEach(function(g) {
    var box = document.createElement("div");
    box.className = "sw-group";
    var online = g.items.every(function(s) { return s.online; });
    box.innerHTML = '<h3><span class="dot' + (online ? "" : " off") + '"></span>' + esc(g.device) +
      (g.items.length > 1 ? " · " + g.items.length + " outlets" : "") + (online ? "" : " · not answering") + "</h3>";
    var tiles = document.createElement("div");
    tiles.className = "sw-tiles";
    g.items.forEach(function(s) {
      var t = document.createElement("div");
      t.className = "sw-cell" + (s.icon === "tv" ? " wide" : "");
      var b = document.createElement("button");
      b.type = "button";
      b.dataset.id = s.id;
      b.className = "sw-tile k-" + s.icon + (s.on ? " on" : "") + (s.online ? "" : " offline") + (switchBusy[s.id] ? " busy" : "");
      b.setAttribute("aria-pressed", s.on ? "true" : "false");
      b.title = s.online ? "Turn " + (s.on ? "off" : "on") : "Not answering";
      b.innerHTML = '<span class="sw-icon">' + (SW_ICONS[s.icon] || SW_ICONS.plug) + '</span><span class="sw-name">' +
        esc(niceName(s.name)) + '</span><span class="sw-state">' +
        (!s.online ? "Not answering" : s.on ? (s.since ? onFor(s.since) : "On") : "Off") + '</span><span class="sw-pill"></span>';
      b.onclick = function() { flipSwitch(s, b); };
      var e = document.createElement("button");
      e.type = "button";
      e.className = "sw-edit";
      e.title = "Rename";
      e.textContent = "✎";
      e.onclick = function(ev) { ev.stopPropagation(); renameSwitch(s); };
      t.appendChild(b);
      t.appendChild(e);
      if (s.icon === "tv" && /^(samsung|firetv)-/.test(s.id)) {
        var r = document.createElement("button");
        r.type = "button";
        r.className = "sw-remote";
        r.innerHTML = '<span class="ico">🎮</span>Open remote';
        r.onclick = function(ev) { ev.stopPropagation(); openRemote(s.id); };
        t.appendChild(r);
      }
      tiles.appendChild(t);
    });
    box.appendChild(tiles);
    body.appendChild(box);
  });
  if (focused) { var f = body.querySelector('[data-id="' + focused + '"]'); if (f) f.focus(); }
}
async function pollSwitches() {
  clearTimeout(switchTimer);
  var next = 30000;  // closed: only the "1 on" count in the header
  try {
    var r = await fetch("/api/switches");
    if (r.ok) {
      var data = await r.json();
      if (!Object.keys(switchBusy).length) renderSwitches(data);
      if (switchesShown() || data.scanning) next = data.scanning ? 2000 : 4000;
    }
  } catch (e) {}
  switchTimer = setTimeout(pollSwitches, document.hidden ? Math.max(next, 60000) : next);
}
function switchMsg(text) {
  document.getElementById("sw-msg").textContent = text || "";
}
async function switchPost(path, body) {
  var r = await fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json", "X-Sentinel-Request": "1" },
    body: JSON.stringify(body)
  });
  var data = {};
  try { data = await r.json(); } catch (e) {}
  if (r.status === 401) location.href = "/login";
  return { ok: r.ok, data: data };
}
async function flipSwitch(s, tile) {
  if (!s.online || switchBusy[s.id]) return;
  var want = !s.on;
  switchBusy[s.id] = true;
  tile.classList.add("busy");
  tile.classList.toggle("on", want);  // show it straight away; put it back if the plug says no
  switchMsg("");
  try {
    var res = await switchPost("/api/switches/set", { id: s.id, on: want });
    delete switchBusy[s.id];
    if (res.data.switches) renderSwitches(res.data);
    if (!res.ok) {
      switchMsg(res.data.error || "That didn't work.");
      var again = document.querySelector('.sw-tile[data-id="' + s.id + '"]');
      if (again) { again.classList.add("shake"); setTimeout(function() { again.classList.remove("shake"); }, 450); }
    }
  } catch (e) {
    delete switchBusy[s.id];
    switchMsg("Couldn't reach Sentinel.");
    pollSwitches();
  }
}
async function renameSwitch(s) {
  var name = prompt("New name for “" + niceName(s.name) + "” (this is also what you say to Jarvis):", s.name);
  if (name === null || !name.trim() || name.trim() === s.name) return;
  var res = await switchPost("/api/switches/rename", { id: s.id, name: name.trim() });
  switchMsg(res.ok ? "" : (res.data.error || "That didn't work."));
  pollSwitches();
}
async function scanSwitches() {
  switchMsg("");
  await switchPost("/api/switches/scan", {});
  pollSwitches();
}
// ---------------- TV remote ----------------
var tvId = null, tvState = null, tvQueue = Promise.resolve(), tvRepeat = null, tvKeys = null;
function remoteOpen() { return document.getElementById("tvr").classList.contains("open"); }
function remoteMsg(text, ok) {
  var m = document.getElementById("tvr-msg");
  m.textContent = text || "";
  m.className = ok ? "ok" : "";
}
function remoteShow(v) {
  tvState = v;
  document.getElementById("tvr-name").textContent = niceName(v.name);
  document.getElementById("tvr-state").textContent = !v.online ? "Not answering" : v.on ? "On" : "Off · tap ⏻ to turn it on";
  document.getElementById("tvr-power").classList.toggle("on", !!v.on);
}
function remoteTvs() {
  return switchList.filter(function(s) { return s.icon === "tv" && /^(samsung|firetv)-/.test(s.id); });
}
async function openRemote(id) {
  tvId = id;
  store("sentinel.remoteTv", id);
  var tvs = remoteTvs(), pick = document.getElementById("tvr-pick");  // more than one TV: switch between them here
  pick.hidden = tvs.length < 2;
  document.getElementById("tvr-name").hidden = !pick.hidden;  // the picker already names it
  pick.innerHTML = tvs.map(function(s) { return '<option value="' + esc(s.id) + '">' + esc(niceName(s.name)) + "</option>"; }).join("");
  pick.value = id;
  document.getElementById("tvr").classList.add("open");
  remoteMsg("");
  var apps = document.getElementById("tvr-apps");
  apps.innerHTML = "";
  screenShow(null);
  try {
    var r = await fetch("/api/tv/" + encodeURIComponent(id));
    if (r.status === 401) { location.href = "/login"; return; }
    var v = await r.json();
    if (!r.ok) { remoteMsg(v.error || "That TV isn't answering."); return; }
    remoteShow(v);
    tvKeys = v.keys || null;
    document.querySelectorAll("#tvr [data-key]").forEach(function(b) {  // only the buttons this device has
      b.style.display = !v.keys || v.keys.indexOf(b.dataset.key) >= 0 ? "" : "none";
    });
    document.getElementById("tvr-live").hidden = !v.live;
    document.getElementById("tvr-grid").hidden = !v.grid;
    liveBackShow(v.live_back);
    loadShortcuts();
    screenShow(v.screen ? id : null);
    v.apps.forEach(function(a) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "tvr-app";
      b.title = "Open " + a.name;
      var ic = document.createElement("span");
      ic.className = "ic";
      ic.textContent = a.name.charAt(0).toUpperCase();  // until (or unless) the TV's own icon arrives
      var img = new Image();
      img.alt = "";
      img.onload = function() { img.classList.add("ready"); };
      img.onerror = function() { img.remove(); };
      img.src = "/api/tv/" + encodeURIComponent(id) + "/icon/" + encodeURIComponent(a.id);
      ic.appendChild(img);
      var label = document.createElement("span");
      label.textContent = a.name;
      b.appendChild(ic);
      b.appendChild(label);
      b.onclick = function() { remoteApp(a); };
      apps.appendChild(b);
    });
  } catch (e) { remoteMsg("Couldn't reach Sentinel."); }
  var first = document.querySelector("#tvr-dpad .ok");
  if (first && !isPhone()) first.focus();
}
async function openRemoteFromHome(want) {
  try {
    var r = await fetch("/api/switches");
    if (r.ok) renderSwitches(await r.json());
  } catch (e) {}
  var tvs = remoteTvs(), last = recall("sentinel.remoteTv");
  if (!tvs.length) {  // nothing to open: show the switches, which say what was found
    if (isPhone()) setTab("switches"); else toggleSwitches(true);
    switchMsg("No TV with a remote was found.");
    return;
  }
  if (!tvs.some(function(s) { return s.id === want; })) want = tvs.some(function(s) { return s.id === last; }) ? last : tvs[0].id;
  openRemote(want);
}
// ---- the TV's screen on the phone (Fire TV: sentinel/engine/tv_mirror.py) ----
var screenFor = null, screenRetry = null;
function screenShow(id) {
  var box = document.getElementById("tvr-screen-box"), img = document.getElementById("tvr-screen-img"),
      msg = document.getElementById("tvr-screen-msg");
  clearTimeout(screenRetry);
  screenFor = id;
  box.hidden = !id;
  img.onload = img.onerror = null;
  img.removeAttribute("src");  // closes the stream: the TV stops recording a few seconds later
  if (!id || document.hidden) return;
  msg.textContent = "Getting the TV's picture… (a few seconds)";
  img.style.visibility = "hidden";
  img.onerror = function() {
    msg.textContent = "The TV isn't sending its picture. Trying again…";
    img.style.visibility = "hidden";
    clearTimeout(screenRetry);
    screenRetry = setTimeout(function() { if (screenFor === id) screenShow(id); }, 5000);
  };
  img.src = "/api/tv/" + encodeURIComponent(id) + "/screen?t=" + Date.now();
  (function arrived() {  // (not every browser fires "load" for a stream of pictures)
    if (screenFor !== id || !img.getAttribute("src")) return;
    if (img.naturalWidth) { msg.textContent = ""; img.style.visibility = ""; return; }
    screenRetry = setTimeout(arrived, 400);
  })();
}
document.addEventListener("visibilitychange", function() {  // phone locked or tab hidden: let the TV rest
  if (tvId && remoteOpen() && screenFor !== null) screenShow(tvId);
});
document.getElementById("tvr-screen").addEventListener("click", function(ev) {
  if (!tvId) return;
  var r = this.getBoundingClientRect(), x = (ev.clientX - r.left) / r.width, y = (ev.clientY - r.top) / r.height;
  var dot = document.createElement("span");
  dot.className = "dot";
  dot.style.left = (x * 100) + "%";
  dot.style.top = (y * 100) + "%";
  this.appendChild(dot);
  setTimeout(function() { dot.remove(); }, 700);
  if (navigator.vibrate) { try { navigator.vibrate(8); } catch (e) {} }
  var id = tvId;
  tvQueue = tvQueue.then(async function() {
    try {
      var res = await switchPost("/api/tv/" + encodeURIComponent(id) + "/tap", { x: x, y: y });
      if (!res.ok) remoteMsg(res.data.error || "That didn't work.");
    } catch (e) { remoteMsg("Couldn't reach Sentinel."); }
  });
});
function closeRemote() {
  stopRepeat();
  screenShow(null);
  document.getElementById("tvr").classList.remove("open");
  tvId = null;
  pollSwitches();
}
function remoteKey(key) {
  if (!tvId) return;
  if (navigator.vibrate) { try { navigator.vibrate(8); } catch (e) {} }
  var id = tvId;
  tvQueue = tvQueue.then(async function() {  // one at a time, in order
    try {
      var res = await switchPost("/api/tv/" + encodeURIComponent(id) + "/key", { key: key });
      if (!res.ok) { remoteMsg(res.data.error || "That didn't work."); stopRepeat(); }
      else if (document.getElementById("tvr-msg").className !== "ok") remoteMsg("");
      if (res.ok && recording) loadShortcuts();
    } catch (e) { remoteMsg("Couldn't reach Sentinel."); stopRepeat(); }
  });
}
async function remotePower() {
  if (!tvId || !tvState) return;
  var want = !tvState.on;
  remoteMsg(want ? "Turning the TV on (about 15 seconds)…" : "Turning the TV off…", true);
  var res = await switchPost("/api/switches/set", { id: tvId, on: want });
  if (!res.ok) { remoteMsg(res.data.error || "That didn't work."); return; }
  remoteMsg("");
  remoteShow(Object.assign({}, tvState, { on: want }));
  if (res.data.switches) renderSwitches(res.data);
}
async function remoteApp(a) {
  remoteMsg("Opening " + a.name + (tvState && !tvState.on ? " (turning the TV on first)…" : "…"), true);
  var res = await switchPost("/api/tv/" + encodeURIComponent(tvId) + "/open", { app: a.id });
  if (!res.ok) { remoteMsg(res.data.error || "That didn't work."); return; }
  remoteMsg(a.name + " ✓", true);
  if (recording) loadShortcuts();
  if (tvState && !tvState.on) remoteShow(Object.assign({}, tvState, { on: true }));
}
async function remoteLive(reload, back) {
  remoteMsg(back ? "Going back…" : reload ? "Reloading the cameras…" : "Showing the cameras…", true);
  var res = await switchPost("/api/tv/" + encodeURIComponent(tvId) + "/live", { reload: reload, back: back === true });
  remoteMsg(res.ok ? (back ? "Back to " + res.data.message + " ✓" : reload ? "Reloaded ✓" : "Cameras on ✓")
                   : (res.data.error || "That didn't work."), res.ok);
  if (res.ok) liveBackRefresh();
}
// "⏎ Back to Netflix": what the cameras covered on a Samsung, still running behind them
function liveBackShow(name) {
  var row = document.getElementById("tvr-live-back");
  row.hidden = !name;
  row.querySelector("button").textContent = "⏎ Back to " + (name || "");
}
async function liveBackRefresh() {
  var id = tvId;
  try {
    var r = await fetch("/api/tv/" + encodeURIComponent(id));
    if (r.ok && id === tvId) liveBackShow((await r.json()).live_back);
  } catch (e) {}
}
async function remoteType(grid) {
  var box = document.getElementById("tvr-text");
  if (!box.value.trim() || !tvId) return;
  if (grid) remoteMsg("Typing… (one letter at a time, watch the TV)", true);
  var res = await switchPost("/api/tv/" + encodeURIComponent(tvId) + "/type", { text: box.value.trim(), grid: grid === true });
  if (res.ok) { box.value = ""; remoteMsg(grid ? "Searched ✓" : "Sent ✓ (it goes into the box the TV's keyboard is open on)", true); if (recording) loadShortcuts(); }
  else remoteMsg(res.data.error || "That didn't work.");
}
// ---- shortcuts (beta): presses recorded on this remote, played back (sentinel/engine/tv_shortcuts.py) ----
var recording = false;
async function loadShortcuts() {
  var id = tvId;
  if (!id) return;
  try {
    var r = await fetch("/api/tv/" + encodeURIComponent(id) + "/shortcuts");
    if (!r.ok || id !== tvId) return;
    showShortcuts(await r.json());
  } catch (e) {}
}
function showShortcuts(v) {
  recording = !!v.recording;
  document.getElementById("tvr-recbar").hidden = !recording;
  document.getElementById("tvr-rec-start").hidden = recording;
  document.getElementById("tvr-rec-count").textContent = "Recording… " + v.steps + (v.steps === 1 ? " step" : " steps");
  var box = document.getElementById("tvr-shortcuts");
  box.innerHTML = "";
  v.shortcuts.forEach(function(sc) {
    var row = document.createElement("div");
    row.className = "tvr-sc";
    var play = document.createElement("button");
    play.type = "button";
    play.className = "tvr-btn play";
    play.innerHTML = "▶ " + esc(sc.name) + " <small>" + sc.steps + " steps</small>";
    play.onclick = function() { shortcutPlay(sc); };
    var del = document.createElement("button");
    del.type = "button";
    del.className = "tvr-btn del";
    del.title = "Delete " + sc.name;
    del.textContent = "✕";
    del.onclick = function() { shortcutDelete(sc); };
    row.appendChild(play);
    row.appendChild(del);
    box.appendChild(row);
  });
}
async function recordStart() {
  var res = await switchPost("/api/tv/" + encodeURIComponent(tvId) + "/record", {});
  if (!res.ok) { remoteMsg(res.data.error || "That didn't work."); return; }
  remoteMsg("Recording. Open the app first, then press buttons as usual.", true);
  loadShortcuts();
}
async function recordSave() {
  var box = document.getElementById("tvr-rec-name");
  var res = await switchPost("/api/tv/" + encodeURIComponent(tvId) + "/record_save", { name: box.value.trim() });
  remoteMsg(res.ok ? res.data.message : (res.data.error || "That didn't work."), res.ok);
  if (res.ok) box.value = "";
  loadShortcuts();
}
async function recordCancel() {
  await switchPost("/api/tv/" + encodeURIComponent(tvId) + "/record_cancel", {});
  remoteMsg("Recording discarded.", true);
  loadShortcuts();
}
async function shortcutPlay(sc) {
  var res = await switchPost("/api/tv/" + encodeURIComponent(tvId) + "/shortcut_play", { id: sc.id });
  remoteMsg(res.ok ? res.data.message : (res.data.error || "That didn't work."), res.ok);
}
async function shortcutDelete(sc) {
  if (!confirm("Delete the shortcut “" + sc.name + "”?")) return;
  var res = await switchPost("/api/tv/" + encodeURIComponent(tvId) + "/shortcut_delete", { id: sc.id });
  if (!res.ok) remoteMsg(res.data.error || "That didn't work.");
  loadShortcuts();
}
function stopRepeat() {
  if (tvRepeat) { clearTimeout(tvRepeat.t); clearInterval(tvRepeat.i); tvRepeat.btn.classList.remove("pressed"); tvRepeat = null; }
}
// Buttons: a press sends once; holding an arrow, volume or channel button repeats it.
document.querySelectorAll("#tvr [data-key]").forEach(function(btn) {
  var key = btn.dataset.key, repeat = btn.hasAttribute("data-repeat");
  btn.addEventListener("pointerdown", function(e) {
    if (e.pointerType === "mouse" && e.button !== 0) return;
    if (btn.closest("#tvr-dpad") && e.pointerType !== "mouse") return;  // the circle's own swipe/tap handling below
    e.preventDefault();
    stopRepeat();
    btn.classList.add("pressed");
    var hold = btn.dataset.hold && tvKeys && tvKeys.indexOf(btn.dataset.hold) >= 0 ? btn.dataset.hold : null;
    tvRepeat = { btn: btn, t: 0, i: 0, hold: hold, held: false };
    if (hold) {  // a tap sends the button when it's let go; holding it sends the TV's long press (Home: quick menu)
      tvRepeat.t = setTimeout(function() {
        if (!tvRepeat || tvRepeat.btn !== btn) return;
        tvRepeat.held = true;
        remoteKey(hold);
        remoteMsg("Held ✓ (the quick menu takes a few seconds to appear)", true);
      }, 700);
      return;
    }
    remoteKey(key);
    if (repeat) tvRepeat.t = setTimeout(function() { if (tvRepeat) tvRepeat.i = setInterval(function() { remoteKey(key); }, 220); }, 450);
  });
  btn.addEventListener("pointerup", function() {
    if (tvRepeat && tvRepeat.btn === btn && tvRepeat.hold && !tvRepeat.held) remoteKey(key);  // let go early: a tap
  });
  btn.addEventListener("click", function(e) { if (e.detail === 0) remoteKey(key); });  // keyboard / TV-browser "click"
});
["pointerup", "pointercancel", "pointerleave"].forEach(function(t) {
  document.getElementById("tvr").addEventListener(t, function(e) { if (t !== "pointerleave" || e.target.matches && e.target.matches("[data-key]")) stopRepeat(); });
});
// The D-pad circle on a touch screen: swipe for an arrow, tap a spot for that button.
(function() {
  var pad = document.getElementById("tvr-dpad"), start = null;
  pad.addEventListener("pointerdown", function(e) {
    if (e.pointerType === "mouse") return;
    e.preventDefault();
    start = { x: e.clientX, y: e.clientY, btn: e.target.closest("[data-key]") };
    if (start.btn) start.btn.classList.add("pressed");
  });
  pad.addEventListener("pointerup", function(e) {
    if (!start) return;
    var dx = e.clientX - start.x, dy = e.clientY - start.y, btn = start.btn;
    if (btn) btn.classList.remove("pressed");
    start = null;
    if (Math.max(Math.abs(dx), Math.abs(dy)) > 28) remoteKey(Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? "right" : "left") : (dy > 0 ? "down" : "up"));
    else if (btn) remoteKey(btn.dataset.key);
  });
  pad.addEventListener("pointercancel", function() { if (start && start.btn) start.btn.classList.remove("pressed"); start = null; });
})();
document.addEventListener("keydown", function(e) {
  if (!remoteOpen()) return;
  if (e.target && (e.target.id === "tvr-text" || e.target.id === "tvr-rec-name")) { if (e.key === "Escape") e.target.blur(); return; }
  var keys = { ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right", Enter: "ok", Backspace: "back",
               "+": "vol_up", "=": "vol_up", "-": "vol_down", m: "mute", PageUp: "ch_up", PageDown: "ch_down" };
  if (e.key === "Escape") { closeRemote(); e.stopImmediatePropagation(); e.preventDefault(); return; }
  if (keys[e.key] && !(e.target && e.target.closest && e.target.closest(".tvr-app, .tvr-btn") && e.key === "Enter")) {
    remoteKey(keys[e.key]);
    e.preventDefault();
    e.stopImmediatePropagation();
  }
}, true);

document.addEventListener("keydown", function(e) {
  if (e.key === "Escape" && !isPhone() && document.getElementById("switches").classList.contains("open")
      && !playerOpen && !focusedCamId) toggleSwitches(false);
});

function setTab(tab) {
  currentTab = tab;
  document.body.classList.toggle("tab-live", tab === "live");
  document.body.classList.toggle("tab-events", tab === "events");
  document.body.classList.toggle("tab-switches", tab === "switches");
  document.querySelectorAll("#tabs button").forEach(function(b) { b.classList.toggle("active", b.dataset.tab === tab); });
  if (isPhone()) {
    document.getElementById("timeline").hidden = false;  // the tab decides on phones
    window.scrollTo(0, 0);
    if (tab === "events" && tl) applyZoom(true);  // it had no width while hidden
    if (tab === "switches") pollSwitches();
    if (tab === "live") liveCams.forEach(function(c, i) {
      var img = document.querySelectorAll("#grid .tile img")[i];
      if (img) updateTile(c.id, img);  // refresh right away, not on the next tick
    });
  }
}

var LIST_PAGE = 50;
var listShown = LIST_PAGE;
function renderList() {
  var root = document.getElementById("tl-list");
  root.innerHTML = "";
  var clips = visibleClips().slice().reverse();
  clips.slice(0, listShown).forEach(function(clip) {
    var row = document.createElement("button");
    row.type = "button";
    row.className = "ev" + (playingClip && playingClip.id === clip.id ? " playing" : "");
    row.dataset.id = clip.id;
    var sw = document.createElement("span");
    sw.className = "sw " + (clip.reason === "telegram_clip_request" ? "clip-request" : "g-" + clipGroup(clip));
    var t = document.createElement("span");
    t.className = "t";
    t.textContent = fmtOffset(clip.s);
    var what = document.createElement("span");
    what.className = "what";
    var title = document.createElement("b");
    title.textContent = cameraName(clip.cam);
    var detail = document.createElement("span");
    var bits = [];
    if (clip.labels && clip.labels.length) bits.push(clip.labels.join(", "));
    else if (clip.reason === "telegram_clip_request") bits.push("requested clip");
    else bits.push("motion");
    bits.push(Math.max(1, Math.round(clip.e - clip.s)) + "s");
    if (clip.hd) bits.push("HD");
    detail.textContent = bits.join(" · ");
    what.appendChild(title);
    what.appendChild(detail);
    row.appendChild(sw); row.appendChild(t); row.appendChild(what);
    row.onclick = function() { openClip(clip); };
    root.appendChild(row);
  });
  if (clips.length > listShown) {
    var more = document.createElement("button");
    more.type = "button";
    more.id = "tl-more";
    more.textContent = "Show more (" + (clips.length - listShown) + " older)";
    more.onclick = function() { listShown += LIST_PAGE; renderList(); };
    root.appendChild(more);
  }
}

function toggleTimeline(show) {
  var el = document.getElementById("timeline");
  if (typeof show !== "boolean") show = el.hidden;
  el.hidden = !show;
  var btn = document.getElementById("tl-toggle");
  btn.classList.toggle("active", show);
  btn.textContent = show ? "Timeline: on" : "Timeline: off";
  store("sentinel.timeline", show ? "1" : "0");
  layoutGrid();
  if (show && tl) applyZoom(false);  // it had no width while hidden
}

// Desktop grid: try every column count and keep the one where each camera picture
// (16:9, or the cameras' real shape once known) is largest in the space available.
var camAspect = 16 / 9;
var GRID_CSS = !window.CSS || !CSS.supports || CSS.supports("display", "grid");
function layoutGrid() {
  var grid = document.getElementById("grid");
  var n = grid.querySelectorAll(".tile:not(.weather)").length;
  if (isPhone() || !n) {
    grid.style.gridTemplateColumns = grid.style.gridAutoRows = "";
    // Phones: under the cameras, once a place is set.
    weatherTile.hidden = !(isPhone() && weather && weather.configured);
    return;
  }
  var gap = 2, pad = 4;
  var W = grid.clientWidth - pad, H = grid.clientHeight - pad;
  if (W <= 0 || H <= 0) return;
  var best = null;
  for (var cols = 1; cols <= n; cols++) {
    var rows = Math.ceil(n / cols);
    var w = Math.min((W - gap * (cols - 1)) / cols, ((H - gap * (rows - 1)) / rows) * camAspect);
    if (!best || w > best.w + 0.5) best = { cols: cols, w: w };
  }
  var tw = Math.floor(best.w), th = Math.floor(best.w / camAspect);
  grid.style.gridTemplateColumns = "repeat(" + best.cols + ", " + tw + "px)";
  grid.style.gridAutoRows = th + "px";
  weatherTile.style.setProperty("--cqh", th / 100 + "px");  // for browsers without container units (grid.css)
  weatherTile.style.setProperty("--cqw", tw / 100 + "px");
  if (!GRID_CSS) {  // see grid.css: the same sizes, on each tile
    Array.prototype.forEach.call(grid.querySelectorAll(".tile"), function(t) {
      t.style.width = tw + "px";
      t.style.height = th + "px";
    });
  }
  // Only in a cell that would otherwise be empty: it never shrinks the cameras.
  weatherTile.hidden = !weather || best.cols * Math.ceil(n / best.cols) <= n;
}
window.addEventListener("resize", layoutGrid);
document.addEventListener("keydown", function(e) {
  if ((e.key === "t" || e.key === "T") && !e.ctrlKey && !e.metaKey && !e.altKey && !playerOpen && !focusedCamId
      && !/^(INPUT|SELECT|TEXTAREA)$/.test((e.target.tagName || "")) && !isPhone()) toggleTimeline();
});

function shiftDay(dir) {
  if (!tl) return;
  var d = dir < 0 ? tl.prev_date : tl.next_date;
  if (d) loadTimeline(d);
}

async function loadTimeline(date) {
  var url = "/api/recordings" + (date ? "?date=" + encodeURIComponent(date) : "");
  try {
    var r = await fetch(url);
    if (r.status === 401) { location.href = "/login"; return; }
    if (!r.ok) throw new Error(r.status);
    var data = await r.json();
    var first = !tl || tl.date !== data.date;
    tl = data;
    renderTimeline(first);
  } catch (e) {
    document.getElementById("tl-summary").textContent = "Timeline unavailable";
  }
}

function renderTimeline(scrollToFocus) {
  document.getElementById("tl-date").value = tl.date;
  document.getElementById("tl-next").disabled = !tl.next_date;
  var n = visibleClips().length;
  document.getElementById("tl-summary").textContent = n + (n === 1 ? " clip" : " clips");
  document.getElementById("tl-latest").disabled = n === 0;
  document.getElementById("tl-empty").hidden = n > 0;

  var day = tl.day_seconds;
  var labels = document.getElementById("tl-labels");
  var tracks = document.getElementById("tl-tracks");
  labels.innerHTML = "";
  tracks.innerHTML = "";
  var trackByCam = {};
  tl.cameras.forEach(function(c) {
    var label = document.createElement("div");
    label.className = "tl-label";
    label.textContent = c.name;
    label.title = c.name;
    labels.appendChild(label);
    var track = document.createElement("div");
    track.className = "tl-track";
    tracks.appendChild(track);
    trackByCam[c.key] = track;
  });

  var frag = {};
  visibleClips().forEach(function(clip) {
    var track = trackByCam[clip.cam];
    if (!track) return;
    var mark = document.createElement("div");
    mark.className = "tl-mark " + (clip.reason === "telegram_clip_request" ? "clip-request" : "g-" + clipGroup(clip));
    if (playingClip && playingClip.id === clip.id) mark.classList.add("playing");
    mark.style.left = (clip.s / day * 100) + "%";
    mark.style.width = (Math.max(clip.e - clip.s, 1) / day * 100) + "%";
    mark.title = cameraName(clip.cam) + " · " + fmtOffset(clip.s) + (clip.labels && clip.labels.length ? " · " + clip.labels.join(", ") : "");
    mark.dataset.id = clip.id;
    mark.onclick = function() { openClip(clip); };
    (frag[clip.cam] = frag[clip.cam] || document.createDocumentFragment()).appendChild(mark);
  });
  Object.keys(frag).forEach(function(k) { trackByCam[k].appendChild(frag[k]); });
  if (scrollToFocus) listShown = LIST_PAGE;
  renderList();

  var nowLine = document.getElementById("tl-now");
  if (tl.now_offset != null) {
    nowLine.hidden = false;
    nowLine.style.left = (tl.now_offset / day * 100) + "%";
  } else {
    nowLine.hidden = true;
  }
  applyZoom(scrollToFocus);
}

function renderAxis() {
  var axis = document.getElementById("tl-axis");
  axis.innerHTML = "";
  if (!tl) return;
  var hours = Math.round(tl.day_seconds / 3600);
  var step = tlZoom >= 24 ? 0.25 : (tlZoom >= 4 ? 1 : 3);   // hours between labels
  for (var h = 0; h < hours; h += step) {
    var tick = document.createElement("div");
    tick.className = "tl-tick";
    tick.style.left = (h * 3600 / tl.day_seconds * 100) + "%";
    var span = document.createElement("span");
    span.textContent = fmtOffset(h * 3600).slice(0, 5);
    tick.appendChild(span);
    axis.appendChild(tick);
  }
}

function applyZoom(scrollToFocus) {
  var scroller = document.getElementById("tl-scroll");
  var inner = document.getElementById("tl-inner");
  var oldWidth = inner.offsetWidth || 1;
  var center = (scroller.scrollLeft + scroller.clientWidth / 2) / oldWidth;
  inner.style.width = (tlZoom * 100) + "%";
  document.querySelectorAll(".tl-zoom button").forEach(function(b) {
    b.classList.toggle("active", Number(b.dataset.zoom) === tlZoom);
  });
  renderAxis();
  var newWidth = inner.offsetWidth;
  if (scrollToFocus && tl) {
    // New day: show "now" (today) or the last clip (past days).
    var focus = tl.now_offset != null ? tl.now_offset
      : (tl.clips.length ? tl.clips[tl.clips.length - 1].s : 0);
    center = focus / tl.day_seconds;
  }
  scroller.scrollLeft = center * newWidth - scroller.clientWidth / 2;
}

function setZoom(z) {
  tlZoom = z;
  store("sentinel.timelineZoom", String(z));
  applyZoom(false);
}

// ---------------- Clip player ----------------
function sameCameraClips(clip) {
  return visibleClips().filter(function(c) { return c.cam === clip.cam; });
}

function openClip(clip) {
  playingClip = clip;
  playerOpen = true;
  var overlay = document.getElementById("player-overlay");
  var video = document.getElementById("player-video");
  var status = document.getElementById("player-status");
  document.getElementById("player-title").textContent =
    cameraName(clip.cam) + " — " + tl.date + " " + fmtOffset(clip.s) +
    (clip.labels && clip.labels.length ? "  · " + clip.labels.join(", ") : "") + (clip.hd ? "  · HD" : "");
  status.textContent = "Preparing clip…";
  status.hidden = false;
  video.src = "/recordings/" + clip.id + "/video";
  overlay.style.display = "flex";

  var list = sameCameraClips(clip);
  var idx = list.findIndex(function(c) { return c.id === clip.id; });
  document.getElementById("player-prev").disabled = idx <= 0;
  document.getElementById("player-next").disabled = idx < 0 || idx >= list.length - 1;

  document.querySelectorAll(".tl-mark.playing, .ev.playing").forEach(function(m) { m.classList.remove("playing"); });
  document.querySelectorAll('.tl-mark[data-id="' + clip.id + '"], .ev[data-id="' + clip.id + '"]').forEach(function(m) {
    m.classList.add("playing");
  });
  document.getElementById("player-next").focus();
}

function stepClip(dir) {
  if (!playingClip) return;
  var list = sameCameraClips(playingClip);
  var idx = list.findIndex(function(c) { return c.id === playingClip.id; });
  var next = list[idx + dir];
  if (next) openClip(next);
}

function playLatest() {
  var clips = visibleClips();
  if (clips.length) openClip(clips[clips.length - 1]);
}

function closePlayer() {
  var video = document.getElementById("player-video");
  video.pause();
  video.removeAttribute("src");
  video.load();
  document.getElementById("player-overlay").style.display = "none";
  playerOpen = false;
}

(function initPlayer() {
  var video = document.getElementById("player-video");
  var status = document.getElementById("player-status");
  video.addEventListener("loadeddata", function() { status.hidden = true; });
  video.addEventListener("error", function() {
    if (!video.getAttribute("src")) return;
    status.textContent = "This clip could not be played (it may have been deleted).";
    status.hidden = false;
  });
  document.addEventListener("keydown", function(e) {
    if (!playerOpen) return;
    if (e.key === "Escape") closePlayer();
    else if (e.key === "ArrowLeft" && e.target !== video) stepClip(-1);
    else if (e.key === "ArrowRight" && e.target !== video) stepClip(1);
  });
})();

(function initTimeline() {
  var z = Number(recall("sentinel.timelineZoom"));
  if (z === 1 || z === 4 || z === 24) tlZoom = z;
  toggleTimeline(recall("sentinel.timeline") !== "0");
  // The Home page's tiles: Cameras (the cameras, not whichever tab was used last), Switches (#switches), Remotes (#remote)
  setTab(location.hash === "#switches" && isPhone() ? "switches" : "live");
  if (location.hash === "#switches" && !isPhone()) toggleSwitches(true);
  var rm = /^#remote(?:=([\w.:-]+))?$/.exec(location.hash);  // (#remote=<id>: that TV, from the House page)
  if (rm) openRemoteFromHome(rm[1]);
  PHONE_QUERY.addEventListener("change", function() {
    if (isPhone()) setTab(currentTab);
    else toggleTimeline(recall("sentinel.timeline") !== "0");  // back to the desktop preference
    layoutGrid();
  });
  loadTimeline(null);
  window.addEventListener("resize", function() { if (tl) applyZoom(false); });
  pollMusic();
  pollSwitches();
  // Keep today's timeline current.
  setInterval(function() {
    if (tl && tl.now_offset != null && !document.hidden) loadTimeline(tl.date);
  }, 30000);
})();
