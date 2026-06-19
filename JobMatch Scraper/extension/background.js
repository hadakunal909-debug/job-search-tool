"use strict";
// Background service worker: runs the Tesla auto-import on a DAILY alarm, so the feed
// gets Tesla jobs without anyone clicking anything. The fetch rides the browser's own
// tesla.com cookies; if Akamai still challenges a cold session, the run records a note
// and the tesla_auto.js content script covers it the next time a Tesla page is open.
importScripts("tesla_shared.js", "filler.js");      // filler.js: jmFillApplication / jmClickSubmit / jmApplyState

function schedule() {
  chrome.alarms.create("tesla-auto", { delayInMinutes: 3, periodInMinutes: 24 * 60 });
}
chrome.runtime.onInstalled.addListener(schedule);
chrome.runtime.onStartup.addListener(schedule);

chrome.alarms.onAlarm.addListener((a) => {
  // canUseTabs: when Akamai 403s the worker's own fetch, the run transparently
  // retries through a real tesla.com tab (existing or throwaway-inactive).
  if (a.name === "tesla-auto") jmRunTeslaImport({ trigger: "alarm", canUseTabs: true });
});

// The popup's "Run Tesla import now" button (works from any page).
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "run-tesla-now") {
    jmRunTeslaImport({ trigger: "manual", canUseTabs: true }).then(sendResponse);
    return true;                                   // keep the channel open for the async reply
  }
  // The review overlay (in the page, isolated world) asks us to log a submitted application.
  // We do it here because the background holds the backend host permission, so the POST isn't
  // subject to the apply page's CSP.
  if (msg && msg.type === "jm_log_application") {
    fetch(msg.apibase + "/api/ext/save", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token: msg.token, title: msg.title, company: msg.company,
        url: msg.url, resume_name: msg.resume_name || ""
      })
    }).then((r) => r.json()).then(sendResponse).catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }
});


// ===================== Batch auto-apply runner =====================
// Sequentially opens each queued job in a BACKGROUND tab, tailors + fills via /api/ext/tailor +
// jmFillApplication, then auto-submits ONLY when the form is cleanly filled (résumé attached, no
// required gaps) and there's no CAPTCHA/login wall. dryRun skips the real submit. Live progress is
// written to chrome.storage.local("jm_queue") so the popup can render it. Keep the inter-job delay
// under ~25s so the MV3 service worker isn't evicted between jobs (activity keeps it alive).
const JM_Q = { stop: false, running: false };
const jmSleep = (ms) => new Promise((r) => setTimeout(r, ms));
const jmSaveQueue = (state) => new Promise((r) => chrome.storage.local.set({ jm_queue: state }, r));

function jmWaitForLoad(tabId, timeout) {
  return new Promise((resolve) => {
    let done = false;
    const finish = () => { if (done) return; done = true; clearTimeout(to); chrome.tabs.onUpdated.removeListener(onUpd); resolve(); };
    const onUpd = (id, info) => { if (id === tabId && info.status === "complete") finish(); };
    const to = setTimeout(finish, timeout || 25000);
    chrome.tabs.onUpdated.addListener(onUpd);
  });
}

async function jmProcessOne(item, cfg) {
  let t;
  try {
    t = await fetch(cfg.apibase + "/api/ext/tailor", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: cfg.token, job_url: item.url, company: item.company, format: "pdf" })
    }).then((r) => r.json());
  } catch (e) { return { status: "error", reason: "tailor network error" }; }
  if (!t || !t.ok) return { status: "error", reason: "tailor: " + ((t && t.error) || "failed") };

  const tab = await chrome.tabs.create({ url: item.url, active: false });
  try {
    await jmWaitForLoad(tab.id, 25000);
    await jmSleep(1800);                                   // let JS-rendered forms settle
    const out = await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true }, world: "MAIN",
      func: jmFillApplication, args: [{ fields: t.fields, file: t.file, defaults: t.defaults }]
    });
    const res = (out || []).map((o) => o && o.result).filter(Boolean).find((x) => x && x.found) || { found: false };
    if (!res.found) return { status: "skipped", reason: "no supported form on page" };
    if (res.captcha) return { status: "needs_you", reason: "CAPTCHA on page" };
    if (res.login) return { status: "needs_you", reason: "login / account wall" };
    if (!res.fileAttached) return { status: "needs_you", reason: "résumé didn't attach" };
    if (res.unfilled && res.unfilled.length)
      return { status: "needs_you", reason: (res.unfilled.length + " required: " + res.unfilled.slice(0, 3).map((u) => u.label).join("; ")).slice(0, 100) };

    if (cfg.dryRun) return { status: "ready", reason: "dry run — would submit (" + res.filled + "/" + res.total + " filled)" };
    if (!cfg.autosubmit) return { status: "ready", reason: "filled (auto-submit off)" };

    const sub = await chrome.scripting.executeScript({
      target: { tabId: tab.id }, world: "MAIN", func: jmClickSubmit, args: [res.submitSelector]
    });
    if (!(sub && sub[0] && sub[0].result && sub[0].result.clicked))
      return { status: "needs_you", reason: "submit button not found" };
    await jmSleep(4000);
    const stt = await chrome.scripting.executeScript({
      target: { tabId: tab.id }, world: "MAIN", func: jmApplyState, args: [item.url]
    });
    const s = (stt && stt[0] && stt[0].result) || {};
    if (s.captcha) return { status: "needs_you", reason: "CAPTCHA after submit" };
    fetch(cfg.apibase + "/api/ext/save", {                 // log to tracker (best-effort)
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: cfg.token, title: item.title, company: item.company, url: item.url })
    }).catch(() => {});
    return { status: "submitted", reason: s.confirmed ? "confirmation detected" : (s.changed ? "submitted (page advanced)" : "submit clicked (unverified)") };
  } catch (e) {
    return { status: "error", reason: String((e && e.message) || e).slice(0, 140) };
  } finally {
    try { await chrome.tabs.remove(tab.id); } catch (e) {}
  }
}

async function jmRunQueue(cfg) {
  if (JM_Q.running) return { ok: false, error: "already running" };
  JM_Q.running = true; JM_Q.stop = false;
  const state = {
    running: true, dryRun: cfg.dryRun, autosubmit: cfg.autosubmit,
    total: cfg.items.length, idx: 0, startedAt: Date.now(),
    items: cfg.items.map((it) => ({ url: it.url, title: it.title || "", company: it.company || "", status: "queued", reason: "" }))
  };
  await jmSaveQueue(state);
  for (let i = 0; i < state.items.length; i++) {
    if (JM_Q.stop) { state.items[i].status = "stopped"; break; }
    state.idx = i; state.items[i].status = "running"; await jmSaveQueue(state);
    const r = await jmProcessOne(state.items[i], cfg);
    state.items[i].status = r.status; state.items[i].reason = r.reason;
    await jmSaveQueue(state);
    if (i < state.items.length - 1 && !JM_Q.stop) await jmSleep(Math.min(cfg.delayMs || 8000, 20000));
  }
  state.running = false; JM_Q.running = false;
  await jmSaveQueue(state);
  return { ok: true };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return;
  if (msg.type === "jm_queue_start") {
    jmRunQueue({
      items: msg.items || [], apibase: msg.apibase, token: msg.token,
      dryRun: msg.dryRun !== false, autosubmit: !!msg.autosubmit, delayMs: msg.delayMs || 8000
    }).then(sendResponse);
    return true;
  }
  if (msg.type === "jm_queue_stop") { JM_Q.stop = true; sendResponse({ ok: true }); return; }
});
