// Sentinel's pages report requests that took long on this device (over 2.5 s: a camera picture, the switches list,
// the page itself) to /api/slow. Each says how long it took here, how long until the first byte came back, and how
// long Sentinel itself spent on it (its Server-Timing header), so the log tells a slow connection (the phone, the
// internet tunnel) from a slow Sentinel. Nothing is sent while everything is quick.
(function () {
  if (!window.PerformanceObserver || !window.XMLHttpRequest) return;
  var SLOW_MS = 2500, queue = [], timer = null;
  function pathOf(url) {
    try { var u = new URL(url, location.href); return u.origin === location.origin ? u.pathname : null; } catch (e) { return null; }
  }
  function send() {
    timer = null;
    if (!queue.length) return;
    var x = new XMLHttpRequest();  // (not fetch: the TV's old browser leaves the sign-in cookie off fetch())
    x.open("POST", "/api/slow");
    x.setRequestHeader("Content-Type", "application/json");
    x.send(JSON.stringify({ page: location.pathname, hidden: document.hidden,
                            conn: (navigator.connection && navigator.connection.effectiveType) || "", items: queue.splice(0, 20) }));
  }
  var observer;
  try {
    observer = new PerformanceObserver(function (list) {
      list.getEntries().forEach(function (e) {
        var path = pathOf(e.name);
        if (!path || path === "/api/slow" || e.duration < SLOW_MS || /^\/stream|\/screen|^\/api\/talk/.test(path)) return;
        var app = (e.serverTiming || []).filter(function (t) { return t.name === "app"; })[0];
        queue.push({ path: path, type: e.initiatorType || e.entryType, ms: Math.round(e.duration),
                     wait: e.responseStart ? Math.round(e.responseStart - e.startTime) : null,
                     took: app ? Math.round(app.duration) : null });
      });
      if (queue.length && !timer) timer = setTimeout(send, 3000);
      if (performance.getEntriesByType && performance.getEntriesByType("resource").length > 200) performance.clearResourceTimings();
    });
  } catch (e) { return; }
  try {  // (buffered: the page's own load, which happened before this ran, counts too)
    observer.observe({ type: "navigation", buffered: true });
    observer.observe({ type: "resource", buffered: true });
  } catch (e) {
    try { observer.observe({ entryTypes: ["resource", "navigation"] }); } catch (e2) {}  // (older browsers: the TVs)
  }
})();
