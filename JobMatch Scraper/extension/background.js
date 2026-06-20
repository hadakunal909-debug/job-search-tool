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
const JM_Q = { stop: false, running: false, currentTabId: null };
// jmSleep is already defined in tesla_shared.js (imported above) — reuse it (don't redeclare,
// or the shared worker scope throws "jmSleep already declared" → SW registration fails, code 15).
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

async function jmProcessOne(item, cfg) {
  let t = null;
  for (let attempt = 0; attempt < 2; attempt++) {     // one retry on a transient network blip
    try {
      t = await fetch(cfg.apibase + "/api/ext/tailor", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: cfg.token, job_url: item.url, company: item.company, format: "pdf" })
      }).then((r) => r.json());
      break;
    } catch (e) { await jmSleep(1000); }
  }
  if (!t) return { status: "error", reason: "tailor network error" };
  if (!t.ok) return { status: "error", reason: "tailor: " + (t.error || "failed") };

  let keepTab = false;                                 // leave the tab open if a human must finish it
  const tab = await chrome.tabs.create({ url: item.url, active: false });
  JM_Q.currentTabId = tab.id;
  try {
    await jmWaitForLoad(tab.id, 25000);
    if (JM_Q.stop) return { status: "stopped", reason: "stopped" };
    // Wait for the form to actually render (SPAs: Ashby / SmartRecruiters / EU Greenhouse), and
    // click "Apply" once if the fields haven't appeared after ~3s.
    let ready = false;
    for (let k = 0; k < 9 && !ready && !JM_Q.stop; k++) {
      await jmStoppableSleep(1000);
      try {
        const rr = await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, world: "MAIN", func: jmFormReady });
        ready = (rr || []).some((o) => o && o.result);
      } catch (e) {}
      if (!ready && k === 2) {
        try { await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, world: "MAIN", func: jmClickApply }); } catch (e) {}
      }
    }
    if (JM_Q.stop) return { status: "stopped", reason: "stopped" };
    const out = await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true }, world: "MAIN",
      func: jmFillApplication, args: [{ fields: t.fields, file: t.file, defaults: t.defaults }]
    });
    let res = (out || []).map((o) => o && o.result).filter(Boolean).find((x) => x && x.found) || { found: false };
    if (!res.found) return { status: "skipped", reason: "no supported form on page" };
    if (res.captcha) return { status: "needs_you", reason: "CAPTCHA on page" };
    if (res.login) return { status: "needs_you", reason: "login / account wall" };

    // AI pass: for whatever the deterministic fill left empty, have the backend AI map the user's
    // profile + résumé onto those fields, apply the answers, then recompute the fill result.
    if ((res.unfilled && res.unfilled.length) || !res.fileAttached) {
      let aiF = 0, aiN = 0, aiM = 0;                    // fields snapshotted, AI answers, applied
      try {
        const snap = await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, world: "MAIN", func: jmSnapshotForm });
        let fields = [];
        (snap || []).forEach((o) => { if (o && Array.isArray(o.result)) fields = fields.concat(o.result); });
        aiF = fields.length;
        if (fields.length) {
          const a = await fetch(cfg.apibase + "/api/ext/answer", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ token: cfg.token, company: item.company, fields: fields.slice(0, 30) })
          }).then((r) => r.json()).catch(() => null);
          if (a && a.ok && a.answers) {
            aiN = Object.keys(a.answers).length;
            if (aiN) {
              const ap = await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, world: "MAIN", func: jmApplyAnswers, args: [a.answers] });
              aiM = (ap || []).reduce((s, o) => s + ((o && o.result && o.result.applied) || 0), 0);
              const out2 = await chrome.scripting.executeScript({
                target: { tabId: tab.id, allFrames: true }, world: "MAIN",
                func: jmFillApplication, args: [{ fields: t.fields, file: t.file, defaults: t.defaults }]
              });
              res = (out2 || []).map((o) => o && o.result).filter(Boolean).find((x) => x && x.found) || res;
            }
          } else if (a && a.error === "no_ai_key") {
            res._aiNoKey = true;
          }
        }
      } catch (e) {}
      res._ai = "AI f" + aiF + " a" + aiN + " ok" + aiM;   // diagnostic: snapshot / answered / applied
    }

    if (!res.fileAttached) return { status: "needs_you", reason: ("resume not attached | " + (res._ai || "")).slice(0, 120) };
    if (res.unfilled && res.unfilled.length)
      return { status: "needs_you", reason: ((res._aiNoKey ? "(set GEMINI_API_KEY) " : "") + res.unfilled.length + " req: " + res.unfilled.slice(0, 2).map((u) => u.label).join("; ") + " | " + (res._ai || "")).slice(0, 150) };

    if (cfg.dryRun) return { status: "ready", reason: "dry run — would submit (" + res.filled + "/" + res.total + " filled)" };
    if (!cfg.autosubmit) return { status: "ready", reason: "filled (auto-submit off)" };
    if (JM_Q.stop) return { status: "stopped", reason: "stopped" };   // never submit after a hard-stop

    const sub = await chrome.scripting.executeScript({
      target: { tabId: tab.id }, world: "MAIN", func: jmClickSubmit, args: [res.submitSelector]
    });
    if (!(sub && sub[0] && sub[0].result && sub[0].result.clicked))
      return { status: "needs_you", reason: "submit button not found" };
    await jmStoppableSleep(4000);
    const stt = await chrome.scripting.executeScript({
      target: { tabId: tab.id }, world: "MAIN", func: jmApplyState, args: [item.url]
    });
    const s = (stt && stt[0] && stt[0].result) || {};
    if (s.captcha) {                                   // genuine challenge popped on submit — let the user solve it
      keepTab = true;
      try { await chrome.tabs.update(tab.id, { active: true }); } catch (e) {}
      return { status: "needs_you", reason: "CAPTCHA on submit — tab left open; solve it & click submit" };
    }
    // Log to the tracker either way (submit was clicked) so there's a record to verify.
    fetch(cfg.apibase + "/api/ext/save", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: cfg.token, title: item.title, company: item.company, url: item.url })
    }).catch(() => {});
    // Only claim success on a REAL confirmation ("thank you / application received"). A mere URL
    // change ("page advanced") often just means a multi-step form moved on — don't call that done.
    if (s.confirmed) return { status: "submitted", reason: "confirmation page detected" };
    keepTab = true;                                    // leave open so the user can verify / finish
    return { status: "check", reason: s.changed
      ? "submit clicked, page advanced — VERIFY (no confirmation; may be a multi-step form)"
      : "submit clicked — VERIFY (no confirmation seen)" };
  } catch (e) {
    if (JM_Q.stop) return { status: "stopped", reason: "stopped" };   // tab was killed by hard-stop
    return { status: "error", reason: String((e && e.message) || e).slice(0, 140) };
  } finally {
    JM_Q.currentTabId = null;
    if (!keepTab) { try { await chrome.tabs.remove(tab.id); } catch (e) {} }
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
    if (i < state.items.length - 1 && !JM_Q.stop) await jmStoppableSleep(Math.min(cfg.delayMs || 8000, 20000));
  }
  if (JM_Q.stop) state.items.forEach((it) => { if (it.status === "queued" || it.status === "running") it.status = "stopped"; });
  state.running = false; JM_Q.running = false; JM_Q.currentTabId = null;
  await jmSaveQueue(state);
  return { ok: true };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return;
  if (msg.type === "jm_queue_start") {
    // Fire-and-forget: a run can take many minutes. Acknowledge immediately so the popup's message
    // channel closes cleanly (progress is read from chrome.storage). Previously we returned true and
    // awaited the whole queue, which logged "message channel closed before a response" once the popup
    // closed — harmless, but noisy.
    jmRunQueue({
      items: msg.items || [], apibase: msg.apibase, token: msg.token,
      dryRun: msg.dryRun !== false, autosubmit: !!msg.autosubmit, delayMs: msg.delayMs || 8000
    });
    sendResponse({ ok: true, started: true });
    return;                                            // synchronous reply — don't hold the channel
  }
  if (msg.type === "jm_queue_stop") {
    // Hard stop: flag it AND kill the in-flight tab so the current job's executeScript/waits abort
    // immediately instead of finishing first.
    JM_Q.stop = true;
    if (JM_Q.currentTabId) { try { chrome.tabs.remove(JM_Q.currentTabId); } catch (e) {} JM_Q.currentTabId = null; }
    sendResponse({ ok: true });
    return;
  }
});
