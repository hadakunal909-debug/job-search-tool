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

  // Visa routes. Written as strict JSON literals (double quotes, no trailing commas) because
  // scripts/feed_parity.py lifts them out of this file and json.loads them, then asserts they
  // equal core.VISA_TAGS / core.VISA_TAG_LABELS — otherwise the card, the email digest and the
  // server filter could drift apart silently.
  var VISA_TAGS = ["h1b", "green_card", "stem_opt", "e3", "h1b1"];
  var VISA_LABELS = {"h1b": "H-1B", "green_card": "Green Card", "stem_opt": "STEM-OPT",
                     "e3": "E-3", "h1b1": "H-1B1"};
  var VISA_TIPS = {
    "h1b": "Has certified H-1B labor condition applications. Past filings, not a promise.",
    "green_card": "Has certified PERM applications — sponsors permanent residency, not just temporary visas.",
    "stem_opt": "Enrolled E-Verify employer, required for the STEM-OPT 24-month extension. Confirm at e-verify.gov.",
    "e3": "Has filed E-3 applications (Australian nationals).",
    "h1b1": "Has filed H-1B1 applications (Chile / Singapore nationals)."
  };

  var q = document.getElementById("q"), minR = document.getElementById("min"),
      minLab = document.getElementById("minlab"), sortSel = document.getElementById("sort"),
      dateSel = document.getElementById("date"), countEl = document.getElementById("count"),
      emptyEl = document.getElementById("empty"), moreBtn = document.getElementById("loadmore"),
      toasts = document.getElementById("toasts"), hideNo = document.getElementById("hidenospon"),
      expSel = document.getElementById("exp"),
      // Visa-route filter. Same hidden-input trick as trackSel below: the five checkboxes only
      // write into this, and every filter path reads its `.value`, so the parity harness can
      // stub one control instead of a NodeList.
      visaSel = document.getElementById("visatags"),
      internSel = document.getElementById("intern"),
      // Career track ("any" | "dev" | "mgmt"). A hidden input, not the buttons: every filter
      // path reads `.value` the same way it reads the selects, and the parity harness can stub
      // it with the same ctl() shim. The segmented buttons only write to it.
      trackSel = document.getElementById("track"),
      trackSeg = document.getElementById("trackseg"),
      fmoreBtn = document.getElementById("fmore"), rail = document.getElementById("filterrail"),
      // Two badges: one in the rail head, one on the collapsed strip. Only one is ever visible,
      // and markFilters writes to both, so a folded rail still shows that filters are on.
      fbadges = document.querySelectorAll(".fbadge"),
      feedLayout = document.getElementById("feedlayout"),
      railCollapseBtn = document.getElementById("railcollapse"),
      railExpandBtn = document.getElementById("railexpand"),
      locInp = document.getElementById("loc"), remoteOnly = document.getElementById("remoteonly"),
      minSalSel = document.getElementById("minsal"), hideAgency = document.getElementById("hideagency"),
      showClosed = document.getElementById("showclosed"), groupedEl = document.getElementById("grouped"),
      tabBtns = document.querySelectorAll(".tab");
  // The viewer's own work-authorization situation, so the E-Verify / cap-exempt badges can say
  // what they mean FOR THEM rather than reciting a general rule. Absent = generic wording.
  var VISA = {};
  try { VISA = JSON.parse(feed.getAttribute("data-visa") || "{}") || {}; } catch (e) { VISA = {}; }
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
  function toast(msg, undoFn) {
    if (!toasts) return;
    var t = document.createElement("div"); t.className = "toast";
    t.appendChild(document.createTextNode(msg));
    var timer;
    if (undoFn) {
      // Undo inside the toast rather than a confirm dialog: hiding a job should cost one
      // click and be reversible for a few seconds, not interrupt the scan with a modal.
      var u = document.createElement("button");
      u.className = "undo"; u.type = "button"; u.textContent = "Undo";
      u.addEventListener("click", function () { clearTimeout(timer); t.remove(); undoFn(); });
      t.appendChild(u);
    }
    toasts.appendChild(t);
    timer = setTimeout(function () {
      t.style.transition = "opacity .3s"; t.style.opacity = "0";
      setTimeout(function () { t.remove(); }, 300);
    }, undoFn ? 6000 : 2300);   // longer window when there's something to undo
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
        var rel = relTime(s);
        if (ps[i].getAttribute("data-added")) {
          // "Added today" reads better than "Added Today"; older values are already lowercase.
          ps[i].textContent = "Added " + (rel === "Today" || rel === "Yesterday" ? rel.toLowerCase() : rel);
          ps[i].title = "This employer publishes no posting date. First seen in your feed on " + s + ".";
        } else {
          ps[i].textContent = rel;
          ps[i].title = (ps[i].getAttribute("data-verified") ? "Verified posting date · " : "Posted ") + s;
        }
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
    // Deliberately j.date only, NOT rowDate(): "New" means newly POSTED, and for a job with no
    // publisher date we don't know that. Firing on first_seen would also paint hundreds of badges
    // at once every time the extension re-imports a board like Tesla wholesale.
    var newFlag = (relTime(j.date) === "Today") ? '<span class="newflag">New</span>' : '';
    var badges = "";
    if (j.intern)
      badges += '<span class="intl" title="Internship / co-op — OPT &amp; STEM-OPT eligible">Internship</span>';
    // Visa routes this employer has actually filed for (DOL LCA + PERM + E-Verify). Capped at
    // three chips: a big sponsor carries all five, and with Internship / No-lottery / Agency /
    // experience / pay / Remote alongside them one card could otherwise show a dozen.
    var vt = (j.visa || []);
    for (var vi = 0; vi < vt.length && vi < 3; vi++) {
      var vk = vt[vi], vtip = VISA_TIPS[vk] || "";
      if (vk === "h1b" && j.strength)
        vtip += " ~" + (j.strength_n || 0) + " USCIS approvals FY2019-23.";
      badges += '<span class="vt vt-' + H(vk) + '" title="' + H(vtip) + '">' +
        esc(VISA_LABELS[vk] || vk) + (vk === "h1b" && j.strength === "high" ? " ★" : "") + '</span>';
    }
    if (vt.length > 3)
      badges += '<span class="vt vt-more" title="' + H(vt.map(function (k) {
        return VISA_LABELS[k] || k; }).join(", ")) + '">+' + (vt.length - 3) + '</span>';
    // Fallback for when visa_tags.json hasn't been built: the old name-list H-1B flag.
    if (!vt.length && j.sponsors_h1b === "yes") {
      var h1bTip = "Company has sponsored H-1B before";
      if (j.strength) h1bTip += " · ~" + (j.strength_n || 0) + " approvals FY2019-23";
      h1bTip += ". USCIS H-1B Data Hub; past filings, not a promise.";
      badges += '<span class="h1b" title="' + H(h1bTip) + '">H1B' +
        (j.strength === 'high' ? ' (top sponsor)' : '') + '</span>';
    }
    if (j.cap_exempt)
      badges += '<span class="cx" title="Likely H-1B cap-exempt (university / nonprofit hospital / research) — no H-1B lottery' +
        (VISA.needsLottery ? ", so this route does not depend on the March registration you're waiting on" : "") +
        '. Verify.">No lottery</span>';
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
    // Some employers publish no posting date anywhere, so the card falls back to when the job
    // reached us. It carries data-added so formatDates() labels it "Added …" and styles it
    // apart — an approximate arrival date must never read as a posting date. The text starts
    // out as "Added <iso>" so there's no flash of a bare date before formatDates() runs.
    var posted = '';
    if (j.date) {
      posted = ' · <span class="posted" data-d="' + H(j.date) + '"' +
        (j.date_verified ? ' data-verified="1"' : '') + '>' + H(j.date) + '</span>';
    } else if (j.first_seen) {
      posted = ' · <span class="posted added" data-d="' + H(j.first_seen) +
        '" data-added="1">Added ' + H(j.first_seen) + '</span>';
    }
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
    var d = new Date();
    d.setDate(d.getDate() - parseInt(dateSel.value, 10));
    // LOCAL date parts, deliberately not toISOString(): that converts to UTC, so during the
    // hours when the local and UTC dates differ the client cut one day more than web.py's
    // _date_cutoff (which uses date.today(), local) and the two filters disagreed — measured
    // as a 424-vs-410 split. Matters more now that "Past 30 days" is the default.
    var mm = d.getMonth() + 1, dd = d.getDate();
    return d.getFullYear() + "-" + (mm < 10 ? "0" : "") + mm + "-" + (dd < 10 ? "0" : "") + dd;
  }
  // Mirror of web.py _row_date(): the date a job is ordered and filtered by. Falls back to
  // first_seen so a job whose employer publishes no posting date (Tesla) is judged on when it
  // reached us rather than escaping every date filter. feed_parity.py checks this twin.
  function rowDate(j) { return j.date || j.first_seen || ""; }
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
  // Mirror of core.parse_visa_pref(): the ticked routes, canonical order, junk dropped.
  function visaWanted() {
    var raw = (visaSel && visaSel.value) || "";
    if (!raw) return [];
    var want = raw.toLowerCase().split(","), out = [];
    for (var i = 0; i < VISA_TAGS.length; i++)
      if (want.indexOf(VISA_TAGS[i]) !== -1) out.push(VISA_TAGS[i]);
    return out;
  }
  // Mirror of core.visa_tags_match(): OR, and no ticked routes means no filter.
  function visaHit(j, wanted) {
    if (!wanted || !wanted.length) return true;
    var have = j.visa || [];
    for (var i = 0; i < wanted.length; i++)
      if (have.indexOf(wanted[i]) !== -1) return true;
    return false;
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
    if (ok && cut) { var dt = rowDate(j); if (dt && dt < cut) ok = false; }
    if (ok && hideNo && hideNo.checked && j.sponsor_jd === "blocked") ok = false;
    if (ok && !visaHit(j, visaWanted())) ok = false;
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
    // Career track: "dev" (software/data/infra) vs "mgmt" (project/product/ops). Every row
    // carries exactly one, so the two settings partition the feed — see core.role_track.
    if (ok && trackSel && trackSel.value !== "any" && j.track !== trackSel.value) ok = false;
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
      if (sortBy === "newest") return rowDate(b).localeCompare(rowDate(a));
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

  // How many NARROWING filters are on — the eight under "Filters" in the rail. Search, track and
  // sort are excluded: they START a search rather than narrow one, and they stay visible at every
  // width. The count matters most below 900px, where the rail body collapses and would otherwise
  // hide active filters — the bug any disclosure invites.
  function activeFilterCount() {
    var n = 0;
    if (minVal > 0) n++;
    if (dateSel && dateSel.value !== "any") n++;
    if (expSel && expSel.value !== "any") n++;
    if (internSel && internSel.value !== "any") n++;
    if (locInp && locInp.value.trim()) n++;
    if (minSalSel && minSalSel.value) n++;
    if (visaWanted().length) n++;      // the whole visa group counts as ONE filter, not five
    if (hideNo && hideNo.checked) n++;
    if (remoteOnly && remoteOnly.checked) n++;
    if (hideAgency && hideAgency.checked) n++;
    if (showClosed && showClosed.checked) n++;
    return n;
  }
  function markFilters() {
    setFlag(sortSel, sortSel && sortSel.value !== "score");
    setFlag(dateSel, dateSel && dateSel.value !== "any");
    setFlag(expSel, expSel && expSel.value !== "any");
    setFlag(internSel, internSel && internSel.value !== "any");
    var vbox = document.querySelectorAll(".visack input[data-vt]");
    for (var vb = 0; vb < vbox.length; vb++)
      setFlag(vbox[vb].closest(".ck"), vbox[vb].checked);
    setFlag(hideNo && hideNo.closest(".ck"), hideNo && hideNo.checked);
    setFlag(remoteOnly && remoteOnly.closest(".ck"), remoteOnly && remoteOnly.checked);
    setFlag(minSalSel, minSalSel && minSalSel.value);
    setFlag(locInp, locInp && locInp.value.trim());
    var n = activeFilterCount();
    for (var b = 0; b < fbadges.length; b++) {
      fbadges[b].textContent = n;
      fbadges[b].hidden = !n;
    }
    if (fmoreBtn) fmoreBtn.classList.toggle("fset", !!n);
    // Segmented buttons follow the hidden input, so prefs-seeded state and clicks agree.
    if (trackSeg && trackSel) {
      var bs = trackSeg.querySelectorAll(".segb");
      for (var i = 0; i < bs.length; i++) {
        var on = bs[i].getAttribute("data-track") === trackSel.value;
        bs[i].classList.toggle("on", on);
        bs[i].setAttribute("aria-pressed", on ? "true" : "false");
      }
    }
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
    var vw = visaWanted();
    if (vw.length) ps.push("visatags=" + encodeURIComponent(vw.join(",")));
    if (hideNo && hideNo.checked) ps.push("hidenospon=1");
    if (internSel && internSel.value !== "any") ps.push("intern=" + encodeURIComponent(internSel.value));
    if (trackSel && trackSel.value !== "any") ps.push("track=" + encodeURIComponent(trackSel.value));
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
  // Shrink + fade the card out, then hand back to the caller to re-render (which drops the
  // node and reflows the grid). Two rAFs so the browser paints the start state before the
  // class that changes it, otherwise there's nothing to transition FROM. `done` fires on a
  // timer rather than transitionend: a backgrounded tab never fires that event and the card
  // would sit half-faded forever.
  function collapseCard(card, done) {
    if (!card || !card.parentNode) { done(); return; }
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) { done(); return; }
    card.classList.add("card-collapse");
    requestAnimationFrame(function () {
      requestAnimationFrame(function () { card.classList.add("gone"); });
    });
    setTimeout(done, 240);
  }

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
  // One delegated listener over the visa group: the checkboxes are the UI, visaSel.value is
  // the state every filter path actually reads.
  function syncVisa() {
    if (!visaSel) return;
    var on = [], boxes = document.querySelectorAll(".visack input[data-vt]");
    for (var i = 0; i < VISA_TAGS.length; i++)
      for (var b = 0; b < boxes.length; b++)
        if (boxes[b].getAttribute("data-vt") === VISA_TAGS[i] && boxes[b].checked)
          on.push(VISA_TAGS[i]);
    visaSel.value = on.join(",");
  }
  var visaGroup = visaSel && visaSel.parentNode;
  if (visaGroup) visaGroup.addEventListener("change", function (e) {
    if (!e.target || !e.target.getAttribute || !e.target.getAttribute("data-vt")) return;
    syncVisa(); render(true);
  });
  if (hideNo) hideNo.addEventListener("change", function () { render(true); });
  // Location is free text, so debounce it like the search box rather than firing per keystroke.
  if (locInp) locInp.addEventListener("input", function () { if (PAGED) debouncedRender(); else render(true); });
  if (remoteOnly) remoteOnly.addEventListener("change", function () { render(true); });
  if (minSalSel) minSalSel.addEventListener("change", function () { render(true); });
  if (hideAgency) hideAgency.addEventListener("change", function () { render(true); });
  if (showClosed) showClosed.addEventListener("change", function () { render(true); });

  // ---- career track: one click swaps the whole feed between the two careers in the corpus ----
  if (trackSeg && trackSel) trackSeg.addEventListener("click", function (e) {
    var b = e.target.closest ? e.target.closest(".segb") : null;
    if (!b) return;
    var v = b.getAttribute("data-track") || "any";
    if (v === trackSel.value) return;            // already there: don't re-render for nothing
    trackSel.value = v;
    render(true);
    window.scrollTo({ top: 0, behavior: "smooth" });
  });

  // ---- "Filters" disclosure — below 900px only ----
  // Above that the rail shows its body from CSS alone, so neither a JS failure nor a stale
  // localStorage value can leave the feed with no visible filters. That was a live hazard in the
  // old toolbar: #fpanel shipped with the `hidden` ATTRIBUTE and only this function cleared it,
  // so a returning user whose jm_fpanel was "0" would have got an always-open rail that never
  // actually appeared. Below 900px .railbody is display:none until .rail carries .open, which is
  // all this toggles. Open state persists per browser so someone who works with pay + location
  // open every session doesn't re-open the rail each visit.
  var fmoreLbl = fmoreBtn ? fmoreBtn.querySelector(".railtoggle-t") : null;
  function setPanel(open) {
    if (!rail || !fmoreBtn) return;
    rail.classList.toggle("open", !!open);
    fmoreBtn.setAttribute("aria-expanded", open ? "true" : "false");
    fmoreBtn.setAttribute("aria-label", (open ? "Hide" : "Show") + " filters");
    fmoreBtn.classList.toggle("open", !!open);
    if (fmoreLbl) fmoreLbl.textContent = open ? "Hide" : "Show";
    try { localStorage.setItem("jm_fpanel", open ? "1" : "0"); } catch (err) { /* private mode */ }
  }
  if (fmoreBtn && rail) {
    var wasOpen = "0";
    try { wasOpen = localStorage.getItem("jm_fpanel") || "0"; } catch (err) { wasOpen = "0"; }
    setPanel(wasOpen === "1");
    fmoreBtn.addEventListener("click", function () {
      var opening = !rail.classList.contains("open");
      EV("filter_panel", { open: opening });
      setPanel(opening);
    });
  }

  // ---- collapse the whole rail to a strip (desktop) ----
  // Purely presentational: no filter value changes, so nothing re-renders. Persisted per browser
  // because whether you want the filters parked is a standing preference, not a per-visit one.
  // Below 900px the CSS ignores .railcollapsed entirely — there the rail is already collapsible.
  function setRail(collapsed) {
    if (!feedLayout) return;
    feedLayout.classList.toggle("railcollapsed", !!collapsed);
    if (railCollapseBtn) railCollapseBtn.setAttribute("aria-expanded", collapsed ? "false" : "true");
    try { localStorage.setItem("jm_railcollapsed", collapsed ? "1" : "0"); } catch (err) { /* private mode */ }
  }
  if (feedLayout && (railCollapseBtn || railExpandBtn)) {
    var railWas = "0";
    try { railWas = localStorage.getItem("jm_railcollapsed") || "0"; } catch (err) { railWas = "0"; }
    setRail(railWas === "1");
    if (railCollapseBtn) railCollapseBtn.addEventListener("click", function () {
      EV("rail", { open: false });
      setRail(true);
    });
    if (railExpandBtn) railExpandBtn.addEventListener("click", function () {
      EV("rail", { open: true });
      setRail(false);
      var f = document.getElementById("q");        // land focus somewhere useful on reopen
      if (f) f.focus({ preventScroll: true });
    });
  }

  // ---- "Clear" — reset every NARROWING filter, leaving search text and track alone ----
  var clearBtn = document.getElementById("clearfilters");
  if (clearBtn) clearBtn.addEventListener("click", function () {
    // Captured BEFORE the resets: how many filters were stacked when someone gave up is the
    // interesting number. A high value means people over-filter into an empty feed, which
    // argues for a "no results — loosen these?" affordance rather than more filters.
    EV("clear_filters", { n: activeFilterCount() });
    if (minR) { minR.value = 0; minVal = 0; setFill(); }
    if (locInp) locInp.value = "";
    if (minSalSel) minSalSel.value = "";
    if (dateSel) dateSel.value = "any";
    if (expSel) expSel.value = "any";
    if (internSel) internSel.value = "any";
    var vclr = document.querySelectorAll(".visack input[data-vt]");
    for (var vc = 0; vc < vclr.length; vc++) vclr[vc].checked = false;
    if (visaSel) visaSel.value = "";
    if (hideNo) hideNo.checked = false;
    if (remoteOnly) remoteOnly.checked = false;
    if (hideAgency) hideAgency.checked = false;
    if (showClosed) showClosed.checked = false;
    render(true);
  });
  // "Save as default" — remember the current toolbar as this user's search, which also
  // decides what lands in their email digest. Search text is deliberately NOT saved: it's a
  // one-off lookup, not a standing preference.
  var savePrefsBtn = document.getElementById("saveprefs");
  if (savePrefsBtn) savePrefsBtn.addEventListener("click", function () {
    var body = {
      min: minVal, loc: locInp ? locInp.value.trim() : "",
      remote: !!(remoteOnly && remoteOnly.checked),
      minsal: minSalSel ? (parseInt(minSalSel.value, 10) || 0) : 0,
      hideagency: !!(hideAgency && hideAgency.checked),
      visatags: visaWanted().join(","),
      hidenospon: !!(hideNo && hideNo.checked),
      exp: expSel ? expSel.value : "any", intern: internSel ? internSel.value : "any",
      track: trackSel ? trackSel.value : "any",
      date: dateSel ? dateSel.value : "any", sort: sortBy
    };
    savePrefsBtn.disabled = true;
    fetch("/prefs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json(); }).then(function (d) {
      savePrefsBtn.disabled = false;
      if (d && d.ok) toast(d.note ? "Saved — " + d.note : "Saved as your default search.");
      else toast("Couldn't save: " + ((d && d.error) || "unknown error"));
    }).catch(function () {
      savePrefsBtn.disabled = false;
      toast("Couldn't save your default search.");
    });
  });
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
        btn.disabled = false; if (!ok) return;
        j.status = next;
        // Hiding is the one action that removes the card from view, so it gets the collapse
        // + Undo treatment. The others just relabel in place and re-render as before.
        if (next === "hidden" && tab !== "hidden") {
          collapseCard(card, function () { afterAction(j); });
          toast("Hidden", function () {
            doAction(j.url, cur).then(function (ok2) {
              if (!ok2) { toast("Couldn't undo — try again."); return; }
              j.status = cur; render(true);
            });
          });
        } else {
          toast(actLabel(next));
          afterAction(j);
        }
      });
      return;
    }
    var lnk = e.target.closest && e.target.closest("a");
    if (lnk) {                                                    // Apply/Tailor links open normally
      if (lnk.hasAttribute("data-apply")) {                      // clicking Apply auto-logs it
        var ac = lnk.closest(".card"), aj = ac && byUrl[ac.getAttribute("data-url")];
        // Recorded on EVERY click, not only the first. doAction below fires only when the job
        // isn't already applied, so without this a second visit to the same posting is
        // invisible and outbound clicks are undercounted.
        if (aj) EV("apply_click", { co: aj.company, sc: aj.score, where: "card" });
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
      // The modal has room, so it shows every route rather than the card's top three.
      var mvt = j.visa || [];
      for (var mi = 0; mi < mvt.length; mi++)
        spn += '<span class="vt vt-' + H(mvt[mi]) + '" title="' + H(VISA_TIPS[mvt[mi]] || "") +
          '">' + esc(VISA_LABELS[mvt[mi]] || mvt[mi]) + '</span>';
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
      // where:"modal" vs "card" answers whether the detail panel is where people decide, or
      // just a reference they read past. If nobody ever applies from here, its action buttons
      // are dead weight; if nobody opens it at all, /api/job and the JD storage aren't earning.
      if (j) EV("apply_click", { co: j.company, sc: j.score, where: "modal" });
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
