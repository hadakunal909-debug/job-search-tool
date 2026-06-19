"use strict";
// Background service worker: runs the Tesla auto-import on a DAILY alarm, so the feed
// gets Tesla jobs without anyone clicking anything. The fetch rides the browser's own
// tesla.com cookies; if Akamai still challenges a cold session, the run records a note
// and the tesla_auto.js content script covers it the next time a Tesla page is open.
importScripts("tesla_shared.js");

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
