/* ev.js — the small residue of usage events the server can't already see.
 *
 * The feed is in paged mode, so app.js serialises the entire toolbar into every /api/feed
 * request: tabs, search, all 12 filters, sort, and paging are captured server-side for free.
 * This file exists only for what never reaches the server — rail toggles, the filter panel,
 * how many filters were stacked when someone gave up and hit Clear, outbound Apply clicks,
 * and dwell/scroll depth.
 *
 * Rules it must never break: never throw into the page, never retry, never grow unbounded, and
 * never delay an unload. ES5 to match app.js. External file, so CSP `script-src 'self'` covers
 * it without a nonce.
 */
(function () {
  "use strict";

  /* Defined immediately as a no-op, BEFORE anything can fail below. app.js calls EV()
     unconditionally, so if this file 404s or the user opted out, those calls must still be
     harmless rather than a ReferenceError that blanks the feed. */
  window.EV = function () {};

  var body = document.body;
  if (!body) return;
  var SID = body.getAttribute("data-sid") || "";
  if (!SID) return;                                   // logged out, or analytics off server-side
  try {
    if (localStorage.getItem("jm_ev_off") === "1") return;
  } catch (e) { /* private mode: carry on */ }

  var buf = [], timer = null, T0 = Date.now(), maxScroll = 0, sent = false;

  function flush() {
    if (!buf.length) return;
    var payload = JSON.stringify({ ev: buf.splice(0, buf.length) });
    try {
      /* sendBeacon first, always. It survives page unload (where a fetch is cancelled and the
         session-end event is simply lost), is queued off the main thread, and carries the
         same-origin session cookie so no extra auth is needed. A Blob with an explicit JSON
         type triggers no preflight because the request is same-origin. */
      if (navigator.sendBeacon) {
        var blob = new Blob([payload], { type: "application/json" });
        if (navigator.sendBeacon("/api/ev", blob)) return;
      }
      /* Fallback only when sendBeacon is missing or returns false (its queue budget is full). */
      fetch("/api/ev", {
        method: "POST", body: payload, keepalive: true, credentials: "same-origin",
        headers: { "Content-Type": "application/json" }
      })["catch"](function () { /* dropped on purpose — analytics never retries */ });
    } catch (e) { /* and never throws */ }
  }

  window.EV = function (name, props) {
    try {
      if (buf.length >= 50) buf.shift();              // bounded: drop the oldest
      buf.push({ e: String(name), p: props || {} });
      if (buf.length >= 10) { flush(); return; }
      if (!timer) {
        timer = setTimeout(function () { timer = null; flush(); }, 8000);
      }
    } catch (e) { /* never throws into a click handler */ }
  };

  /* Scroll depth, throttled with rAF so a scroll never costs layout work. Answers "is anyone
     reaching page 2", which decides whether FEED_TOPN=400 and the 60-card page are oversized. */
  var ticking = false;
  addEventListener("scroll", function () {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(function () {
      ticking = false;
      try {
        var h = document.documentElement.scrollHeight - innerHeight;
        if (h > 0) {
          var pct = Math.round(100 * (window.pageYOffset || document.documentElement.scrollTop) / h);
          if (pct > maxScroll) maxScroll = Math.min(pct, 100);
        }
      } catch (e) {}
    });
  }, { passive: true });

  function leave() {
    if (sent) return;                                 /* visibilitychange and pagehide both fire
                                                         on a real close; only report once */
    sent = true;
    try { EV("page_leave", { ms: Date.now() - T0, scroll: maxScroll }); } catch (e) {}
    flush();
  }
  addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") leave();
  });
  addEventListener("pagehide", leave);
})();
