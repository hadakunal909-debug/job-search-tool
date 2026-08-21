/* The little /job needs that the server cannot do.
 *
 * An external file, not an inline <script>, so CSP script-src 'self' already covers it and it
 * needs no nonce. app.js is deliberately NOT loaded on this page: its DOM contract is
 * #feed / #feeddata / #loadmore, none of which exist here.
 *
 * Two jobs:
 *   1. Kick off company research for an employer we have none for, then poll until it lands.
 *   2. Beacon the apply click, matching the event the feed already sends.
 */
(function () {
  "use strict";

  // ---- apply click -------------------------------------------------------------------------
  // Two things, both of which must be unable to stop the link opening.
  //
  //   1. Beacon apply_click: the same event name and props the feed's delegate sends, plus
  //      where:"job" so the two surfaces can be told apart without a second event name.
  //      window.EV is global on every page for a logged-in user (ev.js, from base.html).
  //   2. Park the posting for the "did you apply?" prompt (applyask.js). This page never
  //      recorded an application on click -- only the feed did, which is the bug that produced
  //      129 of them -- so this is new behaviour here, not a correction: the job page had no
  //      way to log an application at all except the no-JS Save/Hide form below.
  //
  // The job's identity comes from data attributes on the .jobwrap rather than from the DOM
  // text, because the heading carries badges and a company link and scraping it back out would
  // be a parser nobody asked for.
  var meta = document.querySelector("[data-job-url]");
  var job = meta ? {
    url: meta.getAttribute("data-job-url"),
    title: meta.getAttribute("data-job-title") || "",
    company: meta.getAttribute("data-job-company") || ""
  } : null;
  var applies = document.querySelectorAll('a[data-apply="1"]');
  for (var i = 0; i < applies.length; i++) {
    applies[i].addEventListener("click", function () {
      if (window.EV) {
        try {
          window.EV("apply_click", { where: "job" });
        } catch (e) { /* a beacon must never block opening the employer's page */ }
      }
      try {
        if (job && job.url && window.ApplyAsk) window.ApplyAsk.pend(job);
      } catch (e) { /* same rule: never block the click */ }
    });
  }

  // ---- company research --------------------------------------------------------------------
  var box = document.getElementById("coresearch");
  if (!box) return;
  var company = box.getAttribute("data-research");
  // The server only sets data-pending when it actually started a crawl: it decides on the index,
  // the domain, the cooldown and the kill switch, none of which this file can see.
  if (!company || box.getAttribute("data-pending") !== "1") return;

  var csrf = box.getAttribute("data-csrf") || "";
  var tries = 0, MAX = 8, EVERY = 2000;

  function swap(html) {
    if (html && html.indexOf("<") >= 0) box.innerHTML = html;
  }

  function poll() {
    tries++;
    fetch("/job/research?c=" + encodeURIComponent(company), { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) {
        if (html === null) return;
        // The endpoint returns the SAME Jinja partial the page rendered, so a finished crawl is
        // recognised by the fragment no longer carrying the spinner rather than by parsing JSON
        // the server would have to keep in step with the template.
        if (html.indexOf("class=\"spin\"") < 0) { swap(html); return; }
        if (tries < MAX) setTimeout(poll, EVERY);
        else swap(html);          // gave up: show whatever the honest empty state is now
      })
      .catch(function () { /* offline or navigated away; the next page load retries */ });
  }

  // Start the crawl, then poll. A bodyless POST with the CSRF header, the pattern admin.html
  // already uses.
  fetch("/job/research", {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf, "Content-Type": "application/x-www-form-urlencoded" },
    body: "c=" + encodeURIComponent(company)
  }).then(function () { setTimeout(poll, EVERY); })
    .catch(function () { /* the section keeps its "not researched yet" copy */ });
})();

// Company-logo fallback, the same contract app.js gives the feed and the company page.
//
// This page deliberately does not load app.js, so it never got one. The logo is a coloured
// tile with the company's initial, and the favicon <img> sits absolutely on top of it with a
// white background — so when the favicon 404s (the domain is guessed from the company name and
// is often wrong), the failed image stays a WHITE SQUARE covering the letter that was supposed
// to be the fallback. Hiding the <img> lets the tile behind it show.
//
// In JS rather than an inline onerror= so the CSP can keep forbidding inline handlers.
(function () {
  var imgs = document.querySelectorAll(".logo-img");
  for (var i = 0; i < imgs.length; i++) {
    (function (img) {
      // Walk the chain before giving up: logo.dev -> favicon -> the coloured letter tile.
      // data-fallback is set server-side and is empty when there is no second provider, in
      // which case this behaves exactly as it did before.
      function fail() {
        var fb = img.getAttribute("data-fallback");
        if (fb && img.getAttribute("src") !== fb) { img.src = fb; return; }
        img.style.display = "none";
      }
      img.addEventListener("error", fail);
      // An image cached as broken, or one that failed before this ran, never fires "error".
      // complete && naturalWidth === 0 is how you detect that after the fact.
      if (img.complete && img.naturalWidth === 0) fail();
    })(imgs[i]);
  }
})();
