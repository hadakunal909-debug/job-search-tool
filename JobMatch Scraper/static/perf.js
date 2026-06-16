// Lightweight page-speed logger. On every page load it prints the REAL timing of the page
// you're on (from the browser's Navigation Timing API — zero extra cost) plus one tiny /healthz
// ping for a DB-free server baseline. Open DevTools (F12) -> Console to read it. No visible UI.
(function () {
  "use strict";
  function ms(n) { return (n == null || n < 0 || !isFinite(n)) ? "n/a" : Math.round(n) + " ms"; }

  function run() {
    var rows = {};
    var nav = null;
    try { nav = (performance.getEntriesByType("navigation") || [])[0] || null; } catch (e) {}
    if (nav) {
      rows["DNS + connect"]   = { time: ms(nav.connectEnd - nav.startTime) };
      rows["TTFB (server)"]   = { time: ms(nav.responseStart - nav.requestStart) };
      rows["HTML download"]   = { time: ms(nav.responseEnd - nav.responseStart) };
      rows["DOM ready"]       = { time: ms(nav.domContentLoadedEventEnd - nav.startTime) };
      rows["Full page load"]  = { time: ms(nav.loadEventEnd - nav.startTime) };
      rows["Over the wire"]   = { time: nav.transferSize ? Math.round(nav.transferSize / 1024) + " KB"
                                                          : "n/a (cached?)" };
    }

    function show() {
      var label = "⏱ JobMatch speed — " + location.pathname;
      try {
        if (console.groupCollapsed) console.groupCollapsed(label); else console.log(label);
        if (console.table) console.table(rows); else console.log(rows);
        console.log("TTFB = your server+DB time. 1st load after idle = cold; reload = warm.");
        if (console.groupEnd) console.groupEnd();
      } catch (e) {}
    }

    // server-only baseline (no DB): time a /healthz round-trip, then print everything
    var t0 = (performance.now ? performance.now() : Date.now());
    fetch("/healthz", { cache: "no-store" })
      .then(function () {
        var t1 = (performance.now ? performance.now() : Date.now());
        rows["/healthz (server, no DB)"] = { time: ms(t1 - t0) };
        show();
      })
      .catch(show);
  }

  if (document.readyState === "complete") run();
  else window.addEventListener("load", run);
})();
