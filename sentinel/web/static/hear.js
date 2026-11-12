// "Hear alerts" switch on the home page and the camera page: while it's on, the page reads Sentinel's alerts out loud in Jarvis's
// voice (someone at the door, a car pulling in). sentinel/engine/heard.py, /api/heard.
// Browsers only play sound on a page that has been tapped, so after a reload the switch may ask for one tap.
(function () {
  if (/SMART-TV|Tizen|AFT\w|FKB/.test(navigator.userAgent)) return;  // wall screens and TVs: the house speaker's job
  var bar = document.querySelector("header .bar");            // the home page
  var strip = bar ? null : document.querySelector("header");  // the camera page: one of its header buttons
  if (!bar && !strip) return;
  var KEY = "sentinel-hear", EVERY_MS = 3000;
  var SILENCE = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQAAAAA=";

  var css = document.createElement("style");
  css.textContent =
    ".bar #hear-btn{margin:0;padding:6px 12px;border-radius:999px;border:1px solid #16303a;background:#0c151c;" +
    "color:#8fb6c4;font:600 14px system-ui,sans-serif;cursor:pointer;white-space:nowrap}" +
    "header #hear-btn.on{border-color:#00a8a8;background:rgba(0,255,255,.14);color:#fff}" +
    "header #hear-btn.tap{border-color:#ffb84d;color:#ffd699}" +
    "#hear-box{position:fixed;left:18px;right:18px;top:64px;z-index:9400;max-width:520px;margin:0 auto;box-sizing:border-box;" +
    "background:#0c151c;border:1px solid #00a8a8;border-radius:14px;padding:12px 14px;color:#fff;" +
    "font:17px/1.4 system-ui,sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.6)}" +
    "#hear-box[hidden]{display:none}";
  document.head.appendChild(css);

  var btn = document.createElement("button");
  btn.id = "hear-btn";
  btn.type = "button";
  var box = document.createElement("div");
  box.id = "hear-box";
  box.hidden = true;
  box.setAttribute("role", "status");
  if (bar) bar.insertBefore(btn, bar.querySelector("form"));
  else strip.insertBefore(btn, strip.lastElementChild);
  document.body.appendChild(box);

  var player = new Audio();
  var state = "off";  // off | tap (wanted, but the browser needs a tap first) | on
  var timer = null, hide = null, playing = false;
  var last = null;  // the newest alert this tab has been handed; kept while moving between pages
  try { last = /^\d+$/.test(sessionStorage.getItem(KEY) || "") ? sessionStorage.getItem(KEY) : null; } catch (e) {}
  var queue = [];

  function wanted() { try { return localStorage.getItem(KEY) === "1"; } catch (e) { return false; } }
  function remember(on) { try { on ? localStorage.setItem(KEY, "1") : localStorage.removeItem(KEY); } catch (e) {} }

  function draw() {
    btn.className = (strip ? "header-btn " : "") + (state === "on" ? "on" : state === "tap" ? "tap" : "");
    btn.textContent = state === "on" ? "🔔 Alerts on" : state === "tap" ? "🔕 Tap to hear alerts" : "🔕 Hear alerts";
    btn.title = state === "on" ? "This page reads alerts out loud. Tap to turn that off."
      : "Have this page read alerts out loud (someone at the door, a car pulling in) while it's open.";
    btn.setAttribute("aria-pressed", state === "on" ? "true" : "false");
  }

  function show(text) {
    box.textContent = "🔔 " + text;
    box.hidden = false;
    clearTimeout(hide);
    hide = setTimeout(function () { box.hidden = true; }, 15000);
  }

  function browserVoice(text, done) {
    if (!window.speechSynthesis || !window.SpeechSynthesisUtterance) return done();
    var u = new SpeechSynthesisUtterance(text);
    u.onend = u.onerror = done;
    try { speechSynthesis.speak(u); } catch (e) { done(); }
  }

  function say(item, done) {
    var finished = false, guard = setTimeout(end, 30000);
    function end() { if (finished) return; finished = true; clearTimeout(guard); player.onended = player.onerror = null; done(); }
    fetch("/api/heard/" + item.id + ".wav", { credentials: "same-origin" }).then(function (r) {
      if (!r.ok) throw new Error("no voice");
      return r.blob();
    }).then(function (blob) {
      var url = URL.createObjectURL(blob);
      player.onended = player.onerror = function () { URL.revokeObjectURL(url); end(); };
      player.src = url;
      return player.play();
    }).catch(function () { browserVoice(item.text, end); });
  }

  function next() {
    if (playing || !queue.length || state !== "on") return;
    playing = true;
    var item = queue.shift();
    if (item.id) show(item.text);
    say(item, function () { playing = false; next(); });
  }

  // One ask at a time, and each alert once: on a slow phone connection the asks used to pile up while they waited,
  // then all came back with the same alert, which was read out once per ask (2026-11-12).
  var asking = false;
  function poll() {
    if (state !== "on" || asking) return;
    asking = true;
    var stopper = window.AbortController ? new AbortController() : null;
    var giveUp = setTimeout(function () { if (stopper) stopper.abort(); }, 10000);
    fetch("/api/heard" + (last === null ? "" : "?after=" + last),
          { credentials: "same-origin", signal: stopper ? stopper.signal : undefined }).then(function (r) {
      if (r.status === 401 || r.status === 403) { stop(); state = "off"; draw(); throw new Error("signed out"); }
      return r.json();
    }).then(function (d) {
      (d.items || []).forEach(function (i) {
        if (last !== null && Number(i.id) <= Number(last)) return;  // already read out
        if (queue.some(function (q) { return q.id === i.id; })) return;
        queue.push(i);
      });
      last = d.latest;  // (also right after Sentinel restarts: the numbers only go up)
      try { sessionStorage.setItem(KEY, String(last)); } catch (e) {}
      next();
    }).catch(function () {}).then(function () { clearTimeout(giveUp); asking = false; });
  }

  function stop() { clearInterval(timer); timer = null; queue = []; try { player.pause(); } catch (e) {} playing = false; }

  function start(hello) {
    state = "on"; draw();
    if (hello) { queue.push({ id: 0, text: "I'll read alerts out here." }); next(); }
    if (!timer) { poll(); timer = setInterval(poll, EVERY_MS); }
  }

  // Sound is only allowed once the page has been tapped: try, and ask for the tap when the browser says no
  function unlock(fromTap) {
    player.src = SILENCE;
    var p = player.play();
    if (!p || !p.then) return start(fromTap);
    p.then(function () { start(fromTap); }, function () { state = "tap"; draw(); });
  }

  btn.onclick = function () {
    if (state === "on") { remember(false); stop(); state = "off"; draw(); box.hidden = true; return; }
    remember(true);
    unlock(true);
  };

  draw();
  if (wanted()) unlock(false);
})();
