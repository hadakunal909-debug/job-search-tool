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
function jmParseTeslaState(d) {
  function pick(o, keys) { for (const k of keys) { const v = o[k]; if (v !== undefined && v !== null && v !== "") return v; } return null; }
  function longestString(o) { let best = ""; for (const k in o) { const v = o[k]; if (typeof v === "string" && v.length > best.length) best = v; } return best; }
  let listings = Array.isArray(d.listings) ? d.listings : (Array.isArray(d.jobs) ? d.jobs : null);
  if (!listings) { for (const k in d) { if (Array.isArray(d[k]) && d[k].length && typeof d[k][0] === "object") { listings = d[k]; break; } } }
  if (!listings) return null;
  // location lookup table may live in several places depending on app version
  const containers = [d.lookup, d.lookups, d.geo, d];
  let locs = {};
  for (const c of containers) {
    if (c && typeof c === "object") {
      const m = c.locations || c.location || c.locs;
      if (m && typeof m === "object") { locs = m; break; }
    }
  }
  const out = [];
  for (const j of listings) {
    if (!j || typeof j !== "object") continue;
    let title = pick(j, ["t", "title", "name", "jobTitle", "positionTitle"]) || longestString(j);
    const id = pick(j, ["id", "jobId", "reqId", "jobNum", "j"]);
    if (!title || id == null) continue;          // no per-job id -> can't build a unique URL
    let loc = pick(j, ["l", "loc", "location", "city", "locations"]);
    if (Array.isArray(loc)) loc = loc.map((x) => (locs && locs[x]) || x).filter(Boolean).join("; ");
    else if (loc != null && typeof loc !== "string" && locs && locs[loc] != null) loc = locs[loc];
    loc = (typeof loc === "string") ? loc.trim() : String(loc == null ? "" : loc);
    if (!/[a-z]/i.test(loc)) loc = "";           // unresolved numeric code -> unknown, KEEP the job
    out.push({
      title: String(title).trim(),
      url: "https://www.tesla.com/careers/search/job/" + id,
      location: loc, company: "Tesla",
    });
  }
  return out;
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
  const base = (cfg.apibase || "https://stemjobs.astrochakra.co").replace(/\/+$/, "");
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

  const jobs = state ? jmParseTeslaState(state) : null;
  if (!jobs || !jobs.length) {
    if (viaTab && viaTab.created) try { chrome.tabs.remove(viaTab.tabId); } catch (e) {}
    return finish({ ok: false, note: "Tesla data had no job list (layout change?)" });
  }
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

  // 3) JDs for the jobs that were actually NEW, so they score properly.
  let jdsStored = 0;
  const addedUrls = (bulk.added_urls || []).slice(0, JM_JD_LIMIT);
  const ids = addedUrls.map((u) => (u.split("/job/")[1] || "").split(/[/?#]/)[0]).filter(Boolean);
  if (ids.length) {
    const byId = {};
    try {
      if (viaTab) Object.assign(byId, (await jmInTab(viaTab.tabId, jmPageFetchJds, [ids])) || {});
      else for (const id of ids) { const jd = await jmFetchTeslaJd(id); if (jd) byId[id] = jd; await jmSleep(500); }
    } catch (e) {}
    const jds = {};
    for (const u of addedUrls) {
      const id = (u.split("/job/")[1] || "").split(/[/?#]/)[0];
      if (byId[id]) jds[u] = byId[id];
    }
    if (Object.keys(jds).length) {
      try {
        const r = await fetch(base + "/api/ext/jds", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token: cfg.token, jds: jds }),
        });
        const j = await r.json();
        if (j && j.ok) jdsStored = j.stored || 0;
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
                  note: (bulk.added === 0 ? "0 new" + dropNote + " | first parsed: " + sample : "") });
}
