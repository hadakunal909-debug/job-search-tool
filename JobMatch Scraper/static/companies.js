/* The company directory: /companies.
 *
 * Self-contained on purpose. app.js is not loaded on this page -- it owns the feed's card
 * contract, scripts/feed_parity.py lifts functions out of it BY SOURCE TEXT, and it holds a
 * raw NUL that hides it from ripgrep. Nothing here needs any of that.
 *
 * Everything is client-side over one inlined array: 2.1k rows is ~38 KB gzipped, so a
 * server-side ?q= would buy nothing and would owe a parity test forever.
 *
 * Row shape, from web.py::companies:
 *   [0] name  [1] sectorIdx (-1 = Unsorted)  [2] careers ("gh|slug" or a URL)
 *   [3] careersKind 0 none / 1 native / 2 board / 3 site-root
 *   [4] domain  [5] h1b count  [6] flagMask  [7] live open roles  [8] name to link as
 *
 * The ~50 lines of popover machinery near the bottom are a deliberate copy of app.js's, not a
 * shared module. Extracting one would put a third file inside the source-text dependency graph
 * that feed_parity.py walks, which costs more than duplicating presentation code.
 */
(function () {
  var host = document.getElementById('companies');
  if (!host) return;

  function readJSON(id, dflt) {
    var el = document.getElementById(id);
    if (!el) return dflt;
    try { return JSON.parse(el.textContent || '{}'); } catch (e) { return dflt; }
  }

  var ROWS = readJSON('codata', []);
  var META = readJSON('cometa', {});
  var SECTORS = META.sectors || [];
  var PREFIX = META.prefix || {};
  var LI_KW = META.li_kw || {};
  var LABELS = META.labels || {};
  /* The harvest manifest: slug -> [ext, aspectRatio, monoFlag]. No alias map -- see logoFor
     below for why one cannot be used from here. Absent manifest = every tile renders a
     monogram, which is a coherent page rather than a broken one. */
  var LOGOS = (META.logos || {}).ar || {};
  var LOGOV = (META.logos || {}).v || 0;
  /* Monograms come from the SERVER, index-parallel to ROWS. Deliberately not computed here:
     the rule needs core.norm_company, which strips Technologies/Group/Labs as well as the legal
     suffixes, and a JavaScript copy of that list is exactly the kind of twin CLAUDE.md's filter
     triplet warns about. Measured before it was deleted: a raw-name version here disagreed with
     Python on 225 of 2,695 names and collapsed every "<X> Technologies" employer onto AT.
     Parked on the row itself as r[9] so nothing has to thread an index through render(); the
     SERVED row is still nine fields, which is what test_companies_page.py freezes. */
  var MONO = META.mono || [];
  for (var mi = 0; mi < ROWS.length; mi++) ROWS[mi][9] = MONO[mi] || '?';
  var UNSORTED = 'Unsorted';

  /* Bits 0-4 are core._VISA_BITS, reused so they cannot drift from core.VISA_TAGS. The labels
     ride in from core.VISA_TAG_LABELS rather than being restated here for the same reason. */
  var VISA_BITS = [[1, 'h1b'], [2, 'green_card'], [4, 'stem_opt'], [8, 'e3'], [16, 'h1b1']];
  var CAP_EXEMPT = 32, AGENCY = 64;

  var SECTION_CAP = 24;                 /* tiles per section before "Show all" */
  var expanded = {};                    /* sectorName -> true once expanded */
  var state = { q: '', sector: '', sort: 'live', liveOnly: true };
  var openPop = null;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function num(n) { return Number(n || 0).toLocaleString('en-US'); }
  function sectorOf(row) { return row[1] >= 0 && SECTORS[row[1]] ? SECTORS[row[1]] : UNSORTED; }

  function careersURL(row) {
    var v = row[2];
    if (!v) return '';
    var i = v.indexOf('|');
    if (i < 0) return v;
    var tag = v.slice(0, i);
    return PREFIX[tag] ? PREFIX[tag] + v.slice(i + 1) : v;
  }
  function linkedinURL(name) {
    return 'https://www.linkedin.com/jobs/search/?keywords=' +
      encodeURIComponent(LI_KW[name] || name) + '&location=United%20States';
  }

  /* ---------------------------------------------------------------- the logo lockup */
  /* THE TWIN OF scripts/build_logos.py::slugify AND web.py::_logo_slug. Frozen against the
     whole corpus by scripts/test_logos.py, on the day it was introduced rather than after it
     drifts -- the filter triplet in CLAUDE.md is what happens otherwise. */
  function slug(name) {
    return String(name || '').toLowerCase().replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '') || 'x';
  }
  /* DIRECT SLUG ONLY, and the alias step that used to be here is gone rather than fixed.
     It read ALIAS[slug(name)] while web.py builds that map keyed on core.norm_company -- which
     strips Technologies, Group, Labs and the legal suffixes and joins on SPACES, so the keys
     are "1star networks" against a lookup of "1star-networks-llc". It could never hit, for any
     name, and measured over all 2,695 directory rows it cost exactly zero tiles because every
     one of them resolves directly.
     It cannot be fixed here either: norm_company is the reason the MONOGRAMS are computed
     server-side and shipped in cometa (see web.py's note there), and a JS copy of the suffix
     list is the twin that comment exists to avoid. So the map is no longer sent at all -- ~16 KB
     of JSON on every load of this page. The employers it would have served are corpus spellings
     the route merges in after the last build; those resolve on the next build_companies.py run,
     which is how they get a sector too. */
  function logoFor(name) {
    var s = slug(name);
    if (!LOGOS[s]) {
      s = '';
    }
    return s ? { src: '/static/logos/' + s + '.' + LOGOS[s][0] + '?v=' + LOGOV,
                 ar: LOGOS[s][1], mono: LOGOS[s][2] } : null;
  }
  /* ONE CHILD, NEVER TWO. The old markup put an absolutely-positioned <img> with an opaque
     white background ON TOP of the letter tile, so a blank-but-200 response painted over the
     very fallback it was meant to reveal. Either/or cannot do that. */
  /* data-mono IS EMITTED HERE TOO, and its absence was a real bug: the dark-mode rule
     [data-theme="dark"] .colock img[data-mono="1"] never matched on this page, so all 234
     single-ink marks kept the light plate that commit 3df171d was written to remove -- while
     the feed, whose markup lives in app.js, un-plated them correctly. logoFor() had computed
     the flag one line above and thrown it away.
     Note the attribute means two different things in this file by inheritance: "one dark ink"
     on the <img>, and the two-letter monogram on the .cocard wrapper. Both are read by
     selectors narrow enough not to collide, but do not widen either one. */
  function lockup(name, mono) {
    var l = logoFor(name);
    if (l) {
      return '<div class="colock"><img src="' + esc(l.src) + '" alt="" loading="lazy" ' +
             'decoding="async"' + (l.mono ? ' data-mono="1"' : '') + '></div>';
    }
    return '<div class="colock"><span class="comono" aria-hidden="true">' +
           esc(mono || '?') + '</span></div>';
  }

  /* ---------------------------------------------------------------- searching */
  /* Substring match with a leading-token boost, so typing "uni" surfaces "University of ..."
     above "... Communications". Rank, not filter: a mid-word hit still matches. */
  function score(name, q) {
    var n = name.toLowerCase();
    var at = n.indexOf(q);
    if (at < 0) return -1;
    if (at === 0) return 0;
    return n.charAt(at - 1) === ' ' ? 1 : 2;
  }

  function visible() {
    var q = state.q.toLowerCase();
    var out = [];
    for (var i = 0; i < ROWS.length; i++) {
      var r = ROWS[i];
      if (state.liveOnly && !r[7]) continue;
      if (state.sector && sectorOf(r) !== state.sector) continue;
      var s = 0;
      if (q) { s = score(r[0], q); if (s < 0) continue; }
      out.push([s, r]);
    }
    var by = state.sort;
    out.sort(function (a, b) {
      if (a[0] !== b[0]) return a[0] - b[0];                 /* query relevance first */
      if (by === 'az') return a[1][0].localeCompare(b[1][0]);
      if (by === 'h1b') return (b[1][5] - a[1][5]) || (b[1][7] - a[1][7]);
      return (b[1][7] - a[1][7]) || (b[1][5] - a[1][5]);
    });
    return out.map(function (p) { return p[1]; });
  }

  /* Section order, hoisted out of render() so paintFacets() can use the SAME order. They
     disagreed before: the pill row ordered by the SECTORS array while the sections ordered by
     open roles, so the third pill led to the sixth section down the page. */
  function sectorOrder(groups, names) {
    return names.slice().sort(function (a, b) {
      if (a === UNSORTED) return 1;
      if (b === UNSORTED) return -1;
      var sa = groups[a].reduce(function (t, r) { return t + r[7]; }, 0);
      var sb = groups[b].reduce(function (t, r) { return t + r[7]; }, 0);
      return sb - sa || groups[b].length - groups[a].length;
    });
  }

  /* ---------------------------------------------------------------- one tile */
  function card(r, showSector) {
    var name = r[0], h1b = r[5], mask = r[6], live = r[7], linkAs = r[8] || r[0];
    var h = [];

    /* One meta line carries the whole in-feed / apply-direct distinction. No badge for it:
       a badge on 2,000 cards is noise, and the sentence is shorter than the badge plus its
       explanation would be. */
    var meta = live
      ? '<span class="conum">' + num(live) + '</span> open role' + (live === 1 ? '' : 's')
      : (r[3] === 2 ? 'Board watched, nothing open' : 'Not scraped, apply on their site');

    /* At most two route chips plus cap-exempt, and colour is the only place it appears on this
       page -- style.css states the rule: colour means sponsorship, everything else is ink. */
    var chips = [];
    if (mask & AGENCY) {
      chips.push('<span class="agency">Staffing agency</span>');
    } else {
      if (h1b >= 1000) chips.push('<span class="vt vt-h1b">~' + num(h1b) + ' H-1B</span>');
      for (var i = 0; i < VISA_BITS.length && chips.length < 2; i++) {
        var bit = VISA_BITS[i][0], key = VISA_BITS[i][1];
        if (!(mask & bit)) continue;
        if (key === 'h1b' && h1b >= 1000) continue;           /* already said, with a number */
        chips.push('<span class="vt vt-' + key + '">' + esc(LABELS[key] || key) + '</span>');
      }
      if ((mask & CAP_EXEMPT) && chips.length < 3) {
        chips.push('<span class="cx">Cap-exempt</span>');
      }
    }

    var careers = careersURL(r);
    var links = [];
    if (careers) {
      links.push('<a class="btn sm" href="' + esc(careers) + '" target="_blank" ' +
        'rel="noopener nofollow">' + (r[3] === 3 ? 'Website' : 'Careers') +
        '<span class="ic ic-external" aria-hidden="true"></span></a>');
    }
    links.push('<a class="btn sm" href="' + esc(linkedinURL(name)) + '" target="_blank" ' +
      'rel="noopener nofollow">LinkedIn<span class="ic ic-external" aria-hidden="true"></span></a>');

    /* A DIV, not an <a>, even though the whole tile is clickable. The two outbound buttons
       live inside it, and an <a> inside an <a> is invalid HTML -- the browser hoists them out
       and the tile ships with no careers or LinkedIn link at all. Same shape .card uses on the
       feed: a div, cursor:pointer, and the delegated handler below. */
    var href = '/company?c=' + encodeURIComponent(linkAs);
    h.push('<div class="card cocard" data-href="' + esc(href) +
      '" data-mono="' + esc(r[9] || '?') + '">');
    h.push(lockup(name, r[9]));
    /* The name is a real link, so the tile is reachable and openable-in-a-new-tab by keyboard
       even though the click handler covers the rest of the surface. No tabindex on the div:
       that would be a second, duplicate tab stop on every one of 2,695 tiles. */
    h.push('<h3><a href="' + esc(href) + '">' + esc(name) + '</a></h3>');
    h.push('<div class="coline">' + meta + '</div>');
    /* Only in the flat view. In the grouped view the section header two inches up already says
       it, and repeating it is how a card starts looking padded. */
    if (showSector) h.push('<div class="cosector">' + esc(sectorOf(r)) + '</div>');
    /* ALWAYS rendered, even empty: 818 of 2,695 employers carry no visa bits at all, so a
       conditional chip row makes a third of the grid sit at a different height. */
    h.push('<div class="kw">' + chips.join('') + '</div>');
    h.push('<div class="colinks">' + links.join('') + '</div>');
    h.push('</div>');
    return h.join('');
  }

  /* ---------------------------------------------------------------- rendering */
  function render() {
    var rows = visible();
    var flat = !!(state.sector || state.q);
    document.getElementById('empty').classList.toggle('u-hide', rows.length > 0);

    /* ONE class, one meaning. #companies used to be .codir in flat mode and '' in grouped mode,
       with inner .codir elements, so a single selector described two different structures. */
    host.className = 'cosections';
    if (flat) {
      host.innerHTML = '<div class="codir">' +
        rows.map(function (r) { return card(r, true); }).join('') + '</div>';
    } else {
      var groups = {}, names = [];
      rows.forEach(function (r) {
        var s = sectorOf(r);
        if (!groups[s]) { groups[s] = []; names.push(s); }
        groups[s].push(r);
      });
      host.innerHTML = sectorOrder(groups, names).map(function (s) {
        var list = groups[s];
        var show = expanded[s] ? list : list.slice(0, SECTION_CAP);
        var more = list.length - show.length;
        /* "Show all" moved INTO the header, right aligned. It used to be a centred button
           floating between two grids, which was the most generic element on the page. */
        return '<div class="cosec-h"><h2>' + esc(s) + '</h2>' +
          '<span class="cosec-n">' + num(list.length) + '</span>' +
          (more > 0 ? '<button type="button" class="btn sm ghost" data-more="' + esc(s) +
            '">Show All ' + num(list.length) + '</button>' : '') +
          '</div>' +
          /* Rewritten twice as the bucket shrank, because a note that stops being true is
             worse than no note. It began as 916 employers ("below the curation line"), and is
             now 28: names too ambiguous to place on a name alone, plus a handful the scraper
             stored badly. Guessing a sector for either would be worse than saying this. */
          (s === UNSORTED ? '<p class="fnote">No sector assigned. The name alone is not ' +
            'enough to place these, and a few are recorded oddly by the scraper. Everything ' +
            'else about them is accurate.</p>' : '') +
          '<div class="codir">' +
          show.map(function (r) { return card(r, false); }).join('') + '</div>';
      }).join('');
    }
    paintFacets(rows.length);
  }

  /* Pill counts describe what a click would show, so they respect the open-roles toggle and
     the search box but NOT the sector already chosen. */
  function paintFacets(shown) {
    var q = state.q.toLowerCase(), tally = {}, total = 0;
    ROWS.forEach(function (r) {
      if (state.liveOnly && !r[7]) return;
      if (q && score(r[0], q) < 0) return;
      var s = sectorOf(r);
      tally[s] = (tally[s] || 0) + 1;
      total++;
    });
    var groups = {}, names = [];
    ROWS.forEach(function (r) {
      if (state.liveOnly && !r[7]) return;
      if (q && score(r[0], q) < 0) return;
      var s = sectorOf(r);
      if (!groups[s]) { groups[s] = []; names.push(s); }
      groups[s].push(r);
    });

    /* .roletile gives a 38px row, a DRAWN tick from --tick-mark, :focus-within and a
       right-aligned tabular count for free. The radio is visually hidden but FOCUSABLE, so
       arrow keys move through the group natively and it is one tab stop, not sixteen. */
    function row(value, label, n, on) {
      return '<label class="roletile' + (on ? ' on' : '') + '">' +
        '<input type="radio" name="cosector" value="' + esc(value) + '"' +
        (on ? ' checked' : '') + '>' +
        '<span class="roletile-b"><span class="rolelab">' + esc(label) + '</span>' +
        '<span class="rolen">' + num(n) + '</span></span></label>';
    }
    var html = [row('', 'All Sectors', total, !state.sector)];
    sectorOrder(groups, names).forEach(function (s) {
      html.push(row(s, s, tally[s], state.sector === s));
    });
    document.getElementById('cofacet-sector').innerHTML = html.join('');

    var chip = document.getElementById('chip-sector');
    document.getElementById('chipv-sector').textContent = state.sector || '';
    chip.classList.toggle('on', !!state.sector);

    document.getElementById('scope-live').setAttribute('aria-pressed', String(state.liveOnly));
    document.getElementById('scope-all').setAttribute('aria-pressed', String(!state.liveOnly));
    document.getElementById('scope-live').classList.toggle('on', state.liveOnly);
    document.getElementById('scope-all').classList.toggle('on', !state.liveOnly);

    /* The result line. It says what the sort is actually doing, because visible() ranks by
       query relevance BEFORE the chosen sort, so with a query typed the select is partly
       overridden and nothing used to admit that. */
    var bits = ['<b>' + num(shown) + '</b> of ' + num(ROWS.length) + ' companies'];
    bits.push('<span class="sep">·</span> ' + (state.liveOnly ? 'Hiring now' : 'All employers'));
    if (state.sector) bits.push('<span class="sep">·</span> ' + esc(state.sector));
    if (state.q) bits.push('<span class="sep">·</span> Ranked by name match');
    if (state.q || state.sector || !state.liveOnly) {
      bits.push('<button type="button" class="btn sm ghost" data-clear="1">Clear</button>');
    }
    document.getElementById('coresult').innerHTML = bits.join(' ');
  }

  /* ---------------------------------------------------------------- popover */
  /* Ported from app.js. Two changes on the way in: the positioned host is #cotools rather than
     the feed layout, and the fixed branch measures #cobar. That fixed branch is MANDATORY, not
     optional: the @media (max-width:900px) rule that turns .fpop into a full-width sheet is
     written unscoped in the filter-bar block, so it applies to this page too. */
  var tools = document.querySelector('.cotools');
  function closePop() {
    if (!openPop) return;
    var el = document.getElementById(openPop);
    if (el) el.hidden = true;
    var btn = document.querySelector('[data-pop="' + openPop + '"]');
    if (btn) btn.setAttribute('aria-expanded', 'false');
    openPop = null;
  }
  function placePop(el, btn) {
    el.hidden = false;                                    /* measurable only once shown */
    if (getComputedStyle(el).position === 'fixed') {
      el.style.left = '';
      var bar = document.getElementById('cobar');
      el.style.top = Math.round((bar ? bar.getBoundingClientRect().bottom : 56) + 6) + 'px';
      return;
    }
    var hb = (tools || document.body).getBoundingClientRect();
    var bb = btn.getBoundingClientRect();
    var left = bb.left - hb.left;
    if (left + el.offsetWidth > hb.width) left = Math.max(0, hb.width - el.offsetWidth);
    el.style.left = Math.round(left) + 'px';
    el.style.top = Math.round(bb.bottom - hb.top + 6) + 'px';
    el.style.maxHeight = Math.max(220, Math.round(window.innerHeight - bb.bottom - 24)) + 'px';
  }
  function togglePop(id, btn) {
    var was = openPop;
    closePop();
    if (was === id) return;
    var el = document.getElementById(id);
    if (!el) return;
    placePop(el, btn);
    btn.setAttribute('aria-expanded', 'true');
    openPop = id;
    var first = el.querySelector('input');
    if (first) first.focus();
  }

  /* ---------------------------------------------------------------- wiring */
  var t = null;
  document.getElementById('q').addEventListener('input', function (e) {
    var v = e.target.value;
    clearTimeout(t);
    /* 2.1k rows is one synchronous pass, so this debounce is only about not rebuilding the
       DOM on every keystroke. */
    t = setTimeout(function () { state.q = v.trim(); render(); }, 90);
  });
  document.getElementById('sort').addEventListener('change', function (e) {
    state.sort = e.target.value; render();
  });
  document.getElementById('coscope').addEventListener('click', function (e) {
    var b = e.target.closest('[data-scope]');
    if (!b) return;
    state.liveOnly = b.getAttribute('data-scope') === 'live';
    render();
  });
  var sectorChip = document.getElementById('chip-sector');
  sectorChip.addEventListener('click', function (e) {
    e.stopPropagation();
    togglePop('pop-sector', sectorChip);
  });
  /* CLOSE, THEN RENDER, THEN RETURN FOCUS. render() rewrites the popover's innerHTML, which
     destroys the focused radio and drops focus to <body>; a keyboard user would lose their
     place after every selection. Closing on choice is what a single-select menu should do
     anyway, and the trigger is where focus belongs afterwards. */
  document.getElementById('pop-sector').addEventListener('change', function (e) {
    if (!e.target || e.target.name !== 'cosector') return;
    state.sector = e.target.value;
    closePop();
    render();
    sectorChip.focus();
  });
  document.getElementById('coresult').addEventListener('click', function (e) {
    if (!e.target.closest('[data-clear]')) return;
    state.q = '';
    state.sector = '';
    state.liveOnly = true;
    document.getElementById('q').value = '';
    render();
  });
  document.addEventListener('click', function (e) {
    if (!openPop) return;
    if (e.target.closest && (e.target.closest('.fpop') || e.target.closest('.fchip'))) return;
    closePop();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && openPop) {
      var btn = document.querySelector('[data-pop="' + openPop + '"]');
      closePop();
      if (btn) btn.focus();
    }
  });
  window.addEventListener('resize', closePop);

  host.addEventListener('click', function (e) {
    var b = e.target.closest('[data-more]');
    if (b) {
      expanded[b.getAttribute('data-more')] = true;
      render();
      return;
    }
    /* Whole-tile click. Skipped when the click landed on a link, so the two outbound buttons
       and the name link keep their own targets (and their middle-click / ctrl-click). */
    if (e.target.closest('a')) return;
    var tile = e.target.closest('[data-href]');
    if (tile) location.href = tile.getAttribute('data-href');
  });
  /* A 404 here means the manifest and static/logos/ disagree, which scripts/build_logos.py
     --check makes a build failure. At runtime the honest response is the monogram. The `load`
     twin catches a 200 carrying a 1x1, which no error event ever fires for. */
  function toMono(img) {
    var wrap = img.parentNode;
    if (!wrap) return;
    /* .cocard, NOT [data-mono]. closest() starts at the element ITSELF, and the <img> now
       carries data-mono="1" for the dark-mode inversion -- so the old selector matched the
       image and every broken logo rendered a monogram reading "1". The attribute name is
       overloaded in this file; the tile is addressed by its class instead. */
    var tile = img.closest('.cocard');
    wrap.innerHTML = '<span class="comono" aria-hidden="true">' +
      esc((tile && tile.getAttribute('data-mono')) || '?') + '</span>';
  }
  host.addEventListener('error', function (e) {
    if (e.target && e.target.tagName === 'IMG') toMono(e.target);
  }, true);
  host.addEventListener('load', function (e) {
    var img = e.target;
    if (img && img.tagName === 'IMG' && img.naturalWidth < 8) toMono(img);
  }, true);

  render();
})();
