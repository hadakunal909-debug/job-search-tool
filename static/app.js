// Feed: filter/sort/paginate + click-to-open job detail modal + toasts + no-reload actions.
(function () {
  "use strict";
  var feed = document.getElementById("feed");
  if (!feed) return;
  var q = document.getElementById("q"), minR = document.getElementById("min"),
      minLab = document.getElementById("minlab"), sortSel = document.getElementById("sort"),
      dateSel = document.getElementById("date"), countEl = document.getElementById("count"),
      emptyEl = document.getElementById("empty"), moreBtn = document.getElementById("loadmore"),
      toasts = document.getElementById("toasts"), hideNo = document.getElementById("hidenospon"),
      tabBtns = document.querySelectorAll(".tab");
  var tab = "recommended", PAGE = 36, limit = PAGE;

  function esc(s) { var d = document.createElement("div"); d.textContent = s == null ? "" : s; return d.innerHTML; }
  function toast(msg) {
    if (!toasts) return;
    var t = document.createElement("div"); t.className = "toast"; t.textContent = msg;
    toasts.appendChild(t);
    setTimeout(function () { t.style.transition = "opacity .3s"; t.style.opacity = "0"; setTimeout(function () { t.remove(); }, 300); }, 2300);
  }
  function relTime(s) {
    if (!s) return "";
    var d = new Date(s + "T00:00:00"); if (isNaN(d.getTime())) return s;
    var days = Math.floor((Date.now() - d.getTime()) / 86400000);
    if (days <= 0) return "Today";
    if (days === 1) return "Yesterday";
    if (days < 7) return days + "d ago";
    if (days < 30) return Math.floor(days / 7) + "w ago";
    if (days < 365) return Math.floor(days / 30) + "mo ago";
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  }
  function formatDates() {
    var ps = feed.querySelectorAll(".posted");
    for (var i = 0; i < ps.length; i++) {
      var s = ps[i].getAttribute("data-d");
      if (s) { ps[i].textContent = relTime(s); ps[i].title = "Posted " + s; }
    }
  }

  function cards() { return feed.querySelectorAll(".card"); }
  function cardByUrl(u) { var l = cards(); for (var i = 0; i < l.length; i++) if (l[i].getAttribute("data-url") === u) return l[i]; return null; }
  function paint(card, status) {
    var like = card.querySelector('[data-act="liked"]'), app = card.querySelector('[data-act="applied"]');
    if (like) like.textContent = (status === "liked") ? "💚" : "🤍";
    if (app) app.textContent = (status === "applied") ? "✅" : "📨";
  }

  function dateCutoff() {
    if (!dateSel || dateSel.value === "any") return "";
    var d = new Date(); d.setDate(d.getDate() - parseInt(dateSel.value, 10)); return d.toISOString().slice(0, 10);
  }
  function matches(c, cut) {
    var st = c.getAttribute("data-status") || "", sc = parseInt(c.getAttribute("data-score"), 10) || 0, ok;
    if (tab === "liked") ok = st === "liked";
    else if (tab === "applied") ok = st === "applied";
    else if (tab === "hidden") ok = st === "hidden";
    else ok = (st !== "hidden") && (sc >= (minR ? parseInt(minR.value, 10) || 0 : 0));
    if (ok && q && q.value) ok = (c.getAttribute("data-text") || "").indexOf(q.value.toLowerCase().trim()) !== -1;
    if (ok && cut) { var dt = c.getAttribute("data-date") || ""; if (dt && dt < cut) ok = false; }
    if (ok && hideNo && hideNo.checked && c.getAttribute("data-sponsor") === "blocked") ok = false;
    return ok;
  }
  function sortCards() {
    var by = sortSel ? sortSel.value : "score", arr = Array.prototype.slice.call(cards());
    arr.sort(function (a, b) {
      if (by === "newest") return (b.getAttribute("data-date") || "").localeCompare(a.getAttribute("data-date") || "");
      return (parseInt(b.getAttribute("data-score"), 10) || 0) - (parseInt(a.getAttribute("data-score"), 10) || 0);
    });
    var f = document.createDocumentFragment(); arr.forEach(function (c) { f.appendChild(c); }); feed.appendChild(f);
  }
  function render(reset) {
    if (reset) limit = PAGE;
    var cut = dateCutoff(), l = cards(), total = 0, shown = 0;
    for (var i = 0; i < l.length; i++) {
      if (matches(l[i], cut)) { total++; if (shown < limit) { l[i].style.display = ""; shown++; } else l[i].style.display = "none"; }
      else l[i].style.display = "none";
    }
    if (countEl) countEl.textContent = total;
    if (emptyEl) emptyEl.style.display = total ? "none" : "";
    if (moreBtn) moreBtn.style.display = (total > limit) ? "" : "none";
    if (moreBtn && total > limit) moreBtn.textContent = "Load more (" + (total - limit) + " more)";
  }

  function doAction(url, next) {
    return fetch("/api/action", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: url, status: next }) })
      .then(function (r) { return r.json(); }).then(function (j) {
        if (!j || !j.ok) { toast("Couldn't save — try again."); return false; }
        var c = cardByUrl(url); if (c) { c.setAttribute("data-status", next); paint(c, next); }
        recordApplied(url, next);
        return true;
      }).catch(function () { toast("Network error."); return false; });
  }
  function actLabel(s) { return s === "liked" ? "Saved" : s === "applied" ? "Marked applied" : s === "hidden" ? "Hidden" : "Removed"; }

  // controls
  for (var t = 0; t < tabBtns.length; t++) tabBtns[t].addEventListener("click", function () {
    for (var k = 0; k < tabBtns.length; k++) tabBtns[k].classList.remove("on");
    this.classList.add("on"); tab = this.getAttribute("data-tab"); render(true); window.scrollTo({ top: 0, behavior: "smooth" });
  });
  if (q) q.addEventListener("input", function () { render(true); });
  if (minR) minR.addEventListener("input", function () { if (minLab) minLab.textContent = minR.value; render(true); });
  if (sortSel) sortSel.addEventListener("change", function () { sortCards(); render(true); });
  if (dateSel) dateSel.addEventListener("change", function () { render(true); });
  if (hideNo) hideNo.addEventListener("change", function () { render(true); });
  if (moreBtn) moreBtn.addEventListener("click", function () { limit += PAGE; render(false); });

  // feed clicks: action buttons, or open modal
  feed.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest("button[data-act]") : null;
    if (btn) {
      e.preventDefault();
      var card = btn.closest(".card"); if (!card) return;
      var act = btn.getAttribute("data-act"), cur = card.getAttribute("data-status") || "", next = (cur === act) ? "" : act;
      btn.disabled = true;
      doAction(card.getAttribute("data-url"), next).then(function (ok) {
        btn.disabled = false; if (!ok) return; toast(actLabel(next));
        if (next === "hidden" && tab !== "hidden") { card.style.transition = "opacity .25s"; card.style.opacity = "0"; setTimeout(function () { card.style.opacity = ""; render(false); }, 250); }
        else render(false);
      });
      return;
    }
    if (e.target.closest && e.target.closest("a")) return;       // Apply/Tailor links
    var c = e.target.closest ? e.target.closest(".card") : null;
    if (c) openModal(c);
  });

  // ---- job detail modal ----
  var modal = document.getElementById("jobmodal"), mUrl = "";
  function $(id) { return document.getElementById(id); }
  function openModal(card) {
    if (!modal) return;
    mUrl = card.getAttribute("data-url");
    var lg = card.querySelector(".logo");
    $("m-logo").innerHTML = lg ? lg.innerHTML : ""; $("m-logo").style.background = lg ? lg.style.background : "";
    $("m-title").textContent = card.querySelector(".ctitle") ? card.querySelector(".ctitle").textContent : "";
    $("m-meta").textContent = card.querySelector(".cmeta") ? card.querySelector(".cmeta").textContent : "";
    $("m-skills").innerHTML = ""; $("m-jd").textContent = "Loading…"; $("m-chip").innerHTML = "";
    $("m-apply").href = mUrl; $("m-tailor").href = "/tailor?url=" + encodeURIComponent(mUrl);
    syncModal(card.getAttribute("data-status") || "");
    modal.classList.add("open"); document.body.style.overflow = "hidden";
    fetch("/api/job?url=" + encodeURIComponent(mUrl)).then(function (r) { return r.json(); }).then(function (j) {
      if (!j || !j.ok) { $("m-jd").textContent = "Couldn't load details."; return; }
      var chip = $("m-chip"); chip.className = "chip " + (j.score >= 55 ? "strong" : j.score >= 42 ? "good" : "low"); chip.textContent = j.score + "%";
      var sk = "";
      if (j.missing && j.missing.length) sk += '<div class="sechdr">Add these to your résumé</div><div class="kw">' + j.missing.map(function (k) { return '<span class="tag miss">' + esc(k) + '</span>'; }).join("") + '</div>';
      if (j.have && j.have.length) sk += '<div class="sechdr">Skills you already match</div><div class="kw">' + j.have.map(function (k) { return '<span class="tag have">' + esc(k) + '</span>'; }).join("") + '</div>';
      var spn = "";
      if (j.cap_exempt) spn += '<span class="cx">🎓 Likely cap-exempt — no H-1B lottery</span>';
      if (j.sponsor_jd === "blocked") spn += '<span class="nospon">🚫 ' + esc(j.sponsor_reason || "Likely no sponsorship") + '</span>';
      else if (j.sponsor_jd === "open") spn += '<span class="spon">✅ ' + esc(j.sponsor_reason || "Offers sponsorship") + '</span>';
      if (spn) sk = '<div class="kw" style="margin-bottom:10px">' + spn + '</div>' + sk;
      $("m-skills").innerHTML = sk;
      $("m-jd").textContent = j.jd || "No description stored — click Apply to read it on the company site.";
    }).catch(function () { $("m-jd").textContent = "Couldn't load details."; });
  }
  function closeModal() { if (modal) { modal.classList.remove("open"); document.body.style.overflow = ""; } }
  function syncModal(status) {
    var l = $("m-like"), h = $("m-hide");
    if (l) l.textContent = status === "liked" ? "💚 Saved" : "🤍 Save";
    if (h) h.textContent = status === "hidden" ? "🚫 Unhide" : "🚫 Hide";
  }
  if (modal) {
    $("m-close").addEventListener("click", closeModal);
    modal.addEventListener("click", function (e) { if (e.target === modal) closeModal(); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeModal(); });
    $("m-like").addEventListener("click", function () {
      var c = cardByUrl(mUrl), cur = c ? c.getAttribute("data-status") : "", next = cur === "liked" ? "" : "liked";
      doAction(mUrl, next).then(function (ok) { if (ok) { toast(actLabel(next)); syncModal(next); render(false); } });
    });
    $("m-hide").addEventListener("click", function () {
      var c = cardByUrl(mUrl), cur = c ? c.getAttribute("data-status") : "", next = cur === "hidden" ? "" : "hidden";
      doAction(mUrl, next).then(function (ok) { if (ok) { toast(next ? "Hidden" : "Unhidden"); closeModal(); render(false); } });
    });
  }

  // ---- OPT / work-authorization tracker (personal; saved in this browser only) ----
  var optEls = { since: document.getElementById("opt_since"), prior: document.getElementById("opt_prior"),
                 emp: document.getElementById("opt_emp"), stem: document.getElementById("opt_stem"),
                 out: document.getElementById("opt_out"), fill: document.getElementById("opt_fill") };
  function recordApplied(url, status) {           // remember WHEN a job was marked applied
    try {
      var m = JSON.parse(localStorage.getItem("applied_dates") || "{}");
      if (status === "applied") m[url] = new Date().toISOString().slice(0, 10); else delete m[url];
      localStorage.setItem("applied_dates", JSON.stringify(m));
    } catch (e) {}
    optRender();
  }
  function appliedThisWeek() {
    var m = {}; try { m = JSON.parse(localStorage.getItem("applied_dates") || "{}"); } catch (e) {}
    var cut = Date.now() - 7 * 86400000, n = 0;
    for (var k in m) { if (m[k] && new Date(m[k] + "T00:00:00").getTime() >= cut) n++; }
    return n;
  }
  function optRender() {
    if (!optEls.out) return;
    var limit = (optEls.stem && optEls.stem.checked) ? 150 : 90;
    var used = parseInt(optEls.prior && optEls.prior.value, 10) || 0, paused = optEls.emp && optEls.emp.checked;
    if (!paused && optEls.since && optEls.since.value) {
      var d = Math.floor((Date.now() - new Date(optEls.since.value + "T00:00:00").getTime()) / 86400000);
      if (d > 0) used += d;
    }
    if (used > limit) used = limit;
    var left = Math.max(0, limit - used), pct = Math.min(100, Math.round(100 * used / limit));
    if (optEls.fill) { optEls.fill.style.width = pct + "%"; optEls.fill.className = "optfill " + (pct >= 89 ? "r" : pct >= 66 ? "a" : "g"); }
    var wk = appliedThisWeek();
    var head = paused ? "✅ Employed — unemployment clock paused"
      : (left <= 15 ? "⚠️ " + left + " unemployment days left" : left + " unemployment days left");
    optEls.out.innerHTML = "<b>" + head + "</b> · " + used + " / " + limit + " days used"
      + (wk ? (" · 📨 " + wk + " applied this week") : "");
    try {
      if (optEls.since) localStorage.setItem("opt_since", optEls.since.value);
      if (optEls.prior) localStorage.setItem("opt_prior", optEls.prior.value);
      if (optEls.emp) localStorage.setItem("opt_emp", optEls.emp.checked ? "1" : "0");
      if (optEls.stem) localStorage.setItem("opt_stem", optEls.stem.checked ? "1" : "0");
    } catch (e) {}
  }
  (function initOpt() {
    if (!optEls.out) return;
    try {
      var s = localStorage.getItem("opt_since"), p = localStorage.getItem("opt_prior");
      if (s && optEls.since) optEls.since.value = s;
      if (p && optEls.prior) optEls.prior.value = p;
      if (optEls.emp) optEls.emp.checked = localStorage.getItem("opt_emp") === "1";
      if (optEls.stem) optEls.stem.checked = localStorage.getItem("opt_stem") === "1";
    } catch (e) {}
    ["since", "prior", "emp", "stem"].forEach(function (k) {
      if (optEls[k]) { optEls[k].addEventListener("input", optRender); optEls[k].addEventListener("change", optRender); }
    });
    optRender();
  })();

  formatDates();
  render(true);
})();
