"use strict";
// Shared Tesla auto-import pipeline. Loaded by BOTH:
//   - background.js (service worker, daily chrome.alarms run), and
//   - tesla_auto.js (content script that fires when you browse tesla.com/careers).
// Tesla's Akamai bot-wall 403s every server-side scraper, but requests from the user's
// own browser (cookies + real Chrome fingerprint) pass — so the extension is the one
// place this import can live. Results land via the same /api/ext/bulk_jobs endpoint as
// the manual popup button (same title/US filter, sponsor flag, and dedupe server-side),
// then /api/ext/jds attaches descriptions so the jobs get real match scores.

const JM_THROTTLE_MS = 20 * 60 * 60 * 1000;   // auto-runs at most ~once a day
const JM_JD_LIMIT = 30;                        // max detail fetches per run (politeness)

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

// Defensive parser for /cua-api/apps/careers/state (same logic as the popup's manual
// import — Tesla ships compact field names that have changed before).
function jmParseTeslaState(d) {
  function pick(o, keys) { for (const k of keys) { const v = o[k]; if (v !== undefined && v !== null && v !== "") return v; } return null; }
  function longestString(o) { let best = ""; for (const k in o) { const v = o[k]; if (typeof v === "string" && v.length > best.length) best = v; } return best; }
  let listings = Array.isArray(d.listings) ? d.listings : (Array.isArray(d.jobs) ? d.jobs : null);
  if (!listings) { for (const k in d) { if (Array.isArray(d[k]) && d[k].length && typeof d[k][0] === "object") { listings = d[k]; break; } } }
  if (!listings) return null;
  const lk = d.lookup || {};
  const locs = lk.locations || lk.location || lk.locs || {};
  const out = [];
  for (const j of listings) {
    if (!j || typeof j !== "object") continue;
    let title = pick(j, ["t", "title", "name", "jobTitle", "positionTitle"]) || longestString(j);
    const id = pick(j, ["id", "jobId", "reqId", "jobNum", "j"]);
    let loc = pick(j, ["l", "loc", "location", "city", "locations"]);
    if (Array.isArray(loc)) loc = loc.map((x) => (locs && locs[x]) || x).filter(Boolean).join("; ");
    else if (loc != null && typeof loc !== "string" && locs && locs[loc] != null) loc = locs[loc];
    if (typeof loc !== "string") loc = "";
    if (title && id != null) out.push({
      title: String(title).trim(),
      url: "https://www.tesla.com/careers/search/job/" + id,
      location: String(loc).trim(), company: "Tesla",
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

// The whole pipeline. opts.trigger: "alarm" | "page" | "manual" (manual skips the throttle).
async function jmRunTeslaImport(opts) {
  const trigger = (opts && opts.trigger) || "alarm";
  const cfg = await jmGet(["token", "apibase", "tesla_last"]);
  if (!cfg.token) return { ok: false, note: "no token saved" };
  const base = (cfg.apibase || "https://stemjobs.astrochakra.co").replace(/\/+$/, "");
  const last = cfg.tesla_last || {};
  if (trigger !== "manual" && last.ok && Date.now() - (last.at || 0) < JM_THROTTLE_MS) {
    return { ok: true, note: "ran recently — skipped" };
  }

  let state;
  try {
    const r = await fetch("https://www.tesla.com/cua-api/apps/careers/state",
                          { headers: { Accept: "application/json" }, credentials: "include" });
    if (!r.ok) {
      const res = { ok: false, at: Date.now(), note: "Tesla returned HTTP " + r.status +
                    " — open tesla.com/careers/search once and it will import automatically." };
      await jmSet({ tesla_last: res });
      return res;
    }
    state = await r.json();
  } catch (e) {
    const res = { ok: false, at: Date.now(), note: "fetch failed: " + e.message };
    await jmSet({ tesla_last: res });
    return res;
  }

  const jobs = jmParseTeslaState(state);
  if (!jobs || !jobs.length) {
    const res = { ok: false, at: Date.now(), note: "Tesla data had no job list (layout change?)" };
    await jmSet({ tesla_last: res });
    return res;
  }

  let bulk;
  try {
    const r = await fetch(base + "/api/ext/bulk_jobs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: cfg.token, jobs: jobs }),
    });
    bulk = await r.json();
  } catch (e) {
    const res = { ok: false, at: Date.now(), note: "app unreachable: " + e.message };
    await jmSet({ tesla_last: res });
    return res;
  }
  if (!bulk || !bulk.ok) {
    const res = { ok: false, at: Date.now(), note: "import failed: " + ((bulk && bulk.error) || "?") };
    await jmSet({ tesla_last: res });
    return res;
  }

  // JD follow-up for the jobs that were actually NEW, so they score properly.
  let jdsStored = 0;
  const added = (bulk.added_urls || []).slice(0, JM_JD_LIMIT);
  if (added.length) {
    const jds = {};
    for (const u of added) {
      const id = (u.split("/job/")[1] || "").split(/[/?#]/)[0];
      if (!id) continue;
      const jd = await jmFetchTeslaJd(id);
      if (jd) jds[u] = jd;
      await jmSleep(500);
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

  const res = { ok: true, at: Date.now(), added: bulk.added, scanned: bulk.scanned,
                jds: jdsStored, trigger: trigger };
  await jmSet({ tesla_last: res });
  return res;
}
