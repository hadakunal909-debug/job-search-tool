// Feed: instant tab/search/min filtering, paged rendering (Load more), no-reload actions.
(function () {
  "use strict";
  var feed = document.getElementById("feed");
  if (!feed) return;
  var q = document.getElementById("q");
  var minR = document.getElementById("min");
  var minLab = document.getElementById("minlab");
  var countEl = document.getElementById("count");
  var emptyEl = document.getElementById("empty");
  var moreBtn = document.getElementById("loadmore");
  var tabBtns = document.querySelectorAll(".tab");
  var tab = "recommended";
  var PAGE = 36, limit = PAGE;

  function cards() { return feed.querySelectorAll(".card"); }

  function matches(c) {
    var st = c.getAttribute("data-status") || "";
    var sc = parseInt(c.getAttribute("data-score"), 10) || 0;
    var ok;
    if (tab === "liked") ok = st === "liked";
    else if (tab === "applied") ok = st === "applied";
    else if (tab === "hidden") ok = st === "hidden";
    else ok = (st !== "hidden") && (sc >= (minR ? parseInt(minR.value, 10) || 0 : 0));
    if (ok && q && q.value) {
      ok = (c.getAttribute("data-text") || "").indexOf(q.value.toLowerCase().trim()) !== -1;
    }
    return ok;
  }

  function render(reset) {
    if (reset) limit = PAGE;
    var list = cards(), total = 0, shown = 0;
    for (var i = 0; i < list.length; i++) {
      var c = list[i];
      if (matches(c)) {
        total++;
        if (shown < limit) { c.style.display = ""; shown++; }
        else c.style.display = "none";
      } else {
        c.style.display = "none";
      }
    }
    if (countEl) countEl.textContent = total;
    if (emptyEl) emptyEl.style.display = total ? "none" : "";
    if (moreBtn) {
      if (total > limit) { moreBtn.style.display = ""; moreBtn.textContent = "Load more (" + (total - limit) + " more)"; }
      else moreBtn.style.display = "none";
    }
  }

  for (var t = 0; t < tabBtns.length; t++) {
    tabBtns[t].addEventListener("click", function () {
      for (var k = 0; k < tabBtns.length; k++) tabBtns[k].classList.remove("on");
      this.classList.add("on");
      tab = this.getAttribute("data-tab");
      render(true);
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  }
  if (q) q.addEventListener("input", function () { render(true); });
  if (minR) minR.addEventListener("input", function () {
    if (minLab) minLab.textContent = minR.value;
    render(true);
  });
  if (moreBtn) moreBtn.addEventListener("click", function () { limit += PAGE; render(false); });

  // like / applied / hide — no reload
  feed.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest("button[data-act]") : null;
    if (!btn) return;
    e.preventDefault();
    var card = btn.closest(".card");
    if (!card) return;
    var act = btn.getAttribute("data-act");
    var cur = card.getAttribute("data-status") || "";
    var next = (cur === act) ? "" : act;
    btn.disabled = true;
    fetch("/api/action", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: card.getAttribute("data-url"), status: next })
    }).then(function (r) { return r.json(); }).then(function (j) {
      btn.disabled = false;
      if (!j || !j.ok) { alert("Couldn't save — try again."); return; }
      card.setAttribute("data-status", next);
      paint(card, next);
      if (next === "hidden" && tab !== "hidden") {
        card.style.transition = "opacity .25s ease"; card.style.opacity = "0";
        setTimeout(function () { card.style.opacity = ""; render(false); }, 250);
      } else { render(false); }
    }).catch(function () { btn.disabled = false; alert("Network error — try again."); });
  });

  function paint(card, status) {
    var like = card.querySelector('[data-act="liked"]');
    var app = card.querySelector('[data-act="applied"]');
    if (like) like.textContent = (status === "liked") ? "💚" : "🤍";
    if (app) app.textContent = (status === "applied") ? "✅" : "📨";
  }

  render(true);
})();
