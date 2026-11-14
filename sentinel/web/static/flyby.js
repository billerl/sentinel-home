// Fly-by: on the TVs' camera page, a plane about to pass over the house (/api/planes/overhead: within 3 miles
// sideways, any height) flies across the screen as a drawing of its kind in its airline's colours, with a card saying
// who it is. Drawn side views, nose to the right (mirrored for planes heading west). Plain ES5: Chrome 56 TV.
(function() {
  // ---------------------------------------------------------------- which drawing: ICAO type code -> family
  var FAMILIES = [
    [/^(C17|C5|C5M)$/, "airlifter"],
    [/^(C130|C30J|C27J|C160|A400)$/, "c130"],
    [/^(MD11|DC10|L101|KC10)$/, "trijet"],
    [/^(B74|A34|A38|K35|KC135|E3|B52|E6|C135|B70)/, "quad"],
    [/^(A30|A31|A33|A35|B76|B77|B78|KC46|IL96)/, "wide"],
    [/^(B71|MD8|MD9|DC9|CRJ|E135|E145|E35L|F100|F70)/, "rj"],
    [/^(E17|E19|E75|E29|BCS)/, "ejet"],
    [/^(A1[89]|A2|B73|B3|B75|A32|P8)/, "narrow"],
    [/^(AT4|AT7|DH8|DHC[78]|SF34|E120|B190|D328|SB20)/, "turboprop"],
    [/^(F1[5-8]|F35|F22|A10|T38|F18|EUFI|F16|T7)/, "fighter"],
    [/^(R22|R44|R66|EC|AS[35]|B06|B07|B40|B42|B47|A1[0-6]9|A139|S76|H60|UH60|H47|CH47|H64|LAKO|H1|H500|MD52|AS65|EH)/, "heli"],
    [/^(C25|C5[0-9]|C6|C7[05]|LJ|GL|GLF|CL[36]|E50|E55|E54|HDJT|FA|F2TH|F900|H25|PRM|PC24|SF50|BE40|G150|G280|ASTR)/, "bizjet"],
    [/^(PA3[14]|PA44|PA23|BE5[58]|BE76|BE9|BE20|BE30|B350|DA42|DA62|C3[0-4]|C4[0-4]|P68|AC[0-9]|MU2|C12)/, "twin"],
    [/^(C1[5-9]|C20[5-8]|C21|PA18|J3|CH7|M7|BE18|C162)/, "high"],
    [/^(P28|PA2|PA32|PA46|P46|SR2|S22|DA4|DA2|M20|BE3|TBM|PC12|AT[568]T|RV|T6|C77R)/, "low"]
  ];
  var BY_CATEGORY = { A1: "low", A2: "bizjet", A3: "narrow", A4: "narrow", A5: "wide", A6: "fighter", A7: "heli" };

  function family(p) {
    var code = (p.type || "").toUpperCase();
    for (var i = 0; i < FAMILIES.length; i++) if (code && FAMILIES[i][0].test(code)) return FAMILIES[i][1];
    return BY_CATEGORY[p.category] || "narrow";
  }

  // Shape of each family in a 200 x 64 box: body from x0 (tail) to x1 (nose), top yt, bottom yb; how wide it
  // flies across the screen (share of the screen's width); where the wing and engines go; how many seconds it takes.
  var SHAPES = {
    narrow:    { x0: 14, x1: 190, yt: 28, yb: 45, wing: "low",  eng: "wing",  tail: "conv", w: 0.36, secs: 14 },
    wide:      { x0: 10, x1: 192, yt: 28, yb: 46, wing: "low",  eng: "big",   tail: "conv", w: 0.46, secs: 15 },
    quad:      { x0: 10, x1: 192, yt: 28, yb: 46, wing: "low",  eng: "four",  tail: "conv", w: 0.5, secs: 16, hump: true },
    trijet:    { x0: 10, x1: 192, yt: 28, yb: 46, wing: "low",  eng: "big",   tail: "conv", w: 0.46, secs: 15, tailEngine: true },
    rj:        { x0: 20, x1: 186, yt: 30, yb: 43, wing: "low",  eng: "rear",  tail: "t",    w: 0.3, secs: 14 },
    ejet:      { x0: 18, x1: 188, yt: 29, yb: 44, wing: "low",  eng: "wing",  tail: "conv", w: 0.32, secs: 14 },
    turboprop: { x0: 18, x1: 186, yt: 29, yb: 43, wing: "high", eng: "props", tail: "t",    w: 0.3, secs: 16 },
    c130:      { x0: 14, x1: 184, yt: 30, yb: 48, wing: "high", eng: "props", tail: "conv", w: 0.38, secs: 17, blunt: true },
    airlifter: { x0: 12, x1: 188, yt: 29, yb: 48, wing: "high", eng: "hung",  tail: "t",    w: 0.46, secs: 16, blunt: true },
    bizjet:    { x0: 30, x1: 182, yt: 31, yb: 44, wing: "low",  eng: "rear",  tail: "t",    w: 0.22, secs: 12 },
    twin:      { x0: 34, x1: 176, yt: 30, yb: 44, wing: "low",  eng: "props", tail: "conv", w: 0.2, secs: 16, small: true },
    high:      { x0: 42, x1: 168, yt: 30, yb: 47, wing: "high", eng: "nose",  tail: "conv", w: 0.18, secs: 18, small: true, gear: true },
    low:       { x0: 42, x1: 168, yt: 30, yb: 45, wing: "low",  eng: "nose",  tail: "conv", w: 0.18, secs: 18, small: true },
    heli:      { w: 0.22, secs: 20 },
    fighter:   { w: 0.3, secs: 8 }
  };

  // ---------------------------------------------------------------- airline colours (by flight-number prefix)
  // body, belly, tail, accent (band on the tail), engine; optional stripe (cheat line) and crown (top band)
  function L(body, belly, tail, accent, engine, extra) {
    var o = { body: body, belly: belly, tail: tail, accent: accent, engine: engine };
    for (var k in extra || {}) o[k] = extra[k];
    return o;
  }
  var DELTA = L("#f4f6f8", "#13294b", "#c8102e", "#13294b", "#13294b");
  var UNITED = L("#f4f6f8", "#0c2340", "#0c2340", "#1b75bb", "#0c2340");
  var AMERICAN = L("#c9d0d6", "#c9d0d6", "#0b4ea2", "#c8102e", "#b7bfc6");
  var ALASKA = L("#f4f6f8", "#f4f6f8", "#01426a", "#48a9c5", "#01426a", { stripe: "#48a9c5" });
  var LIVERIES = {
    DAL: DELTA, EDV: DELTA,
    UAL: UNITED, GJS: UNITED, ASH: UNITED, UCA: UNITED,
    AAL: AMERICAN, ENY: AMERICAN, JIA: AMERICAN, PDT: AMERICAN, AWI: AMERICAN,
    SWA: L("#304cb2", "#ffbf27", "#304cb2", "#e31837", "#304cb2", { stripe: "#e31837" }),
    NKS: L("#ffe600", "#ffe600", "#ffe600", "#1b1b1b", "#ffe600"),
    FFT: L("#f4f6f8", "#f4f6f8", "#00843d", "#7ab800", "#00843d"),
    ASA: ALASKA, QXE: ALASKA,
    JBU: L("#f4f6f8", "#0033a0", "#0033a0", "#5d9be3", "#0033a0"),
    AAY: L("#f4f6f8", "#f4f6f8", "#003a70", "#f99d1c", "#003a70", { stripe: "#f99d1c" }),
    SCX: L("#f4f6f8", "#f4f6f8", "#f58025", "#0f2a4a", "#f58025"),
    MXY: L("#f4f6f8", "#f4f6f8", "#14324f", "#36b5c9", "#14324f"),
    FDX: L("#f4f6f8", "#d9dde1", "#4d148c", "#ff6600", "#4d148c", { crown: "#4d148c" }),
    UPS: L("#4a2e22", "#e6e6e6", "#4a2e22", "#ffb500", "#4a2e22"),
    GTI: L("#f4f6f8", "#f4f6f8", "#13294b", "#ff9900", "#13294b"),
    ACA: L("#f4f6f8", "#1b1b1b", "#1b1b1b", "#d22630", "#1b1b1b"),
    JZA: L("#f4f6f8", "#1b1b1b", "#1b1b1b", "#d22630", "#1b1b1b")
  };
  var MILITARY = L("#737d84", "#737d84", "#69737a", "#5a6369", "#626b71");
  var PLAIN = L("#f4f6f8", "#d5dce2", "#8796a3", "#5f7182", "#c3cad0");
  var PRIVATE_STRIPES = ["#1f5fa8", "#a83232", "#2f7d4a", "#d18a1a", "#5a3fa0", "#1d7f8c"];

  function livery(p, fam) {
    if (p.military) return MILITARY;
    var prefix = (p.callsign || "").slice(0, 3);
    if (/^[A-Z]{3}\d/.test(p.callsign || "") && LIVERIES[prefix]) return LIVERIES[prefix];
    if (/^N\d/.test(p.callsign || p.reg || "") || fam === "heli" || SHAPES[fam].small) {
      // private planes and helicopters: white with a stripe, the same colour every time for the same plane
      var id = p.reg || p.callsign || p.id || "", h = 0;
      for (var i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) % 997;
      var c = PRIVATE_STRIPES[h % PRIVATE_STRIPES.length];
      return L("#f4f6f8", fam === "heli" ? c : "#f4f6f8", "#f4f6f8", c, "#c3cad0", { stripe: c });
    }
    return PLAIN;
  }

  // ---------------------------------------------------------------- drawing
  var uid = 0;
  function pts(list) { return list.map(function(q) { return q[0].toFixed(1) + "," + q[1].toFixed(1); }).join(" "); }
  function poly(list, fill, extra) { return '<polygon points="' + pts(list) + '" fill="' + fill + '"' + (extra || "") + "/>"; }
  function rrect(x, y, w, h, r, fill, extra) {
    return '<rect x="' + x.toFixed(1) + '" y="' + y.toFixed(1) + '" width="' + w.toFixed(1) + '" height="' + h.toFixed(1) +
      '" rx="' + r.toFixed(1) + '" fill="' + fill + '"' + (extra || "") + "/>";
  }
  var GLASS = "#1d2a35", WING = "#b9c2ca", EDGE = ' stroke="rgba(0,0,0,0.25)" stroke-width="0.5"';

  function airplane(s, c) {
    var x0 = s.x0, x1 = s.x1, yt = s.yt, yb = s.yb, Lw = x1 - x0, H = yb - yt, out = [];
    var id = "fb" + (++uid);
    var noseLen = s.small ? H * 0.7 : s.blunt ? H * 0.75 : H * 1.15;
    var body = "M" + x0 + "," + (yt + H * 0.15) + " L" + (x0 + Lw * 0.18) + "," + yt + " L" + (x1 - noseLen) + "," + yt +
      " C" + (x1 - noseLen * 0.3) + "," + yt + " " + x1 + "," + (yt + H * 0.42) + " " + x1 + "," + (yt + H * 0.62) +
      " C" + x1 + "," + (yt + H * 0.88) + " " + (x1 - noseLen * 0.35) + "," + yb + " " + (x1 - noseLen) + "," + yb +
      " L" + (x0 + Lw * 0.28) + "," + yb + " C" + (x0 + Lw * 0.16) + "," + yb + " " + (x0 + Lw * 0.06) + "," + (yt + H * 0.55) +
      " " + x0 + "," + (yt + H * 0.42) + " Z";
    var finH = s.small ? H * 1.25 : H * 1.45, finTop = yt - finH;
    // fin (behind the body), with the airline's band across it
    var fin = [[x0 + Lw * 0.2, yt + 1], [x0 + Lw * 0.03, yt + H * 0.2], [x0, finTop], [x0 + Lw * 0.075, finTop]];
    out.push(poly(fin, c.tail, EDGE));
    out.push(poly([[x0 + Lw * 0.12, yt + 0.5], [x0 + Lw * 0.06, yt + 0.5], [x0 + Lw * 0.03, finTop + finH * 0.35],
                   [x0 + Lw * 0.075, finTop + finH * 0.2]], c.accent));
    if (s.tail === "t") {
      out.push(poly([[x0 - Lw * 0.02, finTop], [x0 + Lw * 0.12, finTop], [x0 + Lw * 0.1, finTop + H * 0.22], [x0 - Lw * 0.03, finTop + H * 0.22]], c.tail, EDGE));
    }
    if (s.tailEngine) out.push(rrect(x0 + Lw * 0.02, yt - H * 0.45, Lw * 0.17, H * 0.5, H * 0.22, c.engine, EDGE));
    // far wing (high-wing planes: on top) and engines hung under a high wing go behind the body
    if (s.wing === "high") {
      out.push(rrect(x0 + Lw * 0.38, yt - H * 0.16, Lw * 0.24, H * 0.24, H * 0.1, WING, EDGE));
    }
    // body, belly, stripes, windows
    out.push('<clipPath id="' + id + '"><path d="' + body + '"/></clipPath>');
    out.push('<path d="' + body + '" fill="' + c.body + '"' + EDGE + "/>");
    out.push('<g clip-path="url(#' + id + ')">' + rrect(x0, yt + H * 0.6, Lw + 2, H, 0, c.belly) +
      (c.stripe ? rrect(x0, yt + H * 0.5, Lw + 2, H * 0.11, 0, c.stripe) : "") +
      (c.crown ? rrect(x0, yt - 1, Lw + 2, H * 0.3, 0, c.crown) : "") + "</g>");
    var winY = yt + H * 0.33, winFrom = x0 + Lw * (s.small ? 0.5 : 0.26), winTo = x1 - noseLen * (s.small ? 1.05 : 1.45);
    if (s.small) {
      out.push(rrect(winFrom, yt + H * 0.18, winTo - winFrom, H * 0.3, H * 0.08, GLASS, ' opacity="0.8"'));
    } else {
      out.push('<line x1="' + winFrom + '" y1="' + winY + '" x2="' + winTo + '" y2="' + winY + '" stroke="' + GLASS +
        '" stroke-width="' + (H * 0.13).toFixed(1) + '" stroke-dasharray="1.1 1.5" opacity="0.75"/>');
    }
    if (s.hump) {  // 747: the upper deck
      out.push('<path d="M' + (x1 - Lw * 0.42) + "," + (yt + 0.5) + " C" + (x1 - Lw * 0.32) + "," + (yt - H * 0.42) + " " +
        (x1 - Lw * 0.16) + "," + (yt - H * 0.5) + " " + (x1 - noseLen * 0.55) + "," + (yt + H * 0.25) + ' Z" fill="' + c.body + '"/>');
      out.push('<line x1="' + (x1 - Lw * 0.3) + '" y1="' + (yt - H * 0.12) + '" x2="' + (x1 - Lw * 0.14) + '" y2="' + (yt - H * 0.12) +
        '" stroke="' + GLASS + '" stroke-width="' + (H * 0.1).toFixed(1) + '" stroke-dasharray="1.1 1.5" opacity="0.75"/>');
    }
    // cockpit windows
    out.push(poly([[x1 - noseLen * 0.85, yt + H * 0.24], [x1 - noseLen * 0.3, yt + H * 0.26], [x1 - noseLen * 0.18, yt + H * 0.42],
                   [x1 - noseLen * 0.85, yt + H * 0.42]], GLASS));
    // tail plane (conventional tails: at the tail cone)
    if (s.tail !== "t") {
      out.push(poly([[x0 + Lw * 0.01, yt + H * 0.3], [x0 + Lw * 0.14, yt + H * 0.4], [x0 + Lw * 0.12, yt + H * 0.58],
                     [x0 - Lw * 0.01, yt + H * 0.52]], c.tail, EDGE));
    }
    // near wing (low wings: swept down toward us)
    if (s.wing === "low") {
      out.push(poly([[x0 + Lw * 0.56, yb - H * 0.25], [x0 + Lw * 0.38, yb - H * 0.2], [x0 + Lw * 0.27, yb + H * 0.6],
                     [x0 + Lw * 0.35, yb + H * 0.6]], WING, EDGE));
    }
    // engines
    function jet(x, y, w, h) {
      return rrect(x, y, w, h, h * 0.45, c.engine, EDGE) + '<ellipse cx="' + (x + w).toFixed(1) + '" cy="' + (y + h / 2).toFixed(1) +
        '" rx="' + (h * 0.18).toFixed(1) + '" ry="' + (h * 0.42).toFixed(1) + '" fill="#2a3036"/>';
    }
    function prop(x, y, h) {
      return '<ellipse cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) + '" rx="' + (h * 0.09).toFixed(1) + '" ry="' + (h * 0.95).toFixed(1) +
        '" fill="rgba(30,34,38,0.28)"/><circle cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) + '" r="' + (h * 0.13).toFixed(1) + '" fill="#2a3036"/>';
    }
    if (s.eng === "wing") out.push(jet(x0 + Lw * 0.47, yb - H * 0.05, Lw * 0.13, H * 0.48));
    if (s.eng === "big") out.push(jet(x0 + Lw * 0.47, yb - H * 0.02, Lw * 0.14, H * 0.55));
    if (s.eng === "four") {
      out.push(jet(x0 + Lw * 0.36, yb + H * 0.18, Lw * 0.11, H * 0.45));
      out.push(jet(x0 + Lw * 0.49, yb - H * 0.04, Lw * 0.12, H * 0.5));
    }
    if (s.eng === "rear") out.push(jet(x0 + Lw * 0.17, yt - H * 0.12, Lw * 0.15, H * 0.5) +
      rrect(x0 + Lw * 0.22, yt + H * 0.25, Lw * 0.05, H * 0.2, 0.5, c.engine));
    if (s.eng === "hung") out.push(jet(x0 + Lw * 0.5, yt + H * 0.12, Lw * 0.12, H * 0.42));
    if (s.eng === "props") {
      var ny = s.wing === "high" ? yt + H * 0.12 : yb - H * 0.12;
      out.push(rrect(x0 + Lw * 0.5, ny - H * 0.17, Lw * 0.12, H * 0.34, H * 0.15, c.engine, EDGE) + prop(x0 + Lw * 0.62, ny, H));
    }
    if (s.eng === "nose") out.push(prop(x1 + 0.8, yt + H * 0.55, H));
    if (s.wing === "high" && s.small) {  // Cessna: the wing strut
      out.push('<line x1="' + (x0 + Lw * 0.56) + '" y1="' + (yb - H * 0.2) + '" x2="' + (x0 + Lw * 0.5) + '" y2="' + (yt - H * 0.02) +
        '" stroke="' + WING + '" stroke-width="1.2"/>');
    }
    if (s.gear) {  // fixed wheels
      [[x0 + Lw * 0.6, yb + H * 0.4], [x1 - noseLen * 0.6, yb + H * 0.4]].forEach(function(w) {
        out.push('<line x1="' + w[0].toFixed(1) + '" y1="' + (yb - 1) + '" x2="' + w[0].toFixed(1) + '" y2="' + w[1].toFixed(1) +
          '" stroke="#3a4148" stroke-width="1"/><circle cx="' + w[0].toFixed(1) + '" cy="' + w[1].toFixed(1) + '" r="' + (H * 0.18).toFixed(1) +
          '" fill="#2a3036"/>');
      });
    }
    return out.join("");
  }

  function helicopter(c) {
    var out = [];
    out.push(poly([[118, 33], [34, 35], [34, 38.5], [118, 42]], c.body, EDGE));  // tail boom
    out.push(poly([[30, 22], [40, 34], [36, 39], [28, 39]], c.belly, EDGE));  // fin
    out.push('<ellipse cx="33" cy="30" rx="7" ry="7" fill="rgba(30,34,38,0.25)"/>');  // tail rotor
    out.push('<path d="M104,30 C104,22 120,20 140,20 C160,20 176,26 180,36 C182,42 176,48 164,48 L112,48 C106,48 104,42 104,36 Z" fill="' +
      c.body + '"' + EDGE + "/>");
    out.push('<path d="M104,38 L182,38 C182,44 176,48 164,48 L112,48 C106,48 104,44 104,38 Z" fill="' + c.belly + '"/>');
    out.push('<path d="M156,22 C168,23 177,29 180,36 L158,36 Z" fill="' + GLASS + '" opacity="0.85"/>');  // windscreen
    out.push(rrect(124, 25, 24, 9, 2, GLASS, ' opacity="0.7"'));  // cabin window
    out.push(rrect(132, 13, 6, 8, 1, "#3a4148"));  // mast
    out.push('<ellipse cx="130" cy="13" rx="66" ry="1.8" fill="rgba(30,34,38,0.45)"/>');  // main rotor (a blur)
    out.push('<line x1="112" y1="56" x2="176" y2="56" stroke="#3a4148" stroke-width="2" stroke-linecap="round"/>' +
      '<line x1="124" y1="48" x2="120" y2="56" stroke="#3a4148" stroke-width="1.5"/><line x1="162" y1="48" x2="166" y2="56" stroke="#3a4148" stroke-width="1.5"/>');
    return out.join("");
  }

  function fighter(c) {
    return poly([[24, 30], [56, 29], [40, 8], [28, 8]], c.tail, EDGE) +
      '<path d="M18,30 L150,29 C166,29 182,31 196,34 C182,37 166,38 150,38 L22,38 Z" fill="' + c.body + '"' + EDGE + "/>" +
      '<path d="M138,29 C142,23 160,23 168,30 Z" fill="#3d5a73"/>' +
      poly([[70, 36], [118, 35], [60, 50], [48, 50]], "#6b747b", EDGE) +
      poly([[20, 33], [44, 34], [36, 42], [16, 41]], "#6b747b", EDGE) +
      '<ellipse cx="18" cy="34" rx="2" ry="3.4" fill="#2a3036"/>';
  }

  function drawing(p) {
    var fam = family(p), c = livery(p, fam);
    var inner = fam === "heli" ? helicopter(c) : fam === "fighter" ? fighter(c) : airplane(SHAPES[fam], c);
    return { fam: fam, svg: '<svg viewBox="-4 0 208 64" width="100%" height="100%" preserveAspectRatio="xMidYMid meet">' + inner + "</svg>" };
  }

  // ---------------------------------------------------------------- the card
  // (quotes too: these go inside attribute values; a text node's innerHTML only escapes & < >)
  function esc(t) { return String(t == null ? "" : t).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  var WAYS = ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"];
  function cardHtml(p) {
    var number = (p.callsign || "").replace(/^[A-Z]{3}(?=\d)/, "");
    var title = p.military ? "Military" + (p.callsign ? " · " + p.callsign : "")
      : p.airline && p.callsign ? p.airline + " " + number
      : p.reg || p.callsign || "A plane";
    var lines = [];
    if (p.kind) lines.push(esc(p.kind));
    if (p.from && p.to) lines.push(esc(p.from) + " → " + esc(p.to));
    var bits = [];
    if (typeof p.alt === "number") bits.push(Math.round(p.alt / 100) * 100 >= 1000
      ? (Math.round(p.alt / 100) * 100).toLocaleString("en-US") + " ft" : Math.round(p.alt) + " ft");
    if (p.speed_kt) bits.push(Math.round(p.speed_kt * 1.15078) + " mph");
    if (p.track != null) bits.push("heading " + WAYS[Math.round(p.track / 45) % 8]);
    if (bits.length) lines.push(bits.join(" · "));
    var miles = Math.round(p.miss_mi || 0);
    var tag = p.demo ? "✈️ Test fly-by" : p.side && miles >= 1
      ? "✈️ Passing " + (miles === 1 ? "a mile " : miles + " miles ") + p.side : "✈️ Over the house now";
    return '<div class="fb-tag">' + tag + '</div><div class="fb-title">' + esc(title) + "</div>" +
      lines.map(function(l) { return "<div>" + l + "</div>"; }).join("");
  }

  // ---------------------------------------------------------------- flying it across
  var busy = false, queue = [];
  function fly(p) {
    if (busy) { queue.push(p); return; }
    busy = true;
    var d = drawing(p), shape = SHAPES[d.fam], secs = shape.secs;
    var west = p.track != null && p.track > 180 && p.track < 360;  // heading west: right to left
    var W = window.innerWidth, w = Math.round(W * shape.w), h = Math.round(w * 64 / 208);
    var trail = typeof p.alt === "number" && p.alt >= 25000 && d.fam !== "heli" && !shape.small;
    var el = document.createElement("div");
    el.className = "fb";
    el.style.width = w + "px";
    el.style.height = h + "px";
    el.innerHTML = '<div class="fb-in' + (west ? " west" : "") + '">' + (trail ? '<div class="fb-trail"></div>' : "") + d.svg + "</div>";
    var card = document.createElement("div");
    card.className = "fb-card";
    card.innerHTML = cardHtml(p);
    var from = west ? W + 20 : -w - 20, to = west ? -w - 20 : W + 20;
    el.style.transform = el.style.webkitTransform = "translate3d(" + from + "px,0,0)";
    document.body.appendChild(el);
    document.body.appendChild(card);
    el.offsetWidth;  // (start from there before moving)
    el.style.transition = el.style.webkitTransition = "transform " + secs + "s linear";
    el.style.transform = el.style.webkitTransform = "translate3d(" + to + "px,0,0)";
    card.className = "fb-card show";
    setTimeout(function() { card.className = "fb-card"; }, (secs - 1.5) * 1000);
    setTimeout(function() {
      el.parentNode && el.parentNode.removeChild(el);
      card.parentNode && card.parentNode.removeChild(card);
      busy = false;
      if (queue.length) fly(queue.shift());
    }, secs * 1000 + 300);
  }

  // ---------------------------------------------------------------- watching
  var shown = {};  // plane id -> when its fly-by was planned (a plane is shown once per pass)
  function check() {
    var x = new XMLHttpRequest();  // (Chrome 56's fetch leaves the login cookie behind)
    x.open("GET", "/api/planes/overhead");
    x.timeout = 9000;
    x.onload = function() {
      var passes = [];
      try { passes = JSON.parse(x.responseText).passes || []; } catch (e) { return; }
      var now = Date.now();
      for (var id in shown) if (now - shown[id] > 10 * 60 * 1000) delete shown[id];
      passes.forEach(function(p) {
        if (shown[p.id]) return;
        var half = SHAPES[family(p)].secs / 2, wait = p.eta - half;  // over the house mid-screen
        if (wait > 11) return;  // (the next check, 10 s on, knows better)
        shown[p.id] = now;
        setTimeout(function() { fly(p); }, Math.max(0, wait) * 1000);
      });
    };
    x.send();
  }

  window.Flyby = {
    watch: function() { check(); setInterval(check, 10000); },
    fly: fly,  // (for trying it out: Flyby.fly({type: "B738", callsign: "SWA1234", airline: "Southwest", ...}))
    drawing: drawing
  };
})();
