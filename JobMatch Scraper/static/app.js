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
      expSel = document.getElementById("exp"), everifyOnly = document.getElementById("everifyonly"),
      tabBtns = document.querySelectorAll(".tab");
  var tab = "recommended", PAGE = 36, limit = PAGE;

  function esc(s) { var d = document.createElement("div"); d.textContent = s == null ? "" : s; return d.innerHTML; }
  function scoreRing(s) {
    var cls = s >= 55 ? 'ring-strong' : s >= 42 ? 'ring-good' : 'ring-low';
    var off = (113.1 * (1 - s / 100)).toFixed(1);
    return '<svg class="score-ring ' + cls + '" width="46" height="46" viewBox="0 0 46 46" aria-label="' + s + '% match">' +
      '<circle cx="23" cy="23" r="18" fill="none" stroke="var(--ring-track)" stroke-width="4.5"/>' +
      '<circle cx="23" cy="23" r="18" fill="none" stroke="var(--ring-color)" stroke-width="4.5" stroke-dasharray="113.1" stroke-dashoffset="' + off + '" stroke-linecap="round" transform="rotate(-90 23 23)"/>' +
      '<text x="23" y="27" text-anchor="middle" font-size="10" font-weight="800" fill="var(--ring-color)" font-family="Inter,-apple-system,sans-serif">' + s + '%</text>' +
      '</svg>';
  }
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

  // Company-logo fallback: if the favicon fails to load, try data-fallback once (when
  // present), then hide the broken <img> so the colored letter-avatar behind it shows.
  // Done in JS (not an inline onerror= attribute) so the strict CSP can forbid inline handlers.
  function wireLogoFallback(img) {
    if (!img || img.getAttribute("data-fb-wired")) return;
    img.setAttribute("data-fb-wired", "1");
    function fail() {
      var fb = img.getAttribute("data-fallback");
      if (fb && img.getAttribute("src") !== fb) { img.src = fb; return; }
      img.style.display = "none";                              // let the letter avatar show
    }
    img.addEventListener("error", fail);
    if (img.complete && img.naturalWidth === 0) fail();        // already failed before JS ran
  }
  function wireLogos(root) {
    var imgs = (root || feed).querySelectorAll(".logo-img");
    for (var i = 0; i < imgs.length; i++) wireLogoFallback(imgs[i]);
  }

  function cards() { return feed.querySelectorAll(".card"); }
  function cardByUrl(u) { var l = cards(); for (var i = 0; i < l.length; i++) if (l[i].getAttribute("data-url") === u) return l[i]; return null; }
  function paint(card, status) {
    var like = card.querySelector('[data-act="liked"]'), app = card.querySelector('[data-act="applied"]');
    if (like) like.textContent = (status === "liked") ? "Saved" : "Save";
    if (app) app.textContent = (status === "applied") ? "Applied" : "Mark";
  }

  function dateCutoff() {
    if (!dateSel || dateSel.value === "any") return "";
    var d = new Date(); d.setDate(d.getDate() - parseInt(dateSel.value, 10)); return d.toISOString().slice(0, 10);
  }
  function matches(c, cut) {
    var st = c.getAttribute("data-status") || "", sc = parseInt(c.getAttribute("data-score"), 10) || 0, ok;
    var searching = q && q.value.trim();
    if (tab === "liked") ok = st === "liked";
    else if (tab === "applied") ok = st === "applied";
    else if (tab === "hidden") ok = st === "hidden";
    // An active SEARCH bypasses the min-match slider: if you typed "deloitte" you want
    // to SEE Deloitte's jobs, not have them silently hidden because they score 40%.
    else ok = (st !== "hidden") && (searching || sc >= (minR ? parseInt(minR.value, 10) || 0 : 0));
    if (ok && searching) ok = (c.getAttribute("data-text") || "").indexOf(q.value.toLowerCase().trim()) !== -1;
    if (ok && cut) { var dt = c.getAttribute("data-date") || ""; if (dt && dt < cut) ok = false; }
    if (ok && hideNo && hideNo.checked && c.getAttribute("data-sponsor") === "blocked") ok = false;
    if (ok && everifyOnly && everifyOnly.checked && c.getAttribute("data-everify") !== "1") ok = false;
    // Experience filter: a job whose JD states no year count (data-exp="") is ALWAYS kept
    // — lots of genuine entry roles never say "0-2 years", so we don't punish missing data.
    if (ok && expSel && expSel.value !== "any") {
      var ev = c.getAttribute("data-exp");
      if (ev !== "" && ev != null) {
        var yrs = parseInt(ev, 10);
        if (!isNaN(yrs)) {
          if (expSel.value === "senior") { if (yrs >= 6) ok = false; }
          else if (yrs > (parseInt(expSel.value, 10) || 99)) ok = false;
        }
      }
    }
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
  if (expSel) expSel.addEventListener("change", function () { render(true); });
  if (everifyOnly) everifyOnly.addEventListener("change", function () { render(true); });
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
    var lnk = e.target.closest && e.target.closest("a");
    if (lnk) {                                                    // Apply/Tailor links open normally
      if (lnk.hasAttribute("data-apply")) {                      // clicking Apply auto-logs it
        var ac = lnk.closest(".card");
        if (ac && ac.getAttribute("data-status") !== "applied") {
          doAction(ac.getAttribute("data-url"), "applied").then(function (ok) {
            if (ok) { toast("Added to Applications"); render(false); }
          });
        }
      }
      return;
    }
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
    var mlImg = $("m-logo").querySelector(".logo-img");        // clone lost its handler; re-wire it
    if (mlImg) { mlImg.removeAttribute("data-fb-wired"); wireLogoFallback(mlImg); }
    $("m-title").textContent = card.querySelector(".ctitle") ? card.querySelector(".ctitle").textContent : "";
    $("m-meta").textContent = card.querySelector(".cmeta") ? card.querySelector(".cmeta").textContent : "";
    $("m-chip").innerHTML = '<span class="skel skel-chip"></span>';
    $("m-skills").innerHTML = '<div class="skel-row"><span class="skel skel-tag"></span><span class="skel skel-tag"></span><span class="skel skel-tag" style="width:88px"></span></div>' +
      '<span class="skel skel-bar w75"></span><span class="skel skel-bar w55"></span>';
    $("m-jd").innerHTML = '<div class="loading-jd"><span class="spin"></span>Loading description…</div>';
    $("m-apply").href = /^https?:\/\//i.test(mUrl) ? mUrl : "#";   // never make a javascript: link clickable
    $("m-tailor").href = "/tailor?url=" + encodeURIComponent(mUrl);
    syncModal(card.getAttribute("data-status") || "");
    modal.classList.add("open"); document.body.style.overflow = "hidden";
    fetch("/api/job?url=" + encodeURIComponent(mUrl)).then(function (r) { return r.json(); }).then(function (j) {
      if (!j || !j.ok) { $("m-jd").textContent = "Couldn't load details."; return; }
      $("m-chip").innerHTML = scoreRing(j.score);
      var sk = "";
      if (j.missing && j.missing.length) sk += '<div class="sechdr">Add these to your résumé</div><div class="kw">' + j.missing.map(function (k) { return '<span class="tag miss">' + esc(k) + '</span>'; }).join("") + '</div>';
      if (j.have && j.have.length) sk += '<div class="sechdr">Skills you already match</div><div class="kw">' + j.have.map(function (k) { return '<span class="tag have">' + esc(k) + '</span>'; }).join("") + '</div>';
      var spn = "";
      if (j.exp_years !== "" && j.exp_years != null) {
        var ey = parseInt(j.exp_years, 10);
        var ec = ey >= 6 ? "exp-hi" : ey >= 3 ? "exp-mid" : "exp-lo";
        spn += '<span class="exp ' + ec + '">' + ey + '+ yrs experience</span>';
      }
      if (j.everify) spn += '<span class="ev" title="Confirm current status at e-verify.gov">✅ E-Verify · STEM-OPT OK</span>';
      if (j.cap_exempt) spn += '<span class="cx">🎓 Likely cap-exempt — no H-1B lottery</span>';
      if (j.sponsor_jd === "blocked") spn += '<span class="nospon">🚫 ' + esc(j.sponsor_reason || "Likely no sponsorship") + '</span>';
      else if (j.sponsor_jd === "open") spn += '<span class="spon">✅ ' + esc(j.sponsor_reason || "Offers sponsorship") + '</span>';
      if (spn) sk = '<div class="kw" style="margin-bottom:10px">' + spn + '</div>' + sk;
      var skEl = $("m-skills"), jdEl = $("m-jd");
      skEl.style.opacity = "0"; jdEl.style.opacity = "0";
      skEl.innerHTML = sk;
      jdEl.textContent = j.jd || "No description stored — click Apply to read it on the company site.";
      requestAnimationFrame(function () {
        skEl.style.transition = "opacity .25s"; skEl.style.opacity = "1";
        jdEl.style.transition = "opacity .25s"; jdEl.style.opacity = "1";
      });
    }).catch(function () { $("m-jd").textContent = "Couldn't load details."; });
  }
  function closeModal() { if (modal) { modal.classList.remove("open"); document.body.style.overflow = ""; } }
  function syncModal(status) {
    var l = $("m-like"), h = $("m-hide");
    if (l) l.textContent = status === "liked" ? "Saved" : "Save";
    if (h) h.textContent = status === "hidden" ? "Unhide" : "Hide";
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
    $("m-apply").addEventListener("click", function () {          // Apply in the modal also auto-logs
      var c = cardByUrl(mUrl);
      if (c && c.getAttribute("data-status") !== "applied") {
        doAction(mUrl, "applied").then(function (ok) { if (ok) { toast("Added to Applications"); syncModal("applied"); render(false); } });
      }
    });
  }

  formatDates();
  wireLogos();
  render(true);
})();
