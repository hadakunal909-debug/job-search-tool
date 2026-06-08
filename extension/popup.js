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
    try {                                          // prefill résumé name + autocomplete list
      const pr = await (await fetch(cfg.apibase + "/api/ext/profile?token=" + encodeURIComponent(cfg.token))).json();
      if (pr && pr.ok) {
        if (pr.default_resume && !$("resume").value) $("resume").value = pr.default_resume;
        const dl = $("rnames");
        if (dl) dl.innerHTML = (pr.resume_names || []).map(function (n) {
          return '<option value="' + String(n).replace(/"/g, "&quot;") + '">'; }).join("");
      }
    } catch (e) {}
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

init();
