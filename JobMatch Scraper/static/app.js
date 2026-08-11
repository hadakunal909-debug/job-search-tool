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
    "green_card": "Has certified PERM applications, so they sponsor permanent residency, not just temporary visas.",
    "stem_opt": "Enrolled E-Verify employer, required for the STEM-OPT 24-month extension. Confirm at e-verify.gov.",
    "e3": "Has filed E-3 applications (Australian nationals).",
    "h1b1": "Has filed H-1B1 applications (Chile / Singapore nationals)."
  };

  var q = document.getElementById("q"), minR = document.getElementById("min"),
      minLab = document.getElementById("minlab"), sortSel = document.getElementById("sort"),
      dateSel = document.getElementById("date"), countEl = document.getElementById("count"),
      emptyEl = document.getElementById("empty"), moreBtn = document.getElementById("loadmore"),
      toasts = document.getElementById("toasts"), hideNo = document.getElementById("hidenospon"),
      verifiedOnly = document.getElementById("verifiedonly"),
      // Same hidden-input trick as #visatags and #track: the checkboxes are the UI, this is the
      // state every filter path reads, so feed_parity can stub one control not a NodeList.
      rolesSel = document.getElementById("roles"),
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
      showClosed = document.getElementById("showclosed"),
      tabBtns = document.querySelectorAll(".tab");
  // The viewer's own work-authorization situation (web._visa_badge_context), which used to
  // personalize the cap-exempt badge's tooltip.
  // CURRENTLY UNREAD: that tooltip went with the rest of the card's hover prose. Kept — along
  // with the data-visa attribute and the server side that fills it — because it is the plumbing
  // any "what this route means for YOU" wording would need, and deleting it is a separate
  // decision from de-noising the cards.
  var VISA = {};
  try { VISA = JSON.parse(feed.getAttribute("data-visa") || "{}") || {}; } catch (e) { VISA = {}; }
  // ---- filter memory ------------------------------------------------------------------
  // Every nav in this app is a full page load, and the toolbar's only store was the DOM — so
  // walking to a company page and back silently threw away whatever you had set. "Save as
  // default" persisted, but that is a deliberate, cross-device baseline, not a scratchpad.
  //
  // Kept CLIENT-SIDE on purpose. Auto-saving to the server would fire save_prefs on every
  // keystroke, and that route calls _rows_cache.clear() — which is process-wide, not per-user,
  // so one person dragging the Match slider would rebuild the scored corpus for everybody.
  // localStorage is also already the pattern here (jm_fpanel, jm_railcollapsed, theme).
  //
  // Written from markFilters() rather than from the ~15 change listeners: every one of them
  // already routes through render() -> markFilters(), so there is no listener to forget.
  // PER USER, not just per browser. localStorage belongs to the machine, so an unnamespaced key
  // hands the next person to log in here the previous one's filters — surprising on a shared
  // computer, and it quietly says what they were searching for. Caught while testing a second
  // account: their fresh feed came up carrying the first account's saved sort.
  var FILTER_KEY = "jm_filters:" + (feed.getAttribute("data-user") || ""), FILTER_V = 1;
  try { localStorage.removeItem("jm_filters"); } catch (e) { /* the pre-namespace key */ }
  function _ctlMap() {
    // #q is deliberately excluded off the main feed. Both pages have one, but they mean
    // different things — "search every job" vs company.html's "filter these roles" — so sharing
    // it would leak an employer-page filter back into the feed. #filterrail exists only on
    // feed.html, which is the discriminator. #sort IS shared: that one is a global preference.
    return { q: (rail ? q : null), min: minR, sort: sortSel, date: dateSel, exp: expSel,
             intern: internSel, minsal: minSalSel, loc: locInp, visatags: visaSel,
             track: trackSel, roles: rolesSel, hidenospon: hideNo, verifiedonly: verifiedOnly,
             remoteonly: remoteOnly, hideagency: hideAgency, showclosed: showClosed };
  }
  function _readStore() {
    // A stored blob from an older shape is discarded whole rather than half-applied — see the
    // jm_fpanel hazard note further down, where a stale value once hid the entire rail.
    try {
      var o = JSON.parse(localStorage.getItem(FILTER_KEY) || "null");
      return (o && typeof o === "object" && o.v === FILTER_V) ? o : {};
    } catch (e) { return {}; }
  }
  function saveFilterState() {
    // MERGE over what is stored, and only for controls that exist on THIS page. company.html
    // renders just #q and #sort; a wholesale overwrite from there would wipe the feed's other
    // twelve filters.
    var s = _readStore(), m = _ctlMap(), k;
    for (k in m) if (m[k]) s[k] = (m[k].type === "checkbox") ? !!m[k].checked : m[k].value;
    if (tabBtns.length) s.tab = tab;
    s.v = FILTER_V;
    try { localStorage.setItem(FILTER_KEY, JSON.stringify(s)); } catch (e) { /* private mode */ }
  }
  function applyFilterState() {
    var s = _readStore(), m = _ctlMap(), k;
    for (k in m) {
      if (!m[k] || !(k in s)) continue;
      if (m[k].type === "checkbox") m[k].checked = !!s[k];
      else m[k].value = s[k] == null ? "" : String(s[k]);
    }
    // The five visa checkboxes are the UI for the hidden #visatags input, so re-tick them to
    // match the value we just restored or the group would read as empty.
    if (visaSel && "visatags" in s) {
      var want = String(s.visatags || "").split(","),
          boxes = document.querySelectorAll(".visack input[data-vt]");
      for (var i = 0; i < boxes.length; i++)
        boxes[i].checked = want.indexOf(boxes[i].getAttribute("data-vt")) >= 0;
    }
    return s;
  }
  // Runs BEFORE sortBy/minVal/tab are read below, so those pick up the restored values rather
  // than the server-rendered ones. Cards come from feed_rows|tojson and are drawn by JS, so
  // only the controls repaint — there is no card flash.
  var SAVED = applyFilterState();
  var _savedTab = SAVED.tab && document.querySelector('.tab[data-tab="' + SAVED.tab + '"]')
                  ? SAVED.tab : "recommended";
  var tab = _savedTab, PAGE = 60, limit = PAGE, sortBy = sortSel ? sortSel.value : "score";
  var minVal = minR ? (parseInt(minR.value, 10) || 0) : 0;
  for (var _t0 = 0; _t0 < tabBtns.length; _t0++)
    tabBtns[_t0].classList.toggle("on", tabBtns[_t0].getAttribute("data-tab") === tab);
  // Large corpus: the server inlines only the top-N matches and we fetch the rest (search/filter/
  // paging) from /api/feed, so the payload stays small at any scale. Small corpus: data-paged is
  // empty and everything stays client-side (instant) exactly as before.
  var PAGED = feed.getAttribute("data-paged") === "1";
  var HAS_RESUME = feed.getAttribute("data-hasresume") === "1";

  // Without a résumé every score is the same flat baseline, so "Best match" sorts on noise
  // while looking authoritative. user_scores already suppresses the number on the card; this
  // stops the ordering pretending too. Newest is the honest default when nothing can be ranked.
  if (!HAS_RESUME && sortSel) {
    var so = sortSel.querySelector('option[value="score"]');
    if (so) {
      so.disabled = true;
      so.textContent = "Sort: Best match (add a résumé)";
    }
    // sortBy was captured from the select a few lines above, so the variable has to move with
    // it or the control would read "Newest" while the feed kept sorting on score.
    if (sortSel.value === "score") { sortSel.value = "newest"; sortBy = "newest"; }
  }

  var shown = 0, _seq = 0, _deb;
  // Set on the per-company page: every /api/feed request is pinned to that one employer,
  // server-side and before the filters run. Empty string on the main feed.
  var COMPANY = feed.getAttribute("data-company") || "";

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
    // No résumé, no score. This used to fall through to the stored match_score, which is
    // computed against the SCRAPER's resume.txt — so a new account saw a feed of confident
    // 62-64% rings derived from somebody else's CV, drawn identically to a real match. A
    // number that looks personalised and isn't is worse than no number.
    if (!HAS_RESUME)
      return '<span class="score-none" title="Add your résumé to see how well each job matches you. Until then there is nothing to compare against.">·</span>';
    if (j && j.score_pending)
      return '<span class="score-pending" title="This description is too short to score reliably yet. It\'ll get a match score once the full job description is fetched.">JD pending</span>';
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
  // `root` defaults to the card grid; the detail panel passes its own header so the same
  // relative-date rendering (and the same tooltips) apply there too.
  function formatDates(root) {
    var ps = (root || feed).querySelectorAll(".posted");
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
  // Company logos, fetched from gstatic DIRECTLY rather than through
  // www.google.com/s2/favicons, which 301-redirects here anyway.
  //
  // Measured 2026-08-09 against the live service. The redirect costs a second TLS connection
  // and a second round trip (0.140s vs 0.074s), but the real problem is caching: the 301
  // carries max-age=1800, so every logo was re-requested every 30 minutes, while the image it
  // points at carries max-age=604800. A feed page fires one of these per card, which is why a
  // capture showed a dozen 301s at ~500ms each returning 0.0 kB.
  //
  // The shard number in the Location header varies per domain (t1, t2, t3), but any shard
  // serves any domain: t0 and t1 both returned the identical 658-byte PNG for amazon.com. One
  // host is also better than four over HTTP/2, which multiplexes on a single connection.
  //
  // Mirrored in templates/company.html for the employer page. Keep the two in step.
  var LOGO_BASE = "https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&size=64&url=http://";

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

  // Company name -> a link to that employer's page. Plain text when we're already ON that page
  // (a link to here is just a dead end) or when the row has no company.
  //
  // Escaping is not weakened by the anchor: encodeURIComponent runs FIRST, so it has already
  // percent-encoded &, ", ', < and > before H() ever sees the value, and esc() still owns the
  // visible label. No click handler is needed either — the feed's delegated handler bails on any
  // <a> before it reaches openModal, so this navigates instead of opening the job panel. Don't
  // "fix" that with a stopPropagation: the same branch is what auto-logs an Apply click.
  function companyLink(name) {
    if (!name) return esc("Unknown company");
    if (COMPANY) return esc(name);
    return '<a href="/company?c=' + H(encodeURIComponent(name)) + '">' + esc(name) + '</a>';
  }

  // Build one card's HTML from its data object — mirrors the old Jinja <article> exactly.
  function cardHTML(j) {
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
      badges += '<span class="intl" title="OPT &amp; STEM-OPT eligible">Internship</span>';
    // Visa routes this employer has actually filed for (DOL LCA + PERM + E-Verify). Capped at
    // three chips: a big sponsor carries all five, and with Internship / No-lottery / Agency /
    // experience / pay / Remote alongside them one card could otherwise show a dozen.
    //
    // NO tooltips on these. "H-1B" and "Green Card" say what they are, and a paragraph of
    // caveats on every chip of every card was a wall of hover text. The caveats didn't go away:
    // the detail panel still shows every route WITH its VISA_TIPS sentence, which is the right
    // home for them — one view, read once, instead of forty times down a feed.
    // Ordered so the routes THIS user is filtering on come first. A big sponsor carries all
    // five and only three fit, so an unranked list can push the one route that matters to the
    // reader into the "+2". This is the payoff for asking the sponsorship question at all:
    // the chips answer "can I take this job" rather than "what has this employer ever filed".
    var vt = rankVisa(j.visa || []);
    for (var vi = 0; vi < vt.length && vi < 3; vi++) {
      var vk = vt[vi];
      badges += '<span class="vt vt-' + H(vk) + '">' + esc(VISA_LABELS[vk] || vk) +
        (vk === "h1b" && j.strength === "high" ? " · top sponsor" : "") + '</span>';
    }
    // This one KEEPS its tooltip: the chip reads "+2", so it is the only place the remaining
    // routes are named. That's data, not prose.
    if (vt.length > 3)
      badges += '<span class="vt vt-more" title="' + H(vt.map(function (k) {
        return VISA_LABELS[k] || k; }).join(", ")) + '">+' + (vt.length - 3) + '</span>';
    // Fallback for when visa_tags.json hasn't been built: the old name-list H-1B flag.
    if (!vt.length && j.sponsors_h1b === "yes")
      badges += '<span class="h1b">H1B' + (j.strength === 'high' ? ' (top sponsor)' : '') + '</span>';
    if (j.cap_exempt)
      badges += '<span class="cx">No lottery</span>';
    if (j.agency)
      badges += '<span class="agency">Agency</span>';
    // No title on this one: the chip already reads "5+ yrs".
    if (j.exp_years !== "" && j.exp_years != null) {
      var ec = j.exp_level === 'senior' ? 'exp-hi' : (j.exp_level === 'mid' ? 'exp-mid' : 'exp-lo');
      badges += '<span class="exp ' + ec + '">' + H(j.exp_years) + '+ years</span>';
    }
    if (j.sponsor_jd === 'blocked')
      badges += '<span class="nospon" title="' + H(j.sponsor_reason) + '">No sponsorship</span>';
    else if (j.sponsor_jd === 'open')
      badges += '<span class="spon" title="' + H(j.sponsor_reason) + '">Sponsors</span>';
    // Pay needs no tooltip — the chip shows the range. Remote keeps the "per the posting" hedge,
    // which is the single place that caveat now lives (the rail checkbox dropped its copy).
    if (j.salary_label)
      badges += '<span class="pay">' + H(j.salary_label) + '</span>';
    if (j.remote)
      badges += '<span class="rem" title="Remote per the posting">Remote</span>';
    if (j.closed)
      badges += '<span class="closed">Closed</span>';
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
    var cls = "card" + (j.closed ? " is-closed" : "");
    return '<article class="' + cls + '"' +
      ' data-url="' + H(j.url) + '" data-status="' + H(st) + '">' + newFlag +
      '<div class="cardtop">' +
        '<div class="logo" style="background:' + H(j.logo_color) + '">' + H(j.initial) +
          '<img class="logo-img" src="' + LOGO_BASE + H(j.logo_domain) +
          '" alt="" loading="lazy"></div>' +
        scoreCell(j) +
      '</div>' +
      '<div class="ctitle">' + esc(j.title) + '</div>' +
      '<div class="cmeta">' + companyLink(j.company) + ' · ' + esc(j.location || 'n/a') + posted + badges + '</div>' +
      '<div class="cardact">' +
        '<a class="btn primary sm" href="' + H(applyHref) + '" target="_blank" rel="noopener" data-apply="1">Apply ↗</a>' +
        '<a class="btn sm" href="/brain?job=' + encodeURIComponent(j.url) + '">Tailor</a>' +
        '<span class="spacer"></span>' +
        // No title= here: each button's visible text already IS the tooltip, and this block
        // renders once per card, so the duplication was three tooltips on every row of the feed.
        '<span class="acts">' +
          '<button class="ico" data-act="liked">' + (st === 'liked' ? 'Saved' : 'Save') + '</button>' +
          '<button class="ico" data-act="applied">' + (st === 'applied' ? 'Applied' : 'Mark applied') + '</button>' +
          '<button class="ico" data-act="hidden">' + (st === 'hidden' ? 'Hidden' : 'Hide') + '</button>' +
        '</span>' +
      '</div>' +
    '</article>';
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
  // Which role families the user picked. Mirror of core.parse_roles_pref.
  function rolesWanted() {
    if (!rolesSel) return [];
    var out = [], parts = String(rolesSel.value || "").split(",");
    for (var i = 0; i < parts.length; i++) if (parts[i]) out.push(parts[i]);
    return out;
  }
  // Mirror of core.roles_match. j.roles is computed SERVER-side by core.roles_for_title, so the
  // phrase vocabulary has one definition and this only has to intersect two lists. OR across
  // picks, like the visa filter: someone who ticks Project Manager and Data Analyst wants
  // either, not both at once.
  function roleHit(j, wanted) {
    if (!wanted.length) return true;
    var have = j.roles || [];
    for (var i = 0; i < have.length; i++)
      if (wanted.indexOf(have[i]) >= 0) return true;
    return false;
  }
  // Mirror of web.py _row_sponsor_rank(): lowest first. Ranking rather than filtering, because
  // hiding rows with no federal record would bin 41 Northrop Grumman postings and 4 at Penn
  // State (cap-exempt, the best H-1B route there is) — absence from a DOL file is "no data",
  // not "no sponsorship". j.visa is already narrowed per posting server-side.
  function sponsorRank(j) {
    var v = j.visa || [], h1b = v.indexOf("h1b") >= 0, ev = v.indexOf("stem_opt") >= 0, tier;
    if (h1b && ev) tier = 0;
    else if (h1b) tier = 1;
    else if (ev) tier = 2;
    else if (j.sponsor_jd === "blocked") tier = 4;
    else tier = 3;
    return [tier, -(j.strength_n || 0), -(j.score || 0)];
  }
  // The one comparator, so the server twin has exactly one thing to match. Lifted by name into
  // scripts/feed_parity.py (JS_FUNCS) rather than re-typed there — a hand-copied third version
  // is how these drift.
  function sortCmp(a, b, sortBy) {
    if (sortBy === "newest") return rowDate(b).localeCompare(rowDate(a));
    if (sortBy === "sponsor") {
      var ra = sponsorRank(a), rb = sponsorRank(b);
      for (var i = 0; i < ra.length; i++) if (ra[i] !== rb[i]) return ra[i] - rb[i];
      return 0;
    }
    return (b.score || 0) - (a.score || 0);
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
    // date_trusted is computed server-side by core.is_trusted_date, so this reads a flag
    // rather than re-deriving the "bare ISO means a publisher stated it" rule in JS.
    if (ok && verifiedOnly && verifiedOnly.checked && !j.date_trusted) ok = false;
    if (ok && !roleHit(j, rolesWanted())) ok = false;
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
    // Experience filter. exp_years is the HIGHEST year count the JD states (core.
    // experience_years), so "8+ years required; 2 years of SQL preferred" is an 8-year job and
    // "<=2 yrs" drops it. A job whose JD states no year count (exp_years "") is ALWAYS kept.
    // Mirrors web._filter_rows — scripts/feed_parity.py diffs the two.
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
    matched.sort(function (a, b) { return sortCmp(a, b, sortBy); });
    // One row = one card, so `limit` paginates jobs directly.
    var slice = matched.slice(0, limit), html = "";
    for (var k = 0; k < slice.length; k++) html += cardHTML(slice[k]);
    feed.innerHTML = html;
    formatDates(); wireLogos();
    setCount(matched.length);
    if (!matched.length) renderEmpty(null);
    setShown(emptyEl, !matched.length);
    if (moreBtn) {
      setShown(moreBtn, matched.length > limit);
      if (matched.length > limit) moreBtn.textContent = "Load more (" + (matched.length - limit) + " more)";
    }
  }

  // Highlight any control set to a non-default value (so active filters are obvious at a glance).
  // Routes the user has ticked in the Sponsorship filter, hoisted to the front. Stable
  // otherwise, so a card with no filter set looks exactly as it did.
  var _wantVisa = [];
  function rankVisa(vt) {
    if (!_wantVisa.length || vt.length < 2) return vt;
    var first = [], rest = [];
    for (var i = 0; i < vt.length; i++)
      (_wantVisa.indexOf(vt[i]) >= 0 ? first : rest).push(vt[i]);
    return first.concat(rest);
  }

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
    if (verifiedOnly && verifiedOnly.checked) n++;
    if (rolesWanted().length) n++;     // the whole role selection counts as ONE filter
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
    setFlag(verifiedOnly && verifiedOnly.closest(".ck"), verifiedOnly && verifiedOnly.checked);
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
    syncChips();
    // The one place filter state is persisted. Every change listener reaches here via render(),
    // so nothing can change a filter without this running.
    saveFilterState();
  }

  // ---- the chip bar ----
  // Chips are a FACADE over the real inputs, which keep their ids and live inside the popovers.
  // Nothing in the filter pipeline knows this layer exists, which is why feed_parity.py and
  // test_filter_memory.py still drive the same functions with the same stubbed controls. Same
  // arrangement #trackseg already had over #track, applied to the whole bar.
  var POPS = ["pop-roles", "pop-loc", "pop-spon", "pop-date", "pop-more"];
  var openPop = null;

  // Show the denominator only when it differs from the numerator. "24,918 of 24,918 jobs" is
  // noise; "1,284 of 24,918 jobs" is the useful form.
  var countTot = document.getElementById("counttot");
  var TOTAL_ALL = countTot ? (parseInt((countTot.textContent || "").replace(/[^0-9]/g, ""), 10) || 0) : 0;
  function setCount(n) {
    if (countEl) countEl.textContent = n;
    if (countTot) countTot.hidden = !TOTAL_ALL || n === TOTAL_ALL;
  }

  // Visibility for elements that start hidden in the markup via .u-hide.
  //
  // THE BUG THIS FIXES: those elements used to carry style="display:none", and JS revealed them
  // with style.display = "". When the inline styles became utility classes in the token pass,
  // that stopped working in one direction only: an inline "none" still outranks the class, so
  // hiding worked, but clearing the inline property just let .u-hide apply again. The empty
  // state, Load more, the admin scrape bar and the tailor key field could all be hidden and
  // never shown. Visibility is a class now, and the inline property is cleared so a leftover
  // value from an older session cannot outrank it.
  function setShown(el, on) {
    if (!el) return;
    el.classList.toggle("u-hide", !on);
    if (el.style.display) el.style.display = "";
  }

  function chipSet(id, on, value) {
    var chip = document.getElementById("chip-" + id);
    if (!chip) return;
    chip.classList.toggle("on", !!on);
    var v = document.getElementById("chipv-" + id);
    if (v) v.textContent = on ? value : "";
  }
  // Reads the SAME state the filters read, so a chip can never disagree with the results.
  function syncChips() {
    var roles = rolesWanted();
    var rl = document.getElementById("rolebtn-t");
    chipSet("roles", roles.length,
            roles.length === 1 && rl ? rl.textContent.trim() : roles.length + " picked");

    var loc = locInp ? locInp.value.trim() : "";
    var rem = remoteOnly && remoteOnly.checked;
    chipSet("loc", loc || rem, loc && rem ? loc + " + remote" : (loc || "Remote only"));

    var vt = visaWanted(), hn = hideNo && hideNo.checked, vlab = "";
    if (vt.length === 1) {
      // The route's real label ("H-1B", not "H1B") is already rendered next to its checkbox,
      // from core.VISA_LABELS. Reading it back beats re-deriving a label from the key here and
      // then having two spellings to keep in step.
      var one = document.getElementById("visa-" + vt[0]);
      vlab = one && one.parentNode ? one.parentNode.textContent.trim() : vt[0];
    }
    chipSet("spon", vt.length || hn,
            vt.length ? (vt.length === 1 ? vlab : vt.length + " routes") : "Sponsoring only");

    var d = dateSel ? dateSel.value : "any";
    var DL = { "1": "Past 24 hours", "7": "Past 7 days", "30": "Past 30 days", "90": "Past 90 days" };
    var vo = verifiedOnly && verifiedOnly.checked;
    chipSet("date", (d && d !== "any") || vo, (DL[d] || "Any time") + (vo ? " · confirmed" : ""));

    var pc = document.getElementById("popcount");
    if (pc && countEl) pc.textContent = countEl.textContent;
  }

  // ---- the empty state ----
  // The old one guessed and named a CONTROL: "No jobs match. Lower Match or clear your search."
  // The server can simply know which single filter, dropped, brings back the most jobs (see
  // web._relax_suggestions), so the dead end names a VALUE and offers to undo it. Preventing
  // the empty result beats styling it.
  var RELAX_CTL = {
    min: function () { if (minR) { minR.value = 0; minVal = 0; setFill(); } },
    loc: function () { if (locInp) locInp.value = ""; },
    visatags: function () {
      if (visaSel) visaSel.value = "";
      // The five checkboxes are the UI for the hidden input, so untick them too or the group
      // reads as still-on. Same pairing applyFilterState() has to do on restore.
      var bx = document.querySelectorAll(".visack input[data-vt]");
      for (var i = 0; i < bx.length; i++) bx[i].checked = false;
    },
    date: function () { if (dateSel) dateSel.value = "any"; },
    minsal: function () { if (minSalSel) minSalSel.value = ""; },
    exp: function () { if (expSel) expSel.value = "any"; },
    intern: function () { if (internSel) internSel.value = "any"; },
    remote: function () { if (remoteOnly) remoteOnly.checked = false; },
    hidenospon: function () { if (hideNo) hideNo.checked = false; },
    verifiedonly: function () { if (verifiedOnly) verifiedOnly.checked = false; },
    hideagency: function () { if (hideAgency) hideAgency.checked = false; },
    roles: function () {
      if (rolesSel) rolesSel.value = "";
      var rb = document.querySelectorAll(".rolepick input[data-role]");
      for (var r = 0; r < rb.length; r++) rb[r].checked = false;
      document.dispatchEvent(new CustomEvent("roles:sync"));   // let rolepick.js relabel
    },
    track: function () { if (trackSel) trackSel.value = "any"; }
  };
  function renderEmpty(relax) {
    if (!emptyEl) return;
    var h = '<p class="empty-h">No jobs match these filters.</p>';
    if (relax && relax.length) {
      h += "<p>";
      for (var i = 0; i < relax.length; i++)
        h += (i ? " " : "") + "Removing " + esc(relax[i].label) + " would show " +
             relax[i].n.toLocaleString() + " job" + (relax[i].n === 1 ? "" : "s") + ".";
      h += "</p><div class=\"empty-acts\">";
      for (var k = 0; k < relax.length; k++)
        h += '<button type="button" class="btn sm" data-relax="' + esc(relax[k].key) + '">Remove ' +
             esc(relax[k].label) + "</button>";
      h += '<button type="button" class="btn sm ghost" data-relax="*">Clear all filters</button></div>';
    } else {
      h += '<div class="empty-acts"><button type="button" class="btn sm ghost" data-relax="*">Clear all filters</button></div>';
    }
    emptyEl.innerHTML = h;
  }
  if (emptyEl) emptyEl.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-relax]");
    if (!b) return;
    var key = b.getAttribute("data-relax");
    if (key === "*") { var cb = document.getElementById("clearfilters"); if (cb) cb.click(); return; }
    // Same event the Clear button emits, with which affordance did the recovery work, so the
    // two paths can be compared rather than guessed at.
    EV("clear_filters", { n: activeFilterCount(), via: "empty_state" });
    if (RELAX_CTL[key]) RELAX_CTL[key]();
    render(true);
  });

  function closePop() {
    if (!openPop) return;
    var el = document.getElementById(openPop);
    if (el) el.hidden = true;
    var btn = document.querySelector('[data-pop="' + openPop + '"]');
    if (btn) btn.setAttribute("aria-expanded", "false");
    openPop = null;
  }
  function placePop(el, btn) {
    // Absolute inside .feedlayout (position:relative), so it scrolls with the bar it belongs
    // to. Clamped to the layout's own box rather than the viewport, because the feed is
    // centred and a viewport clamp would let a right-hand popover drift off the content.
    el.hidden = false;                                    // measurable only once shown
    // Below 900px the popover becomes a full-width fixed sheet whose left AND right insets come
    // from CSS. An inline left would win over that and leave it 12px off-centre, so the inline
    // placement is cleared and only the vertical offset is set, from the bar it belongs to.
    if (getComputedStyle(el).position === "fixed") {
      el.style.left = "";
      var bar = document.getElementById("filterbar");
      el.style.top = Math.round((bar ? bar.getBoundingClientRect().bottom : 56) + 6) + "px";
      return;
    }
    var host = feedLayout || el.offsetParent || document.body;
    var hb = host.getBoundingClientRect(), bb = btn.getBoundingClientRect();
    var w = el.offsetWidth;
    var left = bb.left - hb.left;
    if (left + w > hb.width) left = Math.max(0, hb.width - w);
    el.style.left = Math.round(left) + "px";
    el.style.top = Math.round(bb.bottom - hb.top + 6) + "px";
  }
  function togglePop(id, btn) {
    var was = openPop;
    closePop();
    if (was === id) return;
    var el = document.getElementById(id);
    if (!el) return;
    placePop(el, btn);
    btn.setAttribute("aria-expanded", "true");
    openPop = id;
  }
  for (var pi = 0; pi < POPS.length; pi++) {
    (function (id) {
      var btn = document.querySelector('[data-pop="' + id + '"]');
      if (btn) btn.addEventListener("click", function (e) { e.stopPropagation(); togglePop(id, btn); });
    })(POPS[pi]);
  }
  document.addEventListener("click", function (e) {
    if (!openPop) return;
    if (e.target.closest && (e.target.closest(".fpop") || e.target.closest(".fchip"))) {
      if (e.target.closest("[data-popclose]")) closePop();
      return;
    }
    closePop();
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closePop(); });
  // A popover positioned against its chip is wrong the moment the bar reflows.
  addEventListener("resize", closePop);

  // Dispatcher: small corpus renders locally from the inline DATA (instant); large corpus
  // (data-paged) fetches each page from /api/feed so the payload stays small at any scale.
  function render(reset) {
    markFilters();
    _wantVisa = visaWanted();          // once per render, not once per card
    if (PAGED) renderServer(reset); else renderLocal(reset);
  }

  // Every filter control as query params, with NO paging — buildParams adds that.
  function filterParams() {
    var ps = ["tab=" + encodeURIComponent(tab), "min=" + (minVal || 0),
              "sort=" + encodeURIComponent(sortBy)];
    // Pins the request to one employer on the /company page. Server-side and applied before
    // the filters, so matches() has no company clause to mirror.
    if (COMPANY) ps.push("company=" + encodeURIComponent(COMPANY));
    if (q && q.value.trim()) ps.push("q=" + encodeURIComponent(q.value.trim()));
    if (dateSel && dateSel.value !== "any") ps.push("date=" + encodeURIComponent(dateSel.value));
    if (expSel && expSel.value !== "any") ps.push("exp=" + encodeURIComponent(expSel.value));
    var vw = visaWanted();
    if (vw.length) ps.push("visatags=" + encodeURIComponent(vw.join(",")));
    if (hideNo && hideNo.checked) ps.push("hidenospon=1");
    if (verifiedOnly && verifiedOnly.checked) ps.push("verifiedonly=1");
    var rw = rolesWanted();
    if (rw.length) ps.push("roles=" + encodeURIComponent(rw.join(",")));
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
    if (reset) { shown = 0; feed.innerHTML = '<div class="loading-jd" style="padding:28px"><span class="spin"></span>Loading…</div>'; }
    var mySeq = ++_seq;                                   // ignore out-of-order responses
    fetch("/api/feed?" + buildParams(reset ? 0 : shown)).then(function (r) { return r.json(); }).then(function (d) {
      if (mySeq !== _seq) return;
      var rows = (d && d.rows) || [], htmlc = "";
      for (var i = 0; i < rows.length; i++) byUrl[rows[i].url] = rows[i];
      for (var k = 0; k < rows.length; k++) htmlc += cardHTML(rows[k]);
      if (reset) feed.innerHTML = htmlc; else feed.insertAdjacentHTML("beforeend", htmlc);
      shown += rows.length;                               // one row = one card
      formatDates(); wireLogos();
      var jobs = (d && d.total) || 0;
      setCount(jobs);
      if (!jobs) renderEmpty(d && d.relax);
      setShown(emptyEl, !jobs);
      if (moreBtn) { var more = !!(d && d.has_more); setShown(moreBtn, more); if (more) moreBtn.textContent = "Load more (" + (jobs - shown) + " more)"; }
    }).catch(function () { if (mySeq === _seq && reset) feed.innerHTML = '<div class="empty">We couldn\'t load jobs. Try again.</div>'; });
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
        if (!j || !j.ok) { toast("Couldn't save. Try again."); return false; }
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
  if (verifiedOnly) verifiedOnly.addEventListener("change", function () { render(true); });

  // The picker itself lives in static/rolepick.js, because it renders on the onboarding wizard
  // too and this file is only loaded on the feed. It owns #roles and tells us when it changed;
  // we only ever read the value. Neither file needs the other to exist.
  document.addEventListener("roles:change", function (e) {
    EV("roles_set", { n: ((e.detail && e.detail.roles) || []).length });
    render(true);
  });
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
    if (verifiedOnly) verifiedOnly.checked = false;
    // Roles are owned by rolepick.js; clicking its own Clear keeps the tiles, the counter and
    // the rail button in step, which reaching in here from the outside would not.
    var rclr = document.getElementById("roleclear");
    if (rclr) rclr.click();
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
      verifiedonly: !!(verifiedOnly && verifiedOnly.checked),
      roles: rolesWanted().join(","),
      exp: expSel ? expSel.value : "any", intern: internSel ? internSel.value : "any",
      track: trackSel ? trackSel.value : "any",
      date: dateSel ? dateSel.value : "any", sort: sortBy
    };
    function doneSaving() {
      savePrefsBtn.disabled = false;
      savePrefsBtn.classList.remove("is-loading");
      savePrefsBtn.removeAttribute("aria-busy");
    }
    // Loading, not just disabled. A disabled button and a dead button look identical, and
    // this one posts the whole toolbar, so on a slow connection it read as broken.
    savePrefsBtn.disabled = true;
    savePrefsBtn.classList.add("is-loading");
    savePrefsBtn.setAttribute("aria-busy", "true");
    fetch("/prefs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json(); }).then(function (d) {
      doneSaving();
      if (d && d.ok) toast(d.note ? "Saved. " + d.note : "Saved. This is now your default search and your daily email.");
      else toast("Couldn't save: " + ((d && d.error) || "unknown error"));
    }).catch(function () {
      doneSaving();
      toast("Couldn't save your default search.");
    });
  });
  if (moreBtn) moreBtn.addEventListener("click", function () { limit += PAGE; render(false); });

  // feed clicks: action buttons, Apply auto-log, company link, or open modal
  feed.addEventListener("click", function (e) {
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
              if (!ok2) { toast("Couldn't undo. Try again."); return; }
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

  // The panel header, BUILT from the row rather than copied out of the card.
  //
  // It used to be `$("m-meta").textContent = card.querySelector(".cmeta").textContent`, which
  // flattened the badge <span>s into one unbroken run — "Irving, TX3d agoH-1BGreen CardE-3
  // $120k-$150k" — because their only separator was a CSS margin that .textContent discards.
  // Three FACTS live here; every badge belongs in the chip row below, which already lays them
  // out with a real flex gap (and was rendering the visa routes a second time regardless).
  function metaHTML(j) {
    if (!j) return "";
    var parts = [companyLink(j.company), esc(j.location || "Location not stated")];
    // The card's own markup, so formatDates() renders it identically here — "3d ago" with the
    // posted/verified tooltip, or the italic "Added <date>" for employers who publish none.
    if (j.date)
      parts.push('<span class="posted" data-d="' + H(j.date) + '"' +
                 (j.date_verified ? ' data-verified="1"' : '') + '>' + H(j.date) + '</span>');
    else if (j.first_seen)
      parts.push('<span class="posted added" data-d="' + H(j.first_seen) +
                 '" data-added="1">Added ' + H(j.first_seen) + '</span>');
    return parts.join(" · ");
  }

  // ---- job description: plain text -> a readable document ----
  // /api/job returns the JD verbatim (db.get_job_jd, truncated at 7,000 chars) and this is its
  // only consumer, so the shaping lives here beside every other bit of card/modal markup.
  //
  // SAFETY: every fragment goes through esc() BEFORE it is placed inside a tag, and no substring
  // of the JD is ever concatenated into innerHTML unescaped — esc() is the only door in. If URL
  // linkification is ever added it must run on the ALREADY-ESCAPED string; getting that order
  // backwards is the one way this function becomes an XSS hole.
  var JD_BULLET = /^\s*(?:[•·▪●◦‣⁃*–—-]|\(?\d{1,2}[.)])\s+/;
  var JD_HEAD = /^(?:about|responsibilit|qualificat|requirement|what you|who you|the role|your role|benefit|perks|compensation|skills|experience|education|duties|essential|preferred|minimum|basic|nice to have|equal (?:employment )?opportunity|eeo|how to apply|why join|our team|job (?:summary|description|details))/i;

  function isJdHeading(t) {
    if (!t || t.length > 70 || JD_BULLET.test(t)) return false;
    var letters = t.replace(/[^A-Za-z]/g, "");
    if (letters.length >= 3 && t === t.toUpperCase()) return true;   // short ALL-CAPS line
    if (/:$/.test(t) && t.split(/\s+/).length <= 8) return true;     // "Requirements:"
    // A known section name with no colon. Digits and $ disqualify, or "Salary: $120,000" —
    // which matches /^salary/ — would be promoted from a fact to a heading.
    return JD_HEAD.test(t) && t.split(/\s+/).length <= 8 &&
           !/[.;,]$/.test(t) && !/[\d$]/.test(t);
  }

  // ---- splitting a description that arrives as ONE unbroken run of text ----
  // Measured on the live corpus: 5 of 6 sampled descriptions contain ZERO newlines. Workday,
  // iCIMS and Amazon all hand us the whole posting as a single ~4,000-character string, so the
  // line-based pass below has nothing to work with and emitted one enormous paragraph. These
  // split that run the only two ways its own text allows: on the section names every ATS
  // template repeats, then on sentence boundaries.
  //
  // Two tiers, because precision matters more than recall here — a false heading mid-sentence
  // is far uglier than a missed one. STRONG names are unambiguous enough to match on a colon OR
  // a following capital ("Overview This is a hybrid role" is how iCIMS writes it). WEAK ones
  // are ordinary words that appear constantly in prose ("5 years of experience"), so they need
  // an explicit colon before they count as a heading.
  var JD_SECTION_STRONG = "job description|position purpose|position summary|role summary|" +
    "essential (?:functions?|duties)|basic qualifications|minimum qualifications|" +
    "preferred qualifications|additional qualifications|key responsibilities|" +
    "primary responsibilities|what you(?:'|\u2019)ll (?:do|bring)|what you will do|" +
    "what you bring|what we(?:'|\u2019)re looking for|what we offer|who you are|" +
    "about (?:us|the role|the team|the company|the job|this role)|required skills|" +
    "day in the life|nice to have|how to apply|why join(?: us)?|our team|" +
    "equal (?:employment )?opportunity(?: employer)?|eeo statement|" +
    "pay range|salary range|compensation range|overview";
  var JD_SECTION_WEAK = "summary|responsibilities|requirements|qualifications|benefits|perks|" +
    "compensation|education|experience|skills|duties";
  var JD_SECTION = new RegExp(
    "\\b(" + JD_SECTION_STRONG + ")\\b(?::\\s+|\\s+(?=[A-Z]))" +
    "|\\b(" + JD_SECTION_WEAK + ")\\b:\\s+", "gi");
  var JD_PARA_MAX = 360;          // chars; above this a run is split on sentence boundaries

  // ---- lists that lost their bullets AND their punctuation ----
  // The iCIMS/Workday "Essential Functions" idiom: a dozen requirement lines concatenated with
  // no bullet, no newline and no full stop — "…algorithmic solutions Demonstrated ability to
  // serve as a lead software engineer Ability to decompose functional requirements…". Measured:
  // 5,748 of 17,004 descriptions over 800 chars (34%) carry fewer than 6 sentence stops per
  // 1,000 characters, which is that shape.
  //
  // The boundary is a capital following a lowercase word — but splitting on ANY capital would
  // wreck real prose, because the most frequent capitals in that position across the corpus are
  // proper nouns: Boeing (2,164), Capital (2,037), Company, Engineering, One, States. So we
  // split only before words that actually START a requirement, and only inside a run that has
  // already failed the punctuation test. Deliberately excluded despite being common bullet
  // openers: Lead, Support, Design, Manage, Build, Drive, Track — each is an ordinary noun that
  // shows up capitalised mid-title ("team Lead Engineer"), and a false cut reads far worse than
  // a missed one.
  var JD_ITEM_START =
    "Demonstrated|Demonstrates|Ability|Abilities|Proven|Proficien(?:cy|t|cies)|Familiarity|" +
    "Knowledge|Understanding|Excellent|Strong|Solid|Exceptional|Experience|Expertise|" +
    "Bachelor'?s?|Master'?s?|Minimum|Preferred|Required|Must|Should|Responsible|Working|" +
    "Assists?|Develops?|Ensures?|Maintains?|Performs?|Provides?|Coordinates?|Participates?|" +
    "Collaborates?|Implements?|Analyzes?|Prepares?|Monitors?|Reviews?|Conducts?|Evaluates?|" +
    "Recommends?|Identifies|Identify|Communicates?|Translates?|Oversees?|Establishes?|" +
    "Contributes?|Executes?|Delivers?|Partners?|Serves?|Troubleshoots?|Utilizes?";
  // Keep the matched preceding character and mark the cut with a control char, rather than
  // using a lookbehind: Safari only shipped those recently and this file is ES5 throughout.
  // U+0001 cannot occur in a description, so splitting on it is unambiguous.
  var JD_ITEM_RE = new RegExp("([a-z)\\]])\\s+(?=(?:" + JD_ITEM_START + ")\\b)", "g");
  var JD_CUT = "";
  var JD_ITEM_MIN = 25;           // a real requirement line is long; a short piece means a bad cut
  // A cut is wrong if the text before it ends on a word that cannot end a sentence. Measured
  // over 600 real descriptions, this is the whole of the remaining false-positive class:
  // "a Bachelor's degree in Engineering" cut after "of a", and "NDAs, SOWs, and Master Service
  // Agreements" cut after "and" — both because Bachelor/Master legitimately start a bullet too.
  // Cheaper and more general than dropping those words, which would lose the real cuts.
  var JD_DANGLING = /\b(?:a|an|the|and|or|of|with|to|for|in|on|at|by|from|as|plus|per|our|your|their|its|this|that|these|those|is|are|be|been|has|have|had|will|shall|may|any|all|each|other|including|includes|include)$/i;

  function jdFlatList(t) {
    // Punctuation gate first: prose that already has sentences must go to the sentence splitter.
    var stops = (t.match(/[.!?]/g) || []).length;
    if (stops / (t.length / 1000) >= 6) return null;
    var raw = t.replace(JD_ITEM_RE, "$1" + JD_CUT).split(JD_CUT);
    // Heal the bad cuts by gluing a dangling piece back onto the one after it, rather than
    // throwing the whole split away — one wrong boundary shouldn't cost the other eleven.
    var parts = [];
    for (var i = 0; i < raw.length; i++) {
      var piece = raw[i].trim();
      if (!piece) continue;
      while (JD_DANGLING.test(piece) && i + 1 < raw.length) piece += " " + raw[++i].trim();
      parts.push(piece);
    }
    if (parts.length < 4) return null;                 // a lead-in plus at least 3 items
    // From 1, not 0: parts[0] is whatever preceded the first item, not an item itself, and it
    // is routinely a stub — Garmin's list opens mid-phrase on "0 as a general rule)". Holding
    // the lead-in to the item length threw away the whole list for a fragment that is simply
    // the tail of the sentence above it.
    for (var j = 1; j < parts.length; j++)
      if (parts[j].length < JD_ITEM_MIN) return null;  // cut mid-sentence — abandon it
    return parts;
  }

  // One run of prose -> one or more blocks. Never emits a paragraph much longer than
  // JD_PARA_MAX unless a single sentence is.
  function jdChunk(t) {
    t = (t || "").trim();
    if (!t) return "";
    // Some boards flatten a real <ul> into "• a • b • c" on one line. Two or more bullets is a
    // list; a single one is just a stray glyph inside a sentence.
    if ((t.match(/\u2022/g) || []).length >= 2) {
      var parts = t.split(/\s*\u2022\s*/), lead = "", items = "";
      if (parts.length && parts[0].trim() && t.charAt(0) !== "\u2022")
        lead = "<p>" + esc(parts.shift().trim()) + "</p>";
      for (var b = 0; b < parts.length; b++)
        if (parts[b].trim()) items += "<li>" + esc(parts[b].trim()) + "</li>";
      return lead + (items ? "<ul>" + items + "</ul>" : "");
    }
    if (t.length <= JD_PARA_MAX) return "<p>" + esc(t) + "</p>";
    // A requirements list that lost its bullets AND its full stops. Rendered as a real <ul>,
    // because that is what it is \u2014 the first piece is the lead-in sentence before the list.
    var flat = jdFlatList(t);
    if (flat) {
      var head = "<p>" + esc(flat.shift().trim()) + "</p>", li = "";
      for (var f = 0; f < flat.length; f++) li += "<li>" + esc(flat[f].trim()) + "</li>";
      return head + "<ul>" + li + "</ul>";
    }
    var sent = t.match(/[^.!?]+(?:[.!?]+["'\u2019)\]]*|$)/g) || [t];
    var out = "", buf = "";
    for (var i = 0; i < sent.length; i++) {
      var s = sent[i].trim();
      if (!s) continue;
      if (buf && buf.length + s.length > JD_PARA_MAX) { out += "<p>" + esc(buf) + "</p>"; buf = ""; }
      buf = buf ? buf + " " + s : s;
    }
    return out + (buf ? "<p>" + esc(buf) + "</p>" : "");
  }

  function jdParagraphs(text) {
    var t = String(text || "").trim();
    if (!t) return "";
    var out = "", last = 0, m;
    JD_SECTION.lastIndex = 0;        // module-level regex carrying /g: its state is shared
    while ((m = JD_SECTION.exec(t)) !== null) {
      if (m.index === JD_SECTION.lastIndex) { JD_SECTION.lastIndex++; continue; }  // no progress
      // Not a heading if it is finishing the sentence in front of it — "Cintas Corporation is
      // proud to be an" / "Equal Opportunity Employer" is one sentence, and lifting the tail
      // out of it leaves a paragraph dangling on "an".
      if (JD_DANGLING.test(t.slice(last, m.index).trim())) continue;
      out += jdChunk(t.slice(last, m.index));
      out += '<h4 class="jdh">' + esc(m[1] || m[2]) + "</h4>";
      last = JD_SECTION.lastIndex;
    }
    return out + jdChunk(t.slice(last));
  }

  function jdHTML(text) {
    var src = String(text == null ? "" : text).replace(/\r\n?/g, "\n").replace(/ /g, " ");
    var lines = src.split("\n"), out = [], para = [], list = null;
    // jdParagraphs, not a bare <p>: on most boards a "line" here is the ENTIRE posting.
    function flushPara() { if (para.length) out.push(jdParagraphs(para.join(" "))); para = []; }
    function flushList() { if (list && list.length) out.push("<ul>" + list.join("") + "</ul>"); list = null; }
    for (var i = 0; i < lines.length; i++) {
      var t = lines[i].trim();
      if (!t) { flushPara(); flushList(); continue; }   // a blank ends the block; runs of them vanish
      var bm = t.match(JD_BULLET);
      if (bm) {
        flushPara();
        if (!list) list = [];
        list.push("<li>" + esc(t.slice(bm[0].length).trim()) + "</li>");
        continue;
      }
      if (isJdHeading(t)) {
        flushPara(); flushList();
        out.push('<h4 class="jdh">' + esc(t.replace(/:$/, "")) + "</h4>");
        continue;
      }
      flushList();
      // Hard-wrapped prose: a long previous line that doesn't end a sentence is mid-paragraph,
      // so join onto it. Otherwise start a new <p>, so separate one-line statements — which is
      // how most bullet-less JDs are written — don't get glued into a wall.
      var prev = para.length ? para[para.length - 1] : "";
      if (prev && !(prev.length > 62 && !/[.:;!?]$/.test(prev))) flushPara();
      para.push(t);
    }
    flushPara(); flushList();
    return out.join("");
  }
  function openModal(card) {
    if (!modal) return;
    mUrl = card.getAttribute("data-url");
    var lg = card.querySelector(".logo");
    $("m-logo").innerHTML = lg ? lg.innerHTML : ""; $("m-logo").style.background = lg ? lg.style.background : "";
    var mlImg = $("m-logo").querySelector(".logo-img");
    if (mlImg) { mlImg.removeAttribute("data-fb-wired"); wireLogoFallback(mlImg); }
    $("m-title").textContent = card.querySelector(".ctitle") ? card.querySelector(".ctitle").textContent : "";
    var mm = $("m-meta");
    mm.innerHTML = metaHTML(byUrl[mUrl]);
    formatDates(mm);
    $("m-chip").innerHTML = '<span class="shim shim-chip"></span>';
    $("m-skills").innerHTML = '<div class="shim-row"><span class="shim shim-tag"></span><span class="shim shim-tag"></span><span class="shim shim-tag" style="width:88px"></span></div>' +
      '<span class="shim shim-bar w75"></span><span class="shim shim-bar w55"></span>';
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
      // Every badge lives in this ONE row — the header above carries facts only, so nothing is
      // rendered twice. Pay, Remote, Internship and Closed ride on the feed row rather than the
      // /api/job payload, which doesn't carry them; before this they reached the panel only via
      // the broken .textContent header copy, so pay would have vanished with it.
      var row = byUrl[mUrl] || {};
      var spn = "";
      if (row.salary_label) spn += '<span class="pay">' + H(row.salary_label) + '</span>';
      if (row.remote) spn += '<span class="rem">Remote</span>';
      if (row.intern) spn += '<span class="intl">Internship</span>';
      if (j.exp_years !== "" && j.exp_years != null) {
        var ey = parseInt(j.exp_years, 10);
        var ec = ey >= 6 ? "exp-hi" : ey >= 3 ? "exp-mid" : "exp-lo";
        spn += '<span class="exp ' + ec + '">' + ey + '+ yrs experience</span>';
      }
      // The modal has room, so it shows every route rather than the card's top three — and it
      // is where the "past filings, not a promise" caveat now lives, since the card's chips
      // dropped their tooltips.
      var mvt = j.visa || [];
      for (var mi = 0; mi < mvt.length; mi++)
        spn += '<span class="vt vt-' + H(mvt[mi]) + '" title="' + H(VISA_TIPS[mvt[mi]] || "") +
          '">' + esc(VISA_LABELS[mvt[mi]] || mvt[mi]) + '</span>';
      if (j.cap_exempt) spn += '<span class="cx">Likely cap-exempt, no H-1B lottery</span>';
      if (j.agency) spn += '<span class="agency">Agency</span>';
      // Fixed label, specific reason in the tooltip — the same shape cardHTML uses, so the card
      // and the panel can never word this differently. The label is decided HERE rather than by
      // editing core._SPONSOR_BLOCK's messages, because those strings are substring-matched by
      // core._BLOCKS_EVERYONE to decide whether a blocked posting keeps STEM-OPT or loses every
      // route — rewording them would quietly put "H-1B · Green Card" back on citizens-only
      // roles. Fixing it at the render layer is also the only fix that is correct for the
      // reasons ALREADY stored in the sponsor_reason column.
      if (j.sponsor_jd === "blocked")
        spn += '<span class="nospon" title="' + H(j.sponsor_reason || "") + '">No sponsorship</span>';
      else if (j.sponsor_jd === "open")
        spn += '<span class="spon" title="' + H(j.sponsor_reason || "") + '">Sponsors</span>';
      if (row.closed) spn += '<span class="closed">Closed</span>';
      if (spn) sk = '<div class="kw" style="margin-bottom:10px">' + spn + '</div>' + sk;
      var skEl = $("m-skills"), jdEl = $("m-jd");
      skEl.style.opacity = "0"; jdEl.style.opacity = "0";
      skEl.innerHTML = sk;
      jdEl.innerHTML = jdHTML(j.jd) ||
        "<p>" + esc("No description stored. Click Apply to read it on the company site.") + "</p>";
      requestAnimationFrame(function () {
        skEl.style.transition = "opacity .25s"; skEl.style.opacity = "1";
        jdEl.style.transition = "opacity .25s"; jdEl.style.opacity = "1";
      });
    }).catch(function () { $("m-jd").textContent = "Couldn't load details."; });
  }
  // ---- "More about this employer" panel (company page only) ----
  // Server-rendered and static, so this is just a show/hide — no fetch, no template in JS.
  // Every reference is guarded: the feed has no such button and must not throw.
  var coModal = document.getElementById("comodal"), coBtn = document.getElementById("cobtn");
  if (coModal && coBtn) {
    var coOpen = function () { coModal.classList.add("open"); document.body.style.overflow = "hidden"; };
    var coShut = function () { coModal.classList.remove("open"); document.body.style.overflow = ""; };
    coBtn.addEventListener("click", coOpen);
    for (var ci = 0, cx = ["coclose", "coclose2"]; ci < cx.length; ci++) {
      var cel = document.getElementById(cx[ci]);
      if (cel) cel.addEventListener("click", coShut);
    }
    coModal.addEventListener("click", function (e) { if (e.target === coModal) coShut(); });
    // Own Escape handler rather than sharing the job panel's: both can be open in principle,
    // and closing whichever is on top is what a reader expects.
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && coModal.classList.contains("open")) coShut();
    });
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
    setShown(sBar, true);
    var ph = st.phase, done = st.done || 0, total = st.total || 0, found = st.found || 0;
    var pct = total > 0 ? Math.min(100, Math.round(done / total * 100)) : null;
    var elapsed = elapsedOf(st), el = elapsed != null ? fmtClock(elapsed) + " elapsed" : "";
    if (ph === "queued") { indet(true); sLabel.textContent = "Starting the scrape on GitHub…"; sMeta.textContent = el; }
    else if (ph === "scraping") {
      indet(false); sFill.style.width = (pct != null ? pct : 5) + "%";
      sLabel.textContent = "Scraping job boards" + (pct != null ? ", " + pct + "%" : "…");
      var eta = (pct && elapsed && done > 0) ? " · ~" + fmtClock(elapsed * (total - done) / done) + " left" : "";
      sMeta.textContent = (total ? done + "/" + total + " boards · " : "") + found + " jobs" + (el ? " · " + el : "") + eta;
    }
    else if (ph === "saving") { indet(true); sLabel.textContent = "Saving " + found + " postings…"; sMeta.textContent = el; }
    else if (ph === "scoring") { indet(true); sLabel.textContent = "Scoring jobs to your profile…"; sMeta.textContent = (st.new ? st.new + " new · " : "") + el; }
    else if (ph === "done") { indet(false); sFill.style.width = "100%"; sLabel.textContent = "Done. " + (st.new || 0) + " new job" + ((st.new || 0) === 1 ? "" : "s") + " added."; sMeta.textContent = "Refreshing…"; return "done"; }
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
        if (state === "done") { setTimeout(function () { refreshFeedAfterScrape(); setTimeout(function () { setShown(sBar, false); }, 4000); }, 1000); }
        else if (stale && sBar) { if (sLabel) sLabel.textContent = "Scrape finished (or stopped)."; if (sMeta) sMeta.textContent = "Hit Reload if new jobs don't appear."; setTimeout(function () { setShown(sBar, false); }, 6000); }
      }
    }).catch(function () {});
  }
  function startScrapePolling() { if (sPoll) return; sPollStart = Date.now(); pollScrape(); sPoll = setInterval(pollScrape, 4000); }
  if (sForm) sForm.addEventListener("submit", function (e) {
    e.preventDefault();
    if (sBtn) sBtn.disabled = true;
    if (sBar) { setShown(sBar, true); indet(true); if (sLabel) sLabel.textContent = "Starting…"; if (sMeta) sMeta.textContent = ""; }
    fetch("/scrape", { method: "POST", headers: { "X-Requested-With": "fetch" } }).then(function (r) { return r.json(); }).then(function (j) {
      if (!j || !j.ok) { toast((j && j.msg) || "Couldn't start the scrape."); if (sBtn) sBtn.disabled = false; setShown(sBar, false); return; }
      toast("Scrape started on GitHub Actions."); startScrapePolling();
    }).catch(function () { toast("Couldn't start the scrape."); if (sBtn) sBtn.disabled = false; setShown(sBar, false); });
  });
  // If a scrape is already running (daily cron, or started in another tab), show the bar on load.
  if (sBar) fetch("/api/scrape_status", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (st) {
    var updMs = st && st.updated_at ? Date.parse(st.updated_at) : NaN;
    if (st && st.phase && st.phase !== "done" && !isNaN(updMs) && (Date.now() - updMs < 180000)) { renderScrape(st); startScrapePolling(); }
  }).catch(function () {});

  render(true);
})();
