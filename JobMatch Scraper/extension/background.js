"use strict";
// Background service worker. Two jobs:
//  1) Tesla daily auto-import on an alarm (rides the browser's tesla.com cookies).
//  2) The batch form-filler: open each queued job in a tab, fill it from the user's profile + their
//     saved ("learned") answers — NO AI, no résumé upload, no auto-submit — inject the review overlay,
//     and LEAVE THE TAB OPEN so the user uploads their résumé and submits.
// filler.js provides jmFillApplication / jmSnapshotForm / jmApplyAnswers / jmMatchLearned / jmFormReady.
importScripts("tesla_shared.js", "filler.js");

function schedule() {
  chrome.alarms.create("tesla-auto", { delayInMinutes: 3, periodInMinutes: 24 * 60 });
}
chrome.runtime.onInstalled.addListener(schedule);
chrome.runtime.onStartup.addListener(schedule);

chrome.alarms.onAlarm.addListener((a) => {
  // canUseTabs: when Akamai 403s the worker's own fetch, the run transparently
  // retries through a real tesla.com tab (existing or throwaway-inactive).
  if (a.name === "tesla-auto") jmRunTeslaImport({ trigger: "alarm", canUseTabs: true });
  // Keepalive: a no-op storage touch resets the MV3 service-worker idle timer so a long batch run
  // isn't evicted between jobs. Created while a queue runs, cleared when it ends.
  else if (a.name === "jm-keepalive") { try { chrome.storage.local.get("jm_queue", () => { void chrome.runtime.lastError; }); } catch (e) {} }
});
function jmKeepAlive(on) {
  try { if (on) chrome.alarms.create("jm-keepalive", { periodInMinutes: 0.4 }); else chrome.alarms.clear("jm-keepalive"); } catch (e) {}
}

// The popup's "Run Tesla import now" button + overlay logging + passive training capture.
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
  // Passive "training" capture (autocapture.js): a submitted/advanced application form's answers.
  // We POST from here so the request isn't subject to the apply page's CSP, and we read the token
  // from storage (the content script never sees it). Dropped silently when not signed in. This is how
  // the no-AI filler learns new custom questions — once you answer one, it's remembered next time.
  if (msg && msg.type === "jm_autocapture") {
    const fields = msg.fields || [];
    const sig = fields.length + "|" + fields.map((f) => f.label + "=" + f.value).join("|");
    const now = Date.now();
    if (sig === JM_AC.sig && now - JM_AC.at < 20000) return false;   // de-dupe frames / re-clicks
    JM_AC.sig = sig; JM_AC.at = now;
    chrome.storage.local.get(["token", "apibase"], (st) => {
      const token = st && st.token; if (!token || !fields.length) return;
      const apibase = (st && st.apibase) || "https://stemjobs.astrochakra.co";
      fetch(apibase + "/api/ext/learn", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, company: msg.company || "", fields })
      }).catch(() => {});                                            // fire-and-forget
    });
    return false;
  }
});
// Last passive-capture signature/time, so the same answers from multiple frames or a double-click
// aren't saved repeatedly.
const JM_AC = { sig: "", at: 0 };


// ===================== Batch form-filler (no AI) =====================
// Sequentially opens each queued job in a BACKGROUND tab, fills it from the profile + learned answers,
// shows the review overlay, and leaves the tab open. Nothing is tailored, attached, or submitted — the
// user uploads their résumé and clicks Submit. Live progress -> chrome.storage.local("jm_queue").
const JM_Q = { stop: false, running: false, currentTabId: null, lastFields: [] };
// jmSleep is already defined in tesla_shared.js (imported above) — reuse it (don't redeclare).
const jmSaveQueue = (state) => new Promise((r) => chrome.storage.local.set({ jm_queue: state }, r));
// Interruptible sleep — bails the instant a hard-stop is requested.
async function jmStoppableSleep(ms) {
  const step = 250;
  for (let t = 0; t < ms && !JM_Q.stop; t += step) await jmSleep(Math.min(step, ms - t));
}

function jmWaitForLoad(tabId, timeout) {
  return new Promise((resolve) => {
    let done = false;
    const finish = () => { if (done) return; done = true; clearTimeout(to); chrome.tabs.onUpdated.removeListener(onUpd); resolve(); };
    const onUpd = (id, info) => { if (id === tabId && info.status === "complete") finish(); };
    const to = setTimeout(finish, timeout || 25000);
    chrome.tabs.onUpdated.addListener(onUpd);
  });
}

// Write the per-tab review payload, then inject overlay.js so the user can review the gaps, upload
// their résumé, and submit. Written right before injection; the overlay captures it on load, so
// sequential jobs don't clobber each other's panels.
async function jmInjectReview(tab, cfg, item, res) {
  try {
    await chrome.storage.local.set({ jm_review: {
      result: res, token: cfg.token, apibase: cfg.apibase,
      title: item.title || "", company: item.company || "", url: item.url, fileName: ""
    } });
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["overlay.js"] });
  } catch (e) {}
}

async function jmProcessOne(item, cfg) {
  const fields = (cfg.ctx && cfg.ctx.fields) || {};
  const defaults = (cfg.ctx && cfg.ctx.defaults) || {};
  const learned = (cfg.ctx && cfg.ctx.learned) || {};

  let keepTab = false;                                 // leave the tab open whenever the user must act
  const tab = await chrome.tabs.create({ url: item.url, active: false });
  JM_Q.currentTabId = tab.id;
  try {
    await jmWaitForLoad(tab.id, 25000);
    if (JM_Q.stop) return { status: "stopped", reason: "stopped" };
    // Wait for the form to render (SPAs: Ashby / SmartRecruiters / EU Greenhouse); click "Apply"
    // once if the fields haven't appeared after ~3s.
    let ready = false;
    for (let k = 0; k < 9 && !ready && !JM_Q.stop; k++) {
      await jmStoppableSleep(1000);
      try {
        const rr = await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, world: "MAIN", func: jmFormReady });
        ready = (rr || []).some((o) => o && o.result);
      } catch (e) {}
      if (!ready && (k === 2 || k === 5)) {   // click "Apply" / "I'm interested" to reveal the form (retry once)
        try { await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, world: "MAIN", func: jmClickApply }); } catch (e) {}
      }
    }
    if (JM_Q.stop) return { status: "stopped", reason: "stopped" };

    // FILL: deterministic profile pass, then the learned-answer pass for whatever it left empty. No AI,
    // no file — résumé upload + submit are the user's.
    async function fillPass() {
      const o1 = await chrome.scripting.executeScript({
        target: { tabId: tab.id, allFrames: true }, world: "MAIN",
        func: jmFillApplication, args: [{ fields: fields, defaults: defaults }]
      });
      let r = (o1 || []).map((o) => o && o.result).filter(Boolean).find((x) => x && x.found) || { found: false };
      if (r.found && r.unfilled && r.unfilled.length) {
        try {
          const snap = await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, world: "MAIN", func: jmSnapshotForm });
          let snapFields = [];
          (snap || []).forEach((o) => { if (o && Array.isArray(o.result)) snapFields = snapFields.concat(o.result); });
          JM_Q.lastFields = snapFields;                  // captured for auto-diagnostics on failure
          const answers = jmMatchLearned(snapFields, learned);   // client-side, NO AI
          if (Object.keys(answers).length) {
            await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, world: "MAIN", func: jmApplyAnswers, args: [answers] });
            const o2 = await chrome.scripting.executeScript({
              target: { tabId: tab.id, allFrames: true }, world: "MAIN",
              func: jmFillApplication, args: [{ fields: fields, defaults: defaults }]
            });
            r = (o2 || []).map((o) => o && o.result).filter(Boolean).find((x) => x && x.found) || r;
          }
        } catch (e) {}
      }
      return r;
    }

    const res = await fillPass();
    if (res.login) return { status: "needs_you", reason: "login / account wall — open the tab to finish" };
    if (!res.found) return { status: "skipped", reason: "no supported form on page" };

    // Filled what we could. Show the review panel and leave the tab open for the user.
    keepTab = true;
    await jmInjectReview(tab, cfg, item, res);
    const left = (res.unfilled && res.unfilled.length) ? (" · " + res.unfilled.length + " for you") : "";
    if (res.captcha) return { status: "needs_you", reason: "filled — CAPTCHA on page; solve it, upload résumé & submit" + left };
    return { status: "ready", reason: "filled " + res.filled + "/" + res.total + " — upload résumé & submit" + left };
  } catch (e) {
    if (JM_Q.stop) return { status: "stopped", reason: "stopped" };   // tab was killed by hard-stop
    return { status: "error", reason: String((e && e.message) || e).slice(0, 140) };
  } finally {
    JM_Q.currentTabId = null;
    if (!keepTab) { try { await chrome.tabs.remove(tab.id); } catch (e) {} }   // close only the no-op tabs
  }
}

async function jmRunQueue(cfg) {
  if (JM_Q.running) return { ok: false, error: "already running" };
  JM_Q.running = true; JM_Q.stop = false;
  jmKeepAlive(true);                                                // survive the MV3 SW idle timer
  // Fetch profile + defaults + learned bank ONCE (no AI — this is just the user's data).
  try {
    cfg.ctx = await fetch(cfg.apibase + "/api/ext/profile_fields?token=" + encodeURIComponent(cfg.token)).then((r) => r.json());
  } catch (e) { cfg.ctx = {}; }
  const state = {
    running: true, total: cfg.items.length, idx: 0, startedAt: Date.now(),
    items: cfg.items.map((it) => ({ url: it.url, title: it.title || "", company: it.company || "", status: "queued", reason: "" }))
  };
  await jmSaveQueue(state);
  for (let i = 0; i < state.items.length; i++) {
    if (JM_Q.stop) { state.items[i].status = "stopped"; break; }
    state.idx = i; state.items[i].status = "running"; await jmSaveQueue(state);
    const r = await jmProcessOne(state.items[i], cfg);
    state.items[i].status = r.status; state.items[i].reason = r.reason;
    await jmSaveQueue(state);
    // Auto-report forms we couldn't fully fill (structure only, no user data) so they can be improved.
    if (["needs_you", "error", "skipped"].indexOf(r.status) >= 0) {
      fetch(cfg.apibase + "/api/ext/debug", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: cfg.token, url: state.items[i].url, company: state.items[i].company, status: r.status, reason: r.reason, fields: (JM_Q.lastFields || []).slice(0, 40) })
      }).catch(() => {});
    }
    if (i < state.items.length - 1 && !JM_Q.stop) await jmStoppableSleep(Math.min(cfg.delayMs || 6000, 20000));
  }
  if (JM_Q.stop) state.items.forEach((it) => { if (it.status === "queued" || it.status === "running") it.status = "stopped"; });
  state.running = false; JM_Q.running = false; JM_Q.currentTabId = null;
  jmKeepAlive(false);
  await jmSaveQueue(state);
  return { ok: true };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return;
  if (msg.type === "jm_queue_start") {
    // Fire-and-forget: a run can take a while. Acknowledge immediately so the popup's message channel
    // closes cleanly (progress is read from chrome.storage).
    jmRunQueue({ items: msg.items || [], apibase: msg.apibase, token: msg.token, delayMs: msg.delayMs || 6000 });
    sendResponse({ ok: true, started: true });
    return;                                            // synchronous reply — don't hold the channel
  }
  if (msg.type === "jm_queue_stop") {
    // Hard stop: flag it AND kill the in-flight tab so the current job's executeScript/waits abort now.
    JM_Q.stop = true;
    if (JM_Q.currentTabId) { try { chrome.tabs.remove(JM_Q.currentTabId); } catch (e) {} JM_Q.currentTabId = null; }
    sendResponse({ ok: true });
    return;
  }
});
