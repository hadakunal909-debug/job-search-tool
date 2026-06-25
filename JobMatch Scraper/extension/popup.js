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

// Label for the "import all jobs on this page" button. Tesla gets its tuned importer;
// every other page gets the generic one (JSON-LD job data, else visible job links).
function bulkSiteLabel(url) {
  try {
    const h = new URL(url).hostname.replace(/^www\./, "");
    if (/(^|\.)tesla\.com$/.test(h)) return "Tesla careers detected — import its US jobs into your feed.";
    if (/(^|\.)(linkedin|indeed|glassdoor)\./.test(h)) return "";   // ToS/account-risk sites: single-save only
    return "Import the job listings on this page (best-effort — works on most career sites).";
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
  if (window !== window.top && /(^|\.)tesla\.com$/.test(host)) return { jobs: [] };  // tesla: top frame only

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

    // {code -> name} map: nested {locations} container, lookup as a flat id->label
    // map, or {id,name}-shaped nodes anywhere in the lookup/geo trees (current shape:
    // listing.l = "401022", resolver lives in lookup/geo — confirmed by DIAG 2026-06-11).
    const locs = {};
    for (const c of [d.lookup, d.lookups, d.geo, d]) {
      if (c && typeof c === "object") {
        const m = c.locations || c.location || c.locs;
        if (m && typeof m === "object" && !Array.isArray(m)) Object.assign(locs, m);
      }
    }
    if (d.lookup && typeof d.lookup === "object" && !Array.isArray(d.lookup)) {
      const vals = Object.values(d.lookup);
      const strs = vals.filter(function (v) { return typeof v === "string"; });
      if (strs.length && strs.length >= vals.length * 0.8) Object.assign(locs, d.lookup);
    }
    (function walk(o, depth) {
      if (!o || depth > 7) return;
      if (Array.isArray(o)) { for (const x of o) walk(x, depth + 1); return; }
      if (typeof o !== "object") return;
      let idv = null, namev = null;
      for (const k of ["id", "key", "value", "code", "v", "k"]) {
        const v = o[k]; if (v !== undefined && (typeof v === "string" || typeof v === "number")) { idv = v; break; }
      }
      for (const k of ["name", "label", "title", "text", "n"]) {
        const v = o[k]; if (typeof v === "string" && /[a-z]/i.test(v)) { namev = v; break; }
      }
      if (idv != null && namev && locs[String(idv)] === undefined) locs[String(idv)] = namev;
      for (const k in o) walk(o[k], depth + 1);
    })([d.lookup, d.geo], 0);

    const out = [];
    let unresolved = 0;
    for (const j of listings) {
      if (!j || typeof j !== "object") continue;
      let title = pick(j, ["t", "title", "name", "jobTitle", "positionTitle"]) || longestString(j);
      const id = pick(j, ["id", "jobId", "reqId", "jobNum", "j"]);
      if (!title || id == null) continue;       // no per-job id -> can't build a unique URL
      let loc = pick(j, ["l", "loc", "location", "city", "locations"]);
      if (Array.isArray(loc)) loc = loc.map(function (x) { return locs[String(x)] || x; }).filter(Boolean).join("; ");
      else if (loc != null && !/[a-z]/i.test(String(loc)) && locs[String(loc)] != null) loc = locs[String(loc)];
      loc = String(loc == null ? "" : loc).trim();
      if (!/[a-z]/i.test(loc)) { loc = ""; unresolved++; }   // code didn't resolve to text
      out.push({ title: String(title).trim(),
                 url: "https://www.tesla.com/careers/search/job/" + id,
                 location: loc, company: "Tesla" });
    }
    // Tesla's board is GLOBAL: if most locations failed to resolve, importing would
    // flood the feed with location-less world jobs. Refuse + report the data shape.
    if (out.length && unresolved / out.length > 0.4) {
      const sm = listings.find(function (x) { return x && typeof x === "object"; }) || {};
      function nodeSample(o) {
        try {
          if (!o || typeof o !== "object") return JSON.stringify(o);
          const k = Object.keys(o)[0];
          return "first key=" + k + " -> " + JSON.stringify(o[k]).slice(0, 90);
        } catch (e) { return "?"; }
      }
      return { error: "Tesla location lookup failed for " + unresolved + "/" + out.length +
                      " jobs — not importing to avoid non-US junk. DIAG listing keys=[" +
                      Object.keys(sm).slice(0, 12).join(",") + "] sample loc=" +
                      JSON.stringify(pick(sm, ["l", "loc", "location", "city", "locations"])).slice(0, 40) +
                      " | lookup " + nodeSample(d.lookup) + " | geo " + nodeSample(d.geo) };
    }
    return { jobs: out, sample: out[0] ? (out[0].title + " @ " + (out[0].location || "?")) : "" };
  }

  if (/(^|\.)(linkedin|indeed|glassdoor)\./.test(host)) {
    return { error: "Bulk import is disabled on this site (their terms ban automated collection " +
                    "and your account could get flagged). Use 📌 Save for single jobs here." };
  }

  // ---- GENERIC: any other careers page ----
  // 1) schema.org JobPosting structured data (the same thing Google for Jobs reads) —
  //    present on many career sites and carries title + location + a canonical URL.
  function fromJsonLdJobs() {
    const out = [];
    document.querySelectorAll('script[type="application/ld+json"]').forEach((s) => {
      try {
        const d = JSON.parse(s.textContent);
        const stack = Array.isArray(d) ? d.slice() : [d];
        while (stack.length) {
          const o = stack.pop();
          if (!o || typeof o !== "object") continue;
          if (Array.isArray(o["@graph"])) stack.push.apply(stack, o["@graph"]);
          if (Array.isArray(o.itemListElement)) stack.push.apply(stack, o.itemListElement.map((x) => (x && x.item) || x));
          const t = o["@type"];
          if (!(t === "JobPosting" || (Array.isArray(t) && t.indexOf("JobPosting") >= 0))) continue;
          const jl = Array.isArray(o.jobLocation) ? o.jobLocation[0] : o.jobLocation;
          let loc = "";
          if (typeof jl === "string") loc = jl;
          else if (jl && typeof jl === "object") {
            const addr = jl.address;
            if (typeof addr === "string") loc = addr;          // McKinsey-style bare string
            else if (addr && typeof addr === "object") {
              const ctry = typeof addr.addressCountry === "object" ? addr.addressCountry.name : addr.addressCountry;
              loc = [addr.addressLocality, addr.addressRegion, ctry].filter(Boolean).join(", ");
            }
            if (!loc && jl.name) loc = String(jl.name);
          }
          const org = o.hiringOrganization;
          out.push({
            title: String(o.title || "").trim(),
            url: String(o.url || ""),
            location: loc.trim(),
            company: org ? String(org.name || org) : "",
          });
        }
      } catch (e) {}
    });
    return out.filter((j) => j.title && /^https?:/i.test(j.url));
  }

  // 2) Fallback: harvest the RENDERED job links. Noisy by design — the server's strict
  //    title + US filter keeps only on-target roles, and dedupes by URL.
  function allAnchors() {
    // pierce open shadow roots — web-component job boards hide their links there
    const acc = [];
    (function walk(root) {
      try {
        root.querySelectorAll("a[href]").forEach((a) => acc.push(a));
        root.querySelectorAll("*").forEach((el) => { if (el.shadowRoot) walk(el.shadowRoot); });
      } catch (e) {}
    })(document);
    return acc;
  }
  function fromDomLinks() {
    const seen = new Set(), out = [];
    const jobUrl = /\/(job|jobs|career|careers|position|positions|opening|openings|vacanc|requisition|posting|jobdetail)(s)?\/|[?&](job|jobid|gh_jid|reqid|requisitionid|positionid|pid|posting)=|jobs\.lever\.co\/[^/]+\/[0-9a-f-]{8,}|greenhouse\.io\/[^/]+\/jobs\/|jobs\.ashbyhq\.com\/[^/]+\/[0-9a-f-]{8,}/i;
    const junk = /^(apply|apply now|learn more|view( all)?|see |read |share|save|sign in|log ?in|more|details|search|filter|next|previous|back)/i;
    allAnchors().forEach((a) => {
      const href = a.href || "";
      if (!/^https?:/i.test(href) || !jobUrl.test(href) || seen.has(href)) return;
      let t = (a.getAttribute("aria-label") || a.textContent || "").replace(/\s+/g, " ").trim();
      t = t.split(/ [|•·–—-] /)[0].trim();
      if (t.length < 6 || t.length > 90 || junk.test(t)) return;
      seen.add(href);
      out.push({ title: t, url: href, location: "", company: "" });
    });
    return out;
  }

  let jobs = fromJsonLdJobs();
  let how = "structured data";
  if (jobs.length < 2) {                          // 0-1 from JSON-LD -> try the visible links
    // lazy/infinite-scroll lists only render once you scroll: nudge the page until
    // the link count stops growing (max ~5s), then harvest and scroll back.
    let prev = -1;
    for (let i = 0; i < 6; i++) {
      const cnt = document.querySelectorAll("a[href]").length;
      if (cnt === prev) break;
      prev = cnt;
      window.scrollTo(0, document.body.scrollHeight);
      await new Promise((r) => setTimeout(r, 850));
    }
    window.scrollTo(0, 0);
    const dom = fromDomLinks();
    if (dom.length > jobs.length) { jobs = dom; how = "visible job links"; }
  }
  if (!jobs.length) return { error: "No job listings found on this page. Try the 🔍 board check below — if this site fronts a real job board, the daily scraper can take it from here." };
  return { jobs: jobs.slice(0, 500), how: how,
           sample: jobs[0].title + " @ " + (jobs[0].location || "?") };
}

// Runs IN the page: ATS-ish URLs visible in the LIVE DOM (iframe srcs + links) — the
// server checks these to see if the site fronts a board it can scrape daily.
function collectAtsCandidates() {
  const out = new Set();
  const re = /(greenhouse\.io|lever\.co|ashbyhq\.com|smartrecruiters\.com|myworkdayjobs\.com|myworkdaysite\.com|jibeapply\.com|icims\.com|apply\.workable\.com|recruitee\.com|breezy\.hr|personio\.com|oraclecloud\.com)/i;
  document.querySelectorAll("iframe[src]").forEach((f) => {
    if (/^https?:/i.test(f.src)) out.add(f.src);
  });
  document.querySelectorAll("a[href]").forEach((a) => {
    if (re.test(a.href || "")) out.add(a.href);
  });
  return Array.from(out).slice(0, 10);
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
    // Show "Fill" on any known ATS, OR any page where a real application form is detected
    // (covers company career domains that embed a board + ATS we don't host-match yet).
    let showFill = !!applyAts(tab && tab.url);
    if (!showFill && tab && tab.id) {
      try {
        const fr = await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, func: jmFormReady });
        showFill = (fr || []).some((o) => o && o.result);
      } catch (e) {}
    }
    $("tailorwrap").style.display = showFill ? "block" : "none";
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

// Manage learned answers: list + delete. (Edit = delete here, then re-capture the corrected value
// with "Save my answers from this page".)
$("learnedload").onclick = async () => {
  $("learnedmsg").style.color = "#0b7a52"; $("learnedmsg").textContent = "Loading…";
  try {
    const j = await (await fetch(cfg.apibase + "/api/ext/learned?token=" + encodeURIComponent(cfg.token))).json();
    if (!j.ok) { $("learnedmsg").style.color = "#c0392b"; $("learnedmsg").textContent = "Error: " + (j.error || "failed"); return; }
    const items = j.items || [];
    $("learnedmsg").textContent = items.length + " saved answer(s).";
    $("learnedlist").innerHTML = items.map((it) => {
      const k = (it.key || "").replace(/"/g, "&quot;").replace(/</g, "&lt;");
      return "<div style='padding:3px 0;border-bottom:1px solid #f0f2f6'><b>" +
        (it.label || it.key || "").replace(/</g, "&lt;") + "</b>: " + String(it.value || "").replace(/</g, "&lt;") +
        " <a href='#' data-jmdel='" + k + "' style='color:#c0392b'>✕</a></div>";
    }).join("") || "<div style='color:#888'>No saved answers yet — use “Save my answers from this page”.</div>";
  } catch (e) { $("learnedmsg").style.color = "#c0392b"; $("learnedmsg").textContent = "Network error."; }
};
$("learnedlist").addEventListener("click", async (e) => {
  const a = e.target.closest("[data-jmdel]"); if (!a) return;
  e.preventDefault();
  const key = a.getAttribute("data-jmdel"); if (!key) return;
  try {
    const r = await (await fetch(cfg.apibase + "/api/ext/learn_delete", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: cfg.token, key })
    })).json();
    if (r.ok) $("learnedload").click();                // reload the list
  } catch (e2) {}
});

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

// Which apply-form ATS can we fill? (keep in sync with filler.js + the backend _FILLABLE_HOSTS)
function applyAts(url) {
  let h = "";
  try { h = new URL(url).hostname.toLowerCase(); } catch (e) { return ""; }
  // Every ATS the scraper feeds (keep in sync with scraper/__init__.py detect_board + web.py _FILLABLE_HOSTS).
  // greenhouse/lever/ashby/smartrecruiters have tuned adapters; the rest fall back to filler.js's GENERIC adapter.
  var ATS = [
    [/greenhouse\.io/, "greenhouse"], [/lever\.co/, "lever"], [/ashbyhq\.com/, "ashby"],
    [/smartrecruiters\.com/, "smartrecruiters"], [/recruitee\.com/, "recruitee"], [/breezy\.hr/, "breezy"],
    [/personio\./, "personio"], [/workable\.com/, "workable"], [/ultipro\.com/, "ultipro"],
    [/bamboohr\.com/, "bamboohr"], [/pinpointhq\.com/, "pinpoint"], [/rippling\.com/, "rippling"],
    [/avature\.net/, "avature"], [/jobdiva\.com/, "jobdiva"],
    [/myworkdayjobs\.com|myworkdaysite\.com/, "workday"], [/oraclecloud\.com/, "oracle"],
    [/jibeapply\.com/, "jibe"]
  ];
  for (var i = 0; i < ATS.length; i++) if (ATS[i][0].test(h)) return ATS[i][1];
  return "";
}

// Fill this application from the user's profile + their saved ("learned") answers — NO AI, no résumé,
// no submit. Then show the review panel; the user uploads their résumé and clicks Submit.
$("tailorfill").onclick = async () => {
  const tab = await activeTab();
  const tm = $("tailormsg");
  tm.style.color = "#0b7a52"; tm.textContent = "Filling the form…";
  try {
    const ctx = await (await fetch(cfg.apibase + "/api/ext/profile_fields?token=" + encodeURIComponent(cfg.token))).json();
    if (!ctx || !ctx.fields) { tm.style.color = "#c0392b"; tm.textContent = "Couldn't load your profile — check the App URL / token."; return; }
    const payload = { fields: ctx.fields, defaults: ctx.defaults || {} };   // no file: you upload the résumé
    async function fill() {
      const out = await chrome.scripting.executeScript({
        target: { tabId: tab.id, allFrames: true }, world: "MAIN", func: jmFillApplication, args: [payload] });
      return (out || []).map((o) => o && o.result).filter(Boolean).find((x) => x && x.found) || { found: false };
    }
    let res = await fill();
    if (!res.found) {
      // If a chooser/modal is open in ANY frame (e.g. Workday's "Start Your Application"), DON'T
      // auto-click a gate — it would dismiss the modal. Let the user pick their option first.
      let chooser = false;
      try {
        const cc = await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, world: "MAIN", func: jmChooserOpen });
        chooser = (cc || []).some((o) => o && o.result);
      } catch (e) {}
      if (chooser) {
        tm.style.color = "#c0392b";
        tm.textContent = "Pick an option in the application dialog first (e.g. “Apply Manually” / sign in), then click Fill again.";
        return;
      }
      // Otherwise the form may be behind a single "Apply" / "I'm interested" gate — click it, wait, retry.
      tm.textContent = "Opening the application…";
      try { await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, world: "MAIN", func: jmClickApply }); } catch (e) {}
      for (let k = 0; k < 8; k++) {
        await new Promise((r) => setTimeout(r, 700));
        let ready = false;
        try {
          const rr = await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, world: "MAIN", func: jmFormReady });
          ready = (rr || []).some((o) => o && o.result);
        } catch (e) {}
        if (ready) break;
      }
      tm.textContent = "Filling the form…";
      res = await fill();
    }
    if (!res.found) {
      tm.style.color = "#c0392b";
      tm.textContent = "No application form found — click Apply / I'm interested on the page, then try again.";
      return;
    }
    // Learned-answer pass for whatever's still empty (client-side, NO AI).
    if (res.unfilled && res.unfilled.length) {
      const snap = await chrome.scripting.executeScript({
        target: { tabId: tab.id, allFrames: true }, world: "MAIN", func: jmSnapshotForm });
      let fields = [];
      (snap || []).forEach((o) => { if (o && Array.isArray(o.result)) fields = fields.concat(o.result); });
      const answers = jmMatchLearned(fields, ctx.learned || {});
      if (Object.keys(answers).length) {
        await chrome.scripting.executeScript({
          target: { tabId: tab.id, allFrames: true }, world: "MAIN", func: jmApplyAnswers, args: [answers] });
        res = await fill();
      }
    }
    await set({ jm_review: {
      result: res, token: cfg.token, apibase: cfg.apibase,
      title: $("title").value.trim(), company: $("company").value.trim(), url: tab.url, fileName: "" } });
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["overlay.js"] });
    const left = (res.unfilled && res.unfilled.length) ? (" · " + res.unfilled.length + " left for you") : "";
    tm.textContent = "Filled " + res.filled + "/" + res.total + " — upload your résumé & submit on the page" + left + ".";
    setTimeout(() => window.close(), 1100);          // let the user upload + submit on the page
  } catch (e) {
    tm.style.color = "#c0392b"; tm.textContent = "Error: " + e.message;
  }
};

// TRAIN: capture how the user filled THIS page and save it to their answer bank, so future
// auto-fills prefer their real answers over an AI guess.
$("learnpage").onclick = async () => {
  const tab = await activeTab();
  const tm = $("tailormsg");
  tm.style.color = "#0b7a52"; tm.textContent = "Reading your answers on this page…";
  try {
    const out = await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true }, world: "MAIN", func: jmCaptureFilled });
    let fields = [];
    (out || []).forEach((o) => { if (o && Array.isArray(o.result)) fields = fields.concat(o.result); });
    if (!fields.length) { tm.style.color = "#c0392b"; tm.textContent = "No filled fields found here — fill the form first, then save."; return; }
    const r = await fetch(cfg.apibase + "/api/ext/learn", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: cfg.token, company: $("company").value.trim(), fields })
    });
    const j = await r.json();
    if (!j.ok) { tm.style.color = "#c0392b"; tm.textContent = "Couldn't save: " + (j.error || "failed"); return; }
    tm.style.color = "#0b7a52";
    tm.textContent = "✅ Learned " + (j.saved || 0) + " answer(s). Future fills will use them.";
  } catch (e) { tm.style.color = "#c0392b"; tm.textContent = "Error: " + e.message; }
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

// "Can this site be auto-scraped?" — first click checks (page URL + live-DOM ATS
// candidates -> server detection chain); if a board is found, second click ADDS it
// to the daily scraper. Strictly better than one-off imports when it works.
let boardFound = null;
$("boardcheck").onclick = async () => {
  const tab = await activeTab();
  $("boardmsg").style.color = "#0b7a52";
  if (boardFound) {                                // second click = add it
    $("boardmsg").textContent = "Adding to the daily scraper…";
    try {
      const r = await fetch(cfg.apibase + "/api/ext/detect_board", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: cfg.token, url: boardFound.pageUrl,
                               candidates: boardFound.candidates, add: true }),
      });
      const j = await r.json();
      if (j.ok && j.added) {
        $("boardmsg").textContent = "✓ Added " + (j.name || "board") + " — it joins the next daily scrape (with full descriptions).";
        $("boardcheck").style.display = "none";
      } else { $("boardmsg").style.color = "#c0392b"; $("boardmsg").textContent = "Couldn't add: " + (j.error || "try the ➕ Add board page."); }
    } catch (e) { $("boardmsg").style.color = "#c0392b"; $("boardmsg").textContent = "Network error."; }
    return;
  }
  $("boardmsg").textContent = "Checking (page + embedded boards)…";
  let candidates = [];
  try {
    const out = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: collectAtsCandidates });
    candidates = (out && out[0] && out[0].result) || [];
  } catch (e) {}
  try {
    const r = await fetch(cfg.apibase + "/api/ext/detect_board", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: cfg.token, url: tab.url, candidates: candidates }),
    });
    const j = await r.json();
    if (!j.ok) { $("boardmsg").style.color = "#c0392b"; $("boardmsg").textContent = j.error || "Check failed."; return; }
    if (!j.found) { $("boardmsg").textContent = "No scrapeable board behind this site — use the import button above instead."; return; }
    if (j.builtin) { $("boardmsg").textContent = "✓ Already scraped daily (" + (j.name || j.ats) + ")."; return; }
    boardFound = { pageUrl: tab.url, candidates: candidates };
    $("boardmsg").textContent = "✓ Found: " + (j.name || "?") + " — " + j.ats + " board, ~" + (j.count == null ? "?" : j.count) + " postings. Click again to add it to the daily scraper.";
    $("boardcheck").textContent = "➕ Add " + (j.name || "this board") + " to the daily scraper";
  } catch (e) { $("boardmsg").style.color = "#c0392b"; $("boardmsg").textContent = "Network error — check the App URL."; }
};

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
    // allFrames: pick up boards rendered inside iframes too (classic iCIMS, embeds);
    // results come back one per frame — merge them, dedupe by url.
    const out = await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true }, world: "MAIN", func: grabPageJobs });
    const frames = (out || []).map((o) => o && o.result).filter(Boolean);
    const merged = [], seenU = new Set();
    let how = "", sample = "", err = "";
    for (const f of frames) {
      if (f.error && !err) err = f.error;
      for (const j of (f.jobs || [])) {
        if (j.url && !seenU.has(j.url)) { seenU.add(j.url); merged.push(j); }
      }
      if (!how && f.how) how = f.how;
      if (!sample && f.sample) sample = f.sample;
    }
    res = merged.length ? { jobs: merged, how: how, sample: sample } : { error: err || "No jobs found on this page." };
  } catch (e) {
    $("bulkmsg").style.color = "#c0392b"; $("bulkmsg").textContent = "Couldn't read the page: " + e.message; return;
  }
  if (!res || res.error) {
    $("bulkmsg").style.color = "#c0392b"; $("bulkmsg").textContent = (res && res.error) || "No jobs found on this page."; return;
  }
  const jobs = res.jobs || [];
  if (!jobs.length) { $("bulkmsg").style.color = "#c0392b"; $("bulkmsg").textContent = "No jobs found on this page."; return; }
  const co = $("company").value.trim();             // generic DOM links carry no company —
  jobs.forEach((j) => { if (!j.company && co) j.company = co; });   // use the detected one
  $("bulkmsg").textContent = "Found " + jobs.length + (res.how ? " via " + res.how : "") + " — importing…";
  try {
    const r = await fetch(cfg.apibase + "/api/ext/bulk_jobs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: cfg.token, jobs: jobs })
    });
    const j = await r.json();
    if (j.ok) {
      let m = "Added " + j.added + " new job" + (j.added === 1 ? "" : "s") + " to your feed (scanned " + j.scanned + ").";
      if (j.added === 0) {
        const d = j.dropped || {};
        if (j.dropped) m += " Dropped: " + (d.title || 0) + " off-target, " + (d.dup || 0) + " already known, " + (d.us || 0) + " non-US.";
        if (res.sample) m += " First parsed: " + res.sample;
      }
      $("bulkmsg").textContent = m;
      // Follow up with DESCRIPTIONS (+ real location/date) for the NEW jobs — plus any
      // already-known jobs the server says still lack a JD (backfill on re-runs).
      const addedUrls = (j.added_urls || []).concat(j.needs_jd || []);
      if (addedUrls.length) {
        $("bulkmsg").textContent = m + " Fetching descriptions… keep this popup open (~" +
          Math.min(addedUrls.length, 20) * 1 + "–" + Math.min(addedUrls.length, 20) * 2 + "s).";
        try {
          // generic detail-fetch works for every site incl. Tesla (same-origin from the tab)
          const origin = new URL(tab.url).origin;
          let jds = {};
          const sameOrigin = addedUrls.filter((u) => { try { return new URL(u).origin === origin; } catch (e) { return false; } }).slice(0, 20);
          if (sameOrigin.length) {
            const out2 = await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: "MAIN", func: jmPageFetchGenericDetails, args: [sameOrigin] });
            jds = (out2 && out2[0] && out2[0].result) || {};
          }
          if (Object.keys(jds).length) {
            const r2 = await fetch(cfg.apibase + "/api/ext/jds", {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ token: cfg.token, jds: jds }),
            });
            const j2 = await r2.json();
            m += " ✓ " + ((j2 && j2.stored) || 0) + " descriptions attached" +
                 ((j2 && j2.removed_nonus) ? ", " + j2.removed_nonus + " removed as non-US" : "") + ".";
          } else { m += " (No descriptions readable on this site.)"; }
        } catch (e) { m += " (Description fetch skipped: " + e.message + ")"; }
        $("bulkmsg").textContent = m;
      }
    }
    else { $("bulkmsg").style.color = "#c0392b"; $("bulkmsg").textContent = "Error: " + (j.error || "failed"); }
  } catch (e) {
    $("bulkmsg").style.color = "#c0392b"; $("bulkmsg").textContent = "Network error — check the App URL.";
  }
};

// ---------------- Batch auto-apply runner ----------------
let qJobs = {};   // url -> {title, company} from the last fetch (so the tracker logs nice names)

$("batchtoggle").onclick = () => {
  const w = $("batchwrap");
  const opening = w.style.display === "none";
  w.style.display = opening ? "block" : "none";
  if (opening) { pollQueue(); if (!$("qurls").value.trim()) fetchQueue(); }   // auto-source on open
};

async function fetchQueue() {
  $("qmsg").style.color = "#0b7a52"; $("qmsg").textContent = "Finding your best matched jobs…";
  try {
    const j = await (await fetch(cfg.apibase + "/api/ext/apply_queue?token=" + encodeURIComponent(cfg.token))).json();
    if (!j.ok) { $("qmsg").style.color = "#c0392b"; $("qmsg").textContent = "Error: " + (j.error || "failed"); return; }
    qJobs = {};
    (j.jobs || []).forEach((job) => { qJobs[job.url] = { title: job.title, company: job.company }; });
    $("qurls").value = (j.jobs || []).map((job) => job.url).join("\n");
    $("qmsg").textContent = "Auto-loaded " + (j.count || 0) + " matched job(s) on supported ATS. Press Start.";
  } catch (e) { $("qmsg").style.color = "#c0392b"; $("qmsg").textContent = "Network error."; }
}
$("qfetch").onclick = fetchQueue;

function parseQueueItems() {
  return $("qurls").value.split("\n").map((s) => s.trim()).filter((s) => /^https?:\/\//.test(s))
    .map((u) => ({ url: u, title: (qJobs[u] || {}).title || "", company: (qJobs[u] || {}).company || "" }));
}

$("qstart").onclick = async () => {
  const items = parseQueueItems();
  if (!items.length) { $("qmsg").style.color = "#c0392b"; $("qmsg").textContent = "Add a job URL (or click Fetch)."; return; }
  const delayMs = Math.max(3, Math.min(20, parseInt($("qdelay").value, 10) || 6)) * 1000;
  // Grant broad site access so pages on any company careers domain can be filled (idempotent once granted).
  // Per-origin breaks when an apply page redirects to another host, so we ask for https://*/*.
  const granted = await new Promise((res) =>
    chrome.permissions.request({ origins: ["https://*/*"] }, (r) => { void chrome.runtime.lastError; res(r); }));
  if (!granted) { $("qmsg").style.color = "#c0392b"; $("qmsg").textContent = "Site access denied — needed to fill the pages."; return; }
  // callback form (+ read lastError) so a closed popup doesn't surface an "uncaught (in promise)"
  chrome.runtime.sendMessage({ type: "jm_queue_start", items, apibase: cfg.apibase, token: cfg.token, delayMs }, function () { void chrome.runtime.lastError; });
  $("qmsg").style.color = "#0b7a52";
  $("qmsg").textContent = "Filling " + items.length + " job(s) — each opens in a tab for you to upload your résumé & submit.";
  pollQueue();
};

$("qstop").onclick = () => {
  chrome.runtime.sendMessage({ type: "jm_queue_stop" }, function () { void chrome.runtime.lastError; });
  $("qmsg").style.color = "#c0392b";
  $("qmsg").textContent = "⏹ Stopped — halting the current job now.";
};

let qPollTimer = null;
function pollQueue() {
  if (qPollTimer) clearInterval(qPollTimer);
  const icon = { submitted: "✅", check: "🔍", ready: "🟢", needs_you: "⏸️", skipped: "⏭️", error: "⚠️", running: "⏳", queued: "·", stopped: "⏹️" };
  if (!pollQueue._bound) {                              // one-time: delegated Retry handler on the results list
    pollQueue._bound = true;
    $("qresults").addEventListener("click", (e) => {
      const a = e.target.closest("[data-jmretry]"); if (!a) return;
      e.preventDefault();
      const url = a.getAttribute("data-jmretry"); if (!url) return;
      const it = { url, title: (qJobs[url] || {}).title || "", company: (qJobs[url] || {}).company || "" };
      chrome.runtime.sendMessage({ type: "jm_queue_start", items: [it], apibase: cfg.apibase, token: cfg.token, delayMs: 4000 }, function () { void chrome.runtime.lastError; });
      $("qmsg").style.color = "#0b7a52"; $("qmsg").textContent = "Retrying 1 job…"; pollQueue();
    });
  }
  const tick = () => chrome.storage.local.get(["jm_queue"], (st) => {
    const q = st && st.jm_queue;
    if (!q) return;
    const c = {};
    (q.items || []).forEach((it) => { c[it.status] = (c[it.status] || 0) + 1; });
    const done = (q.items || []).filter((it) => it.status !== "queued" && it.status !== "running").length;
    $("qmsg").style.color = "#0b7a52";
    $("qmsg").textContent = (q.running ? "Filling " : "Done ") + done + "/" + q.total +
      " — 🟢" + (c.ready || 0) + " ⏸️" + (c.needs_you || 0) + " ⏭️" + (c.skipped || 0) + " ⚠️" + (c.error || 0);
    $("qresults").innerHTML = (q.items || []).map((it) => {
      const u = (it.url || "").replace(/"/g, "&quot;").replace(/</g, "&lt;");
      const head = (icon[it.status] || "·") + " <b>" + (it.company || "").replace(/</g, "&lt;") + "</b> " + (it.title || "").replace(/</g, "&lt;");
      const reason = it.reason ? "<br><span style='color:#888'>" + it.reason.replace(/</g, "&lt;") + "</span>" : "";
      const acts = it.url ? "<br><a href='" + u + "' target='_blank'>open</a> · <a href='#' data-jmretry='" + u + "'>retry</a>" : "";
      return "<div style='padding:3px 0;border-bottom:1px solid #f0f2f6'>" + head + reason + acts + "</div>";
    }).join("");
    if (!q.running && qPollTimer) { clearInterval(qPollTimer); qPollTimer = null; }
  });
  tick();
  qPollTimer = setInterval(tick, 1500);
}

init();
