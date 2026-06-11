"use strict";
const $ = (id) => document.getElementById(id);
const get = (keys) => new Promise((r) => chrome.storage.local.get(keys, r));
const set = (obj) => new Promise((r) => chrome.storage.local.set(obj, r));
const activeTab = () => new Promise((r) => chrome.tabs.query({ active: true, currentWindow: true }, (t) => r(t[0])));

let cfg = { token: "", apibase: "https://stemjobs.astrochakra.co" };

function cleanTitle(t) { return (t || "").split(/\s[|\-–—·]\s/)[0].trim(); }

// Runs IN the page: best-effort job title + company from JSON-LD / meta / job-site DOM / URL.
function extractJob() {
  function fromJsonLd() {
    var out = {};
    document.querySelectorAll('script[type="application/ld+json"]').forEach(function (s) {
      try {
        var d = JSON.parse(s.textContent);
        (Array.isArray(d) ? d : [d]).forEach(function (it) {
          var items = (it && it["@graph"]) ? it["@graph"] : [it];
          items.forEach(function (o) {
            if (!o || typeof o !== "object") return;
            var t = o["@type"];
            if (!(t === "JobPosting" || (Array.isArray(t) && t.indexOf("JobPosting") >= 0))) return;
            if (o.title && !out.title) out.title = o.title;
            var org = o.hiringOrganization;
            if (org && !out.company) out.company = typeof org === "string" ? org : (org.name || "");
          });
        });
      } catch (e) {}
    });
    return out;
  }
  function meta(sel) { var m = document.querySelector(sel); return m ? (m.content || "").trim() : ""; }
  function titleCase(s) {
    return (s || "").replace(/[-_]+/g, " ").replace(/\b\w/g, function (c) { return c.toUpperCase(); }).trim();
  }
  function fromUrl() {
    try {
      var host = location.hostname.replace(/^www\./, "");
      if (/(linkedin|indeed|glassdoor|ziprecruiter|monster|dice|simplyhired|builtin|wellfound|angellist|google)\./.test(host)) return "";
      var segs = location.pathname.split("/").filter(Boolean);
      if (/greenhouse\.io$/.test(host) && segs.length) return segs[0] === "embed" ? "" : segs[0];
      if (/lever\.co$/.test(host) && segs.length) return segs[0];
      if (/ashbyhq\.com$/.test(host) && segs.length) return segs[0];
      if (/smartrecruiters\.com$/.test(host) && segs.length) return segs[0];
      if (/myworkdaysite\.com$/.test(host)) { var i = segs.indexOf("recruiting"); return (i >= 0 && segs[i + 1]) ? segs[i + 1] : ""; }
      if (/myworkdayjobs\.com$/.test(host)) return host.split(".")[0];
      var parts = host.split(".");
      if (parts.length >= 3 && /^(careers|jobs|boards|apply|job|recruiting|talent|work|hire|hiring)$/.test(parts[0])) return parts[1];
      return parts.length >= 2 ? parts[parts.length - 2] : host;
    } catch (e) { return ""; }
  }
  var j = fromJsonLd();
  var title = j.title || meta('meta[property="og:title"]') || document.title || "";
  var company = j.company || "";
  if (!company) {
    var el = document.querySelector(
      'a.topcard__org-name-link, .topcard__flavor, .jobs-unified-top-card__company-name a, ' +
      '.jobs-unified-top-card__company-name, .job-details-jobs-unified-top-card__company-name a, ' +
      '.job-details-jobs-unified-top-card__company-name, [data-testid="inlineHeader-companyName"] a, ' +
      '[data-testid="company-name"], [class*="companyName"] a, [itemprop="hiringOrganization"] [itemprop="name"]');
    if (el) company = (el.textContent || el.content || "").trim();
  }
  if (!company) company = meta('meta[property="og:site_name"]');
  if (!company) company = titleCase(fromUrl());
  return { title: (title || "").trim(), company: (company || "").trim() };
}

// Which sites support "import all jobs on this page", and the label to show. Empty = unsupported.
function bulkSiteLabel(url) {
  try {
    const h = new URL(url).hostname.replace(/^www\./, "");
    if (/(^|\.)tesla\.com$/.test(h)) return "Tesla careers detected — import its US jobs into your feed.";
  } catch (e) {}
  return "";
}

// Runs IN the PAGE (MAIN world) so fetch() executes as the page itself: same-origin, carries
// the site's real cookies, and rides the bot-wall session the user already passed in normal
// browsing. That's why this reads Tesla's feed when every server-side scraper gets blocked.
async function grabPageJobs() {
  const host = location.hostname.replace(/^www\./, "");
  function pick(o, keys) { for (const k of keys) { const v = o[k]; if (v !== undefined && v !== null && v !== "") return v; } return null; }
  function longestString(o) { let best = ""; for (const k in o) { const v = o[k]; if (typeof v === "string" && v.length > best.length) best = v; } return best; }

  if (/(^|\.)tesla\.com$/.test(host)) {
    let d;
    try {
      const r = await fetch("/cua-api/apps/careers/state", { headers: { Accept: "application/json" }, credentials: "include" });
      if (!r.ok) return { error: "Tesla returned HTTP " + r.status + ". Open tesla.com/careers/search and let it load, then retry." };
      d = await r.json();
    } catch (e) { return { error: "Couldn't read Tesla jobs: " + e.message }; }

    let listings = Array.isArray(d.listings) ? d.listings : (Array.isArray(d.jobs) ? d.jobs : null);
    if (!listings) { for (const k in d) { if (Array.isArray(d[k]) && d[k].length && typeof d[k][0] === "object") { listings = d[k]; break; } } }
    if (!listings) return { error: "Tesla data had no job list (the page layout may have changed)." };

    const lk = d.lookup || {};
    const locs = lk.locations || lk.location || lk.locs || {};
    const out = [];
    for (const j of listings) {
      if (!j || typeof j !== "object") continue;
      let title = pick(j, ["t", "title", "name", "jobTitle", "positionTitle"]) || longestString(j);
      const id = pick(j, ["id", "jobId", "reqId", "jobNum", "j"]);
      let loc = pick(j, ["l", "loc", "location", "city", "locations"]);
      if (Array.isArray(loc)) loc = loc.map(function (x) { return (locs && locs[x]) || x; }).filter(Boolean).join("; ");
      else if (loc != null && typeof loc !== "string" && locs && locs[loc] != null) loc = locs[loc];
      if (typeof loc !== "string") loc = "";
      const url = id != null ? ("https://www.tesla.com/careers/search/job/" + id) : location.href;
      if (title) out.push({ title: String(title).trim(), url: url, location: String(loc).trim(), company: "Tesla" });
    }
    return { jobs: out };
  }
  return { error: "Page import isn't set up for this site yet — it works on tesla.com/careers." };
}

async function init() {
  cfg = Object.assign(cfg, await get(["token", "apibase"]));
  if (cfg.apibase) $("apibase").value = cfg.apibase;
  if (cfg.token) {
    $("setup").style.display = "none";
    $("main").style.display = "block";
    const tab = await activeTab();
    let title = cleanTitle(tab && tab.title), company = "";
    try {
      const out = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: extractJob });
      const R = out && out[0] && out[0].result;
      if (R) { if (R.title) title = cleanTitle(R.title); if (R.company) company = R.company; }
    } catch (e) {}
    $("title").value = title;
    $("company").value = company;
    const blbl = bulkSiteLabel(tab && tab.url);    // show "import all jobs" only on supported sites
    if (blbl) { $("bulklbl").textContent = blbl; $("bulkwrap").style.display = "block"; }
    else { $("bulkwrap").style.display = "none"; }
    try {                                          // prefill résumé name + autocomplete list
      const pr = await (await fetch(cfg.apibase + "/api/ext/profile?token=" + encodeURIComponent(cfg.token))).json();
      if (pr && pr.ok) {
        if (pr.default_resume && !$("resume").value) $("resume").value = pr.default_resume;
        const dl = $("rnames");
        if (dl) dl.innerHTML = (pr.resume_names || []).map(function (n) {
          return '<option value="' + String(n).replace(/"/g, "&quot;") + '">'; }).join("");
      }
    } catch (e) {}
    refreshAutoStat();
  } else {
    $("main").style.display = "none";
    $("setup").style.display = "block";
  }
}

$("savetok").onclick = async () => {
  const token = $("token").value.trim();
  const apibase = ($("apibase").value.trim() || "https://stemjobs.astrochakra.co").replace(/\/+$/, "");
  if (!token) { $("setupmsg").textContent = "Paste your token first."; return; }
  await set({ token, apibase });
  cfg.token = token; cfg.apibase = apibase;
  init();
};

$("reset").onclick = async () => { await set({ token: "" }); cfg.token = ""; init(); };

$("save").onclick = async () => {
  const tab = await activeTab();
  $("msg").style.color = "#0b7a52"; $("msg").textContent = "Saving…";
  try {
    const r = await fetch(cfg.apibase + "/api/ext/save", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: cfg.token, title: $("title").value.trim(),
                             company: $("company").value.trim(),
                             resume_name: $("resume").value.trim(), url: tab && tab.url })
    });
    const j = await r.json();
    if (j.ok) { $("msg").textContent = j.dup ? "Already in your tracker ✓" : "Saved to JobMatch ✓"; }
    else { $("msg").style.color = "#c0392b"; $("msg").textContent = "Error: " + (j.error || "failed"); }
  } catch (e) {
    $("msg").style.color = "#c0392b"; $("msg").textContent = "Network error — check the App URL.";
  }
};

function autoStatLine(last) {
  if (!last || !last.at) return "🤖 Tesla auto-import: not run yet (runs daily in the background)";
  const when = new Date(last.at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  if (!last.ok) return "🤖 Tesla auto-import " + when + ": " + (last.note || "failed");
  if (last.note) return "🤖 Tesla auto-import " + when + ": " + last.note;
  return "🤖 Tesla auto-import " + when + ": +" + (last.added || 0) + " jobs (" + (last.jds || 0) + " with descriptions)";
}

async function refreshAutoStat() {
  const { tesla_last } = await get(["tesla_last"]);
  $("autostat").textContent = autoStatLine(tesla_last);
}

$("teslanow").onclick = () => {
  $("teslamsg").style.color = "#0b7a52";
  $("teslamsg").textContent = "Importing Tesla jobs… (~30s with descriptions)";
  chrome.runtime.sendMessage({ type: "run-tesla-now" }, (res) => {
    if (chrome.runtime.lastError || !res) {
      $("teslamsg").style.color = "#c0392b";
      $("teslamsg").textContent = "Background import didn't respond — try from a tesla.com/careers tab.";
      return;
    }
    if (res.ok) { $("teslamsg").textContent = "Done — " + (res.note || ("+" + (res.added || 0) + " new, " + (res.jds || 0) + " descriptions.")); }
    else { $("teslamsg").style.color = "#c0392b"; $("teslamsg").textContent = res.note || "Import failed."; }
    refreshAutoStat();
  });
};

$("bulk").onclick = async () => {
  const tab = await activeTab();
  $("bulkmsg").style.color = "#0b7a52"; $("bulkmsg").textContent = "Reading jobs on the page…";
  let res;
  try {
    const out = await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: "MAIN", func: grabPageJobs });
    res = out && out[0] && out[0].result;
  } catch (e) {
    $("bulkmsg").style.color = "#c0392b"; $("bulkmsg").textContent = "Couldn't read the page: " + e.message; return;
  }
  if (!res || res.error) {
    $("bulkmsg").style.color = "#c0392b"; $("bulkmsg").textContent = (res && res.error) || "No jobs found on this page."; return;
  }
  const jobs = res.jobs || [];
  if (!jobs.length) { $("bulkmsg").style.color = "#c0392b"; $("bulkmsg").textContent = "No jobs found on this page."; return; }
  $("bulkmsg").textContent = "Found " + jobs.length + " — importing…";
  try {
    const r = await fetch(cfg.apibase + "/api/ext/bulk_jobs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: cfg.token, jobs: jobs })
    });
    const j = await r.json();
    if (j.ok) { $("bulkmsg").textContent = "Added " + j.added + " new job" + (j.added === 1 ? "" : "s") + " to your feed (scanned " + j.scanned + ")."; }
    else { $("bulkmsg").style.color = "#c0392b"; $("bulkmsg").textContent = "Error: " + (j.error || "failed"); }
  } catch (e) {
    $("bulkmsg").style.color = "#c0392b"; $("bulkmsg").textContent = "Network error — check the App URL.";
  }
};

init();
