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
      internSel = document.getElementById("intern"),
      locInp = document.getElementById("loc"), remoteOnly = document.getElementById("remoteonly"),
      minSalSel = document.getElementById("minsal"), hideAgency = document.getElementById("hideagency"),
      showClosed = document.getElementById("showclosed"), groupedEl = document.getElementById("grouped"),
      tabBtns = document.querySelectorAll(".tab");
  var tab = "recommended", PAGE = 60, limit = PAGE, sortBy = sortSel ? sortSel.value : "score";
  var minVal = minR ? (parseInt(minR.value, 10) || 0) : 0;
  // Large corpus: the server inlines only the top-N matches and we fetch the rest (search/filter/
  // paging) from /api/feed, so the payload stays small at any scale. Small corpus: data-paged is
  // empty and everything stays client-side (instant) exactly as before.
  var PAGED = feed.getAttribute("data-paged") === "1";
  var shown = 0, _seq = 0, _deb;
  // Feed grouping — see the "feed grouping" block in web.py. Both numbers come from the server
  // so one env var (FEED_GROUP_LEAD) moves the client and the server together; 0 disables it.
  var GROUP_LEAD = parseInt(feed.getAttribute("data-group-lead"), 10);
  if (isNaN(GROUP_LEAD)) GROUP_LEAD = 0;
  var GROUP_MIN = GROUP_LEAD + 2;
  // Which groups the user has opened, and how many of their hidden rows are revealed:
  // {groupKey: count}. Only the non-paged path reads it (it re-renders wholesale); the paged
  // path inserts the extra cards into the DOM instead. Cleared whenever the filters change.
  var expanded = Object.create(null);

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
  // The score cell for a card/detail: the % ring, OR a neutral "JD pending" chip when the job's
  // description is too short/truncated to score honestly (score_pending from the server).
  function scoreCell(j) {
    if (j && j.score_pending)
      return '<span class="score-pending" title="This description is too short to score reliably yet — it\'ll get a match score once the full job description is fetched.">JD pending</span>';
    return scoreRing((j && j.score) || 0);
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
      if (s) {
        ps[i].textContent = relTime(s);
        ps[i].title = (ps[i].getAttribute("data-verified") ? "Verified posting date · " : "Posted ") + s;
      }
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
  // `xg` (optional) tags the card as one revealed by expanding that group's "+N more" tile,
  // so collapsing the tile again knows exactly which cards to take back out.
  function cardHTML(j, xg) {
    var st = j.status || "";
    // "New" mirrors the card's own date label: show it iff the displayed date renders as "Today".
    // relTime() is the same fn that renders .posted, so the badge and the date can never disagree,
    // and it's timezone-correct in the viewer's locale (handles UTC-stamped dates that read today).
    var newFlag = (relTime(j.date) === "Today") ? '<span class="newflag">New</span>' : '';
    var badges = "";
    if (j.intern)
      badges += '<span class="intl" title="Internship / co-op — OPT &amp; STEM-OPT eligible">Internship</span>';
    if (j.sponsors_h1b === "yes")
      badges += '<span class="h1b" title="Company has sponsored H-1B before' +
        (j.strength ? ' &middot; ~' + (j.strength_n || 0) + ' filings' : '') + '">H1B' +
        (j.strength === 'high' ? ' (top sponsor)' : '') + '</span>';
    if (j.cap_exempt)
      badges += '<span class="cx" title="Likely H-1B cap-exempt (university / nonprofit hospital / research) — no H-1B lottery. Verify.">No lottery</span>';
    if (j.everify)
      badges += '<span class="ev" title="Listed in an E-Verify enrolled-employer snapshot — required for the STEM-OPT extension. Confirm current status at e-verify.gov before relying on it.">E-Verify</span>';
    if (j.agency)
      badges += '<span class="agency" title="Staffing agency / consultancy — postings are placement or bench roles, not a direct employer\'s own team. Kept for their H-1B sponsorship, flagged so you can skip if you prefer direct employers.">Agency</span>';
    if (j.exp_years !== "" && j.exp_years != null) {
      var ec = j.exp_level === 'senior' ? 'exp-hi' : (j.exp_level === 'mid' ? 'exp-mid' : 'exp-lo');
      badges += '<span class="exp ' + ec + '" title="The description asks for about ' + H(j.exp_years) +
        '+ years of experience">' + H(j.exp_years) + '+ yrs</span>';
    }
    if (j.sponsor_jd === 'blocked')
      badges += '<span class="nospon" title="' + H(j.sponsor_reason) + '">No sponsorship</span>';
    else if (j.sponsor_jd === 'open')
      badges += '<span class="spon" title="' + H(j.sponsor_reason) + '">Sponsors</span>';
    if (j.salary_label)
      badges += '<span class="pay" title="Pay range stated in the job description">' +
        H(j.salary_label) + '</span>';
    if (j.remote)
      badges += '<span class="rem" title="Remote or remote-friendly per the posting">Remote</span>';
    if (j.closed)
      badges += '<span class="closed" title="This posting has disappeared from the company\'s job board across several checks, so it is probably filled or expired.">Closed</span>';
    var posted = j.date ? ' · <span class="posted" data-d="' + H(j.date) + '"' +
      (j.date_verified ? ' data-verified="1"' : '') + '>' + H(j.date) + '</span>' : '';
    var applyHref = /^https?:\/\//i.test(j.apply_url || "") ? j.apply_url : "#";
    var cls = "card" + (j.closed ? " is-closed" : "") + (xg ? " in-group" : "");
    return '<article class="' + cls + '"' + (xg ? ' data-xg="' + H(xg) + '"' : '') +
      ' data-url="' + H(j.url) + '" data-status="' + H(st) + '">' + newFlag +
      '<div class="cardtop">' +
        '<div class="logo" style="background:' + H(j.logo_color) + '">' + H(j.initial) +
          '<img class="logo-img" src="https://www.google.com/s2/favicons?domain=' + H(j.logo_domain) +
          '&sz=64" alt="" loading="lazy"></div>' +
        scoreCell(j) +
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

  // ---- feed grouping (mirror of the "feed grouping" block in web.py) ----
  // Mirror of web.py _group_key(): "" means never group this row.
  function groupKey(j) {
    var t = (j.title || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
    var c = (j.company || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
    if (!t || !c) return "";
    return c + "|" + t;
  }
  // Mirror of web.py _pick_leaders(): best row, then the best one in a different state.
  function pickLeaders(mem, lead) {
    if (mem.length <= lead) return mem.slice();
    var picked = [], seen = Object.create(null), i, s;
    for (i = 0; i < mem.length; i++) {
      s = (mem[i].loc_state || "").toUpperCase();
      if (!seen[s]) { seen[s] = 1; picked.push(i); if (picked.length === lead) break; }
    }
    for (i = 0; i < mem.length && picked.length < lead; i++)
      if (picked.indexOf(i) === -1) picked.push(i);
    picked.sort(function (a, b) { return a - b; });
    var out = [];
    for (i = 0; i < picked.length; i++) out.push(mem[picked[i]]);
    return out;
  }
  // Mirror of web.py _group_units(). A unit is {row, key, more, rest}. `key` and `more` are set
  // on exactly one unit per collapsed group — its LAST leader, the one the tile hangs off — and
  // are empty/0 everywhere else, so "has a key" and "renders a tile" mean the same thing on both
  // sides of the wire. `rest` is the hidden rows, which the non-paged path reveals without a
  // round-trip (the paged path fetches them from /api/group instead).
  function groupUnits(list) {
    var order = [], groups = Object.create(null), i, k;
    for (i = 0; i < list.length; i++) {
      k = groupKey(list[i]);
      if (!k) { order.push({ row: list[i], key: "", more: 0, rest: [] }); continue; }
      if (!groups[k]) { groups[k] = []; order.push({ key: k }); }
      groups[k].push(list[i]);
    }
    var out = [];
    for (i = 0; i < order.length; i++) {
      if (!order[i].key) { out.push(order[i]); continue; }
      var mem = groups[order[i].key];
      var m;
      if (mem.length < GROUP_MIN) {              // too short to be noise — show every card
        for (m = 0; m < mem.length; m++) out.push({ row: mem[m], key: "", more: 0, rest: [] });
        continue;
      }
      var leaders = pickLeaders(mem, GROUP_LEAD), lset = Object.create(null);
      for (m = 0; m < leaders.length; m++) lset[leaders[m].url] = 1;
      var rest = [];
      for (m = 0; m < mem.length; m++) if (!lset[mem[m].url]) rest.push(mem[m]);
      for (m = 0; m < leaders.length; m++) {
        var last = m === leaders.length - 1;
        out.push({ row: leaders[m], key: last ? order[i].key : "", rest: rest,
                   more: last ? rest.length : 0 });
      }
    }
    return out;
  }
  function flatUnits(list) {
    var out = [];
    for (var i = 0; i < list.length; i++) out.push({ row: list[i], key: "", more: 0, rest: [] });
    return out;
  }
  // Mirror of web.py _grouping_on(): Recommended only — never collapse the user's own shortlists.
  function groupingOn() { return GROUP_LEAD > 0 && tab !== "liked" && tab !== "applied" && tab !== "hidden"; }

  // The "+N more at <company>" tile. Sits in the grid right after its group's leader cards and
  // carries its own state: data-more = how many rows it stands for, data-shown = how many of
  // those are currently revealed below it.
  function groupTileHTML(row, key, more, shownN) {
    var left = more - (shownN || 0);
    var label = left > 0 ? "+" + left + " more at " + row.company : "Show less";
    return '<div class="grpmore" data-gk="' + H(key) + '" data-more="' + more +
        '" data-shown="' + (shownN || 0) + '" data-company="' + H(row.company) + '">' +
      '<div class="grpsub">' + esc(row.title) + '</div>' +
      '<button type="button" class="grpbtn">' + esc(label) + '</button>' +
      '<div class="grpnote">' + esc(row.company + " lists this role " + (more + GROUP_LEAD) +
        " times in your current results — collapsed so one employer can't fill the feed.") + '</div>' +
    '</div>';
  }
  function setGroupedNote(jobs, cards) {
    if (!groupedEl) return;
    groupedEl.textContent = (cards && cards < jobs) ? " · grouped into " + cards + " cards" : "";
  }

  function dateCutoff() {
    if (!dateSel || dateSel.value === "any") return "";
    var d = new Date(); d.setDate(d.getDate() - parseInt(dateSel.value, 10)); return d.toISOString().slice(0, 10);
  }
  var HOURS_PER_YEAR = 2080;      // keep in step with web.py _HOURS_PER_YEAR
  function annualize(amount, period) {
    var n = parseInt(amount, 10) || 0;
    return period === "hour" ? n * HOURS_PER_YEAR : n;
  }
  // Mirror of web.py _loc_hit(): metro, state code, or the raw string.
  function locHit(j, needle) {
    if (!needle) return true;
    if (needle === "remote") return !!j.remote;
    if (needle.length === 2) return needle.toUpperCase() === (j.loc_state || "").toUpperCase();
    return ((j.loc_metro || "") + " " + (j.loc_state || "") + " " + (j.location || ""))
      .toLowerCase().indexOf(needle) !== -1;
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
    // Search covers LOCATION too — "boston" and "remote" are things people type in here.
    if (ok && searching)
      ok = ((j.title || "") + " " + (j.company || "") + " " + (j.location || ""))
        .toLowerCase().indexOf(q.value.toLowerCase().trim()) !== -1;
    if (ok && cut) { var dt = j.date || ""; if (dt && dt < cut) ok = false; }
    if (ok && hideNo && hideNo.checked && j.sponsor_jd === "blocked") ok = false;
    if (ok && everifyOnly && everifyOnly.checked && !j.everify) ok = false;
    if (ok && locInp && locInp.value.trim()) ok = locHit(j, locInp.value.trim().toLowerCase());
    if (ok && remoteOnly && remoteOnly.checked && !j.remote) ok = false;
    if (ok && minSalSel && minSalSel.value) {
      // Requires a STATED range — mirrors web.py _filter_rows(). Only ~a third of
      // descriptions state pay, so this narrows the list a lot; the tooltip says so.
      var want = parseInt(minSalSel.value, 10) || 0;
      if (!j.salary_min || annualize(j.salary_min, j.salary_period) < want) ok = false;
    }
    if (ok && hideAgency && hideAgency.checked && j.agency) ok = false;
    // Closed rows stay visible in Saved/Applied so tracker history never breaks.
    if (ok && j.closed && !(showClosed && showClosed.checked) &&
        tab !== "liked" && tab !== "applied") ok = false;
    if (ok && internSel && internSel.value === "only" && !j.intern) ok = false;
    if (ok && internSel && internSel.value === "no" && j.intern) ok = false;
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
    if (reset) { limit = PAGE; expanded = Object.create(null); }
    var cut = dateCutoff(), matched = [];
    for (var i = 0; i < DATA.length; i++) if (matches(DATA[i], cut)) matched.push(DATA[i]);
    matched.sort(function (a, b) {
      if (sortBy === "newest") return (b.date || "").localeCompare(a.date || "");
      return (b.score || 0) - (a.score || 0);
    });
    // `limit` paginates DISPLAY UNITS, so a collapsed group costs one slot rather than 431.
    // Rows revealed by an open tile are extra: they're inside their group, not on the page's tail.
    var units = groupingOn() ? groupUnits(matched) : flatUnits(matched);
    var slice = units.slice(0, limit), html = "";
    for (var k = 0; k < slice.length; k++) {
      var u = slice[k];
      html += cardHTML(u.row);
      if (!u.more) continue;
      var xn = Math.min(expanded[u.key] || 0, u.rest.length);
      for (var x = 0; x < xn; x++) html += cardHTML(u.rest[x], u.key);
      html += groupTileHTML(u.row, u.key, u.more, xn);
    }
    feed.innerHTML = html;
    formatDates(); wireLogos();
    if (countEl) countEl.textContent = matched.length;          // JOBS matched, not cards drawn
    setGroupedNote(matched.length, units.length);
    if (emptyEl) emptyEl.style.display = matched.length ? "none" : "";
    if (moreBtn) {
      moreBtn.style.display = (units.length > limit) ? "" : "none";
      if (units.length > limit) moreBtn.textContent = "Load more (" + (units.length - limit) + " more)";
    }
  }

  // Highlight any control set to a non-default value (so active filters are obvious at a glance).
  function setFlag(el, on) { if (el) el.classList.toggle("fset", !!on); }
  function markFilters() {
    setFlag(sortSel, sortSel && sortSel.value !== "score");
    setFlag(dateSel, dateSel && dateSel.value !== "any");
    setFlag(expSel, expSel && expSel.value !== "any");
    setFlag(internSel, internSel && internSel.value !== "any");
    setFlag(everifyOnly && everifyOnly.closest(".ck"), everifyOnly && everifyOnly.checked);
    setFlag(hideNo && hideNo.closest(".ck"), hideNo && hideNo.checked);
  }

  // Dispatcher: small corpus renders locally from the inline DATA (instant); large corpus
  // (data-paged) fetches each page from /api/feed so the payload stays small at any scale.
  function render(reset) { markFilters(); if (PAGED) renderServer(reset); else renderLocal(reset); }

  // Every filter control as query params, with NO paging — /api/group reuses this so an
  // expansion is scoped to exactly the results the tile was summarising.
  function filterParams() {
    var ps = ["tab=" + encodeURIComponent(tab), "min=" + (minVal || 0),
              "sort=" + encodeURIComponent(sortBy)];
    if (q && q.value.trim()) ps.push("q=" + encodeURIComponent(q.value.trim()));
    if (dateSel && dateSel.value !== "any") ps.push("date=" + encodeURIComponent(dateSel.value));
    if (expSel && expSel.value !== "any") ps.push("exp=" + encodeURIComponent(expSel.value));
    if (everifyOnly && everifyOnly.checked) ps.push("everify=1");
    if (hideNo && hideNo.checked) ps.push("hidenospon=1");
    if (internSel && internSel.value !== "any") ps.push("intern=" + encodeURIComponent(internSel.value));
    if (locInp && locInp.value.trim()) ps.push("loc=" + encodeURIComponent(locInp.value.trim()));
    if (remoteOnly && remoteOnly.checked) ps.push("remote=1");
    if (minSalSel && minSalSel.value) ps.push("minsal=" + encodeURIComponent(minSalSel.value));
    if (hideAgency && hideAgency.checked) ps.push("hideagency=1");
    if (showClosed && showClosed.checked) ps.push("showclosed=1");
    return ps.join("&");
  }
  function buildParams(offset) {
    return filterParams() + "&offset=" + offset + "&limit=" + PAGE;
  }
  function renderServer(reset) {
    if (reset) { shown = 0; expanded = Object.create(null); feed.innerHTML = '<div class="loading-jd" style="padding:28px"><span class="spin"></span>Loading…</div>'; }
    var mySeq = ++_seq;                                   // ignore out-of-order responses
    fetch("/api/feed?" + buildParams(reset ? 0 : shown)).then(function (r) { return r.json(); }).then(function (d) {
      if (mySeq !== _seq) return;
      var rows = (d && d.rows) || [], htmlc = "";
      for (var i = 0; i < rows.length; i++) byUrl[rows[i].url] = rows[i];
      for (var k = 0; k < rows.length; k++) {
        htmlc += cardHTML(rows[k]);
        if (rows[k].group_more)
          htmlc += groupTileHTML(rows[k], rows[k].group_key, rows[k].group_more, 0);
      }
      if (reset) feed.innerHTML = htmlc; else feed.insertAdjacentHTML("beforeend", htmlc);
      // `rows` is a page of DISPLAY UNITS, so `shown` counts units and lines up with `d.units`.
      shown += rows.length;
      formatDates(); wireLogos();
      var jobs = (d && d.total) || 0, units = (d && d.units) || 0;
      if (countEl) countEl.textContent = jobs;             // JOBS matched, not cards drawn
      setGroupedNote(jobs, units);
      if (emptyEl) emptyEl.style.display = jobs ? "none" : "";
      if (moreBtn) { var more = !!(d && d.has_more); moreBtn.style.display = more ? "" : "none"; if (more) moreBtn.textContent = "Load more (" + (units - shown) + " more)"; }
    }).catch(function () { if (mySeq === _seq && reset) feed.innerHTML = '<div class="empty">Couldn\'t load jobs — try again.</div>'; });
  }

  // ---- expanding / collapsing a "+N more at <company>" tile ----
  // Non-paged: record how many rows are open and re-render (every row is already local).
  // Paged: fetch the group's next slice from /api/group and splice the cards in just above the
  // tile, so the rest of the feed and its paging are untouched.
  function expandGroup(tile) {
    var gk = tile.getAttribute("data-gk"), have = parseInt(tile.getAttribute("data-shown"), 10) || 0;
    var total = parseInt(tile.getAttribute("data-more"), 10) || 0;
    if (!PAGED) { expanded[gk] = have + PAGE; renderLocal(false); return; }
    var btn = tile.querySelector(".grpbtn");
    if (btn) { btn.disabled = true; btn.textContent = "Loading…"; }
    fetch("/api/group?gk=" + encodeURIComponent(gk) + "&offset=" + have + "&limit=" + PAGE +
          "&" + filterParams())
      .then(function (r) { return r.json(); }).then(function (d) {
        // The user may have changed a filter mid-flight, which re-rendered the feed and threw
        // this tile away. Inserting relative to a detached node throws, so just drop the page.
        if (!tile.parentNode) return;
        var rows = (d && d.rows) || [], html = "";
        for (var i = 0; i < rows.length; i++) { byUrl[rows[i].url] = rows[i]; html += cardHTML(rows[i], gk); }
        tile.insertAdjacentHTML("beforebegin", html);
        formatDates(); wireLogos();
        setTileState(tile, have + rows.length, total);
        if (btn) btn.disabled = false;
      }).catch(function () {
        if (!tile.parentNode) return;
        if (btn) { btn.disabled = false; }
        setTileState(tile, have, total);
        toast("Couldn't load the rest — try again.");
      });
  }
  function collapseGroup(tile) {
    var gk = tile.getAttribute("data-gk"), total = parseInt(tile.getAttribute("data-more"), 10) || 0;
    if (!PAGED) { delete expanded[gk]; renderLocal(false); return; }
    var xs = feed.querySelectorAll(".card[data-xg]");
    for (var i = 0; i < xs.length; i++)
      if (xs[i].getAttribute("data-xg") === gk && xs[i].parentNode) xs[i].parentNode.removeChild(xs[i]);
    setTileState(tile, 0, total);
  }
  // The tile's label IS its state: "+N more" while rows remain, "Show less" once they're all out.
  function setTileState(tile, shownN, total) {
    tile.setAttribute("data-shown", shownN);
    var btn = tile.querySelector(".grpbtn");
    if (!btn) return;
    var left = total - shownN;
    btn.textContent = left > 0 ? "+" + left + " more at " + (tile.getAttribute("data-company") || "this employer")
                               : "Show less";
  }
  function debouncedRender() { if (_deb) clearTimeout(_deb); _deb = setTimeout(function () { render(true); }, 250); }
  function cardEl(url) { var cs = feed.querySelectorAll(".card"); for (var i = 0; i < cs.length; i++) if (cs[i].getAttribute("data-url") === url) return cs[i]; return null; }
  // After an action: small corpus re-renders locally; paged updates just the touched card in place
  // (or drops it if it no longer matches the current tab/filters) — no full refetch.
  function afterAction(j) {
    if (!PAGED) { render(false); return; }
    var el = cardEl(j.url);
    // Keep the data-xg marker: a re-rendered card that came from an expanded group must still be
    // one, or "Show less" would leave it stranded in the grid.
    if (matches(j, dateCutoff())) { if (el) el.outerHTML = cardHTML(j, el.getAttribute("data-xg")); }
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
  if (internSel) internSel.addEventListener("change", function () { render(true); });
  if (everifyOnly) everifyOnly.addEventListener("change", function () { render(true); });
  if (hideNo) hideNo.addEventListener("change", function () { render(true); });
  // Location is free text, so debounce it like the search box rather than firing per keystroke.
  if (locInp) locInp.addEventListener("input", function () { if (PAGED) debouncedRender(); else render(true); });
  if (remoteOnly) remoteOnly.addEventListener("change", function () { render(true); });
  if (minSalSel) minSalSel.addEventListener("change", function () { render(true); });
  if (hideAgency) hideAgency.addEventListener("change", function () { render(true); });
  if (showClosed) showClosed.addEventListener("change", function () { render(true); });
  if (moreBtn) moreBtn.addEventListener("click", function () { limit += PAGE; render(false); });

  // feed clicks: group tile, action buttons, Apply auto-log, or open modal
  feed.addEventListener("click", function (e) {
    // Checked first: the tile is not a .card, so it must not fall through to the detail modal.
    var gb = e.target.closest ? e.target.closest(".grpbtn") : null;
    if (gb) {
      e.preventDefault();
      var tile = gb.closest(".grpmore"); if (!tile) return;
      var have = parseInt(tile.getAttribute("data-shown"), 10) || 0;
      var tot = parseInt(tile.getAttribute("data-more"), 10) || 0;
      if (have >= tot) collapseGroup(tile); else expandGroup(tile);
      return;
    }
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
      $("m-chip").innerHTML = scoreCell(j);
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
      if (j.agency) spn += '<span class="agency" title="Staffing agency / consultancy — not a direct employer">Agency</span>';
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

  // ---- Update-jobs: trigger the scrape + live progress bar (polls /api/scrape_status) ----
  var sBar = document.getElementById("scrapebar"), sLabel = document.getElementById("sb-label"),
      sMeta = document.getElementById("sb-meta"), sFill = document.getElementById("sb-fill"),
      sForm = document.getElementById("scrapeform"), sBtn = document.getElementById("updatejobs");
  var sPoll = null, sPollStart = 0;
  function fmtClock(sec) { sec = Math.max(0, Math.round(sec || 0)); var m = Math.floor(sec / 60); return m + ":" + ("0" + (sec % 60)).slice(-2); }
  function indet(on) { if (sFill) { if (on) sFill.classList.add("indet"); else sFill.classList.remove("indet"); } }
  function elapsedOf(st) {
    var startMs = st && st.started_at ? Date.parse(st.started_at) : NaN;
    if (isNaN(startMs)) return null;
    var s = (Date.now() - startMs) / 1000;
    return (s < 0 || s > 86400) ? null : s;          // ignore clock-skew / bad timestamps
  }
  function renderScrape(st) {
    if (!sBar || !st || !st.phase) return false;
    sBar.style.display = "";
    var ph = st.phase, done = st.done || 0, total = st.total || 0, found = st.found || 0;
    var pct = total > 0 ? Math.min(100, Math.round(done / total * 100)) : null;
    var elapsed = elapsedOf(st), el = elapsed != null ? fmtClock(elapsed) + " elapsed" : "";
    if (ph === "queued") { indet(true); sLabel.textContent = "Starting the scrape on GitHub…"; sMeta.textContent = el; }
    else if (ph === "scraping") {
      indet(false); sFill.style.width = (pct != null ? pct : 5) + "%";
      sLabel.textContent = "Scraping job boards" + (pct != null ? " — " + pct + "%" : "…");
      var eta = (pct && elapsed && done > 0) ? " · ~" + fmtClock(elapsed * (total - done) / done) + " left" : "";
      sMeta.textContent = (total ? done + "/" + total + " boards · " : "") + found + " jobs" + (el ? " · " + el : "") + eta;
    }
    else if (ph === "saving") { indet(true); sLabel.textContent = "Saving " + found + " postings…"; sMeta.textContent = el; }
    else if (ph === "scoring") { indet(true); sLabel.textContent = "Scoring jobs to your profile…"; sMeta.textContent = (st.new ? st.new + " new · " : "") + el; }
    else if (ph === "done") { indet(false); sFill.style.width = "100%"; sLabel.textContent = "Done — " + (st.new || 0) + " new job" + ((st.new || 0) === 1 ? "" : "s") + " added."; sMeta.textContent = "Refreshing…"; return "done"; }
    return true;
  }
  function refreshFeedAfterScrape() {
    fetch("/reload", { cache: "no-store" }).then(function () {
      if (PAGED) { shown = 0; render(true); } else { location.reload(); }
    }).catch(function () { location.reload(); });
  }
  function pollScrape() {
    fetch("/api/scrape_status", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (st) {
      var state = renderScrape(st);
      var updMs = st && st.updated_at ? Date.parse(st.updated_at) : NaN;
      var stale = !isNaN(updMs) && (Date.now() - updMs > 180000);
      if (state === "done" || stale || (Date.now() - sPollStart > 45 * 60 * 1000)) {
        if (sPoll) { clearInterval(sPoll); sPoll = null; }
        if (sBtn) sBtn.disabled = false;
        if (state === "done") { setTimeout(function () { refreshFeedAfterScrape(); setTimeout(function () { if (sBar) sBar.style.display = "none"; }, 4000); }, 1000); }
        else if (stale && sBar) { if (sLabel) sLabel.textContent = "Scrape finished (or stopped)."; if (sMeta) sMeta.textContent = "Hit Reload if new jobs don't appear."; setTimeout(function () { sBar.style.display = "none"; }, 6000); }
      }
    }).catch(function () {});
  }
  function startScrapePolling() { if (sPoll) return; sPollStart = Date.now(); pollScrape(); sPoll = setInterval(pollScrape, 4000); }
  if (sForm) sForm.addEventListener("submit", function (e) {
    e.preventDefault();
    if (sBtn) sBtn.disabled = true;
    if (sBar) { sBar.style.display = ""; indet(true); if (sLabel) sLabel.textContent = "Starting…"; if (sMeta) sMeta.textContent = ""; }
    fetch("/scrape", { method: "POST", headers: { "X-Requested-With": "fetch" } }).then(function (r) { return r.json(); }).then(function (j) {
      if (!j || !j.ok) { toast((j && j.msg) || "Couldn't start the scrape."); if (sBtn) sBtn.disabled = false; if (sBar) sBar.style.display = "none"; return; }
      toast("Scrape started on GitHub Actions."); startScrapePolling();
    }).catch(function () { toast("Couldn't start the scrape."); if (sBtn) sBtn.disabled = false; if (sBar) sBar.style.display = "none"; });
  });
  // If a scrape is already running (daily cron, or started in another tab), show the bar on load.
  if (sBar) fetch("/api/scrape_status", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (st) {
    var updMs = st && st.updated_at ? Date.parse(st.updated_at) : NaN;
    if (st && st.phase && st.phase !== "done" && !isNaN(updMs) && (Date.now() - updMs < 180000)) { renderScrape(st); startScrapePolling(); }
  }).catch(function () {});

  render(true);
})();
