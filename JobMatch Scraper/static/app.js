// Feed: data ships as compact JSON; we render only the visible slice of cards client-side
// (instead of ~2,500 server-rendered <article> nodes), then filter/sort/paginate + open the
// job-detail modal + no-reload actions. Far less HTML to transfer and far fewer DOM nodes.
(function () {
  "use strict";
  var feed = document.getElementById("feed");
  if (!feed) return;
  var DATA = [];
  var dataEl = document.getElementById("feeddata");
  try { DATA = JSON.parse(dataEl ? dataEl.textContent : "[]") || []; } catch (e) { DATA = []; }
  var byUrl = {};
  for (var di = 0; di < DATA.length; di++) byUrl[DATA[di].url] = DATA[di];

  var q = document.getElementById("q"), minR = document.getElementById("min"),
      minLab = document.getElementById("minlab"), sortSel = document.getElementById("sort"),
      dateSel = document.getElementById("date"), countEl = document.getElementById("count"),
      emptyEl = document.getElementById("empty"), moreBtn = document.getElementById("loadmore"),
      toasts = document.getElementById("toasts"), hideNo = document.getElementById("hidenospon"),
      expSel = document.getElementById("exp"), everifyOnly = document.getElementById("everifyonly"),
      tabBtns = document.querySelectorAll(".tab");
  var tab = "recommended", PAGE = 60, limit = PAGE, sortBy = sortSel ? sortSel.value : "score";
  var minVal = minR ? (parseInt(minR.value, 10) || 0) : 0;
  // Large corpus: the server inlines only the top-N matches and we fetch the rest (search/filter/
  // paging) from /api/feed, so the payload stays small at any scale. Small corpus: data-paged is
  // empty and everything stays client-side (instant) exactly as before.
  var PAGED = feed.getAttribute("data-paged") === "1";
  var shown = 0, _seq = 0, _deb;

  // textContent escape (safe in element text)
  function esc(s) { var d = document.createElement("div"); d.textContent = s == null ? "" : s; return d.innerHTML; }
  // attribute-safe escape (also neutralizes quotes) — matches Jinja autoescaping
  function H(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
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

  // Company-logo fallback: if the favicon fails to load, hide the broken <img> so the
  // colored letter-avatar behind it shows. In JS (not inline onerror=) so the CSP can
  // forbid inline handlers.
  function wireLogoFallback(img) {
    if (!img || img.getAttribute("data-fb-wired")) return;
    img.setAttribute("data-fb-wired", "1");
    function fail() {
      var fb = img.getAttribute("data-fallback");
      if (fb && img.getAttribute("src") !== fb) { img.src = fb; return; }
      img.style.display = "none";
    }
    img.addEventListener("error", fail);
    if (img.complete && img.naturalWidth === 0) fail();
  }
  function wireLogos(root) {
    var imgs = (root || feed).querySelectorAll(".logo-img");
    for (var i = 0; i < imgs.length; i++) wireLogoFallback(imgs[i]);
  }

  // Build one card's HTML from its data object — mirrors the old Jinja <article> exactly.
  function cardHTML(j) {
    var st = j.status || "";
    var badges = "";
    if (j.sponsors_h1b === "yes")
      badges += '<span class="h1b" title="Company has sponsored H-1B before' +
        (j.strength ? ' &middot; ~' + (j.strength_n || 0) + ' filings' : '') + '">H1B' +
        (j.strength === 'high' ? ' (top sponsor)' : '') + '</span>';
    if (j.cap_exempt)
      badges += '<span class="cx" title="Likely H-1B cap-exempt (university / nonprofit hospital / research) — no H-1B lottery. Verify.">No lottery</span>';
    if (j.everify)
      badges += '<span class="ev" title="Listed in an E-Verify enrolled-employer snapshot — required for the STEM-OPT extension. Confirm current status at e-verify.gov before relying on it.">E-Verify</span>';
    if (j.exp_years !== "" && j.exp_years != null) {
      var ec = j.exp_level === 'senior' ? 'exp-hi' : (j.exp_level === 'mid' ? 'exp-mid' : 'exp-lo');
      badges += '<span class="exp ' + ec + '" title="The description asks for about ' + H(j.exp_years) +
        '+ years of experience">' + H(j.exp_years) + '+ yrs</span>';
    }
    if (j.sponsor_jd === 'blocked')
      badges += '<span class="nospon" title="' + H(j.sponsor_reason) + '">No sponsorship</span>';
    else if (j.sponsor_jd === 'open')
      badges += '<span class="spon" title="' + H(j.sponsor_reason) + '">Sponsors</span>';
    var posted = j.date ? ' · <span class="posted" data-d="' + H(j.date) + '">' + H(j.date) + '</span>' : '';
    var applyHref = /^https?:\/\//i.test(j.apply_url || "") ? j.apply_url : "#";
    return '<article class="card" data-url="' + H(j.url) + '" data-status="' + H(st) + '">' +
      '<div class="cardtop">' +
        '<div class="logo" style="background:' + H(j.logo_color) + '">' + H(j.initial) +
          '<img class="logo-img" src="https://www.google.com/s2/favicons?domain=' + H(j.logo_domain) +
          '&sz=64" alt="" loading="lazy"></div>' +
        scoreRing(j.score || 0) +
      '</div>' +
      '<div class="ctitle">' + esc(j.title) + '</div>' +
      '<div class="cmeta">' + esc(j.company) + ' · ' + esc(j.location || 'n/a') + posted + badges + '</div>' +
      '<div class="cardact">' +
        '<a class="btn primary sm" href="' + H(applyHref) + '" target="_blank" rel="noopener" data-apply="1">Apply ↗</a>' +
        '<a class="btn sm" href="/brain?job=' + encodeURIComponent(j.url) + '">Tailor</a>' +
        '<span class="spacer"></span>' +
        '<span class="acts">' +
          '<button class="ico" data-act="liked" title="Save">' + (st === 'liked' ? 'Saved' : 'Save') + '</button>' +
          '<button class="ico" data-act="applied" title="Mark applied">' + (st === 'applied' ? 'Applied' : 'Mark applied') + '</button>' +
          '<button class="ico" data-act="hidden" title="Hide">' + (st === 'hidden' ? 'Hidden' : 'Hide') + '</button>' +
        '</span>' +
      '</div>' +
    '</article>';
  }

  function dateCutoff() {
    if (!dateSel || dateSel.value === "any") return "";
    var d = new Date(); d.setDate(d.getDate() - parseInt(dateSel.value, 10)); return d.toISOString().slice(0, 10);
  }
  function matches(j, cut, ignoreMin) {
    var st = j.status || "", sc = j.score || 0, ok;
    var searching = q && q.value.trim();
    if (tab === "liked") ok = st === "liked";
    else if (tab === "applied") ok = st === "applied";
    else if (tab === "hidden") ok = st === "hidden";
    // An active SEARCH bypasses the match filter: if you typed "deloitte" you want to
    // SEE Deloitte's jobs, not have them hidden because they score 40%.
    else ok = (st !== "hidden") && (searching || ignoreMin || sc >= minVal);
    if (ok && searching)
      ok = ((j.title || "") + " " + (j.company || "")).toLowerCase().indexOf(q.value.toLowerCase().trim()) !== -1;
    if (ok && cut) { var dt = j.date || ""; if (dt && dt < cut) ok = false; }
    if (ok && hideNo && hideNo.checked && j.sponsor_jd === "blocked") ok = false;
    if (ok && everifyOnly && everifyOnly.checked && !j.everify) ok = false;
    // Experience filter: a job whose JD states no year count (exp_years "") is ALWAYS kept.
    if (ok && expSel && expSel.value !== "any") {
      var ev = j.exp_years;
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
  function renderLocal(reset) {
    if (reset) limit = PAGE;
    var cut = dateCutoff(), matched = [];
    for (var i = 0; i < DATA.length; i++) if (matches(DATA[i], cut)) matched.push(DATA[i]);
    matched.sort(function (a, b) {
      if (sortBy === "newest") return (b.date || "").localeCompare(a.date || "");
      return (b.score || 0) - (a.score || 0);
    });
    var slice = matched.slice(0, limit), html = "";
    for (var k = 0; k < slice.length; k++) html += cardHTML(slice[k]);
    feed.innerHTML = html;
    formatDates(); wireLogos();
    if (countEl) countEl.textContent = matched.length;
    if (emptyEl) emptyEl.style.display = matched.length ? "none" : "";
    if (moreBtn) {
      moreBtn.style.display = (matched.length > limit) ? "" : "none";
      if (matched.length > limit) moreBtn.textContent = "Load more (" + (matched.length - limit) + " more)";
    }
  }

  // Dispatcher: small corpus renders locally from the inline DATA (instant); large corpus
  // (data-paged) fetches each page from /api/feed so the payload stays small at any scale.
  function render(reset) { if (PAGED) renderServer(reset); else renderLocal(reset); }

  function buildParams(offset) {
    var ps = ["tab=" + encodeURIComponent(tab), "min=" + (minVal || 0),
              "sort=" + encodeURIComponent(sortBy), "offset=" + offset, "limit=" + PAGE];
    if (q && q.value.trim()) ps.push("q=" + encodeURIComponent(q.value.trim()));
    if (dateSel && dateSel.value !== "any") ps.push("date=" + encodeURIComponent(dateSel.value));
    if (expSel && expSel.value !== "any") ps.push("exp=" + encodeURIComponent(expSel.value));
    if (everifyOnly && everifyOnly.checked) ps.push("everify=1");
    if (hideNo && hideNo.checked) ps.push("hidenospon=1");
    return ps.join("&");
  }
  function renderServer(reset) {
    if (reset) { shown = 0; feed.innerHTML = '<div class="loading-jd" style="padding:28px"><span class="spin"></span>Loading…</div>'; }
    var mySeq = ++_seq;                                   // ignore out-of-order responses
    fetch("/api/feed?" + buildParams(reset ? 0 : shown)).then(function (r) { return r.json(); }).then(function (d) {
      if (mySeq !== _seq) return;
      var rows = (d && d.rows) || [], htmlc = "";
      for (var i = 0; i < rows.length; i++) byUrl[rows[i].url] = rows[i];
      for (var k = 0; k < rows.length; k++) htmlc += cardHTML(rows[k]);
      if (reset) feed.innerHTML = htmlc; else feed.insertAdjacentHTML("beforeend", htmlc);
      shown += rows.length;
      formatDates(); wireLogos();
      if (countEl) countEl.textContent = (d && d.total) || 0;
      if (emptyEl) emptyEl.style.display = (d && d.total) ? "none" : "";
      if (moreBtn) { var more = !!(d && d.has_more); moreBtn.style.display = more ? "" : "none"; if (more) moreBtn.textContent = "Load more (" + ((d.total - shown)) + " more)"; }
    }).catch(function () { if (mySeq === _seq && reset) feed.innerHTML = '<div class="empty">Couldn\'t load jobs — try again.</div>'; });
  }
  function debouncedRender() { if (_deb) clearTimeout(_deb); _deb = setTimeout(function () { render(true); }, 250); }
  function cardEl(url) { var cs = feed.querySelectorAll(".card"); for (var i = 0; i < cs.length; i++) if (cs[i].getAttribute("data-url") === url) return cs[i]; return null; }
  // After an action: small corpus re-renders locally; paged updates just the touched card in place
  // (or drops it if it no longer matches the current tab/filters) — no full refetch.
  function afterAction(j) {
    if (!PAGED) { render(false); return; }
    var el = cardEl(j.url);
    if (matches(j, dateCutoff())) { if (el) el.outerHTML = cardHTML(j); }
    else if (el) { if (el.parentNode) el.parentNode.removeChild(el); if (countEl) { var n = parseInt(countEl.textContent, 10); if (!isNaN(n) && n > 0) countEl.textContent = n - 1; } }
  }

  function doAction(url, next) {
    return fetch("/api/action", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: url, status: next }) })
      .then(function (r) { return r.json(); }).then(function (j) {
        if (!j || !j.ok) { toast("Couldn't save — try again."); return false; }
        return true;
      }).catch(function () { toast("Network error."); return false; });
  }
  function actLabel(s) { return s === "liked" ? "Saved" : s === "applied" ? "Marked applied" : s === "hidden" ? "Hidden" : "Removed"; }

  // controls
  for (var t = 0; t < tabBtns.length; t++) tabBtns[t].addEventListener("click", function () {
    for (var k = 0; k < tabBtns.length; k++) tabBtns[k].classList.remove("on");
    this.classList.add("on"); tab = this.getAttribute("data-tab"); render(true); window.scrollTo({ top: 0, behavior: "smooth" });
  });
  if (q) q.addEventListener("input", function () { if (PAGED) debouncedRender(); else render(true); });
  function setFill() {
    if (!minR) return;
    var mn = parseInt(minR.min, 10) || 0, mx = parseInt(minR.max, 10) || 100, v = parseInt(minR.value, 10) || 0;
    minR.style.setProperty("--p", (mx > mn ? (v - mn) / (mx - mn) * 100 : 0) + "%");
    if (minLab) minLab.textContent = v === 0 ? "Any" : v + "%+";
  }
  if (minR) {
    setFill();
    var _raf;
    minR.addEventListener("input", function () {
      minVal = parseInt(minR.value, 10) || 0; setFill();
      if (PAGED) { debouncedRender(); return; }
      if (_raf) cancelAnimationFrame(_raf);
      _raf = requestAnimationFrame(function () { render(true); });
    });
  }
  if (sortSel) sortSel.addEventListener("change", function () { sortBy = sortSel.value; render(true); });
  if (dateSel) dateSel.addEventListener("change", function () { render(true); });
  if (expSel) expSel.addEventListener("change", function () { render(true); });
  if (everifyOnly) everifyOnly.addEventListener("change", function () { render(true); });
  if (hideNo) hideNo.addEventListener("change", function () { render(true); });
  if (moreBtn) moreBtn.addEventListener("click", function () { limit += PAGE; render(false); });

  // feed clicks: action buttons, Apply auto-log, or open modal
  feed.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest("button[data-act]") : null;
    if (btn) {
      e.preventDefault();
      var card = btn.closest(".card"); if (!card) return;
      var j = byUrl[card.getAttribute("data-url")]; if (!j) return;
      var act = btn.getAttribute("data-act"), cur = j.status || "", next = (cur === act) ? "" : act;
      btn.disabled = true;
      doAction(j.url, next).then(function (ok) {
        btn.disabled = false; if (!ok) return; toast(actLabel(next));
        j.status = next; afterAction(j);
      });
      return;
    }
    var lnk = e.target.closest && e.target.closest("a");
    if (lnk) {                                                    // Apply/Tailor links open normally
      if (lnk.hasAttribute("data-apply")) {                      // clicking Apply auto-logs it
        var ac = lnk.closest(".card"), aj = ac && byUrl[ac.getAttribute("data-url")];
        if (aj && aj.status !== "applied")
          doAction(aj.url, "applied").then(function (ok) { if (ok) { toast("Added to Applications"); aj.status = "applied"; afterAction(aj); } });
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
    var mlImg = $("m-logo").querySelector(".logo-img");
    if (mlImg) { mlImg.removeAttribute("data-fb-wired"); wireLogoFallback(mlImg); }
    $("m-title").textContent = card.querySelector(".ctitle") ? card.querySelector(".ctitle").textContent : "";
    $("m-meta").textContent = card.querySelector(".cmeta") ? card.querySelector(".cmeta").textContent : "";
    $("m-chip").innerHTML = '<span class="skel skel-chip"></span>';
    $("m-skills").innerHTML = '<div class="skel-row"><span class="skel skel-tag"></span><span class="skel skel-tag"></span><span class="skel skel-tag" style="width:88px"></span></div>' +
      '<span class="skel skel-bar w75"></span><span class="skel skel-bar w55"></span>';
    $("m-jd").innerHTML = '<div class="loading-jd"><span class="spin"></span>Loading description…</div>';
    $("m-apply").href = /^https?:\/\//i.test(mUrl) ? mUrl : "#";
    $("m-tailor").href = "/brain?job=" + encodeURIComponent(mUrl);
    syncModal((byUrl[mUrl] && byUrl[mUrl].status) || "");
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
      if (j.everify) spn += '<span class="ev" title="Confirm current status at e-verify.gov">E-Verify · STEM-OPT OK</span>';
      if (j.cap_exempt) spn += '<span class="cx">Likely cap-exempt — no H-1B lottery</span>';
      if (j.sponsor_jd === "blocked") spn += '<span class="nospon">' + esc(j.sponsor_reason || "Likely no sponsorship") + '</span>';
      else if (j.sponsor_jd === "open") spn += '<span class="spon">' + esc(j.sponsor_reason || "Offers sponsorship") + '</span>';
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
      var j = byUrl[mUrl]; if (!j) return; var next = j.status === "liked" ? "" : "liked";
      doAction(mUrl, next).then(function (ok) { if (ok) { toast(actLabel(next)); j.status = next; syncModal(next); afterAction(j); } });
    });
    $("m-hide").addEventListener("click", function () {
      var j = byUrl[mUrl]; if (!j) return; var next = j.status === "hidden" ? "" : "hidden";
      doAction(mUrl, next).then(function (ok) { if (ok) { toast(next ? "Hidden" : "Unhidden"); j.status = next; closeModal(); afterAction(j); } });
    });
    $("m-apply").addEventListener("click", function () {
      var j = byUrl[mUrl];
      if (j && j.status !== "applied")
        doAction(mUrl, "applied").then(function (ok) { if (ok) { toast("Added to Applications"); j.status = "applied"; syncModal("applied"); afterAction(j); } });
    });
  }

  render(true);
})();
