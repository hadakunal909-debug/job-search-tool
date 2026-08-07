"use strict";
// Shared Tesla auto-import pipeline. Loaded by BOTH:
//   - background.js (service worker, daily chrome.alarms run + popup "run now"), and
//   - tesla_auto.js (content script that fires when you browse tesla.com/careers).
// Tesla's Akamai bot-wall 403s every server-side scraper. Requests from the user's
// browser pass — but Akamai sometimes challenges even the extension's service worker,
// so when a direct fetch 403s, the background falls back to running the SAME fetch
// inside a real tesla.com TAB (existing one, or a throwaway inactive tab) where the
// page context is guaranteed to pass. Results land via /api/ext/bulk_jobs (same
// title/US filter + dedupe as every scraped board), then /api/ext/jds attaches
// descriptions so the jobs get real match scores.

const JM_THROTTLE_MS = 20 * 60 * 60 * 1000;   // auto-runs at most ~once a day
const JM_JD_LIMIT = 25;                        // max detail fetches per run (politeness)

// The live JobMatch app. It MOVED: the old stemjobs.astrochakra.co cPanel account was suspended
// in July 2026 and now 302s every path to a suspended-page CGI, so an extension still pointing
// there fails every call with no useful error. jmApiBase() reads the user's saved App URL but
// retires that dead host, which is why an install that was configured before the move heals
// itself on the next popup open instead of looking broken. Loaded by the popup + the worker.
const JM_DEFAULT_APIBASE = "https://stemjobs1.astrochakra.co";
const JM_DEAD_APIBASE_RE = /^https?:\/\/stemjobs\.astrochakra\.co/i;
function jmApiBase(saved) {
  const b = String(saved || "").trim().replace(/\/+$/, "");
  if (!b || JM_DEAD_APIBASE_RE.test(b)) return JM_DEFAULT_APIBASE;
  return b;
}

const jmGet = (keys) => new Promise((r) => chrome.storage.local.get(keys, r));
const jmSet = (obj) => new Promise((r) => chrome.storage.local.set(obj, r));
const jmSleep = (ms) => new Promise((r) => setTimeout(r, ms));

function jmStripHtml(html) {
  return String(html || "")
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/\s+/g, " ").trim();
}

// Accept extracted text as a JD only if it actually reads like one — otherwise we'd
// store nav/footer junk and the match score would be garbage.
function jmLooksLikeJd(txt) {
  return txt.length > 300 && /responsibilit|qualificat|requirement|what you.ll do|we are looking|experience in/i.test(txt);
}

// Defensive parser for /cua-api/apps/careers/state. Tesla ships compact field names
// and location CODES that resolve through a lookup table; when a code doesn't resolve
// to readable text we send "" (unknown) — the server KEEPS unknown locations, so a
// lookup miss can no longer wipe out the whole import (the added-0-of-2000 bug).
// Build a {code -> readable name} map from Tesla's state JSON. Confirmed shape
// (2026-06-11 DIAG): state keys = lookup/departments/geo/listings, listing keys =
// id,t,dp,f,l,y,sp,pu with l = a string code like "401022". The resolver tries:
//   1. a nested {locations:{...}} container (older app versions),
//   2. `lookup` as a FLAT id->label map of strings,
//   3. recursively harvesting {id,name}-shaped nodes anywhere in lookup/geo trees.
function jmBuildTeslaLocMap(d) {
  const map = {};
  for (const c of [d.lookup, d.lookups, d.geo, d]) {
    if (c && typeof c === "object") {
      const m = c.locations || c.location || c.locs;
      if (m && typeof m === "object" && !Array.isArray(m)) Object.assign(map, m);
    }
  }
  if (d.lookup && typeof d.lookup === "object" && !Array.isArray(d.lookup)) {
    const vals = Object.values(d.lookup);
    const strs = vals.filter((v) => typeof v === "string");
    if (strs.length && strs.length >= vals.length * 0.8) Object.assign(map, d.lookup);
  }
  const IDK = ["id", "key", "value", "code", "v", "k"];
  const NAMEK = ["name", "label", "title", "text", "n"];
  (function walk(o, depth) {
    if (!o || depth > 7) return;
    if (Array.isArray(o)) { for (const x of o) walk(x, depth + 1); return; }
    if (typeof o !== "object") return;
    let idv = null, namev = null;
    for (const k of IDK) { const v = o[k]; if (v !== undefined && (typeof v === "string" || typeof v === "number")) { idv = v; break; } }
    for (const k of NAMEK) { const v = o[k]; if (typeof v === "string" && /[a-z]/i.test(v)) { namev = v; break; } }
    if (idv != null && namev && map[String(idv)] === undefined) map[String(idv)] = namev;
    for (const k in o) walk(o[k], depth + 1);
  })([d.lookup, d.geo], 0);
  return map;
}

function jmParseTeslaState(d) {
  function pick(o, keys) { for (const k of keys) { const v = o[k]; if (v !== undefined && v !== null && v !== "") return v; } return null; }
  function longestString(o) { let best = ""; for (const k in o) { const v = o[k]; if (typeof v === "string" && v.length > best.length) best = v; } return best; }
  let listings = Array.isArray(d.listings) ? d.listings : (Array.isArray(d.jobs) ? d.jobs : null);
  if (!listings) { for (const k in d) { if (Array.isArray(d[k]) && d[k].length && typeof d[k][0] === "object") { listings = d[k]; break; } } }
  if (!listings) return null;
  const locs = jmBuildTeslaLocMap(d);
  const out = [];
  let unresolved = 0;
  for (const j of listings) {
    if (!j || typeof j !== "object") continue;
    let title = pick(j, ["t", "title", "name", "jobTitle", "positionTitle"]) || longestString(j);
    const id = pick(j, ["id", "jobId", "reqId", "jobNum", "j"]);
    if (!title || id == null) continue;          // no per-job id -> can't build a unique URL
    let loc = pick(j, ["l", "loc", "location", "city", "locations"]);
    if (Array.isArray(loc)) loc = loc.map((x) => locs[String(x)] || x).filter(Boolean).join("; ");
    else if (loc != null && !/[a-z]/i.test(String(loc)) && locs[String(loc)] != null) loc = locs[String(loc)];
    loc = String(loc == null ? "" : loc).trim();
    if (!/[a-z]/i.test(loc)) { loc = ""; unresolved++; }   // code didn't resolve to text
    out.push({
      title: String(title).trim(),
      url: "https://www.tesla.com/careers/search/job/" + id,
      location: loc, company: "Tesla",
    });
  }
  // ANTI-FLOOD: Tesla's board is GLOBAL. If most locations failed to resolve we can't
  // tell US from Osaka — importing would flood the feed with location-less world jobs
  // (it did once). Refuse, and report the data shape so the lookup can be fixed.
  if (out.length && unresolved / out.length > 0.4) {
    const sample = listings.find((x) => x && typeof x === "object") || {};
    function nodeSample(o) {
      try {
        if (!o || typeof o !== "object") return JSON.stringify(o);
        const k = Object.keys(o)[0];
        return "first key=" + k + " -> " + JSON.stringify(o[k]).slice(0, 90);
      } catch (e) { return "?"; }
    }
    return { error: "Tesla location lookup failed for " + unresolved + "/" + out.length +
                    " jobs — not importing to avoid non-US junk. DIAG listing keys=[" +
                    Object.keys(sample).slice(0, 12).join(",") + "] sample loc=" +
                    JSON.stringify(pick(sample, ["l", "loc", "location", "city", "locations"])).slice(0, 40) +
                    " | lookup " + nodeSample(d.lookup) + " | geo " + nodeSample(d.geo) };
  }
  return { jobs: out };
}

// JD for one job id: try the JSON detail endpoint, fall back to the job page's HTML.
async function jmFetchTeslaJd(id) {
  try {
    const r = await fetch("https://www.tesla.com/cua-api/careers/job/" + id,
                          { headers: { Accept: "application/json" }, credentials: "include" });
    if (r.ok && (r.headers.get("content-type") || "").includes("json")) {
      let txt = "";
      (function walk(o) {
        if (!o) return;
        if (typeof o === "string") { if (o.length > 80) txt += " " + o; return; }
        if (Array.isArray(o)) { o.forEach(walk); return; }
        if (typeof o === "object") { for (const k in o) walk(o[k]); }
      })(await r.json());
      txt = jmStripHtml(txt);
      if (jmLooksLikeJd(txt)) return txt.slice(0, 12000);
    }
  } catch (e) {}
  try {
    const r = await fetch("https://www.tesla.com/careers/search/job/" + id, { credentials: "include" });
    if (r.ok) {
      const txt = jmStripHtml(await r.text());
      if (jmLooksLikeJd(txt)) return txt.slice(0, 12000);
    }
  } catch (e) {}
  return "";
}

// ---------- tab fallback (background only: needs chrome.tabs + chrome.scripting) ----------
// Runs IN the page (MAIN world). Self-contained: injected functions can't see our scope.
function jmPageFetchState() {
  return fetch("/cua-api/apps/careers/state", { headers: { Accept: "application/json" }, credentials: "include" })
    .then((r) => (r.ok ? r.json().then((d) => ({ state: d })) : { error: "HTTP " + r.status }))
    .catch((e) => ({ error: String(e && e.message || e) }));
}

async function jmPageFetchJds(ids) {
  function strip(h) {
    return String(h || "").replace(/<script[\s\S]*?<\/script>/gi, " ").replace(/<style[\s\S]*?<\/style>/gi, " ")
      .replace(/<[^>]+>/g, " ").replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
  }
  function looksJd(t) { return t.length > 300 && /responsibilit|qualificat|requirement|what you.ll do|we are looking|experience in/i.test(t); }
  const out = {};
  for (const id of ids) {
    let jd = "";
    try {
      const r = await fetch("/cua-api/careers/job/" + id, { headers: { Accept: "application/json" }, credentials: "include" });
      if (r.ok && (r.headers.get("content-type") || "").includes("json")) {
        let txt = "";
        (function walk(o) {
          if (!o) return;
          if (typeof o === "string") { if (o.length > 80) txt += " " + o; return; }
          if (Array.isArray(o)) { o.forEach(walk); return; }
          if (typeof o === "object") { for (const k in o) walk(o[k]); }
        })(await r.json());
        txt = strip(txt);
        if (looksJd(txt)) jd = txt.slice(0, 12000);
      }
    } catch (e) {}
    if (!jd) {
      try {
        const r = await fetch("/careers/search/job/" + id, { credentials: "include" });
        if (r.ok) { const t = strip(await r.text()); if (looksJd(t)) jd = t.slice(0, 12000); }
      } catch (e) {}
    }
    if (jd) out[id] = jd;
    await new Promise((res) => setTimeout(res, 400));
  }
  return out;
}

// Generic detail-fetch for ANY careers site (runs IN the page, MAIN world, so fetches
// are same-origin with the site's cookies). For each job URL: read its detail page's
// schema.org JobPosting (description + real location + datePosted), else fall back to
// the page text when it reads like a JD. Returns {url: {jd, location, found_date}}.
// Self-contained: injected functions can't see outer scope.
async function jmPageFetchGenericDetails(urls) {
  function strip(h) {
    return String(h || "").replace(/<script[\s\S]*?<\/script>/gi, " ").replace(/<style[\s\S]*?<\/style>/gi, " ")
      .replace(/<[^>]+>/g, " ").replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">").replace(/\s+/g, " ").trim();
  }
  function looksJd(t) { return t.length > 300 && /responsibilit|qualificat|requirement|what you.ll do|we are looking|experience in/i.test(t); }
  function fromLd(html) {
    const out = { jd: "", location: "", found_date: "" };
    const doc = new DOMParser().parseFromString(html, "text/html");
    doc.querySelectorAll('script[type="application/ld+json"]').forEach((s) => {
      if (out.jd) return;
      try {
        const d = JSON.parse(s.textContent);
        const stack = Array.isArray(d) ? d.slice() : [d];
        while (stack.length) {
          const o = stack.pop();
          if (!o || typeof o !== "object") continue;
          if (Array.isArray(o["@graph"])) stack.push.apply(stack, o["@graph"]);
          const t = o["@type"];
          if (!(t === "JobPosting" || (Array.isArray(t) && t.indexOf("JobPosting") >= 0))) continue;
          const jd = strip(o.description || "");
          if (jd.length > 200) out.jd = jd.slice(0, 12000);
          // jobLocation comes in many shapes: object w/ address object, address as a
          // bare STRING (McKinsey), a Place with just .name, or a plain string.
          const jl = Array.isArray(o.jobLocation) ? o.jobLocation[0] : o.jobLocation;
          let loc = "";
          if (typeof jl === "string") loc = jl;
          else if (jl && typeof jl === "object") {
            const addr = jl.address;
            if (typeof addr === "string") loc = addr;
            else if (addr && typeof addr === "object") {
              const ctry = typeof addr.addressCountry === "object" ? addr.addressCountry.name : addr.addressCountry;
              loc = [addr.addressLocality, addr.addressRegion, ctry].filter(Boolean).join(", ");
            }
            if (!loc && jl.name) loc = String(jl.name);
          }
          if (!loc && o.applicantLocationRequirements) {
            const alr = Array.isArray(o.applicantLocationRequirements)
              ? o.applicantLocationRequirements[0] : o.applicantLocationRequirements;
            if (alr && alr.name) loc = String(alr.name);
          }
          out.location = loc.trim();
          out.found_date = String(o.datePosted || "").slice(0, 10);
          if (out.jd) break;
        }
      } catch (e) {}
    });
    return out;
  }
  const res = {};
  for (const u of urls) {
    try {
      const d = { jd: "", location: "", found_date: "" };
      // Tesla job pages are JS shells with no JSON-LD — but the cua-api detail
      // endpoint answers same-origin requests from inside the page.
      const tm = u.match(/tesla\.com\/careers\/search\/job\/(\d+)/i);
      if (tm) {
        try {
          const r0 = await fetch("/cua-api/careers/job/" + tm[1],
                                 { headers: { Accept: "application/json" }, credentials: "include" });
          if (r0.ok && (r0.headers.get("content-type") || "").includes("json")) {
            let txt = "";
            (function walk(o) {
              if (!o) return;
              if (typeof o === "string") { if (o.length > 80) txt += " " + o; return; }
              if (Array.isArray(o)) { o.forEach(walk); return; }
              if (typeof o === "object") { for (const k in o) walk(o[k]); }
            })(await r0.json());
            txt = strip(txt);
            if (looksJd(txt)) d.jd = txt.slice(0, 12000);
          }
        } catch (e) {}
      }
      if (!d.jd) {
        const r = await fetch(u, { credentials: "include" });
        if (r.ok) {
          const html = await r.text();
          Object.assign(d, fromLd(html), d.jd ? { jd: d.jd } : {});
          if (!d.jd) {
            const txt = strip(html);
            if (looksJd(txt)) d.jd = txt.slice(0, 12000);
          }
        }
      }
      if (d.jd || d.location || d.found_date) res[u] = d;
    } catch (e) {}
    await new Promise((w) => setTimeout(w, 400));
  }
  return res;
}


async function jmTeslaTab() {
  const existing = await chrome.tabs.query({ url: "https://www.tesla.com/*" });
  if (existing && existing.length) return { tabId: existing[0].id, created: false };
  const tab = await chrome.tabs.create({ url: "https://www.tesla.com/careers/search", active: false });
  await new Promise((res) => {
    const t = setTimeout(res, 20000);
    chrome.tabs.onUpdated.addListener(function L(id, info) {
      if (id === tab.id && info.status === "complete") {
        chrome.tabs.onUpdated.removeListener(L); clearTimeout(t);
        setTimeout(res, 4000);                   // let the app boot + Akamai sensor settle
      }
    });
  });
  return { tabId: tab.id, created: true };
}

async function jmInTab(tabId, func, args) {
  const out = await chrome.scripting.executeScript({
    target: { tabId: tabId }, world: "MAIN", func: func, args: args || [],
  });
  return out && out[0] && out[0].result;
}

// ---------- the whole pipeline ----------
// opts.trigger: "alarm" | "page" | "manual" (manual skips the throttle)
// opts.canUseTabs: true in the background worker (enables the tab fallback)
async function jmRunTeslaImport(opts) {
  const trigger = (opts && opts.trigger) || "alarm";
  const canTabs = !!(opts && opts.canUseTabs) && !!(chrome.tabs && chrome.scripting);
  const cfg = await jmGet(["token", "apibase", "tesla_last"]);
  if (!cfg.token) return { ok: false, note: "no token saved" };
  const base = jmApiBase(cfg.apibase);
  const last = cfg.tesla_last || {};
  if (trigger !== "manual" && last.ok && Date.now() - (last.at || 0) < JM_THROTTLE_MS) {
    return { ok: true, note: "ran recently — skipped" };
  }
  const finish = async (res) => { res.at = Date.now(); res.trigger = trigger; await jmSet({ tesla_last: res }); return res; };

  // 1) Tesla's job list: direct fetch first, tab fallback when the bot-wall says no.
  let state = null, viaTab = null;
  try {
    const r = await fetch("https://www.tesla.com/cua-api/apps/careers/state",
                          { headers: { Accept: "application/json" }, credentials: "include" });
    if (r.ok) state = await r.json();
    else if (!canTabs) return finish({ ok: false, note: "Tesla returned HTTP " + r.status +
      " — open tesla.com/careers/search once and it will import automatically." });
  } catch (e) {
    if (!canTabs) return finish({ ok: false, note: "fetch failed: " + e.message });
  }
  if (!state && canTabs) {
    try {
      viaTab = await jmTeslaTab();
      const pr = await jmInTab(viaTab.tabId, jmPageFetchState);
      if (pr && pr.state) state = pr.state;
      else return finish({ ok: false, note: "blocked even via a Tesla tab (" +
        ((pr && pr.error) || "no data") + ") — browse tesla.com/careers once." });
    } catch (e) {
      return finish({ ok: false, note: "tab fallback failed: " + e.message });
    }
  }

  const parsed = state ? jmParseTeslaState(state) : null;
  if (!parsed || parsed.error || !(parsed.jobs || []).length) {
    if (viaTab && viaTab.created) try { chrome.tabs.remove(viaTab.tabId); } catch (e) {}
    return finish({ ok: false, note: (parsed && parsed.error) || "Tesla data had no job list (layout change?)" });
  }
  const jobs = parsed.jobs;
  const sample = jobs[0] ? (jobs[0].title + " @ " + (jobs[0].location || "?")) : "";

  // 2) Push through the server's filter.
  let bulk;
  try {
    const r = await fetch(base + "/api/ext/bulk_jobs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: cfg.token, jobs: jobs }),
    });
    bulk = await r.json();
  } catch (e) {
    if (viaTab && viaTab.created) try { chrome.tabs.remove(viaTab.tabId); } catch (e2) {}
    return finish({ ok: false, note: "app unreachable: " + e.message });
  }
  if (!bulk || !bulk.ok) {
    if (viaTab && viaTab.created) try { chrome.tabs.remove(viaTab.tabId); } catch (e) {}
    return finish({ ok: false, note: "import failed: " + ((bulk && bulk.error) || "?") });
  }

  // 3) Details for the jobs that were actually NEW: the generic detail-fetch reads the
  //    job page's JSON-LD (description + REAL location + datePosted). The server then
  //    scores them properly AND deletes any whose real location turns out non-US.
  let jdsStored = 0, removedNonUs = 0;
  // new jobs first, then known jobs whose JD is still missing (server-driven backfill)
  const addedUrls = (bulk.added_urls || []).concat(bulk.needs_jd || []).slice(0, JM_JD_LIMIT);
  if (addedUrls.length) {
    let jds = {};
    try {
      if (viaTab) jds = (await jmInTab(viaTab.tabId, jmPageFetchGenericDetails, [addedUrls])) || {};
      else if (typeof DOMParser !== "undefined") jds = await jmPageFetchGenericDetails(addedUrls);
      else {                                       // service worker: no DOMParser — id-based fallback
        for (const u of addedUrls) {
          const id = (u.split("/job/")[1] || "").split(/[/?#]/)[0];
          if (!id) continue;
          const jd = await jmFetchTeslaJd(id);
          if (jd) jds[u] = jd;
          await jmSleep(500);
        }
      }
    } catch (e) {}
    if (Object.keys(jds).length) {
      try {
        const r = await fetch(base + "/api/ext/jds", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token: cfg.token, jds: jds }),
        });
        const j = await r.json();
        if (j && j.ok) { jdsStored = j.stored || 0; removedNonUs = j.removed_nonus || 0; }
      } catch (e) {}
    }
  }
  if (viaTab && viaTab.created) try { chrome.tabs.remove(viaTab.tabId); } catch (e) {}

  // dropped tally (new servers send it) makes a 0-added run self-explanatory
  const d = bulk.dropped || {};
  const dropNote = bulk.dropped
    ? " — dropped: " + (d.title || 0) + " off-target, " + (d.dup || 0) + " already known, " + (d.us || 0) + " non-US"
    : "";
  return finish({ ok: true, added: bulk.added, scanned: bulk.scanned, jds: jdsStored,
                  note: (bulk.added === 0 ? "0 new" + dropNote + " | first parsed: " + sample
                         : (removedNonUs ? removedNonUs + " removed as non-US after detail check" : "")) });
}
