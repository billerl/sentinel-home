// Talk button: talk to Jarvis through this phone's microphone (or its headset). sentinel/engine/phone_talk.py.
// Tap, speak, and it stops by itself when you do (or tap again). The answer is shown and spoken here, on the phone.
(function () {
  if (/SMART-TV|Tizen|AFT\w|FKB/.test(navigator.userAgent)) return;  // wall screens and TVs have no mic

  var css = document.createElement("style");
  css.textContent =
    "#talk-btn{position:fixed;right:18px;bottom:18px;z-index:9500;width:68px;height:68px;border-radius:50%;" +
    "border:2px solid #00a8a8;background:#062330;color:#fff;font-size:30px;line-height:1;cursor:pointer;" +
    "box-shadow:0 4px 18px rgba(0,0,0,.6);-webkit-tap-highlight-color:transparent}" +
    "#talk-btn.rec{background:#b3261e;border-color:#ff8080;animation:talk-pulse 1.2s ease-in-out infinite}" +
    "#talk-btn.busy{opacity:.75}" +
    "@keyframes talk-pulse{50%{box-shadow:0 0 0 12px rgba(255,107,107,.25)}}" +
    "@media (prefers-reduced-motion: reduce){#talk-btn.rec{animation:none}}" +
    "#talk-box{position:fixed;left:18px;right:18px;bottom:98px;z-index:9500;max-width:360px;margin-left:auto;box-sizing:border-box;" +
    "background:#0c151c;border:1px solid #16303a;border-radius:14px;padding:12px 14px;color:#cfe;" +
    "font:15px/1.45 system-ui,sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.6)}" +
    "#talk-box[hidden]{display:none}" +
    "#talk-box .you{color:#8fb6c4;margin-bottom:6px}#talk-box .jarvis{color:#fff}" +
    "#talk-box .state{color:#7fe8c8;font-weight:600}#talk-box .err{color:#ff9b9b}" +
    "#talk-box .level{height:6px;border-radius:3px;background:#16303a;margin-top:8px;overflow:hidden}" +
    "#talk-box .level i{display:block;height:100%;width:0;background:#7fe8c8}" +
    "#talk-box .btns{display:flex;gap:8px;margin-top:10px}" +
    "#talk-box .btns button{flex:1;padding:10px;border-radius:10px;border:1px solid #00a8a8;background:rgba(0,255,255,.12);" +
    "color:#fff;font:inherit;font-weight:600;cursor:pointer}" +
    "#talk-box .close{float:right;background:none;border:0;color:#6f97a5;font-size:18px;cursor:pointer;padding:0 0 0 8px}";
  document.head.appendChild(css);

  var btn = document.createElement("button");
  btn.id = "talk-btn";
  btn.type = "button";
  btn.textContent = "🎙️";
  btn.title = btn.ariaLabel = "Talk to Jarvis";
  var box = document.createElement("div");
  box.id = "talk-box";
  box.hidden = true;
  box.setAttribute("aria-live", "polite");
  document.body.appendChild(box);
  document.body.appendChild(btn);

  var player = new Audio();
  var SILENCE = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQAAAAA=";
  var state = "idle";  // idle -> rec -> busy -> (playing) -> idle
  var rec = null, stream = null, chunks = [], audioCtx = null, timer = null, pending = "", dictating = false;

  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function show(html) { box.innerHTML = '<button class="close" type="button" aria-label="Close">✕</button>' + html; box.hidden = false;
    box.querySelector(".close").onclick = function () { stopAll(); box.hidden = true; }; }
  function setState(s) { state = s; btn.className = s === "rec" ? "rec" : s === "busy" ? "busy" : ""; }

  function pickType() {
    if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) return "";
    var types = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm", "audio/ogg;codecs=opus"];
    for (var i = 0; i < types.length; i++) if (MediaRecorder.isTypeSupported(types[i])) return types[i];
    return "";
  }

  function release() {
    clearInterval(timer); timer = null;
    if (stream) stream.getTracks().forEach(function (t) { t.stop(); });  // the phone's "mic in use" light goes off
    stream = null;
    if (audioCtx) { try { audioCtx.close(); } catch (e) {} audioCtx = null; }
  }
  function stopAll() {
    if (rec && rec.state !== "inactive") { rec.onstop = null; try { rec.stop(); } catch (e) {} }
    rec = null; release(); player.pause(); pending = ""; dictating = false; setState("idle");
  }

  btn.onclick = function () {
    if (state === "rec") { if (rec && rec.state !== "inactive") rec.stop(); return; }
    if (state === "busy") return;
    player.pause();
    pending = ""; dictating = false;
    // A phone only lets a page play sound it started from a tap: start the player now, for the answer later
    player.src = SILENCE; player.play().catch(function () {});
    start();
  };

  function start() {
    if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
      show('<div class="err">This page can’t use the microphone here. Open Sentinel by its https:// address '
        + "(the one you use away from home) in Chrome or Safari.</div>");
      return;
    }
    navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } })
      .then(record)
      .catch(function (e) {
        setState("idle");
        show('<div class="err">' + (e && e.name === "NotAllowedError"
          ? "The microphone is blocked for this site. Allow it in the browser's site settings (the 🔒 by the address), then try again."
          : "Couldn't start the microphone (" + esc(e && e.message || e) + ").") + "</div>");
      });
  }

  function record(s) {
    stream = s; chunks = [];
    var type = pickType();
    var opts = { audioBitsPerSecond: 32000 };
    if (type) opts.mimeType = type;
    try { rec = new MediaRecorder(stream, opts); } catch (e) { rec = new MediaRecorder(stream); }
    rec.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
    rec.onstop = function () { var heard = spoke; release(); send(new Blob(chunks, { type: rec.mimeType || type }), heard); };
    rec.start(250);
    setState("rec");
    show('<div class="state">' + (dictating ? "Go ahead, I’m taking the note…" : "Listening…") + "</div>"
      + '<div class="level"><i></i></div><div class="you" style="margin:8px 0 0">Tap the button again when you’re done, or just stop talking.</div>');
    // Stop by itself once they've spoken and gone quiet (longer pauses allowed while taking a note)
    var spoke = false, quietSince = 0, began = Date.now(), noise = 0, frames = 0;
    var quietMs = dictating ? 2500 : 1300, maxMs = dictating ? 30000 : 15000;
    try {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      var an = audioCtx.createAnalyser();
      an.fftSize = 1024;
      audioCtx.createMediaStreamSource(stream).connect(an);
      var buf = new Float32Array(an.fftSize), bar = box.querySelector(".level i");
      timer = setInterval(function () {
        an.getFloatTimeDomainData(buf);
        var sum = 0;
        for (var i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
        var level = Math.sqrt(sum / buf.length), now = Date.now();
        if (bar) bar.style.width = Math.min(100, level * 600) + "%";
        if (frames < 3) { noise += level / 3; frames++; return; }
        var loud = level > Math.max(0.02, noise * 2.5);
        if (loud) { spoke = true; quietSince = 0; } else if (spoke && !quietSince) quietSince = now;
        if ((spoke && quietSince && now - quietSince > quietMs) || now - began > maxMs || (!spoke && now - began > 7000)) {
          if (rec && rec.state !== "inactive") rec.stop();
        }
      }, 100);
    } catch (e) { spoke = true; }  // no level meter: they tap to stop
  }

  function send(blob, spoke) {
    if (!spoke || !blob.size) {
      setState("idle");
      show('<div class="you">I didn’t hear anything. Tap 🎙️ and try again.</div>');
      return;
    }
    setState("busy");
    show('<div class="state">Thinking…</div>');
    var reader = new FileReader();
    reader.onload = function () {
      var b64 = String(reader.result).split(",")[1] || "";
      post("/api/talk", { audio: b64, pending: dictating ? pending : "" });
    };
    reader.readAsDataURL(blob);
  }

  function post(url, body) {
    fetch(url, { method: "POST", credentials: "same-origin",
                 headers: { "Content-Type": "application/json", "X-Sentinel-Request": "1" }, body: JSON.stringify(body) })
      .then(function (r) {
        if (r.status === 401) { location.href = "/login"; throw new Error("signed out"); }
        return r.json().then(function (d) { if (!r.ok) throw new Error(d.error || ("HTTP " + r.status)); return d; });
      })
      .then(answer)
      .catch(function (e) { setState("idle"); show('<div class="err">' + esc(e.message) + "</div>"); });
  }

  function answer(d) {
    dictating = false; pending = "";
    var html = d.heard ? '<div class="you">You: ' + esc(d.heard) + "</div>" : "";
    html += d.reply ? '<div class="jarvis">' + esc(d.reply) + "</div>" : '<div class="you">' + esc(d.note || "") + "</div>";
    if (d.ask === "confirm") html += '<div class="btns"><button type="button" data-yes="1">Yes</button><button type="button">No</button></div>';
    show(html);
    if (d.ask === "confirm") {
      Array.prototype.forEach.call(box.querySelectorAll(".btns button"), function (b) {
        b.onclick = function () {
          player.pause();
          player.src = SILENCE; player.play().catch(function () {});
          setState("busy");
          show('<div class="state">Okay…</div>');
          post("/api/talk/answer", { id: d.id, yes: !!b.getAttribute("data-yes") });
        };
      });
    }
    var next = d.ask === "dictate" ? function () { pending = d.id; dictating = true; start(); } : null;
    setState("idle");
    if (d.audio) {
      player.onended = function () { player.onended = null; if (next) next(); };
      player.src = "data:audio/wav;base64," + d.audio;
      player.play().catch(function () { player.onended = null; if (next) next(); });
    } else if (next) {
      next();
    }
  }
})();
