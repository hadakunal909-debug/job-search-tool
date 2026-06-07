// Client-side feed: instant tab/search/min filtering + no-reload like/hide/apply.
(function () {
  "use strict";
  var feed = document.getElementById("feed");
  if (!feed) return;
  var q = document.getElementById("q");
  var minR = document.getElementById("min");
  var minLab = document.getElementById("minlab");
  var countEl = document.getElementById("count");
  var emptyEl = document.getElementById("empty");
  var tab = "recommended";

  function cards() { return feed.querySelectorAll(".card"); }

  function applyFilter() {
    var term = (q && q.value ? q.value : "").toLowerCase().trim();
    var minv = minR ? parseInt(minR.value, 10) || 0 : 0;
    var shown = 0, list = cards();
    for (var i = 0; i < list.length; i++) {
      var c = list[i];
      var st = c.getAttribute("data-status") || "";
      var sc = parseInt(c.getAttribute("data-score"), 10) || 0;
      var ok;
      if (tab === "liked") ok = st === "liked";
      else if (tab === "applied") ok = st === "applied";
      else if (tab === "hidden") ok = st === "hidden";
      else ok = (st !== "hidden") && (sc >= minv);
      if (ok && term) ok = (c.getAttribute("data-text") || "").indexOf(term) !== -1;
      c.style.display = ok ? "" : "none";
      if (ok) shown++;
    }
    if (countEl) countEl.textContent = shown;
    if (emptyEl) emptyEl.style.display = shown ? "none" : "";
  }

  // Tabs
  var tabs = document.querySelectorAll(".tab");
  for (var t = 0; t < tabs.length; t++) {
    tabs[t].addEventListener("click", function (e) {
      e.preventDefault();
      for (var k = 0; k < tabs.length; k++) tabs[k].classList.remove("on");
      this.classList.add("on");
      tab = this.getAttribute("data-tab");
      // in non-recommended tabs, ignore the min-match slider
      applyFilter();
    });
  }
  if (q) q.addEventListener("input", applyFilter);
  if (minR) minR.addEventListener("input", function () {
    if (minLab) minLab.textContent = minR.value;
    applyFilter();
  });

  // Like / Applied / Hide — no reload
  feed.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest("button[data-act]") : null;
    if (!btn) return;
    e.preventDefault();
    var card = btn.closest(".card");
    if (!card) return;
    var act = btn.getAttribute("data-act");
    var cur = card.getAttribute("data-status") || "";
    var next = (cur === act) ? "" : act;       // click again to un-set
    btn.disabled = true;
    fetch("/api/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: card.getAttribute("data-url"), status: next })
    }).then(function (r) { return r.json(); }).then(function (j) {
      btn.disabled = false;
      if (!j || !j.ok) { alert("Couldn't save — try again."); return; }
      card.setAttribute("data-status", next);
      paintButtons(card, next);
      if (next === "hidden" && tab !== "hidden") {
        card.style.transition = "opacity .25s ease";
        card.style.opacity = "0";
        setTimeout(applyFilter, 250);
      } else {
        applyFilter();
      }
    }).catch(function () { btn.disabled = false; alert("Network error — try again."); });
  });

  function paintButtons(card, status) {
    var like = card.querySelector('[data-act="liked"]');
    var app = card.querySelector('[data-act="applied"]');
    if (like) like.textContent = (status === "liked") ? "💚" : "🤍";
    if (app) app.textContent = (status === "applied") ? "✅" : "📨";
    card.style.opacity = "";   // reset (e.g. un-hide)
  }

  applyFilter();   // initial pass (applies default min-match)
})();
