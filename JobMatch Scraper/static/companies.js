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
  var LOGO = META.logo || {};
  var PALETTE = META.palette || ['#475569'];
  var UNSORTED = 'Unsorted';

  /* Bits 0-4 are core._VISA_BITS, reused so they cannot drift from core.VISA_TAGS. The labels
     ride in from core.VISA_TAG_LABELS rather than being restated here for the same reason. */
  var VISA_BITS = [[1, 'h1b'], [2, 'green_card'], [4, 'stem_opt'], [8, 'e3'], [16, 'h1b1']];
  var CAP_EXEMPT = 32, AGENCY = 64;

  var SECTION_CAP = 24;                 /* tiles per section before "Show all" */
  var expanded = {};                    /* sectorName -> true once expanded */
  var state = { q: '', sector: '', sort: 'live', liveOnly: true };

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
  /* Same rule as web.py::logocolor, so a company's tile is the same colour here as on its own
     page and on its feed cards. */
  function logoColor(name) {
    var s = 0, t = name || 'x';
    for (var i = 0; i < t.length; i++) s += t.charCodeAt(i);
    return PALETTE[s % PALETTE.length];
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

  /* ---------------------------------------------------------------- one tile */
  function card(r) {
    var name = r[0], domain = r[4], h1b = r[5], mask = r[6], live = r[7], linkAs = r[8] || r[0];
    var h = [];

    /* One meta line carries the whole in-feed / apply-direct distinction. No badge for it:
       a badge on 2,000 cards is noise, and the sentence is shorter than the badge plus its
       explanation would be. */
    var meta = live
      ? '<span class="conum">' + num(live) + '</span> open role' + (live === 1 ? '' : 's')
      : (r[3] === 2 ? 'Board watched · nothing open' : 'Not scraped · apply on their site');

    /* At most three chips, and colour is the only place it appears on this page -- style.css
       states the rule: colour means sponsorship, everything else is ink. */
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
    h.push('<div class="card cocard" data-href="' + esc(href) + '">');
    h.push('<div class="logo" style="background:' + logoColor(name) + '">' + esc(name.charAt(0).toUpperCase()));
    if (domain) {
      h.push('<img class="logo-img" alt="" loading="lazy" src="' +
        esc((LOGO.src || '').replace('__D__', domain)) + '" data-fallback="' +
        esc((LOGO.fb || '').replace('__D__', domain)) + '">');
    }
    h.push('</div>');
    /* The name is a real link, so the tile is reachable and openable-in-a-new-tab by keyboard
       even though the click handler covers the rest of the surface. */
    h.push('<h3><a href="' + esc(href) + '">' + esc(name) + '</a></h3>');
    h.push('<div class="coline">' + meta + '</div>');
    if (chips.length) h.push('<div class="kw">' + chips.join('') + '</div>');
    h.push('<div class="colinks">' + links.join('') + '</div>');
    h.push('</div>');
    return h.join('');
  }

  /* ---------------------------------------------------------------- rendering */
  function render() {
    var rows = visible();
    document.getElementById('count').textContent = num(rows.length);
    document.getElementById('empty').classList.toggle('u-hide', rows.length > 0);

    /* Grouped only in the All view. Once a sector is picked the headers would repeat the pill
       that is already lit, so they go away and the section renders in full. */
    if (state.sector || state.q) {
      host.className = 'codir';
      host.innerHTML = rows.map(card).join('');
    } else {
      var groups = {}, order = [];
      rows.forEach(function (r) {
        var s = sectorOf(r);
        if (!groups[s]) { groups[s] = []; order.push(s); }
        groups[s].push(r);
      });
      /* Sections by total open roles, so the part of the directory you can act on is first.
         Unsorted always sinks: it is a residue, not a sector, and it says so. */
      order.sort(function (a, b) {
        if (a === UNSORTED) return 1;
        if (b === UNSORTED) return -1;
        var sa = groups[a].reduce(function (t, r) { return t + r[7]; }, 0);
        var sb = groups[b].reduce(function (t, r) { return t + r[7]; }, 0);
        return sb - sa || groups[b].length - groups[a].length;
      });
      host.className = '';
      host.innerHTML = order.map(function (s) {
        var list = groups[s];
        var show = expanded[s] ? list : list.slice(0, SECTION_CAP);
        var more = list.length - show.length;
        return '<h2 class="sechdr">' + esc(s) + ' <span class="tabn">' + num(list.length) +
          '</span></h2>' +
          /* Rewritten twice as the bucket shrank, because a note that stops being true is
             worse than no note. It began as 916 employers ("below the curation line"), and is
             now 28: names too ambiguous to place on a name alone, plus a handful the scraper
             stored badly. Guessing a sector for either would be worse than saying this. */
          (s === UNSORTED ? '<p class="fnote">No sector assigned — the name alone is not ' +
            'enough to place these, and a few are recorded oddly by the scraper. Everything ' +
            'else about them is accurate.</p>' : '') +
          '<div class="codir">' + show.map(card).join('') + '</div>' +
          (more > 0 ? '<div class="morewrap"><button type="button" class="btn sm ghost" ' +
            'data-more="' + esc(s) + '">Show all ' + num(list.length) + '</button></div>' : '');
      }).join('');
    }
    paintSectors(rows);
  }

  /* Pill counts describe what a click would show, so they respect the open-roles toggle and
     the search box but NOT the sector already chosen. */
  function paintSectors() {
    var q = state.q.toLowerCase(), tally = {}, total = 0;
    ROWS.forEach(function (r) {
      if (state.liveOnly && !r[7]) return;
      if (q && score(r[0], q) < 0) return;
      var s = sectorOf(r);
      tally[s] = (tally[s] || 0) + 1;
      total++;
    });
    var names = SECTORS.filter(function (s) { return tally[s]; });
    if (tally[UNSORTED]) names.push(UNSORTED);
    var html = ['<button class="tab' + (state.sector ? '' : ' on') + '" data-sector="">All ' +
      '<span class="tabn">' + num(total) + '</span></button>'];
    names.forEach(function (s) {
      html.push('<button class="tab' + (state.sector === s ? ' on' : '') + '" data-sector="' +
        esc(s) + '">' + esc(s) + ' <span class="tabn">' + num(tally[s]) + '</span></button>');
    });
    document.getElementById('cosectors').innerHTML = html.join('');
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
  document.getElementById('liveonly').addEventListener('change', function (e) {
    state.liveOnly = e.target.checked; render();
  });
  document.getElementById('cosectors').addEventListener('click', function (e) {
    var b = e.target.closest('[data-sector]');
    if (!b) return;
    state.sector = b.getAttribute('data-sector');
    render();
  });
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
  /* The logo chain, same order web.py::logosrc documents: logo.dev, then the favicon, then
     our own letter tile showing through when both fail. */
  host.addEventListener('error', function (e) {
    var img = e.target;
    if (!img || img.tagName !== 'IMG') return;
    var fb = img.getAttribute('data-fallback');
    if (fb) { img.removeAttribute('data-fallback'); img.src = fb; } else { img.remove(); }
  }, true);

  render();
})();
