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
  if (a.name === "tesla-auto") jmRunTeslaImport({ trigger: "alarm" });
});

// The popup's "Run Tesla import now" button (works from any page).
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "run-tesla-now") {
    jmRunTeslaImport({ trigger: "manual" }).then(sendResponse);
    return true;                                   // keep the channel open for the async reply
  }
});
