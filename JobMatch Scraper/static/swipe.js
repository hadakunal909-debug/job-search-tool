// Swipe deck: a Tinder-style one-card-at-a-time view over the same job corpus as the grid feed.
// Reuses the server contracts the feed uses — GET /api/feed (queue), GET /api/job (details),
// POST /api/action (status) — and adds POST /api/swipe/tailor to build a tailored résumé per
// right-swipe. Right = apply (mark applied + open the company page + tailor); left = pass (hide).
(function () {
  "use strict";
  var deckEl = document.getElementById("deck");
  if (!deckEl) return;

  // ---- tiny helpers (kept local so the working feed's app.js stays untouched) ----
  function esc(s) { var d = document.createElement("div"); d.textContent = s == null ? "" : s; return d.innerHTML; }
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
  function scoreCell(j) {
    if (j && j.score_pending)
      return '<span class="score-pending" title="Too short to score yet — a match score appears once the full description is fetched.">JD pending</span>';
    return scoreRing((j && j.score) || 0);
  }
  var toasts = document.getElementById("toasts");
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
  function formatDates(root) {
    var ps = (root || deckEl).querySelectorAll(".posted");
    for (var i = 0; i < ps.length; i++) {
      var s = ps[i].getAttribute("data-d");
      if (s) { ps[i].textContent = relTime(s); ps[i].title = "Posted " + s; }
    }
  }
  function wireLogoFallback(img) {
    if (!img || img.getAttribute("data-fb-wired")) return;
    img.setAttribute("data-fb-wired", "1");
    function fail() { img.style.display = "none"; }
    img.addEventListener("error", fail);
    if (img.complete && img.naturalWidth === 0) fail();
  }
  function wireLogos(root) {
    var imgs = (root || deckEl).querySelectorAll(".logo-img");
    for (var i = 0; i < imgs.length; i++) wireLogoFallback(imgs[i]);
  }
  function applyHref(j) { var u = (j && (j.apply_url || j.url)) || ""; return /^https?:\/\//i.test(u) ? u : ""; }

  // ---- controls / filters ----
  var minR = document.getElementById("min"), minLab = document.getElementById("minlab"),
      sortSel = document.getElementById("sort"), dateSel = document.getElementById("date"),
      expSel = document.getElementById("exp"), internSel = document.getElementById("intern"),
      everifyOnly = document.getElementById("everifyonly"), hideNo = document.getElementById("hidenospon");
  var loadingEl = document.getElementById("deck-loading"), emptyEl = document.getElementById("deck-empty");
  var passBtn = document.getElementById("sw-pass"), applyBtn = document.getElementById("sw-apply"),
      infoBtn = document.getElementById("sw-info"), undoBtn = document.getElementById("sw-undo");

  var minVal = minR ? (parseInt(minR.value, 10) || 0) : 0;
  var sortBy = sortSel ? sortSel.value : "score";

  // ---- deck state ----
  var deck = [], seen = {}, offset = 0, total = 0, hasMore = true, loading = false, seq = 0;
  var lastSwipe = null, detailCache = {}, animating = false;

  function buildParams(off) {
    var ps = ["tab=recommended", "min=" + (minVal || 0), "sort=" + encodeURIComponent(sortBy),
              "offset=" + off, "limit=30"];
    if (dateSel && dateSel.value !== "any") ps.push("date=" + encodeURIComponent(dateSel.value));
    if (expSel && expSel.value !== "any") ps.push("exp=" + encodeURIComponent(expSel.value));
    if (everifyOnly && everifyOnly.checked) ps.push("everify=1");
    if (hideNo && hideNo.checked) ps.push("hidenospon=1");
    if (internSel && internSel.value !== "any") ps.push("intern=" + encodeURIComponent(internSel.value));
    return ps.join("&");
  }

  function fetchMore(reset) {
    if (loading) return;
    if (!reset && !hasMore) return;
    loading = true;
    if (reset) { seq++; deck = []; seen = {}; offset = 0; hasMore = true; if (loadingEl) loadingEl.style.display = ""; if (emptyEl) emptyEl.style.display = "none"; deckEl.innerHTML = ""; }
    var mySeq = seq;
    fetch("/api/feed?" + buildParams(offset), { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (mySeq !== seq) return;                 // a reset happened mid-flight; discard
        var rows = (d && d.rows) || [];
        total = (d && d.total) || 0;
        hasMore = !!(d && d.has_more);
        offset += rows.length;
        for (var i = 0; i < rows.length; i++) {
          var j = rows[i];
          if (!j || !j.url || seen[j.url]) continue;
          seen[j.url] = 1;
          var st = j.status || "";
          if (st === "hidden" || st === "applied") continue;   // already actioned — skip
          deck.push(j);
        }
        loading = false;
        renderDeck();
        if (hasMore && deck.length < 5) fetchMore(false);       // page was thin after filtering
      })
      .catch(function () {
        if (mySeq !== seq) return;
        loading = false;
        if (deck.length === 0) { if (loadingEl) loadingEl.style.display = "none"; if (emptyEl) { emptyEl.textContent = "Couldn't load jobs — try again."; emptyEl.style.display = ""; } }
      });
  }

  function peekTransform(idx) { return idx === 0 ? "none" : "translateY(" + (idx * 10) + "px) scale(" + (1 - idx * 0.045).toFixed(3) + ")"; }

  function swipeCardHTML(j, idx) {
    var badges = "";
    if (j.intern) badges += '<span class="intl">Internship</span>';
    if (j.sponsors_h1b === "yes") badges += '<span class="h1b">H1B' + (j.strength === 'high' ? ' (top)' : '') + '</span>';
    if (j.cap_exempt) badges += '<span class="cx">No lottery</span>';
    if (j.everify) badges += '<span class="ev">E-Verify</span>';
    if (j.agency) badges += '<span class="agency">Agency</span>';
    if (j.exp_years !== "" && j.exp_years != null) {
      var ec = j.exp_level === 'senior' ? 'exp-hi' : (j.exp_level === 'mid' ? 'exp-mid' : 'exp-lo');
      badges += '<span class="exp ' + ec + '">' + H(j.exp_years) + '+ yrs</span>';
    }
    if (j.sponsor_jd === 'blocked') badges += '<span class="nospon">No sponsorship</span>';
    else if (j.sponsor_jd === 'open') badges += '<span class="spon">Sponsors</span>';
    var posted = j.date ? '<span class="posted" data-d="' + H(j.date) + '">' + H(j.date) + '</span>' : '';
    return '<article class="swipe-card" data-idx="' + idx + '" data-url="' + H(j.url) + '" style="z-index:' + (100 - idx) + ';transform:' + peekTransform(idx) + '">' +
      '<div class="ov ov-like">APPLY</div><div class="ov ov-nope">PASS</div>' +
      '<div class="sc-top">' +
        '<div class="logo" style="background:' + H(j.logo_color) + '">' + H(j.initial) +
          '<img class="logo-img" src="https://www.google.com/s2/favicons?domain=' + H(j.logo_domain) + '&sz=64" alt="" loading="lazy"></div>' +
        scoreCell(j) +
      '</div>' +
      '<div class="sc-title">' + esc(j.title) + '</div>' +
      '<div class="sc-company">' + esc(j.company) + '</div>' +
      '<div class="sc-meta">' + esc(j.location || 'n/a') + (posted ? ' &middot; ' + posted : '') + '</div>' +
      (badges ? '<div class="sc-badges">' + badges + '</div>' : '') +
      '<div class="sc-why" data-why></div>' +
      '<div class="sc-tap">Tap card for the full description</div>' +
    '</article>';
  }

  function setControls(disabled) {
    if (passBtn) passBtn.disabled = disabled;
    if (applyBtn) applyBtn.disabled = disabled;
    if (infoBtn) infoBtn.disabled = disabled;
  }

  function renderDeck() {
    if (deck.length === 0) {
      deckEl.innerHTML = "";
      if (loading) { if (loadingEl) loadingEl.style.display = ""; if (emptyEl) emptyEl.style.display = "none"; }
      else { if (loadingEl) loadingEl.style.display = "none"; if (emptyEl) emptyEl.style.display = ""; }
      setControls(true);
      return;
    }
    if (loadingEl) loadingEl.style.display = "none";
    if (emptyEl) emptyEl.style.display = "none";
    setControls(false);
    var html = "", n = Math.min(3, deck.length);
    for (var idx = 0; idx < n; idx++) html += swipeCardHTML(deck[idx], idx);
    deckEl.innerHTML = html;
    wireLogos(deckEl); formatDates(deckEl);
    var top = deckEl.querySelector('.swipe-card[data-idx="0"]');
    if (top) attachDrag(top);
    hydrateTop();
  }

  // Fill the top card's "you match" line from /api/job (one fetch per card viewed; cached).
  function hydrateTop() {
    var j = deck[0]; if (!j) return;
    var whyEl = deckEl.querySelector('.swipe-card[data-idx="0"] [data-why]');
    if (!whyEl) return;
    if (detailCache[j.url]) { fillWhy(whyEl, detailCache[j.url]); return; }
    fetch("/api/job?url=" + encodeURIComponent(j.url), { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) return;
        detailCache[j.url] = d;
        if (deck[0] && deck[0].url === j.url) fillWhy(whyEl, d);
      }).catch(function () {});
  }
  function fillWhy(el, d) {
    var have = (d.have || []).slice(0, 4);
    if (have.length) el.innerHTML = '<span class="sc-why-lab">You match</span>' + have.map(function (k) { return '<span class="tag have">' + esc(k) + '</span>'; }).join("");
    else el.textContent = "";
  }

  // ---- drag / gestures ----
  function setOverlay(el, dx) {
    var like = el.querySelector(".ov-like"), nope = el.querySelector(".ov-nope");
    var w = el.offsetWidth || 320, t = w * 0.32;
    if (like) like.style.opacity = dx > 0 ? Math.min(1, dx / t) : 0;
    if (nope) nope.style.opacity = dx < 0 ? Math.min(1, -dx / t) : 0;
  }
  function resetCard(el) { el.style.transition = "transform .25s ease"; el.style.transform = ""; setOverlay(el, 0); }
  function attachDrag(el) {
    var startX = 0, startY = 0, dx = 0, dy = 0, dragging = false, decided = false, horiz = false, t0 = 0;
    el.addEventListener("pointerdown", function (e) {
      if (animating) return;
      if (e.target.closest && e.target.closest("a,button")) return;
      dragging = true; decided = false; horiz = false; dx = 0; dy = 0;
      startX = e.clientX; startY = e.clientY; t0 = Date.now();
      try { el.setPointerCapture(e.pointerId); } catch (_) {}
      el.style.transition = "none";
    });
    el.addEventListener("pointermove", function (e) {
      if (!dragging) return;
      dx = e.clientX - startX; dy = e.clientY - startY;
      if (!decided) {
        if (Math.abs(dx) < 6 && Math.abs(dy) < 6) return;
        decided = true; horiz = Math.abs(dx) >= Math.abs(dy);
      }
      if (!horiz) return;
      el.style.transform = "translate(" + dx + "px," + (dy * 0.12).toFixed(1) + "px) rotate(" + (dx * 0.05).toFixed(2) + "deg)";
      setOverlay(el, dx);
    });
    function end() {
      if (!dragging) return;
      dragging = false;
      var elapsed = Date.now() - t0, w = el.offsetWidth || 320, threshold = w * 0.32;
      var fast = elapsed < 300 && Math.abs(dx) > 60;
      if (horiz && (Math.abs(dx) > threshold || fast)) { doCommit(dx > 0 ? 1 : -1); return; }
      if (!decided && Math.abs(dx) < 6 && Math.abs(dy) < 6 && elapsed < 400) { resetCard(el); openDetails(deck[0]); return; }
      resetCard(el);
    }
    el.addEventListener("pointerup", end);
    el.addEventListener("pointercancel", end);
  }

  function doAction(url, status) {
    return fetch("/api/action", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: url, status: status }) })
      .then(function (r) { return r.json(); }).then(function (j) { return !!(j && j.ok); })
      .catch(function () { toast("Network error saving that swipe."); return false; });
  }
  function openApply(j) {
    var href = applyHref(j); if (!href) { toast("No apply link for this job."); return; }
    var w = window.open(href, "_blank", "noopener");
    if (!w) toast("Popup blocked — open the company page from the tray link.");
  }

  // Commit runs inside a user gesture (pointerup / click / keydown) so window.open is allowed.
  function doCommit(dir) {
    if (animating) return;
    var j = deck[0]; if (!j) return;
    animating = true;
    var topEl = deckEl.querySelector('.swipe-card[data-idx="0"]');
    if (dir > 0) {
      doAction(j.url, "applied");
      openApply(j);
      enqueueTailor(j);
      toast("Applied · building your résumé");
    } else {
      doAction(j.url, "hidden");
    }
    lastSwipe = { job: j, dir: dir };
    if (undoBtn) undoBtn.disabled = false;
    deck.shift();
    if (topEl) {
      topEl.style.transition = "transform .32s ease, opacity .32s ease";
      topEl.style.transform = "translateX(" + (dir * 140) + "%) rotate(" + (dir * 18) + "deg)";
      topEl.style.opacity = "0";
    }
    setTimeout(function () {
      animating = false;
      renderDeck();
      if (hasMore && deck.length < 5) fetchMore(false);
    }, 300);
  }

  function undo() {
    if (animating || !lastSwipe) return;
    var j = lastSwipe.job;
    doAction(j.url, "");                 // clear the status back to none
    delete seen[j.url];
    deck.unshift(j);
    lastSwipe = null;
    if (undoBtn) undoBtn.disabled = true;
    renderDeck();
    toast("Brought it back");
  }

  // ---- tailor queue + résumé tray (a bottom sheet opened from the app-bar badge) ----
  var trayList = document.getElementById("tray-list");
  var trayOpenBtn = document.getElementById("tray-open"), trayCount = document.getElementById("tray-count");
  var trayRows = {}, trayOrder = [], tq = [], working = false;

  function enqueueTailor(j) {
    if (!j || !j.url) return;
    if (!trayRows[j.url]) { trayRows[j.url] = { url: j.url, company: j.company || "", title: j.title || "", state: "queued", data: null, error: "" }; trayOrder.unshift(j.url); }
    else { trayRows[j.url].state = "queued"; trayRows[j.url].error = ""; }
    renderTray();
    tq.push({ url: j.url, company: j.company || "" });
    pump();
  }
  function pump() {
    if (working) return;
    var item = tq.shift();
    if (!item) return;
    working = true;
    if (trayRows[item.url]) { trayRows[item.url].state = "building"; renderTray(); }
    fetch("/api/swipe/tailor", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: item.url, company: item.company }) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        working = false;
        var row = trayRows[item.url]; if (!row) { pump(); return; }
        if (d && d.ok && d.file) { row.state = "ready"; row.data = d; }
        else { row.state = "error"; row.error = (d && d.error) || "Tailoring failed."; }
        renderTray(); pump();
      })
      .catch(function () {
        working = false;
        var row = trayRows[item.url]; if (row) { row.state = "error"; row.error = "Network error."; }
        renderTray(); pump();
      });
  }
  function renderTray() {
    if (!trayList) return;
    var html = "";
    for (var i = 0; i < trayOrder.length && i < 12; i++) {
      var row = trayRows[trayOrder[i]]; if (!row) continue;
      var head = '<div class="tr-title">' + esc(row.title || "Résumé") + '</div><div class="tr-co">' + esc(row.company || "") + '</div>';
      var body;
      if (row.state === "ready") {
        var d = row.data || {}, note = (d.notes && d.notes[0]) ? d.notes[0] : (d.ai_used ? "AI-tailored" : "Best-matching résumé");
        body = '<button class="btn primary sm tr-dl" data-dl="' + H(row.url) + '">Download PDF</button>' +
               '<span class="tr-note" title="' + H(note) + '">' + esc(note.length > 46 ? note.slice(0, 44) + "…" : note) + '</span>';
      } else if (row.state === "error") {
        body = '<span class="tr-err">' + esc(row.error || "Failed") + '</span><button class="btn sm tr-retry" data-retry="' + H(row.url) + '">Retry</button>';
      } else {
        body = '<span class="tr-building"><span class="spin"></span> Building résumé…</span>';
      }
      html += '<div class="tr-row">' + head + '<div class="tr-body">' + body + '</div></div>';
    }
    trayList.innerHTML = html || '<div class="tr-empty">Swipe right to build tailored résumés here.</div>';
    var n = trayOrder.length;
    if (trayCount) trayCount.textContent = n;
    if (trayOpenBtn) trayOpenBtn.style.display = n ? "" : "none";
  }
  function downloadFile(file) {
    if (!file || !file.b64) return;
    try {
      var bin = atob(file.b64), bytes = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      var blob = new Blob([bytes], { type: file.mime || "application/octet-stream" });
      var u = URL.createObjectURL(blob), a = document.createElement("a");
      a.href = u; a.download = file.name || "resume.pdf"; a.target = "_self";
      document.body.appendChild(a); a.click();
      setTimeout(function () { URL.revokeObjectURL(u); a.remove(); }, 1500);
    } catch (e) { toast("Couldn't prepare the download."); }
  }
  if (trayList) trayList.addEventListener("click", function (e) {
    var dl = e.target.closest && e.target.closest("[data-dl]");
    if (dl) { var row = trayRows[dl.getAttribute("data-dl")]; if (row && row.data) downloadFile(row.data.file); return; }
    var rt = e.target.closest && e.target.closest("[data-retry]");
    if (rt) { var url = rt.getAttribute("data-retry"), r2 = trayRows[url]; if (r2) { r2.state = "queued"; r2.error = ""; renderTray(); tq.push({ url: url, company: r2.company }); pump(); } }
  });
  // ---- app chrome: drawer + bottom sheets + scrim (mobile-app navigation) ----
  var scrim = document.getElementById("scrim"), drawer = document.getElementById("drawer"),
      filtersheet = document.getElementById("filtersheet"), traysheet = document.getElementById("traysheet");
  function anyOverlayOpen() {
    return (drawer && drawer.classList.contains("open")) ||
           (filtersheet && filtersheet.classList.contains("open")) ||
           (traysheet && traysheet.classList.contains("open"));
  }
  function closeAll() {
    if (drawer) drawer.classList.remove("open");
    if (filtersheet) filtersheet.classList.remove("open");
    if (traysheet) traysheet.classList.remove("open");
    if (scrim) scrim.classList.remove("show");
  }
  function openOverlay(el) { if (!el) return; closeAll(); el.classList.add("open"); if (scrim) scrim.classList.add("show"); }
  if (scrim) scrim.addEventListener("click", closeAll);
  var navOpenBtn = document.getElementById("nav-open"), filtOpenBtn = document.getElementById("filt-open"),
      filtDoneBtn = document.getElementById("filt-done"), trayCloseBtn = document.getElementById("tray-close");
  if (navOpenBtn) navOpenBtn.addEventListener("click", function () { openOverlay(drawer); });
  if (filtOpenBtn) filtOpenBtn.addEventListener("click", function () { openOverlay(filtersheet); });
  if (filtDoneBtn) filtDoneBtn.addEventListener("click", closeAll);
  if (trayOpenBtn) trayOpenBtn.addEventListener("click", function () { openOverlay(traysheet); });
  if (trayCloseBtn) trayCloseBtn.addEventListener("click", closeAll);
  if (drawer) drawer.addEventListener("click", function (e) { if (e.target.closest("a.dw-link")) closeAll(); });

  // drawer theme toggle (the global #themetoggle lives in the hidden top bar on this page)
  var themeBtn = document.getElementById("theme-toggle-drawer");
  if (themeBtn) themeBtn.addEventListener("click", function () {
    var cur = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
    var next = cur === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("theme", next); } catch (e) {}
  });

  // ---- details modal (reuses the feed modal markup + /api/job) ----
  var modal = document.getElementById("jobmodal"), mUrl = "";
  function $(id) { return document.getElementById(id); }
  function openDetails(j) {
    if (!modal || !j) return;
    mUrl = j.url;
    $("m-logo").innerHTML = '<span>' + H(j.initial || "") + '</span><img class="logo-img" src="https://www.google.com/s2/favicons?domain=' + H(j.logo_domain) + '&sz=64" alt="">';
    $("m-logo").style.background = j.logo_color || "";
    wireLogos($("m-logo"));
    $("m-title").textContent = j.title || "";
    $("m-meta").textContent = (j.company || "") + " · " + (j.location || "n/a");
    $("m-chip").innerHTML = scoreCell(j);
    $("m-jd").innerHTML = '<div class="loading-jd"><span class="spin"></span>Loading description…</div>';
    $("m-skills").innerHTML = "";
    $("m-apply").href = applyHref(j) || "#";
    $("m-tailor").href = "/brain?job=" + encodeURIComponent(j.url);
    modal.classList.add("open"); document.body.style.overflow = "hidden";
    var done = function (d) {
      if (!d || !d.ok) { $("m-jd").textContent = "Couldn't load details."; return; }
      $("m-chip").innerHTML = scoreCell(d);
      var sk = "";
      if (d.missing && d.missing.length) sk += '<div class="sechdr">Add these to your résumé</div><div class="kw">' + d.missing.map(function (k) { return '<span class="tag miss">' + esc(k) + '</span>'; }).join("") + '</div>';
      if (d.have && d.have.length) sk += '<div class="sechdr">Skills you already match</div><div class="kw">' + d.have.map(function (k) { return '<span class="tag have">' + esc(k) + '</span>'; }).join("") + '</div>';
      $("m-skills").innerHTML = sk;
      $("m-jd").textContent = d.jd || "No description stored — open Apply to read it on the company site.";
    };
    if (detailCache[j.url]) { done(detailCache[j.url]); return; }
    fetch("/api/job?url=" + encodeURIComponent(j.url), { cache: "no-store" })
      .then(function (r) { return r.json(); }).then(function (d) { detailCache[j.url] = d; done(d); })
      .catch(function () { $("m-jd").textContent = "Couldn't load details."; });
  }
  function closeModal() { if (modal) { modal.classList.remove("open"); document.body.style.overflow = ""; } }
  if (modal) {
    $("m-close").addEventListener("click", closeModal);
    modal.addEventListener("click", function (e) { if (e.target === modal) closeModal(); });
  }

  // ---- button + keyboard controls ----
  if (passBtn) passBtn.addEventListener("click", function () { if (deck[0]) doCommit(-1); });
  if (applyBtn) applyBtn.addEventListener("click", function () { if (deck[0]) doCommit(1); });
  if (infoBtn) infoBtn.addEventListener("click", function () { if (deck[0]) openDetails(deck[0]); });
  if (undoBtn) undoBtn.addEventListener("click", undo);
  document.addEventListener("keydown", function (e) {
    if (modal && modal.classList.contains("open")) { if (e.key === "Escape") closeModal(); return; }
    if (anyOverlayOpen()) { if (e.key === "Escape") closeAll(); return; }
    if (e.target && /^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) return;
    if (e.key === "ArrowRight") { e.preventDefault(); if (deck[0]) doCommit(1); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); if (deck[0]) doCommit(-1); }
    else if (e.key === "ArrowUp" || e.key === "i") { e.preventDefault(); if (deck[0]) openDetails(deck[0]); }
    else if (e.key === "Backspace") { e.preventDefault(); undo(); }
  });

  // ---- filter wiring (each change rebuilds the deck) ----
  function setFill() {
    if (!minR) return;
    var mn = parseInt(minR.min, 10) || 0, mx = parseInt(minR.max, 10) || 100, v = parseInt(minR.value, 10) || 0;
    minR.style.setProperty("--p", (mx > mn ? (v - mn) / (mx - mn) * 100 : 0) + "%");
    if (minLab) minLab.textContent = v === 0 ? "Any" : v + "%+";
  }
  var _deb;
  function resetDeck() { fetchMore(true); }
  function debouncedReset() { if (_deb) clearTimeout(_deb); _deb = setTimeout(resetDeck, 300); }
  if (minR) { setFill(); minR.addEventListener("input", function () { minVal = parseInt(minR.value, 10) || 0; setFill(); debouncedReset(); }); }
  if (sortSel) sortSel.addEventListener("change", function () { sortBy = sortSel.value; resetDeck(); });
  if (dateSel) dateSel.addEventListener("change", resetDeck);
  if (expSel) expSel.addEventListener("change", resetDeck);
  if (internSel) internSel.addEventListener("change", resetDeck);
  if (everifyOnly) everifyOnly.addEventListener("change", resetDeck);
  if (hideNo) hideNo.addEventListener("change", resetDeck);

  renderTray();
  fetchMore(true);
})();
