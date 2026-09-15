// Feed: data ships as compact JSON; we render only the visible slice of cards client-side
// (instead of ~2,500 server-rendered <article> nodes), then filter/sort/paginate + open the
// job-detail modal + no-reload actions. Far less HTML to transfer and far fewer DOM nodes.
(function () {
  "use strict";
  var feed = document.getElementById("feed");
  if (!feed) return;
  var cardSize = document.getElementById("card-size");
  if (cardSize) {
    cardSize.value = document.documentElement.getAttribute("data-card-size") || "comfortable";
    cardSize.addEventListener("change", function () {
      var size = cardSize.value;
      if (["compact", "comfortable", "roomy"].indexOf(size) < 0) return;
      document.documentElement.setAttribute("data-card-size", size);
      try { localStorage.setItem("jm-card-size", size); } catch (e) {}
    });
  }
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
  // The lowest year count each level implies — the inverse of core.exp_level_for's bands, and
  // what lets the experience ceiling answer for a row whose description states no number.
  // Same strict-JSON rule as the visa tables above, and feed_parity asserts it equals
  // core.LEVEL_MIN_YEARS.
  var LEVEL_MIN_YEARS = {"entry": 0, "mid": 3, "senior": 6};
  var VISA_LABELS = {"h1b": "H-1B", "green_card": "Green Card", "stem_opt": "STEM-OPT",
                     "e3": "E-3", "h1b1": "H-1B1"};
  var VISA_TIPS = {
    "h1b": "Has certified H-1B labor condition applications. Past filings, not a promise.",
    "green_card": "Has certified PERM applications, so they sponsor permanent residency, not just temporary visas.",
    "stem_opt": "Enrolled E-Verify employer, required for the STEM-OPT 24-month extension. Confirm at e-verify.gov.",
    "e3": "Has filed E-3 applications (Australian nationals).",
    "h1b1": "Has filed H-1B1 applications (Chile / Singapore nationals)."
  };
  // core.SPONSOR_LIKELY_LABELS used to be mirrored here, because the card named the route:
  // "H-1B Likely" / "Sponsor Likely" / "STEM-OPT Likely". It does not any more — see the chip
  // in cardHTML — so the copy is gone rather than left sitting unused next to the server's.
  // /job and /company still name all five routes; company.html and companies.js hold the
  // labels for those, and core.py remains the single definition.
  // The last fiscal year the sponsorship data actually covers. Read off #feed rather than
  // hardcoded, so refreshing sponsor_years.json moves every label that quotes it in one step.
  // Falls back to the current shipped vintage when the attribute is absent (an older template).

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
      fmoreBtn = document.getElementById("fmore"),
      // "Am I on the feed?" — company.html renders the same #q and #sort but means something
      // different by #q, so the two must not share it. This pointed at #filterrail, which was
      // DELETED when the chip bar replaced the rail, so it has been null on every page since:
      // _ctlMap below then returned q:null on the feed and the search box silently stopped
      // being remembered. test_filter_memory could not catch it because its harness stubs this
      // very variable, which is the standing lesson about a green suite and live behaviour.
      feedOnly = document.getElementById("filterbar"),
      fbadges = document.querySelectorAll(".fbadge"),
      feedLayout = document.getElementById("feedlayout"),
      locInp = document.getElementById("loc"), remoteOnly = document.getElementById("remoteonly"),
      minSalSel = document.getElementById("minsal"), hideAgency = document.getElementById("hideagency"),
      // "Only postings that state their years" — see core.DEFAULT_PREFS.expstated for the
      // measurement (72% of results under a years filter state no number at all).
      expStated = document.getElementById("expstated"),
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
  // localStorage is also already the pattern here (theme, the per-user filter blob).
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
    // THE COMPANY PAGE IS A SCRATCHPAD. It now carries the same filter bar the feed does
    // (_filterbar.html), and none of it is remembered except #sort — dropping the match floor to
    // look through one employer's 500 roles must not silently reset the feed you come back to.
    //
    // That rule already existed for #q alone, guarded by scripts/test_filter_memory.py ("the
    // company page must not clobber the feed's filters"), and it held only because the employer
    // page rendered nothing else. Giving it the full bar without widening the rule would have
    // quietly turned every control into a writer of the feed's saved state.
    //
    // THE DISCRIMINATOR IS data-company, NOT the presence of #filterbar. It used to be the
    // latter, because the bar existed only on feed.html; that test silently became true on both
    // pages the moment the markup was shared. data-company is set by _feedgrid.html only when a
    // company_arg was passed, which is exactly the distinction meant.
    //
    // Read off the DOM rather than through the COMPANY var: applyFilterState() runs during
    // initialisation, ABOVE the line where COMPANY is assigned, so the var would read undefined
    // on exactly the one call that restores saved filters.
    if (feed && feed.getAttribute("data-company")) return { sort: sortSel };
    return { q: q, min: minR, sort: sortSel, date: dateSel, exp: expSel,
             intern: internSel, minsal: minSalSel, loc: locInp, visatags: visaSel,
             track: trackSel, roles: rolesSel, hidenospon: hideNo, verifiedonly: verifiedOnly,
             remoteonly: remoteOnly, hideagency: hideAgency, expstated: expStated,
             showclosed: showClosed };
  }
  function _readStore() {
    // A stored blob from an older shape is discarded whole rather than half-applied. The
    // precedent: jm_fpanel once shipped defaulting to "0" and a stale value could hide the
    // entire filter panel from a returning user, with no error anywhere.
    try {
      var o = JSON.parse(localStorage.getItem(FILTER_KEY) || "null");
      return (o && typeof o === "object" && o.v === FILTER_V) ? o : {};
    } catch (e) { return {}; }
  }
  function saveFilterState() {
    // MERGE over what is stored, and only for the controls _ctlMap reports for THIS page. On an
    // employer page that is #sort alone (see _ctlMap), so narrowing your way through one
    // company's roles leaves the feed's filters exactly as you left them. A wholesale overwrite
    // from there would wipe all fifteen.
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
    // Gated on _ctlMap TOO, not just on the control existing: on an employer page visatags is
    // not a remembered control, and ticking the boxes there without also restoring the hidden
    // input would leave the group looking active while filtering nothing.
    if (visaSel && ("visatags" in m) && "visatags" in s) {
      var want = String(s.visatags || "").split(","),
          boxes = document.querySelectorAll(".visack input[data-vt]");
      for (var i = 0; i < boxes.length; i++)
        boxes[i].checked = want.indexOf(boxes[i].getAttribute("data-vt")) >= 0;
    }
    return s;
  }
  // A comparable snapshot of every filter control. Reads _ctlMap() so there stays exactly ONE
  // definition of "what the filters are" — a control added there is covered here for free.
  function _snapState() {
    var m = _ctlMap(), s = {}, k;
    for (k in m) if (m[k]) s[k] = (m[k].type === "checkbox") ? !!m[k].checked : String(m[k].value);
    return JSON.stringify(s);
  }
  // Captured BEFORE applyFilterState() overwrites the controls, so this is the filter state the
  // server actually built its inline first page from. renderServer compares against it.
  var SERVER_STATE = _snapState();
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
  // How many rows match the default view, counted server-side. null when the page didn't send
  // it, which is the signal that the inline bootstrap can't be trusted as a full page one.
  var _totalAttr = feed.getAttribute("data-total");
  var TOTAL = (_totalAttr === null || _totalAttr === "") ? null : (parseInt(_totalAttr, 10) || 0);

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
  // The meta-row separator, in one place. The dot itself is correct typography and both
  // LinkedIn and Indeed use it, so it stays — but typed bare into the string it inherited
  // the full weight of the text around it, and a screen reader read "Stripe middle dot
  // Boston comma MA". Wrapped, it can recede in CSS and be skipped in the accessible name.
  var SEP = '<span class="sep" aria-hidden="true">·</span>';
  // A TRAFFIC LIGHT: green at 70 and up, amber from 40 to 69, red below 40. The thresholds are
  // here and the three colours are in style.css (.ring-strong / .ring-good / .ring-low), so
  // neither side can be re-tuned by accident from the other.
  //
  // 70 and 40 are the reader's numbers, not the old 55/42 pair, which came from the score
  // DISTRIBUTION rather than from what a percentage means to someone deciding whether to read
  // a posting. Note the feed's own default floor is 45, so an amber ring is the common case and
  // red only appears once the minimum-match slider is pulled below it.
  function scoreRing(s) {
    var cls = s >= 70 ? 'ring-strong' : s >= 40 ? 'ring-good' : 'ring-low';
    var off = (113.1 * (1 - s / 100)).toFixed(1);
    return '<svg class="score-ring ' + cls + '" width="46" height="46" viewBox="0 0 46 46" aria-label="' + s + '% match">' +
      // An OPAQUE disc behind the arc. The card is washed with its route colour now, so
      // without this a green ring sat on the green STEM-OPT fill and disappeared. A
      // translucent veil would tint differently on each of the five washes and could not be
      // contrast-tested; a solid surface is one known background for all of them. r=21 in a
      // 46 box leaves the 2px the arc's round cap needs.
      '<circle cx="23" cy="23" r="21" fill="var(--match-pill)"/>' +
      '<circle cx="23" cy="23" r="18" fill="none" stroke="var(--ring-track)" stroke-width="4.5"/>' +
      '<circle cx="23" cy="23" r="18" fill="none" stroke="var(--ring-color)" stroke-width="4.5" stroke-dasharray="113.1" stroke-dashoffset="' + off + '" stroke-linecap="round" transform="rotate(-90 23 23)"/>' +
      // 700, not 800: the type scale tops out at bold and 800 is not one of its weights, so it
      // was asking Inter for a face the rest of the product never uses.
      '<text x="23" y="27" text-anchor="middle" font-size="10" font-weight="700" fill="var(--ring-color)" font-family="Inter,-apple-system,sans-serif">' + s + '%</text>' +
      '</svg>';
  }
  // The score cell for a card/detail: the % ring, OR a neutral "Not scored" chip when the job's
  // description is too short/truncated to score honestly (score_pending from the server).
  function scoreCell(j) {
    // No résumé, no score. This used to fall through to the stored match_score, which is
    // computed against the SCRAPER's resume.txt — so a new account saw a feed of confident
    // 62-64% rings derived from somebody else's CV, drawn identically to a real match. A
    // number that looks personalised and isn't is worse than no number.
    // Empty, not a typed-in dot. The dashed ring already says "a number belongs here and
    // there isn't one"; a middle dot inside it read as a rendering artefact, and screen
    // readers announced it. The tooltip carries the actual explanation.
    if (!HAS_RESUME)
      return '<span class="score-none" title="Add your résumé for match scores"></span>';
    // Two different facts, and they were one chip until 2026-08-18. "pending" means we have not
    // read the description YET; "unavailable" means this employer refuses every server-side read
    // (Akamai, an AWS WAF challenge) so no run will ever change it. Promising a score that
    // cannot arrive is worse than saying so.
    if (j && j.jd_unavailable)
      return '<span class="score-pending" title="No readable description">No JD</span>';
    if (j && j.score_pending)
      return '<span class="score-pending" title="Not scored yet - the description has not been analysed">Not scored</span>';
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
  // A row's date arrives in one of TWO shapes, and they mean different things.
  //
  //   "2026-08-04"        somebody STATED this. Parsed at LOCAL midnight, so "Today" means
  //                       the reader's today and a UTC-stamped date doesn't read as tomorrow.
  //   "2026-08-04 14:00"  DERIVED. core.is_trusted_date documents this shape as the marker:
  //                       either the scrape stamp for a board that publishes no date at all,
  //                       or _workday_date() converting "Posted 3 Days Ago". Parsed as UTC,
  //                       because the scraper writes a naive datetime.now() from GitHub
  //                       Actions. Guessing local would shift it by the reader's offset and
  //                       could put it in the future.
  //
  // relTime used to do `new Date(s + "T00:00:00")` unconditionally, so the second shape built
  // "2026-08-04 14:00T00:00:00", failed to parse, and fell through to printing the raw string.
  function parseRowDate(s) {
    s = String(s || "");
    if (!s) return null;
    var m = /^(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})/.exec(s);
    var d = m ? new Date(m[1] + "T" + m[2] + ":" + m[3] + ":00Z")
              : new Date(s.slice(0, 10) + "T00:00:00");
    return isNaN(d.getTime()) ? null : d;
  }
  function hasClock(s) { return /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}/.test(String(s || "")); }
  // Whole days since a row's date, or null. The "New" badge asks THIS rather than comparing
  // relTime()'s output against the string "Today": that was a string equality standing in for
  // a date computation, and it would have silently stopped firing the moment relTime learned
  // to answer "3h ago" — the same shape of defect as the #filterrail bug, where a proxy check
  // kept passing after the thing it stood for had moved.
  function daysAgo(s) {
    var d = parseRowDate(s);
    return d ? Math.floor((Date.now() - d.getTime()) / 86400000) : null;
  }
  function relTime(s) {
    if (!s) return "";
    var d = parseRowDate(s);
    if (!d) return s;
    var ms = Date.now() - d.getTime();
    // The stamp is written by whichever machine ran the scrape, so a row can sit a few
    // minutes in the future. "in 2h" would read as a bug, and a negative hour count as a
    // worse one.
    if (ms < 0) ms = 0;
    var days = Math.floor(ms / 86400000);
    // HOURS, and only here: the row has to actually carry a clock, and it has to be inside a
    // day. A stated posting date has no time in it anywhere in this corpus, so an hour count
    // on one would be invented rather than measured.
    if (days < 1 && hasClock(s)) {
      var hrs = Math.floor(ms / 3600000);
      return hrs < 1 ? "Just now" : hrs + "h ago";
    }
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
        } else if (ps[i].getAttribute("data-approx")) {
          // A DERIVED date, per core.is_trusted_date. Deliberately NOT relabelled "Added":
          // this shape covers both the scrape stamp AND _workday_date() converting a relative
          // "Posted 3 Days Ago", the two are indistinguishable once stored, and calling a
          // derived posting date an arrival date would be its own misattribution. The label
          // stays neutral and the tooltip carries the hedge, which is true of both.
          ps[i].textContent = rel;
          ps[i].title = "Approximate. This board publishes no exact posting date, so this is " +
            "derived either from a relative date it showed or from when we pulled the job (" + s + ").";
        } else {
          ps[i].textContent = rel;
          ps[i].title = (ps[i].getAttribute("data-verified") ? "Verified posting date · " : "Posted ") + s;
        }
      }
    }
  }

  // THE LOGO FALLBACK CHAIN IS GONE, 2026-08-22, and so is the essay that used to be here
  // about which favicon shard to request and how its 301 cached. The logos are ours now:
  // scripts/build_logos.py harvests them, judges them on their pixels and commits them to
  // static/logos/, and web.py::logo_url resolves the one URL server-side. A card either has a
  // logo we shipped or it renders the monogram, so there is no second URL to walk and no
  // wiring to do after render. That also deletes the double-fetch this file used to warn
  // about: with no LOGODEV_KEY set, src and data-fallback were the SAME string, so every
  // failing logo was requested twice before the <img> was removed.

  // Company name -> a link to that employer's page. Plain text when we're already ON that page
  // (a link to here is just a dead end) or when the row has no company.
  //
  // Escaping is not weakened by the anchor: encodeURIComponent runs FIRST, so it has already
  // percent-encoded &, ", ', < and > before H() ever sees the value, and esc() still owns the
  // visible label. No click handler is needed either — the feed's delegated handler returns early
  // on any <a>, so this navigates natively. Don't "fix" that with a stopPropagation: the same
  // branch is what auto-logs an Apply click.
  function companyLink(name) {
    if (!name) return esc("Unknown company");
    if (COMPANY) return esc(name);
    return '<a href="/company?c=' + H(encodeURIComponent(name)) + '">' + esc(name) + '</a>';
  }

  // THE BRAND MARK LEADS THE TOP ROW, AT CHIP SCALE. Owner's direction 2026-09-02 after seeing
  // the live feed: the mark should be top-left and small. Read the history before changing it
  // again, because the top-left slot is where it FAILED once -- alone in .cardtop at 36px tall
  // and up to 180px wide, which on a 294px card is 61% of the row for a wordmark like Walmart,
  // 51% for AMD, 49% for Centene, while the company's actual NAME sat two lines below. The logo
  // was doing the naming, at banner scale. It then spent a fortnight as a 20px chip on the
  // identity line.
  //
  // What makes the corner survivable is the CAP, not the position. 32px tall and 132px wide
  // (raised from 24/104 on 2026-09-03, at the owner's request) is 29% of a 452px card at its
  // widest -- a smaller share of the row than the old cap was of the old 294px card, because the
  // card grew when the feed went to three columns. The name still identifies the employer; the
  // mark is decoration next to it, which is why it is capped and why 15% of feed rows having no
  // logo at all costs nothing (measured 2026-09-02 over 42,180 rows).
  //
  // It shares the TITLE's line now (.cardlede), so a card without a logo is not short a row:
  // the title and the identity line set that height between them and the mark is centred
  // against the pair. That is what makes "image or nothing" still work here. It had a 24px row
  // of its own until 2026-09-03, which cost every card ~32px to say nothing.
  //
  // width/height are ATTRIBUTES, not an inline style, and they are the first consumer of
  // logo_ar: they reserve the box before the image loads, so the row does not reflow on a slow
  // connection. web.py::_build_row has shipped logo_ar since the harvest landed.
  //
  // THEY HAVE TO TRACK .cmark's CSS OR THEY ARE WORSE THAN NOTHING. These said 24/104 while the
  // stylesheet said 32/132, so every logo reserved a box 8px short and then grew into a
  // different one -- a guaranteed shift on load, in the exact place the attributes exist to
  // prevent one. If you change the height or the cap in .cmark, change both numbers here.
  //
  // IMAGE OR NOTHING -- deliberately no monogram twin here. A fixed slot would buy alignment
  // this row does not need (nothing sits under the mark that has to line up with it) and would
  // cost a grey chip on 15% of cards. The monogram stays where it has a fixed-width box and no
  // adjacent name at the same size: /companies tiles and the page headers.
  function companyMark(j) {
    if (!j.logo) return '<span class="company-initials" aria-hidden="true">' + H(j.initials || '') + '</span>';
    return '<img class="cmark" src="' + H(j.logo) + '" alt="" loading="lazy" decoding="async"' +
      ' height="32" width="' + Math.round(Math.min(32 * (j.logo_ar || 1), 176)) + '"' +
      (j.logo_mono ? ' data-mono="1"' : '') + '>';
  }

  // ONE FACT, ONE FIXED SLOT -- and the fixed part is the whole feature.
  //
  // The grid tracks are fixed and an unknown value renders an EMPTY cell rather than a shorter
  // row, so pay sits under pay the whole way down the feed and ten jobs can be compared without
  // reading ten cards. Collapse the empties instead and row 3's salary slides under row 2's
  // location, which is the ragged run-on identity line this replaced.
  //
  // `html` is ALREADY ESCAPED by the caller. The experience cell passes a <span> of its own
  // (see expIn below), so this cannot esc() here; every other call site runs esc() itself.
  //
  // NO ICON. Each cell carried a real emoji in a <span class="cfi"> until 2026-09-03 -- owner's
  // direction, removed. `kind` stays as a class on the cell rather than being deleted with the
  // glyph: it is what makes the four cells addressable in CSS and in test_card_meta's ordering
  // check, and it costs nothing now that it draws nothing.
  function factCell(kind, html, title) {
    if (!html) return '<span class="cfact cfact-blank"></span>';
    return '<span class="cfact ' + kind + '"' + (title ? ' title="' + H(title) + '"' : '') +
      '>' + html + '</span>';
  }

  // A REASON, WITHOUT THE NARRATOR. The stored sponsor_reason is written from the pipeline's
  // point of view -- "JD says no visa sponsorship", "JD offers visa sponsorship" -- which tells
  // the reader about our READING rather than about the job. Owner's direction 2026-09-03: state
  // the fact and stop.
  //
  // Rewritten HERE rather than at the source because the scorer stamps that string onto the row:
  // changing the wording in scraper/ would mean re-scoring the whole corpus to fix a label, and
  // every row already banked would keep the old phrasing until it was.
  function plainReason(s) {
    // Only the NARRATOR comes off. An earlier pass also ate the verb, which turned
    // "JD requires a security clearance" into "A security clearance" -- a shorter sentence that
    // says something else. "says"/"states" are the reporting verbs; everything after is the fact.
    s = String(s || '').replace(/^JD\s+(?:says|states)\s+/i, '').replace(/^JD\s+/i, '');
    return s ? s.charAt(0).toUpperCase() + s.slice(1) : '';
  }

  // The ring's own thresholds, in words, under the number.
  //
  // ABSOLUTE, NEVER A PERCENTILE -- CLAUDE.md: the feed SORTS on this number, so a relative
  // scale saturates and the top forty rows all read the same. 70/40 restates scoreRing's pair
  // rather than sharing a constant with it, deliberately: scoreRing is text-lifted out of this
  // file and asserted under node by test_card_meta, so it stays byte-for-byte untouched.
  //
  // The three guards mirror scoreCell's, in the same order: no resume, no readable JD, and not
  // scored yet each mean there is no ring to label, and a label under a dashed placeholder
  // would name a number that is not there.
  function matchLabel(j) {
    if (!HAS_RESUME || (j && (j.jd_unavailable || j.score_pending))) return '';
    var s = (j && j.score) || 0;
    return '<span class="cvlabel">' +
      (s >= 70 ? 'Strong Match' : s >= 40 ? 'Good Match' : 'Low Match') + '</span>';
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
    // Asks daysAgo() for a number instead of comparing relTime()'s words to "Today". The old
    // string test broke the instant relTime could answer "3h ago" for a same-day row — which is
    // now the common case for the 41% of dated rows that carry a clock — and it would have
    // broken SILENTLY, taking the badge off every fresh card.
    //
    // ...AND IT HAS TO HONOUR date_trusted, added 2026-09-08. Every other date surface on
    // this card hedges properly -- formatDates gives three distinct tooltips for verified,
    // derived and arrival-only -- and this one did not. On the 16.8% of active rows whose
    // date is OUR SCRAPE STAMP rather than a publisher date, a posting that had been open
    // on a dateless board for months wore a bare "New" on the day we first saw it, with no
    // tooltip, as the highest-contrast element in the row.
    //
    // The comment above already says "New means newly POSTED, and for a job with no
    // publisher date we do not know that" -- j.date just was not the right test for it.
    // core.is_trusted_date is the one definition and the server ships its answer.
    var d0 = daysAgo(j.date);
    var newFlag = (j.date_trusted && d0 !== null && d0 <= 0) ? '<span class="newflag">New</span>' : '';
    // ONE CHIP, AND IT IS THE ONLY ONE.
    //
    // A card could render eleven at once -- internship, visa route, no-lottery, agency, years,
    // sponsors/no-sponsorship, pay, remote, matched-on-description, repost, closed. Capping
    // that at three earlier the same day was not enough, and the screenshot that prompted this
    // says why: the three that survived the cap were "H-1B Likely, top sponsor to FY2025", "No
    // lottery" and "years not stated", which is a card spending its whole width telling you
    // what it does not know.
    //
    // A CHIP IS A VERDICT; A FACT GOES IN THE GRID. That distinction is what the cap was
    // always really about, and conflating the two is why the card lost facts along with the
    // noise. Pay, place and years are things the EMPLOYER stated -- they are ink in .cfacts
    // below, in fixed columns, and they cost the sponsorship question nothing. What stays
    // capped here is the VERDICT: one hedged claim about sponsorship, or silence.
    //
    // Still only on /job, because each needs a caveat that does not fit in a column:
    // internship, cap-exempt, agency, repost and matched-on-description.
    //
    // THREE STATES, TWO LABELS, AND SILENCE FOR THE THIRD. The posting rules sponsorship out,
    // or the employer has a federal filing record, or we have neither -- and the third gets NO
    // chip. core.py's own rule is "no route shown means no record, not a refusal", so calling
    // 2,020 rows "unlikely" on the strength of an empty index would be inventing a verdict we
    // do not hold. A missing chip already reads as "nothing known", and /job says it in words.
    //
    // NO ROUTE NAME. The chip used to read "H-1B Likely" / "Sponsor Likely" / "STEM-OPT
    // Likely". Which route an employer has filed for is a fact about the EMPLOYER, and /job
    // and /company both name all five; on a card it was a distinction the reader had to decode
    // before it meant anything. core.sponsor_likely still decides the KEY server-side and it
    // still runs on the tuple this posting's own text has already narrowed, so a JD that
    // closes a route can never produce a "likely" chip for it. Only the wording generalised.
    //
    // .spon / .nospon rather than a new pair of classes: both already exist, both are already
    // in the base chip rules, and their names mean exactly what these two chips now say.
    var badges = "";
    var vtop = j.visa_likely || "";
    if (j.sponsor_jd === "blocked") {
      badges += '<span class="nospon" title="' + H(plainReason(j.sponsor_reason)) +
        '">Sponsorship unlikely</span>';
    } else if (vtop || j.sponsor_jd === "open") {
      // THE STAR REPLACES A CLAUSE. This chip used to append ", top sponsor to FY2025", which
      // doubled its width on 30% of cards to carry one bit.
      //
      // The tooltip is three words as of 2026-09-03. It used to spend one sentence on the data
      // vintage and another disclaiming that a filing history describes the EMPLOYER and not
      // this posting -- both true, neither what somebody hovering a star is asking. The vintage
      // still appears once per PAGE, in the legend above the grid ("= top H-1B sponsor
      // (FY2025)"), which is where a window belongs; that legend reads sponsor_data_through()
      // straight from the template, so nothing here has to carry it.
      var top = (vtop === "h1b" && j.strength === "high");
      badges += '<span class="spon"' +
        (top ? ' title="Top H-1B sponsor"' : '') +
        '>Sponsorship likely' +
        (top ? ' <span class="topspon" aria-hidden="true">\u2605</span>' : '') + '</span>';
    }
    // NOT a fact chip -- the posting is dead. Closed rows are hidden from the default feed
    // (web.py::_filter_rows) but come back in Saved and Applied, and a card that says nothing
    // about it there is a lie rather than a tidy one.
    if (j.closed) badges += '<span class="closed">Closed</span>';
    // Some employers publish no posting date anywhere, so the card falls back to when the job
    // reached us. It carries data-added so formatDates() labels it "Added …" and styles it
    // apart — an approximate arrival date must never read as a posting date. The text starts
    // out as "Added <iso>" so there's no flash of a bare date before formatDates() runs.
    var posted = '';
    if (j.date) {
      // data-approx marks a DERIVED date so formatDates can hedge its tooltip instead of
      // announcing "Posted <our own scrape time>", which is the mislabelling
      // scraper/verify_dates.py's docstring complains about. It is also the only shape that
      // carries a clock, so it is the only one that can ever read "3h ago".
      posted = SEP + '<span class="posted" data-d="' + H(j.date) + '"' +
        (j.date_verified ? ' data-verified="1"' : '') +
        (j.date_trusted ? '' : ' data-approx="1"') + '>' + H(j.date) + '</span>';
    } else if (j.first_seen) {
      posted = SEP + '<span class="posted added" data-d="' + H(j.first_seen) +
        '" data-added="1">Added ' + H(j.first_seen) + '</span>';
    }
    // THE EXPERIENCE, AS INK RATHER THAN AS A CHIP. This is not the chip the block above
    // deleted and it must not become one: no background, no colour, no border — it joins the
    // identity line beside the location and the date, which is where facts ABOUT THE POSTING
    // live. The chip cap is about the sponsorship question and is untouched.
    //
    // IT SHOWS ALL THREE ANSWERS, and the third is the reason it exists. The filter help text
    // has been promising that unread postings "are marked years not stated on the card" ever
    // since fe62114 removed the badge that did it, so a reader filtering to "0 to 2 Years" had
    // no way to tell a posting that SAYS two years from one nobody could read. Measured on the
    // live corpus, 80% of what that filter returned was the second kind.
    // FOUR ANSWERS, NOT THREE, AND THE FOURTH IS THE ONE THIS GOT WRONG. "years not stated" is
    // a claim about the POSTING -- that the employer named no number. When exp_max_years is
    // NULL because nothing has ANALYSED the row yet, that claim is false, and the job page
    // proves it false in the same session: it reads the description live and prints "5+ years
    // required" under a card that just said the years were not stated. Measured over the 39,459
    // rows that hold a description, card and page disagreed on 21.5% of them.
    //
    // The two facts that separate the cases are already on the row and already drive the score
    // ring above -- jd_unavailable (this employer publishes nothing a server can read, so no
    // run will ever change it) and score_pending (we have not read this one YET). Saying "not
    // read yet" costs nothing and is true; saying "not stated" was a guess about somebody
    // else's job posting.
    // NOTHING AT ALL WHEN THERE IS NO NUMBER, at the owner's direction after seeing it live.
    // The three no-answer states each said something true -- "years not stated", "not read
    // yet", "unknown" -- and on a feed where most rows are one of them that is a column of
    // cards explaining what the app does not know instead of showing jobs. A missing figure
    // already reads as "no figure"; /job says which of the three it is, in words, for the one
    // posting the reader has decided to open.
    // THE TOOLTIP IS THE FACT, NOT AN ACCOUNT OF HOW WE GOT IT. Owner's direction 2026-09-03,
    // after hovering the years and being handed two sentences about what the posting states and
    // what 96% of similarly-titled postings do.
    //
    // The inferred case gets NO tooltip at all. Its visible words already say "senior role", and
    // the only thing a tooltip could add is the provenance the reader has just said they do not
    // want. That distinction is not lost, it is relocated: /job still separates "the employer
    // did not say" from "we read it off the title", in words, for the one posting somebody has
    // decided to open. Same bargain the chip cap already makes.
    var expTxt = '', expTitle = '';
    if (j.exp_src === 'stated') {
      expTxt = j.exp_eff + '+ yrs';
      expTitle = j.exp_eff + '+ years experience';
    } else if (j.exp_src === 'inferred') {
      // KEPT, because it is an ANSWER rather than an absence -- and it is the answer the
      // experience filter is acting on when it hides this row. Silence here would mean a job
      // vanishing from "0 to 2 Years" with nothing on the card to say why.
      expTxt = 'senior role';
    } else if (j.level === 'entry') {
      // ENTRY, added 2026-09-08, and it is the reason this cell exists at all now.
      // Measured on 2,575 stored product descriptions: of the 417 rows '0 to 2 Years'
      // correctly returns, 241 have no readable year count and this cell printed NOTHING,
      // so the reader fell back to the title -- which on those cards is wrong 95 times.
      // 'Product Manager II', 'Product Owner I' and four Capital One 'Senior Associate,
      // Product Manager' reqs are all entry-level by their own description.
      //
      // Same slot, same class, no tooltip -- the words say it. A verdict goes in
      // .cardverdict and a FACT gets a column (CLAUDE.md); the level a posting asks for is
      // the employer's, so it is ink here and adds no chip and no hue.
      expTxt = 'entry level';
    }
    // cexp-*, not exp-*: .exp-lo/.exp-mid/.exp-hi are the /job page's CHIP and carry a
    // background and a route colour. Reusing those names here would put a chip back on the card
    // by stylesheet accident, which is the thing this must not do.
    //
    // NO SEPARATE SENIORITY CELL, and that call still stands -- what changed on 2026-09-08 is
    // what goes IN this one. The reasoning below was right about the case it considered and
    // silent about the case that matters: "a column reading Entry Level beside one reading
    // 0+ yrs is a restatement" is true when the years are KNOWN, and 57.8% of the rows an
    // entry-level reader is shown have no year count at all. There is nothing to restate,
    // and the cell was blank. One cell, one answer, in whichever words it has.
    var expIn = expTxt
      ? '<span class="cexp cexp-' + (j.exp_src || j.level_src || 'inferred') + '">' + esc(expTxt) + '</span>'
      : '';
    var applyHref = /^https?:\/\//i.test(j.apply_url || "") ? j.apply_url : "#";
    var cls = "card" + (j.closed ? " is-closed" : "");
    // THE ROUTE WASH. One attribute; the whole card is tinted in CSS.
    //
    // Reads the SAME field the chip's label came from, so the colour and the words are one
    // fact and cannot drift. They used to be derived separately from a ranked list that looked
    // personalised and wasn't: with no Sponsorship filter set rankVisa returned early and
    // vt[0] was just the first route in VISA_TAGS order, an undocumented precedence; and with
    // a filter set visaHit had already NARROWED the feed to rows carrying that route, so it
    // was hoisted onto every survivor and the whole grid turned one colour. Neither state
    // carried information.
    //
    // A JD that rules sponsorship out wins over the employer's filing history, because for
    // THIS posting the route is shut no matter what the company has filed before. Anything
    // with no record at all gets the quiet grey, never red: core.py documents absence as
    // "no record, not a refusal", and it is a routine fact here, not an error.
    //
    // One deliberate exception, do not "fix" it: a posting blocked with a generic "no visa
    // sponsorship" keeps stem_opt, so the chip reads STEM-OPT Likely on a grey blocked card.
    // That is right, and the adjacent "No sponsorship" chip makes the pair read as
    // "no sponsorship, but STEM-OPT".
    var route = j.sponsor_jd === "blocked" ? "blocked" : (vtop || "none");
    return '<article class="' + cls + '"' +
      ' data-route="' + H(route) + '"' +
      ' data-url="' + H(j.url) + '" data-status="' + H(st) + '">' +
      // "New" sits INSIDE the top row rather than hanging off the card's top edge as it used
      // to. Two reasons: an overhanging sticker is the one bit of card furniture that read as
      // decoration rather than as data, and the paint containment that makes a long grid cheap
      // to scroll would have clipped it.
      //
      // Employer and match share the header; the title spans the card below it.
      '<div class="cardbody">' +
      '<div class="cardlede">' + companyMark(j) + newFlag +
        '<div class="cardmatch">' + scoreCell(j) + matchLabel(j) + '</div></div>' +
      // A REAL LINK to a real page. It was a <button> that opened the modal, which was itself a
      // fix for the card being an <article> with a click handler and no tabindex — the feed used
      // to be mouse-only. An <a href> keeps all of that and adds what a button structurally
      // cannot: middle-click, ctrl-click, Open in New Tab, a status-bar preview of where you are
      // about to go, and a target the browser's own history can return to. The #feed delegate's
      // existing early return for <a> (below) means navigation is native and no JS runs.
      '<a class="ctitle" href="/job?u=' + H(encodeURIComponent(j.url)) + '">' +
        esc(j.title) + '</a>' +
      // TWO parts, not one inline run. Identity (company, place, when) is a single line that
      // truncates; the chips are a wrapping row below it.
      //
      // As one run it broke badly: employers write locations like "Minneapolis Minnesota
      // United States of America", so the row went to three lines, the sponsorship chips —
      // the entire point of the card — were pushed past where the eye stops, and a wrap
      // could leave a line starting with a bare separator dot. Splitting it means the
      // identity line can never take more than one line and the chips can never be pushed
      // down by a verbose employer.
      '<div class="cmeta">' +
        '<div class="cident">' + companyLink(j.company) + posted + '</div>' +
      '</div>' +
      // THE FACT GRID. Four fixed tracks: place, work mode, pay, years. Every value here was
      // already built by web.py::_build_row and already shipped in the feed JSON -- salary_label
      // and remote had simply never been drawn by anything, and location and the years moved out
      // of the run-on identity line above so they could line up with their neighbours.
      //
      // FOUR, NOT SIX. Jobright's grid also carries an employment type and a three-state work
      // mode; we hold neither. There is no full-time/part-time field anywhere in this corpus,
      // and `remote` is a BOOLEAN -- core.parse_location drops "hybrid" and "onsite" as noise
      // tokens when it picks a city -- so a third state would have to be invented. An empty
      // column is honest; a fabricated one is not.
      // ORDER IS BY COVERAGE, NOT BY IMPORTANCE, because the grid is two cells wide and a
      // blank holds its slot. Measured over 120 live rows: location 98%, years 91%, pay 41%,
      // remote 17%. Putting the two near-universal facts on the FIRST line means the common
      // card -- 52% of them carry exactly two -- reads as one tidy line with an empty line
      // under it. Ordered place/remote/pay/years instead, that same card put its location top
      // left and its years bottom RIGHT, diagonally opposite across two blanks.
      '<div class="cfacts">' +
        factCell('cf-loc', esc(j.location || 'Location not stated'), j.location || '') +
        factCell('cf-exp', expIn, expTitle) +
        factCell('cf-pay', esc(j.salary_label || ''),
                 j.salary_label ? 'Pay range stated in this posting.' : '') +
        // "Remote" or nothing, never "Onsite": absence here means the posting did not say so,
        // which is not the same claim. Same rule core.py already applies to a missing visa
        // route -- no record is not a refusal.
        factCell('cf-rem', j.remote ? 'Remote' : '',
                 j.remote ? 'This posting is remote.' : '') +
      '</div>' +
      (badges ? '<div class="cbadges card-sponsorship">' + badges + '</div>' : '') +
      '<div class="cardact">' +
        '<a class="btn primary sm" href="' + H(applyHref) + '" target="_blank" rel="noopener" data-apply="1">Apply<span class="ic ic-external" aria-hidden="true"></span></a>' +
        '<a class="btn sm" href="/brain/tailor?job=' + encodeURIComponent(j.url) + '">Tailor</a>' +
        '<span class="spacer"></span>' +
        // No title= here: each button's visible text already IS the tooltip, and this block
        // renders once per card, so the duplication was three tooltips on every row of the feed.
        '<span class="acts">' +
          '<button class="ico" data-act="liked">' + (st === 'liked' ? 'Saved' : 'Save') + '</button>' +
          '<button class="ico" data-act="applied">' + (st === 'applied' ? 'Applied' : 'Mark applied') + '</button>' +
          '<button class="ico" data-act="hidden">' + (st === 'hidden' ? 'Hidden' : 'Hide') + '</button>' +
        '</span>' +
      '</div>' +
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
  // TWIN of core.roles_match(). Keep them together -- scripts/feed_parity.py lifts this one by
  // source text and diffs it against the server's, row for row.
  function roleHit(j, wanted) {
    // Declared INSIDE the function on purpose: feed_parity.py lifts these twins by function
    // name, so anything this one leans on from an outer scope is simply absent in the driver
    // (it failed with "DELIVER_ROLES is not defined" the first time). Self-contained or untested.
    var DELIVER_ROLES = ["consultant", "coordinator", "delivery", "pm", "product", "program", "scrum", "transform"];
    if (!wanted.length) return true;
    var have = j.roles || [];
    for (var i = 0; i < have.length; i++)
      if (wanted.indexOf(have[i]) >= 0) return true;
    // A job kept on its DESCRIPTION has no family, because families are read off the title and
    // its title is precisely why it needed rescuing. The rule that admitted it did establish
    // that it is delivery work, so it answers a selection drawn entirely from that group -- and
    // only then. See core.roles_match for the full reasoning.
    if (j.jd_admit) {
      for (var k = 0; k < wanted.length; k++)
        if (DELIVER_ROLES.indexOf(wanted[k]) < 0) return false;
      return true;
    }
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
  // ROLE PRIORITY — the twin of core.ROLE_PRIORITY, in the owner's order: project, then
  // product, then program, then the rest. scripts/feed_parity.py asserts the two lists are
  // identical rather than trusting anyone to keep them so.
  var ROLE_PRIORITY = ["pm", "product", "program", "coordinator", "scrum", "delivery",
    "transform", "consultant", "ba", "ops", "dataanalyst", "supply", "finance",
    "datasci", "dataeng", "ml",
    "swe", "engmgr", "devops", "qa", "systems", "apps", "network", "security"];
  var ROLE_RANK = {};
  for (var _ri = 0; _ri < ROLE_PRIORITY.length; _ri++) ROLE_RANK[ROLE_PRIORITY[_ri]] = _ri;
  var ROLE_RANK_NONE = ROLE_PRIORITY.length;
  // Mirror of core.role_rank(): a row's BEST role, and last when it matched none.
  function roleRank(roles) {
    var best = ROLE_RANK_NONE;
    for (var i = 0; i < (roles || []).length; i++) {
      var r = ROLE_RANK[roles[i]];
      if (r !== undefined && r < best) best = r;
    }
    return best;
  }
  // Mirror of web.py _row_date_num(). localeCompare on the string would order identically on
  // its own, but the comparator now needs date as ONE TERM among three.
  function rowDateNum(j) {
    var s = rowDate(j);
    if (s.length >= 10 && s.charAt(4) === "-" && s.charAt(7) === "-") {
      var n = parseInt(s.slice(0, 4) + s.slice(5, 7) + s.slice(8, 10), 10);
      return isNaN(n) ? 0 : n;
    }
    return 0;
  }
  // The one comparator, so the server twin has exactly one thing to match. Lifted by name into
  // scripts/feed_parity.py (JS_FUNCS) rather than re-typed there — a hand-copied third version
  // is how these drift. Mirror of web.py _sort_key(): role rank ranks WITHIN the chosen sort,
  // never over it, so "Newest" still means newest and a 90% match still beats an 80% one.
  function sortCmp(a, b, sortBy) {
    if (sortBy === "newest") {
      var d = rowDateNum(b) - rowDateNum(a);
      if (d) return d;
      var rn = roleRank(a.roles) - roleRank(b.roles);
      return rn || ((b.score || 0) - (a.score || 0));
    }
    if (sortBy === "sponsor") {
      var ra = sponsorRank(a), rb = sponsorRank(b);
      for (var i = 0; i < ra.length; i++) if (ra[i] !== rb[i]) return ra[i] - rb[i];
      return 0;
    }
    var s = (b.score || 0) - (a.score || 0);
    if (s) return s;
    var rr = roleRank(a.roles) - roleRank(b.roles);
    return rr || (rowDateNum(b) - rowDateNum(a));
  }
  // Mirror of web.py _break_employer_runs(). Both constants are read out of this file by
  // scripts/feed_parity.py and compared with web.py's, because a cap that disagrees across the
  // 4,000-row inline/paged boundary would reorder the feed the moment the corpus grew.
  var EMPLOYER_RUN_MAX = 2;
  var EMPLOYER_RUN_LOOKAHEAD = 200;
  function breakEmployerRuns(rows, cap, lookahead) {
    cap = cap === undefined ? EMPLOYER_RUN_MAX : cap;
    lookahead = lookahead === undefined ? EMPLOYER_RUN_LOOKAHEAD : lookahead;
    var n = rows.length;
    if (cap <= 0 || n < cap + 2) return rows;
    var used = new Uint8Array(n), out = [], last = null, run = 0, i = 0;
    while (out.length < n) {
      while (i < n && used[i]) i++;
      if (i >= n) break;
      var pick = i;
      if (run >= cap && (rows[i].company || "") === last) {
        var j = i + 1, seen = 0;
        while (j < n && seen < lookahead && (j - i) <= lookahead) {
          if (!used[j]) {
            seen++;
            if ((rows[j].company || "") !== last) { pick = j; break; }
          }
          j++;
        }
      }
      used[pick] = 1;
      var emp = rows[pick].company || "";
      run = emp === last ? run + 1 : 1;
      last = emp;
      out.push(rows[pick]);
    }
    return out;
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
  // TWIN of core.visa_tags_match -- scripts/feed_parity.py diffs the two row for row.
  // ABSENCE IS NOT A REFUSAL: an empty tag set is either "no federal filing record"
  // (silence, 10.4% of active rows and 26 of them cap-exempt employers, which are the
  // best H-1B route there is) or "this JD rules sponsorship out" (the employer
  // answering, 23.8%, and hidenospon is the control for it). Only the second is a no.
  function visaHit(j, wanted) {
    if (!wanted || !wanted.length) return true;
    var have = j.visa || [];
    for (var i = 0; i < wanted.length; i++)
      if (have.indexOf(wanted[i]) !== -1) return true;
    return !(have.length || j.sponsor_jd === "blocked");
  }
  // ---- search: typo-tolerant matching + relevance ----
  // TWIN ALERT: web.py has byte-for-byte equivalents (searchHit / searchRank / _within), and
  // scripts/feed_parity.py runs both over the same corpus. Change one, change the other.
  // A function, not a module-level const: feed_parity.py lifts app.js's pure functions by
  // source text, and a bare `var RE = /.../` would not travel with them.
  function searchSplit(s) { return s.split(/[^a-z0-9+#.]+/).filter(Boolean); }
  // Damerau (optimal string alignment), not plain Levenshtein: an adjacent SWAP is the commonest
  // typo and plain Levenshtein charges two edits for it, so "anaylst" would never have reached
  // "analyst" at a 7-character term's tolerance of 1. Bounded, with an early exit.
  function _within(a, b, k) {
    var la = a.length, lb = b.length, i, j;
    if (Math.abs(la - lb) > k) return false;
    if (a === b) return true;
    var inf = k + 1, prev2 = null, prev = [], cur;
    for (j = 0; j <= lb; j++) prev[j] = j;
    for (i = 1; i <= la; i++) {
      cur = [];
      for (j = 0; j <= lb; j++) cur[j] = inf;
      cur[0] = i;
      var lo = Math.max(1, i - k), hi = Math.min(lb, i + k), best = inf;
      for (j = lo; j <= hi; j++) {
        var cost = a.charAt(i - 1) === b.charAt(j - 1) ? 0 : 1;
        var v = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost);
        if (i > 1 && j > 1 && a.charAt(i - 1) === b.charAt(j - 2) && a.charAt(i - 2) === b.charAt(j - 1))
          v = Math.min(v, prev2[j - 2] + 1);  // the transposition
        cur[j] = v;
        if (v < best) best = v;
      }
      if (best > k) return false;             // no cell on this row can still reach k
      prev2 = prev; prev = cur;
    }
    return prev[lb] <= k;
  }
  function searchTol(term) {                  // typos forgiven, by length. Under 4: none.
    var n = term.length;
    return n >= 8 ? 2 : (n >= 4 ? 1 : 0);
  }
  // Shared by searchHit (does this row match?) and searchRank (where does it go?), so the two can
  // never disagree about what counted as a match.
  // _within is pure and its arguments repeat enormously: across a whole corpus the same handful
  // of title words is compared against the same query term thousands of times. Bounded, and
  // cleared wholesale rather than evicted one by one — the contents are worthless once cold.
  // The cache hangs off the function itself and is created on first call, so this stays a single
  // self-contained declaration — feed_parity.py lifts app.js's pure functions by source text, and
  // a companion `var CACHE = {}` outside the function would not travel with it.
  function _withinMemo(a, b, k) {
    if (!_withinMemo.c) { _withinMemo.c = Object.create(null); _withinMemo.n = 0; }
    var key = a + " " + b + " " + k, hit = _withinMemo.c[key];
    if (hit === undefined) {
      if (_withinMemo.n >= 60000) { _withinMemo.c = Object.create(null); _withinMemo.n = 0; }
      hit = _withinMemo.c[key] = _within(a, b, k);
      _withinMemo.n++;
    }
    return hit;
  }
  function termHit(hay, words, term) {
    if (hay.indexOf(term) !== -1) return true; // covers prefixes and infixes for free
    var tol = searchTol(term);
    if (!tol) return false;
    for (var w = 0; w < words.length; w++) {
      var word = words[w];
      // Length window is free (edit distance is at least the length difference); the first-letter
      // gate trades forgiving a typo in character one — the rare case — for removing ~95% of
      // candidate words before any distance is computed.
      if (word.charAt(0) !== term.charAt(0)) continue;
      if (Math.abs(word.length - term.length) > tol) continue;
      if (_withinMemo(word, term, tol)) return true;
    }
    return false;
  }
  // `words` is the haystack already tokenised. matches() passes a per-row cache; everyone else
  // omits it and it is computed LAZILY, so a correctly spelled query — where every term is a
  // plain substring — never tokenises anything at all.
  function searchHit(hay, q, words) {
    if (!q) return true;
    if (hay.indexOf(q) !== -1) return true;   // phrase match: the old behaviour, still first
    var terms = searchSplit(q);
    if (!terms.length) return false;
    if (words === undefined) words = null;
    for (var i = 0; i < terms.length; i++) {
      if (hay.indexOf(terms[i]) !== -1) continue;
      if (words === null) words = searchSplit(hay);
      if (!termHit(hay, words, terms[i])) return false;
    }
    return true;
  }
  // BAND says where the query was answered (title phrase > title words > employer/city > typo
  // only); COVERAGE inside the band says how much of the title the query explains, which is what
  // keeps "Senior Data Scientist" above "Staff Scientist - Real World Evidence and Data" for
  // "data scientst". Integer floor division, not rounding: the Python twin has to produce the
  // identical number and the two languages round halves differently.
  function searchRank(j, q) {
    if (!q) return 0;
    var title = (j.title || "").toLowerCase();
    var hay = title + " " + (j.company || "").toLowerCase() + " " + (j.location || "").toLowerCase();
    var terms = searchSplit(q), twords = searchSplit(title), i, band = 0;
    if (title.indexOf(q) !== -1) band = 4;
    else {
      var allTitle = terms.length > 0;
      for (i = 0; i < terms.length; i++) if (!termHit(title, twords, terms[i])) { allTitle = false; break; }
      if (allTitle) band = 3;
      else if (hay.indexOf(q) !== -1) band = 2;
      else {
        var allHay = terms.length > 0;
        for (i = 0; i < terms.length; i++) if (hay.indexOf(terms[i]) === -1) { allHay = false; break; }
        band = allHay ? 1 : 0;
      }
    }
    var cov = 0;
    if (twords.length && terms.length) {
      var m = 0;
      for (var w = 0; w < twords.length; w++) {
        for (i = 0; i < terms.length; i++) {
          if (termHit(twords[w], [twords[w]], terms[i])) { m++; break; }
        }
      }
      cov = Math.min(Math.floor(m * 100 / twords.length), 99);
    }
    return band * 100 + cov;
  }
  // CSRF. The server now requires a token on cookie-authenticated state changes; forms carry a
  // hidden _csrf field, and these fetch() calls send the same value as a header. Read from the
  // meta tag in base.html because this file is static and cannot be templated.
  function csrfToken() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute("content") : "";
  }

  function matches(j, cut, ignoreMin) {
    var st = j.status || "", sc = j.score || 0, ok;
    var searching = q && q.value.trim();
    if (tab === "liked") ok = st === "liked";
    else if (tab === "applied") ok = st === "applied";
    else if (tab === "hidden") ok = st === "hidden";
    // An active SEARCH bypasses the match filter: if you typed "deloitte" you want to
    // SEE Deloitte's jobs, not have them hidden because they score 40%.
    // HIDDEN AND APPLIED both drop off this tab -- twin of web.py::_filter_rows, which carries
    // the reasoning. `liked` stays: saving something is a reason to keep seeing it.
    else ok = (st !== "hidden" && st !== "applied") && (searching || ignoreMin || sc >= minVal);
    // Search covers LOCATION too — "boston" and "remote" are things people type in here.
    if (ok && searching) {
      // Cached on the row: DATA outlives every keystroke, so the searchable text and its tokens
      // are built once per job rather than once per character typed.
      if (j._sh === undefined) {
        j._sh = ((j.title || "") + " " + (j.company || "") + " " + (j.location || "")).toLowerCase();
        j._sw = searchSplit(j._sh);
      }
      ok = searchHit(j._sh, q.value.toLowerCase().trim(), j._sw);
    }
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
    // "Only postings whose experience we could read." Twin of _filter_rows' exp_stated clause;
    // off by default. Drops rows with NO answer (exp_src ""), so a seniority read off the title
    // survives it. See core.DEFAULT_PREFS.expstated.
    if (ok && expStated && expStated.checked && !j.exp_src) ok = false;
    // THREE ANSWERS, and they are different questions. Mirrors web._filter_rows exactly --
    // scripts/feed_parity.py diffs the two row for row.
    //
    // The numeric values are a CEILING on what the employer demands ("could I be
    // considered"). exp_eff is the highest year count the DESCRIPTION states, or the floor the
    // TITLE implies when it states none, so "8+ years required; 2 years of SQL preferred" is
    // an 8-year job. A job with NEITHER signal is ALWAYS kept.
    //
    // "entry" is a LEVEL and cannot be written as a ceiling: measured, "0 to 2 Years" returns
    // 417 product rows and only 176 are entry-level by their own description.
    //
    // An INTERNSHIP is exempt from the ceiling outright -- 4 of 43 internship-titled product
    // rows were dropped by "0 to 2 Years" because their text names 3 years somewhere.
    //
    // The inference itself is server-side in web._build_row, so this stays one comparison.
    if (ok && expSel && expSel.value !== "any") {
      if (expSel.value === "entry") {
        if ((j.level || "") !== "" && (j.level || "") !== "entry") ok = false;
      } else if (!j.intern) {
        var ev = j.exp_eff;
        if (ev !== "" && ev != null) {
          var yrs = parseInt(ev, 10);
          if (!isNaN(yrs)) {
            if (expSel.value === "senior") { if (yrs >= 6) ok = false; }
            else if (yrs > (parseInt(expSel.value, 10) || 99)) ok = false;
          }
        } else {
          // No year count anywhere, so the LEVEL answers the ceiling — the same thing the
          // "entry" branch above does with the same field. Twin of web._filter_rows and
          // core.prefs_match; the long note at the server copy has the measurement.
          var floor = LEVEL_MIN_YEARS[j.level || ""];
          var ceil = expSel.value === "senior" ? 5 : (parseInt(expSel.value, 10) || 99);
          if (floor !== undefined && floor > ceil) ok = false;
        }
      }
    }
    return ok;
  }
  // ---- arrival: skeletons, then a staggered fade for whatever just landed ----------------
  //
  // TWIN OF templates/_feedgrid.html's skeleton block, and the same eight tiles. The server
  // renders those into #feed so they paint with the HTML; the first innerHTML of any render
  // path then wipes them and they never come back. That left every LATER wait -- switching to
  // Saved, moving the match slider, typing in the search box -- showing one small spinner
  // instead, which is a weaker signal than the one the page opened with. Now every reset
  // render puts the same skeletons back.
  //
  // Kept as a string here rather than cloned from the DOM: by the time a filter changes, the
  // originals have been gone since first paint, so there is nothing left to clone.
  var SKEL_N = 8;
  function skeletonHTML() {
    return new Array(SKEL_N + 1).join(
      '<div class="skel" aria-hidden="true"><div class="skel-lines">' +
      '<span class="skel-bar w70"></span><span class="skel-bar w40"></span>' +
      '<span class="skel-bar w55"></span></div><div class="skel-chip"></div></div>');
  }

  // How many cards may stagger before the delay stops growing. Without the cap the 60th card
  // of a page waits 60 x 18ms = ~1.1s to appear, so the feature that was meant to make the
  // feed feel quicker would make the bottom of it visibly slower than no animation at all.
  var CARD_IN_MAX = 10;
  function animateIn(from) {
    var cs = feed.querySelectorAll(".card");
    for (var i = from; i < cs.length; i++) {
      cs[i].style.animationDelay = (Math.min(i - from, CARD_IN_MAX) * 18) + "ms";
      cs[i].classList.add("card-in");
    }
  }

  // The Load-more button's own busy state. .btn.is-loading already exists in style.css with
  // its spinner, its reduced-motion handling and pointer-events:none -- which is also what
  // stops a second click queueing a second page. aria-busy is the half a screen reader gets.
  function moreLoading(on) {
    if (!moreBtn) return;
    moreBtn.classList.toggle("is-loading", !!on);
    if (on) moreBtn.setAttribute("aria-busy", "true");
    else moreBtn.removeAttribute("aria-busy");
  }

  function renderLocal(reset) {
    if (reset) limit = PAGE;
    var cut = dateCutoff(), matched = [];
    for (var i = 0; i < DATA.length; i++) if (matches(DATA[i], cut)) matched.push(DATA[i]);
    matched.sort(function (a, b) { return sortCmp(a, b, sortBy); });
    // RELEVANCE FIRST while a search is active, the chosen sort within each band. A separate
    // stable pass rather than a compound comparator, so the sort you picked still fully decides
    // the order inside a band and this costs nothing when the box is empty. Array.prototype.sort
    // is stable (ES2019), which is what makes the two-pass form equal to a compound key.
    var qs = q && q.value.trim().toLowerCase();
    if (qs) matched.sort(function (a, b) { return searchRank(b, qs) - searchRank(a, qs); });
    // LAST, after every sort including the relevance pass — the only rule here about the shape
    // of the list rather than the merit of a row. The server applies it at the end of
    // _filter_rows for the paged feed; this is the same rule for the inline one.
    matched = breakEmployerRuns(matched);
    // One row = one card, so `limit` paginates jobs directly.
    var slice = matched.slice(0, limit), html = "";
    for (var k = 0; k < slice.length; k++) html += cardHTML(slice[k]);
    // This path re-renders the WHOLE slice even on Load more, so every node is new and
    // animateIn(0) would re-flash the cards already on screen. The previous page boundary is
    // limit - PAGE, because the click handler grows `limit` before calling render(false).
    var localFrom = reset ? 0 : Math.max(0, limit - PAGE);
    feed.innerHTML = html;
    animateIn(localFrom);
    moreLoading(false);
    formatDates();
    setCount(matched.length);
    if (!matched.length) renderEmpty(null);
    // Only when the reader is short of results -- the same threshold the server
    // uses (web._RELAX_WHEN_UNDER). Above it the panel would be nagging.
    if (matched.length && matched.length < RELAX_WHEN_UNDER)
      renderRelaxNote(localRelax(DATA, 2), matched.length);
    else if (matched.length) setShown(emptyEl, false);
    else setShown(emptyEl, !matched.length);
    if (moreBtn) {
      setShown(moreBtn, matched.length > limit);
      if (matched.length > limit) moreBtn.textContent = "Load more (" + (matched.length - limit) + " more)";
    }
  }

  // rankVisa() and its _wantVisa cache lived here. Both are gone: the card names ONE route now
  // and the server chooses it (core.sponsor_likely), so there is no list left to re-order. The
  // hoisting it did was also never worth what it looked like — see the data-route comment in
  // cardHTML for why it carried no information in either of its two states.

  // Highlight any control set to a non-default value (so active filters are obvious at a glance).
  function setFlag(el, on) { if (el) el.classList.toggle("fset", !!on); }

  // How many NARROWING filters are on: the ones behind the "Filters" chip. Search, track and
  // sort are excluded because they START a search rather than narrow one, and they stay visible
  // at every width. The count is what stops a closed popover from hiding that filters are on,
  // which is the bug any disclosure invites.
  // A search deliberately BYPASSES the match floor (web.py: "search bypasses the match floor"),
  // because typing a company name should find it whatever it scores. That decision stands. The
  // defect was that nothing said so: with a search active the screen showed a slider reading
  // "MATCH 50%+", a "Filters 3" badge counting it, and a grid of cards at 30%, 28%, 26%, 24%.
  // The user's stated minimum was silently not in force. So while `q` is non-empty the control
  // stands DOWN visibly -- greyed, relabelled, and not counted in the badge.
  function searchOverridesMin() {
    return !!(q && q.value.trim());
  }
  function syncMinStandDown() {
    var off = searchOverridesMin();
    var wrap = minR && minR.closest ? minR.closest(".matchfilter") : null;
    if (wrap) wrap.classList.toggle("stood-down", off);
    if (minR) minR.setAttribute("aria-disabled", off ? "true" : "false");
    if (minLab) {
      var v = minR ? (parseInt(minR.value, 10) || 0) : 0;
      minLab.textContent = off ? "Off while searching" : (v === 0 ? "Any" : v + "%+");
    }
  }
  function activeFilterCount() {
    var n = 0;
    if (minVal > 0 && !searchOverridesMin()) n++;
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
    if (expStated && expStated.checked) n++;
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
    // Same reason as the segmented buttons above: Clear, the seeded prefs and the remembered
    // blob all write to #intern directly, and the boxes have to follow or they would show a
    // job type the feed is no longer filtering on.
    syncJobTypeBoxes();
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
  // BOTH NUMBERS ARE WRITTEN HERE, from one value, in one tick. The filter panel's primary
  // button ("Show N jobs") used to be filled by syncChips copying countEl's TEXT -- and
  // syncChips runs synchronously at the end of render(), while countEl is only written later
  // inside the /api/feed .then(). So the button showed the PREVIOUS query's count, every time,
  // on every paged install (this one has 27,524 jobs against a _FEED_INLINE_MAX of 4000, so it
  // is always paged). Measured: searching "product manager" returned 1293 jobs while the button
  // still read "Show 13 jobs" -- a 100x under-report on the control the user acts on.
  // Writing them together makes the pair structurally unable to disagree.
  function setCount(n) {
    if (countEl) countEl.textContent = n;
    if (countTot) countTot.hidden = !TOTAL_ALL || n === TOTAL_ALL;
    var pc = document.getElementById("popcount");
    if (pc) pc.textContent = n;
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
    syncMinStandDown();          // the match control follows the search box -- see U5
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

    // #popcount is written by setCount(), not copied out of the DOM here -- see the note there.
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
    expstated: function () { if (expStated) expStated.checked = false; },
    roles: function () {
      if (rolesSel) rolesSel.value = "";
      var rb = document.querySelectorAll(".rolepick input[data-role]");
      for (var r = 0; r < rb.length; r++) rb[r].checked = false;
      document.dispatchEvent(new CustomEvent("roles:sync"));   // let rolepick.js relabel
    },
    track: function () { if (trackSel) trackSel.value = "any"; },
    // Reachable from a relax suggestion as well as from Clear all -- see the note on the Clear
    // button for why the search box counts as a filter here.
    q: function () { if (q) q.value = ""; }
  };
  // What "nothing here" MEANS depends on which tab you are on, and the generic fallback did not
  // know. On Saved with nothing saved it rendered "No jobs match these filters" over a "Clear
  // all filters" button -- both wrong: the cause is an empty list, not a filter, and clearing
  // every filter provably still yields zero (confirmed live: the chips cleared and the identical
  // message and button remained). That is a dead end offering a remedy that cannot work, which
  // is the exact failure _relax_suggestions was written to eliminate. The relax machinery
  // correctly produced nothing here -- there is no filter that can be relaxed into existence --
  // and the fallback took over without knowing which tab it was on.
  var TAB_EMPTY = {
    liked: ["You haven't saved any job yet.",
            "Tap Save on a card and it will show up here."],
    applied: ["You haven't marked any job as applied yet.",
              "Mark one applied and it moves here, and out of Recommended."],
    hidden: ["You haven't hidden any job.",
             "Hiding one takes it out of Recommended and parks it here."]
  };
  // The relax sentence and its buttons, built once. renderEmpty uses it for the zero case
  // and renderRelaxNote for the short-of-results case; two copies of this markup would be
  // two places for the data-relax contract to drift from the click handler below.
  function relaxBody(relax, lead) {
    var h = '<p class="empty-h">' + esc(lead) + '</p><p>';
    for (var i = 0; i < relax.length; i++)
      h += (i ? ' ' : '') + 'Removing ' + esc(relax[i].label) + ' would show ' +
           relax[i].n.toLocaleString() + ' job' + (relax[i].n === 1 ? '' : 's') + '.';
    h += '</p><div class="empty-acts">';
    for (var k = 0; k < relax.length; k++)
      h += '<button type="button" class="btn sm" data-relax="' + esc(relax[k].key) +
           '">Remove ' + esc(relax[k].label) + '</button>';
    return h + '<button type="button" class="btn sm ghost" data-relax="*">' +
           'Clear all filters</button></div>';
  }
  // SHORT OF RESULTS IS NOT THE SAME AS HAVING NONE, and the app only ever handled the
  // second. Measured 2026-09-08: the default match floor left 34 of 328 entry-level
  // product postings and the reader was told nothing about the other 294 -- the most
  // consequential control in the app was silent in the case that actually happens.
  //
  // #empty is a sibling of #feed, so this renders UNDER the cards rather than instead of
  // them. That is the difference between a dead end and an offer.
  function renderRelaxNote(relax, shown) {
    if (!emptyEl) return;
    if (!relax || !relax.length) { setShown(emptyEl, false); return; }
    emptyEl.innerHTML = relaxBody(
      relax, 'Only ' + shown.toLocaleString() + ' job' + (shown === 1 ? '' : 's') +
      ' match these filters.');
    setShown(emptyEl, true);
  }
  // renderLocal filters in the browser and has no server response to read, so it reuses
  // whatever relax set the last /api/feed answer carried. Null on the inline path, which
  // is correct: below _FEED_INLINE_MAX the browser has the whole corpus and the note would
  // be guessing.
  // Kept in step with web._RELAX_WHEN_UNDER by scripts/feed_parity.py, which lifts this
  // file by source text.
  var RELAX_WHEN_UNDER = 30;
  // WHAT EACH CONTROL IS CALLED when we offer to drop it. Mirrors the third element of
  // web._RELAX so the server-rendered and client-rendered panels read identically; the
  // label describes the VALUE, not the control, because "Removing Past 30 days" is
  // actionable where "Removing the date filter" makes the reader go and look.
  var RELAX_LABEL = {
    min: function (v) { return v + '% match minimum'; },
    date: function (v) {
      return ({ "1": "Past 24 hours", "7": "Past 7 days", "30": "Past 30 days",
                "90": "Past 90 days" })[v] || v;
    },
    minsal: function () { return "the pay minimum"; },
    exp: function () { return "the experience filter"; },
    expstated: function () { return "Hide postings with no experience answer"; },
    intern: function () { return "the internship filter"; },
    visatags: function () { return "the visa route filter"; },
    hidenospon: function () { return "Hide no-sponsorship"; },
    verifiedonly: function () { return "Confirmed posting date"; },
    hideagency: function () { return "Hide staffing agencies"; },
    roles: function () { return "the role filter"; },
    track: function () { return "the career track"; },
    q: function (v) { return '\u201c' + v + '\u201d'; }
  };
  // The DOM control behind each key, and what its default value looks like, so we can tell
  // "set" from "untouched" without a second source of truth for the defaults.
  function relaxState() {
    return {
      min: [minR, minR && minR.value, "0"],
      date: [dateSel, dateSel && dateSel.value, "any"],
      minsal: [minSalSel, minSalSel && minSalSel.value, ""],
      exp: [expSel, expSel && expSel.value, "any"],
      intern: [internSel, internSel && internSel.value, "any"],
      track: [trackSel, trackSel && trackSel.value, "any"],
      roles: [rolesSel, rolesSel && rolesSel.value, ""],
      visatags: [visaSel, visaSel && visaSel.value, ""],
      q: [q, q && q.value.trim(), ""],
      expstated: [expStated, expStated && expStated.checked ? "1" : "", ""],
      hidenospon: [hideNo, hideNo && hideNo.checked ? "1" : "", ""],
      verifiedonly: [verifiedOnly, verifiedOnly && verifiedOnly.checked ? "1" : "", ""],
      hideagency: [hideAgency, hideAgency && hideAgency.checked ? "1" : "", ""]
    };
  }
  // Which single control, dropped, brings back the most jobs. Same method as
  // web._relax_suggestions: revert one, re-run the real filter, count. Only controls that
  // are actually set are tried, so a typical short search costs two or three passes.
  //
  // It mutates the control, counts, and puts it back. Synchronous throughout -- no render
  // happens in between -- which is what makes that safe and what keeps this from needing a
  // parallel copy of every filter's semantics.
  function localRelax(rows, top) {
    var st = relaxState(), out = [], key;
    for (key in st) {
      if (!Object.prototype.hasOwnProperty.call(st, key)) continue;
      var el = st[key][0], cur = st[key][1], def = st[key][2];
      if (!el || cur === def || cur == null || cur === "") continue;
      var isCheck = el.type === "checkbox", had = isCheck ? el.checked : el.value;
      if (isCheck) el.checked = false; else el.value = def;
      // dateCutoff() is re-read AFTER the revert: reverting `date` is one of the things we
      // try, and matches() takes the cutoff as an argument rather than recomputing it.
      var n = 0, cut2 = dateCutoff();
      for (var i = 0; i < rows.length; i++) if (matches(rows[i], cut2)) n++;
      if (isCheck) el.checked = had; else el.value = had;
      out.push({ key: key, n: n, label: (RELAX_LABEL[key] || function (v) { return v; })(cur) });
    }
    out.sort(function (a, b) { return b.n - a.n; });
    return out.slice(0, top || 2);
  }
  function renderEmpty(relax) {
    if (!emptyEl) return;
    setShown(emptyEl, true);
    var own = TAB_EMPTY[tab];
    if (own) {
      emptyEl.innerHTML = '<p class="empty-h">' + esc(own[0]) + "</p><p>" + esc(own[1]) +
        '</p><div class="empty-acts">' +
        '<button type="button" class="btn sm" data-gotab="recommended">Browse Recommended</button></div>';
      return;
    }
    var h;
    if (relax && relax.length) {
      h = relaxBody(relax, 'No jobs match these filters.');
    } else {
      h = '<p class="empty-h">No jobs match these filters.</p>' +
          '<div class="empty-acts"><button type="button" class="btn sm ghost"' +
          ' data-relax="*">Clear all filters</button></div>';
    }
    emptyEl.innerHTML = h;
  }
  if (emptyEl) emptyEl.addEventListener("click", function (e) {
    // The status tabs' empty state offers a tab switch, not a filter reset -- see TAB_EMPTY.
    var g = e.target.closest && e.target.closest("[data-gotab]");
    if (g) {
      var want = g.getAttribute("data-gotab");
      var tb = document.querySelector('.tab[data-tab="' + want + '"]');
      if (tb) tb.click();
      return;
    }
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
      // Keep a useful reading area even when wrapped chips occupy most of a phone.
      var top = Math.max(12, Math.min(Math.round((bar ? bar.getBoundingClientRect().bottom : 56) + 6), window.innerHeight - 560));
      el.style.top = top + "px";
      el.style.maxHeight = Math.max(0, window.innerHeight - top - 12) + "px";
      return;
    }
    var host = feedLayout || el.offsetParent || document.body;
    var hb = host.getBoundingClientRect(), bb = btn.getBoundingClientRect();
    var w = el.offsetWidth;
    var left = bb.left - hb.left;
    if (left + w > hb.width) left = Math.max(0, hb.width - w);
    el.style.left = Math.round(left) + "px";
    el.style.top = Math.round(bb.bottom - hb.top + 6) + "px";
    // Height has to be measured, not guessed in CSS. A max-height in the stylesheet can only
    // subtract a CONSTANT from the viewport, but a popover opens wherever its chip happens to
    // be, and the chip bar sits below a page heading whose height varies. "More filters" ran
    // 36px past the bottom of a 1000px window, which put the primary "Show N jobs" button
    // off screen with no way to scroll to it. Measured from the chip, it always fits.
    el.style.maxHeight = Math.max(220, Math.round(window.innerHeight - bb.bottom - 24)) + "px";
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
    if (expStated && expStated.checked) ps.push("expstated=1");
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
  var _bootUsed = false;
  function renderServer(reset) {
    // FIRST PAINT ONLY. The server already filtered, sorted and rendered page one into
    // #feeddata — it is byte-for-byte what /api/feed?offset=0 would answer — so drawing it here
    // removes a whole serial round trip (~650 ms to this host) before the user sees a job.
    //
    // Only valid while the controls still say what they said when the server rendered.
    // applyFilterState() runs before this and may have restored a DIFFERENT filter set from
    // localStorage, and the tab must be the one the server filtered by. Anything unexpected
    // falls through to the fetch below, which is the unchanged old path — so the failure mode
    // of this optimisation is "no optimisation", never "wrong jobs".
    if (reset && !_bootUsed) {
      _bootUsed = true;
      if (DATA.length && TOTAL !== null && tab === "recommended"
          && _snapState() === SERVER_STATE) {
        var boot = "";
        for (var bi = 0; bi < DATA.length; bi++) boot += cardHTML(DATA[bi]);
        feed.innerHTML = boot;
        animateIn(0);
        shown = DATA.length;
        formatDates();
        setCount(TOTAL);
        setShown(emptyEl, !TOTAL);
        if (moreBtn) {
          var bmore = TOTAL > shown;
          setShown(moreBtn, bmore);
          if (bmore) moreBtn.textContent = "Load more (" + (TOTAL - shown) + " more)";
        }
        return;
      }
    }
    // Skeletons, not a lone spinner. skeletonHTML() is the same eight tiles the server paints
    // on first load, so a tab switch or a filter change now looks like the page opening rather
    // than like a different, smaller kind of wait.
    if (reset) { shown = 0; feed.innerHTML = skeletonHTML(); }
    var mySeq = ++_seq;                                   // ignore out-of-order responses
    fetch("/api/feed?" + buildParams(reset ? 0 : shown)).then(function (r) {
      /* 429 is read EXPLICITLY. This used to be a bare r.json(), so a rate-limited reply
         parsed fine, produced no `rows`, and rendered as "No jobs match these filters" --
         a throttle that lies about the corpus is worse than no throttle. The server sizes
         its short tier above anything this UI can produce, so reaching it means a loop or
         a held key, and the honest response is to say so and catch up. */
      if (r.status === 429) {
        var wait = parseInt(r.headers.get("Retry-After"), 10);
        if (!(wait > 0)) wait = 2;
        if (mySeq === _seq && reset) {
          feed.innerHTML = '<div class="loading-jd" style="padding:28px">' +
            '<span class="spin"></span>Catching up\u2026</div>';
        }
        // The spinner STAYS for this one, deliberately. "Catching up" is a different state
        // from "loading" and should not look identical to it: skeletons promise cards are
        // moments away, where this is a throttle being waited out.
        moreLoading(false);
        /* One retry, and only if nothing newer has been asked for. render() bumps _seq, so
           a later keystroke supersedes this and no retry storm can build up. */
        setTimeout(function () { if (mySeq === _seq) render(reset); }, Math.min(wait, 15) * 1000);
        return null;
      }
      /* 401 gets the same explicit treatment, and for the same reason. login_required used to
         answer an expired session with a 302 to the HTML login page; fetch follows it, gets
         HTML with status 200, r.json() throws, and the .catch below rendered "We couldn't load
         jobs" -- which never once mentioned being signed out, and on Load more printed nothing
         at all. Say what happened and offer the one thing that fixes it. */
      if (r.status === 401) {
        if (mySeq === _seq) {
          var here = encodeURIComponent(location.pathname + location.search);
          feed.innerHTML = '<div class="empty">You have been signed out. ' +
            '<a href="/login?next=' + here + '">Sign in again</a> to see your jobs.</div>';
          setShown(emptyEl, false);
          setShown(moreBtn, false);
        }
        moreLoading(false);
        return null;
      }
      return r.json();
    }).then(function (d) {
      if (d === null || mySeq !== _seq) return;
      var rows = (d && d.rows) || [], htmlc = "";
      for (var i = 0; i < rows.length; i++) byUrl[rows[i].url] = rows[i];
      for (var k = 0; k < rows.length; k++) htmlc += cardHTML(rows[k]);
      // `shown` is still the PREVIOUS card count here -- it is incremented two lines down, and
      // one row is one card -- so it is exactly the index of the first card being appended.
      // Reading it after the += would animate nothing at all on a Load more.
      var appendFrom = reset ? 0 : shown;
      if (reset) feed.innerHTML = htmlc; else feed.insertAdjacentHTML("beforeend", htmlc);
      animateIn(appendFrom);
      moreLoading(false);
      shown += rows.length;                               // one row = one card
      formatDates();
      var jobs = (d && d.total) || 0;
      setCount(jobs);
      if (!jobs) renderEmpty(d && d.relax);
      // d.total, NOT the page-load TOTAL: that one is the whole corpus and never moves,
      // so the note read "Only 29,971 jobs match these filters" over two cards. jobs IS
      // d.total and is already computed two lines up.
      else if (jobs < RELAX_WHEN_UNDER) renderRelaxNote(d && d.relax, jobs);
      else setShown(emptyEl, false);
      if (moreBtn) { var more = !!(d && d.has_more); setShown(moreBtn, more); if (more) moreBtn.textContent = "Load more (" + (jobs - shown) + " more)"; }
    }).catch(function () {
      moreLoading(false);       // or a failed page leaves the button spinning for ever
      if (mySeq === _seq && reset) feed.innerHTML = '<div class="empty">We couldn\'t load jobs. Try again.</div>';
    });
  }

  function debouncedRender() { if (_deb) clearTimeout(_deb); _deb = setTimeout(function () { render(true); }, 250); }
  function cardEl(url) { var cs = feed.querySelectorAll(".card"); for (var i = 0; i < cs.length; i++) if (cs[i].getAttribute("data-url") === url) return cs[i]; return null; }
  // After an action: small corpus re-renders locally; paged updates just the touched card in place
  // (or drops it if it no longer matches the current tab/filters) — no full refetch.
  function afterAction(j) {
    if (!PAGED) { render(false); tabCount(j); return; }
    var el = cardEl(j.url);
    if (matches(j, dateCutoff())) {
      if (el) {
        el.outerHTML = cardHTML(j);
        // cardHTML() emits the RAW date; every other render path calls formatDates() after it
        // and this one did not, so one Save click turned a card reading "3w ago" into
        // "2026-07-28" while its neighbours stayed relative. Re-query the node: outerHTML
        // replaced it, so `el` now points at something detached from the document.
        formatDates(cardEl(j.url) || feed);
      }
    }
    else if (el) { if (el.parentNode) el.parentNode.removeChild(el); if (countEl) { var n = parseInt(countEl.textContent, 10); if (!isNaN(n) && n > 0) setCount(n - 1); } }
    tabCount(j);
  }

  // The Saved / Applied / Hidden tab counters. Server-rendered once into .tabn and then never
  // touched, so saving a job correctly flipped the button to "Saved" while the tab beside it
  // still read "Saved 0" until a full page load. Recomputed from the same status transition the
  // action just made, rather than refetched.
  function tabCount(j) {
    var was = j._prevStatus || "", now = j.status || "";
    if (was === now) return;
    [was, now].forEach(function (s) {
      if (!s) return;
      var btn = document.querySelector('.tab[data-tab="' + s + '"] .tabn');
      if (!btn) return;
      var n = parseInt(btn.textContent, 10);
      if (isNaN(n)) return;
      btn.textContent = Math.max(0, n + (s === now ? 1 : -1));
    });
  }

  // `via` is the origin of the action and reaches the `action` event's via dimension. The server
  // whitelists it (web.py _ACTION_VIA); omitting it is recorded as "api", which is what an older
  // cached copy of this file sends.
  function doAction(url, next, via) {
    return fetch("/api/action", { method: "POST", headers: { "X-CSRF-Token": csrfToken(),  "Content-Type": "application/json" }, body: JSON.stringify({ url: url, status: next, via: via || "card" }) })
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
    syncMinStandDown();          // re-asserts "Off while searching" over the label just written
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
  if (expStated) expStated.addEventListener("change", function () { render(true); });
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

  // ---- job type: two boxes over the #intern select ----
  // The select used to BE the control, and it asked the question three different ways at
  // once: "Intern + full-time" (an inclusion), "Internships & co-ops only" (a restriction)
  // and "Exclude internships" (an exclusion). Two things exist, so two boxes name them and
  // the stored value is derived. #intern stays in the DOM as the value carrier because
  // app.js's own filter, feed_parity.py and test_saved_search.py all read it by id.
  var jtFull = document.getElementById("jt-full"), jtIntern = document.getElementById("jt-intern");
  function jobTypeFromBoxes() {
    var f = jtFull.checked, i = jtIntern.checked;
    // Unticking both would ask for nothing at all and return an empty feed with no way to
    // tell why, so the box you just cleared re-ticks the other one. The control cannot be
    // put into a state that means "show me no jobs".
    if (!f && !i) return null;
    return f && i ? "any" : (i ? "only" : "no");
  }
  function syncJobTypeBoxes() {
    if (!jtFull || !jtIntern || !internSel) return;
    jtFull.checked = internSel.value !== "only";
    jtIntern.checked = internSel.value !== "no";
  }
  if (jtFull && jtIntern && internSel) {
    [jtFull, jtIntern].forEach(function (box) {
      box.addEventListener("change", function () {
        var v = jobTypeFromBoxes();
        if (v === null) { syncJobTypeBoxes(); return; }   // refuse the empty state
        internSel.value = v;
        render(true);
      });
    });
    syncJobTypeBoxes();
  }



  // ---- "Clear" resets search and every filter; the display sort is preserved. ----
  var clearBtn = document.getElementById("clearfilters");
  function clearFilters() {
    // Captured BEFORE the resets: how many filters were stacked when someone gave up is the
    // interesting number. A high value means people over-filter into an empty feed, which
    // argues for a "no results — loosen these?" affordance rather than more filters.
    EV("clear_filters", { n: activeFilterCount() });
    // The SEARCH BOX too. It is the largest, leftmost, most prominent control in the bar, and
    // "Clear all filters" used to reset every chip and leave it still reading "data scientist"
    // -- so the results stayed narrowed by the one input the user could see. Defensible only if
    // search is not a filter, but then the label should not say ALL. Clearing it is the smaller
    // surprise of the two.
    if (q) q.value = "";
    if (trackSel) trackSel.value = "any";
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
    if (expStated) expStated.checked = false;
    if (showClosed) showClosed.checked = false;
    render(true);
  }
  if (clearBtn) clearBtn.addEventListener("click", clearFilters);
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
      expstated: !!(expStated && expStated.checked),
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
      method: "POST", headers: { "X-CSRF-Token": csrfToken(),  "Content-Type": "application/json" },
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
  if (moreBtn) moreBtn.addEventListener("click", function () {
    // Cleared by whichever render path finishes -- including every failure path, or a dead
    // page would leave the button spinning for ever. .btn.is-loading sets pointer-events:none,
    // which is also what stops a second click queueing a second page.
    moreLoading(true);
    limit += PAGE;
    render(false);
  });

  // applyask.js owns the "did you apply?" prompt and does not know what a card is, so it says
  // so and this repaints. Nothing happens if the confirmed job is not on screen.
  document.addEventListener("jm:applied", function (e) {
    var j = e.detail && byUrl[e.detail.url];
    if (!j) return;
    j._prevStatus = j.status || "";       // tabCount() needs the transition, not just the end state
    j.status = "applied";
    afterAction(j);
  });

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
        j._prevStatus = cur;              // tabCount() needs the transition, not just the end state
        j.status = next;
        // Hiding is the one action that removes the card from view, so it gets the collapse
        // + Undo treatment. The others just relabel in place and re-render as before.
        if (next === "hidden" && tab !== "hidden") {
          collapseCard(card, function () { afterAction(j); });
          toast("Hidden", function () {
            doAction(j.url, cur).then(function (ok2) {
              if (!ok2) { toast("Couldn't undo. Try again."); return; }
              j._prevStatus = j.status || ""; j.status = cur; tabCount(j); render(true);
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
      if (lnk.hasAttribute("data-apply")) {                      // clicking Apply asks on return
        var ac = lnk.closest(".card"), aj = ac && byUrl[ac.getAttribute("data-url")];
        // Recorded on EVERY click, not only the first: the same posting opened twice is two
        // outbound clicks, and this is the only place that number comes from.
        if (aj) EV("apply_click", { co: aj.company, sc: aj.score, where: "card" });
        // It used to write "applied" right here, which meant OPENING a posting counted as
        // applying to it — 129 applications on record that nobody had made. Opening a job and
        // applying to it are different events and only the user knows which happened, so park
        // it and let applyask.js ask when they come back.
        if (aj && aj.status !== "applied" && window.ApplyAsk)
          window.ApplyAsk.pend({ url: aj.url, title: aj.title, company: aj.company });
      }
      return;
    }
    // Everything else on a card is inert now. The title is a real <a href="/job?u=...">
    // and the <a> branch above already returned, so there is no whole-card click target:
    // a card whose background is ALSO a link, wrapping a link, three buttons and a company
    // link, is a nested-interactive mess. .ctitle:hover underlines so the card still
    // visibly responds to the pointer.
  });

  // ---- Opening a job: prefetch on intent -----------------------------------------------
  //
  // A card title is a real <a href="/job?u=..."> (see the note in the click handler above), so
  // opening a job is a full navigation to the heaviest route in the app. base.html already shows
  // a progress bar for it, but a progress bar only DECORATES the wait. This removes it: by the
  // time the click lands, the document is already in the browser's prefetch cache.
  //
  // rel=prefetch rather than fetch(): /job sends no Cache-Control, so a fetch() response would
  // warm the server's per-worker caches but would NOT be reused for the navigation itself.
  // Same-origin, so CSP default-src 'self' already covers it and no directive had to be widened.
  //
  // Bounded on purpose. Hovering is not intent, so nothing fires until the pointer has settled --
  // a mouse crossing the grid to reach the filter bar would otherwise prefetch a whole row of
  // cards. PF_MAX caps a long scroll-and-skim session, and metered or slow connections opt out
  // entirely, because speculative bytes are the wrong trade when the user is paying for them.
  var PF_MAX = 6, PF_DWELL = 120, pfSeen = {}, pfCount = 0, pfTimer = null;

  function pfAllowed() {
    var c = navigator.connection;
    if (!c) return true;                              // no Network Information API: assume fine
    if (c.saveData) return false;
    return !/2g/.test(c.effectiveType || "");         // matches both "2g" and "slow-2g"
  }

  function prefetchJob(href) {
    if (!href || pfSeen[href] || pfCount >= PF_MAX || !pfAllowed()) return;
    pfSeen[href] = 1;
    pfCount++;
    var l = document.createElement("link");
    l.rel = "prefetch";
    l.as = "document";
    l.href = href;
    document.head.appendChild(l);
  }

  function pfHrefFrom(e) {
    var a = e.target && e.target.closest && e.target.closest('a[href^="/job?"]');
    return a && !a.hasAttribute("data-apply") ? a.getAttribute("href") : null;
  }

  feed.addEventListener("pointerover", function (e) {
    var href = pfHrefFrom(e);
    clearTimeout(pfTimer);
    if (href) pfTimer = setTimeout(function () { prefetchJob(href); }, PF_DWELL);
  });
  feed.addEventListener("pointerout", function () { clearTimeout(pfTimer); });
  // Touch has no hover, but touchstart lands well before the click -- enough of a head start to
  // be worth taking, and no dwell timer, because a touch already IS the intent.
  feed.addEventListener("touchstart", function (e) {
    var href = pfHrefFrom(e);
    if (href) prefetchJob(href);
  }, { passive: true });

  // A LOGO THAT WILL NOT LOAD IS REMOVED, NOT LEFT AS A BROKEN IMAGE. /companies has had this
  // since the harvest landed and the feed never did, so a manifest that disagreed with
  // static/logos/ -- which scripts/build_logos.py --check makes a build failure, but a
  // half-extracted deploy zip can still produce -- showed a broken-image glyph on every card
  // for that employer.
  //
  // Removing it is the whole fallback here, and that is deliberate: unlike a /companies tile,
  // the card already carries the company's NAME on this very line, so there is nothing to
  // stand in for. The `load` twin catches a 200 carrying a 1x1, which fires no error event.
  function dropMark(img) {
    if (img && img.classList && img.classList.contains("cmark") && img.parentNode) {
      img.parentNode.removeChild(img);
    }
  }
  feed.addEventListener("error", function (e) {
    if (e.target && e.target.tagName === "IMG") dropMark(e.target);
  }, true);
  feed.addEventListener("load", function (e) {
    var img = e.target;
    if (!img || img.tagName !== "IMG") return;
    // The 1x1-carried-on-a-200 case still wins: drop it rather than fading it in.
    if (img.naturalWidth < 8) { dropMark(img); return; }
    // Otherwise fade it in, because a lazily-loaded mark decodes well after its card landed.
    // .mark-in has no fill mode and .cmark carries no opacity of its own, so missing this
    // event costs a fade and never a missing logo -- see the rule in style.css.
    if (img.classList && img.classList.contains("cmark")) img.classList.add("mark-in");
  }, true);

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
    // POST + CSRF: /reload is admin-only now. It wipes the shared corpus cache and every
    // user's stored scores, which is not a thing a GET should be able to do.
    fetch("/reload", {
      method: "POST", cache: "no-store",
      headers: { "X-CSRF-Token": csrfToken(), "X-Requested-With": "fetch" }
    }).then(function () {
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
    fetch("/scrape", { method: "POST", headers: { "X-CSRF-Token": csrfToken(),  "X-Requested-With": "fetch" } }).then(function (r) { return r.json(); }).then(function (j) {
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
