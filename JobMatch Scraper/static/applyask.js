/* "Did you apply?" -- the one thing that decides whether the Applied number is true.
 *
 * Clicking Apply opens the employer's site in a new tab, and what happens there is unknowable
 * from here: most clicks end at a form nobody finished. This file parks the job on click and
 * asks when the user comes back. NOTHING is written until they answer yes.
 *
 * It replaces the opposite behaviour. app.js used to POST status="applied" from the click
 * handler, so OPENING a posting counted as applying to it, and the tracker held 129
 * applications nobody had made.
 *
 * Shared rather than duplicated: app.js returns early unless #feed exists, so the job page
 * cannot use it, and this repo already carries about sixteen function-for-function twins
 * between app.js and web.py that have to be kept in step by hand. A seventeenth, holding the
 * rule for what counts as an application, is not worth the convenience.
 *
 * Talks to the rest of the app in one direction only: it fires `jm:applied` on document when a
 * confirmation lands, and whoever cares about repainting a card listens for that. It reads no
 * other script's state, so load order does not matter.
 */
(function () {
  "use strict";

  var KEY = "jm.pending_apply";
  var TTL = 3 * 24 * 3600 * 1000;   // after three days they will not remember. Drop it silently.
  /* You cannot complete an application in 25 seconds. Coming back faster means the page was
     opened and abandoned, or the user just alt-tabbed -- so keep waiting rather than ask a
     question whose answer is obviously "no" and teach them to swat the prompt away. */
  var MIN_DWELL = 25000;
  var wentAway = false, asking = false;

  function read() {
    try {
      var raw = window.localStorage.getItem(KEY), now = Date.now();
      return (raw ? JSON.parse(raw) : []).filter(function (p) {
        return p && p.u && (now - (p.at || 0)) < TTL;
      });
    } catch (e) { return []; }       /* private mode, or a value written in an older shape */
  }
  function write(list) {
    try {
      if (list.length) window.localStorage.setItem(KEY, JSON.stringify(list));
      else window.localStorage.removeItem(KEY);
    } catch (e) { /* best-effort by design: losing the prompt must never break Apply */ }
  }
  function drop(url) {
    write(read().filter(function (p) { return p.u !== url; }));
  }

  function csrf() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute("content") : "";
  }

  /* Its own toast rather than a hook into app.js's: #toasts is in base.html on every page, and
     six lines of createElement is a smaller price than a load-order dependency. */
  function say(msg) {
    var box = document.getElementById("toasts");
    if (!box) return;
    var t = document.createElement("div");
    t.className = "toast";
    t.textContent = msg;
    box.appendChild(t);
    setTimeout(function () {
      t.style.transition = "opacity .3s"; t.style.opacity = "0";
      setTimeout(function () { t.remove(); }, 300);
    }, 2300);
  }

  /* A toast that ASKS instead of telling, and so does not time out: an unanswered question
     that quietly disappears is worse than never asking, because the user believes they
     answered it. There is no dismiss -- "No" is one click and writes nothing, so there is
     nothing to escape from. Escape therefore answers no. */
  function ask1(msg, onYes, onNo) {
    var box = document.getElementById("toasts");
    if (!box) { onNo(); return; }
    var t = document.createElement("div");
    t.className = "toast toast-ask";
    t.setAttribute("role", "alertdialog");
    var q = document.createElement("span");
    q.className = "ask-q";
    q.textContent = msg;
    t.appendChild(q);
    var row = document.createElement("span");
    row.className = "ask-btns";
    function close() { document.removeEventListener("keydown", onKey); t.remove(); }
    function onKey(e) { if (e.key === "Escape") { close(); onNo(); } }
    [["No", "ask-no", onNo], ["Yes, I applied", "ask-yes", onYes]].forEach(function (s) {
      var b = document.createElement("button");
      b.type = "button"; b.className = s[1]; b.textContent = s[0];
      b.addEventListener("click", function () { close(); s[2](); });
      row.appendChild(b);
    });
    t.appendChild(row);
    box.appendChild(t);
    document.addEventListener("keydown", onKey);
    /* Focus the affirmative so Enter answers it, but never steal focus from something the user
       is already typing into -- the feed's search box is one keystroke away from this. */
    var ae = document.activeElement;
    if (!ae || !ae.matches || !ae.matches("input,textarea,select")) {
      var y = row.querySelector(".ask-yes"); if (y) y.focus();
    }
  }

  /* Ask about ONE posting at a time, oldest first, and only move on once it is answered: four
     stacked questions about four postings is a form, not a prompt. */
  function ask() {
    if (asking) return;
    var due = read().filter(function (p) { return Date.now() - p.at >= MIN_DWELL; });
    if (!due.length) return;
    due.sort(function (a, b) { return a.at - b.at; });
    var p = due[0];
    asking = true;
    var who = p.c ? (p.t ? p.t + " at " + p.c : p.c) : (p.t || "that job");
    ask1("Did you apply to " + who + "?",
      function () {
        drop(p.u);
        fetch("/api/action", {
          method: "POST",
          headers: { "X-CSRF-Token": csrf(), "Content-Type": "application/json" },
          /* via="confirmed" is what separates a real application from an outbound click in
             every aggregate downstream. The server whitelists the value. */
          body: JSON.stringify({ url: p.u, status: "applied", via: "confirmed" })
        }).then(function (r) { return r.json(); }).then(function (j) {
          asking = false;
          if (!j || !j.ok) { say("Couldn't save that. Try again."); return; }
          say("Added to Applications");
          document.dispatchEvent(new CustomEvent("jm:applied", { detail: { url: p.u } }));
          ask();
        }).catch(function () {
          asking = false;
          say("Network error.");
        });
      },
      function () {
        /* Writes NOTHING -- not a status, not an event. "I opened it and did not apply" is
           already on record as the apply_click that parked this question; a second row saying
           the same thing would only be another number to explain later. */
        drop(p.u);
        asking = false;
        ask();
      });
  }

  window.ApplyAsk = {
    /* job: {url, title, company}. A second click on the same posting resets its clock rather
       than queuing a duplicate question. */
    pend: function (job) {
      if (!job || !job.url) return;
      var list = read().filter(function (p) { return p.u !== job.url; });
      list.push({ u: job.url, t: job.title || "", c: job.company || "", at: Date.now() });
      write(list);
    },
    ask: ask
  };

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) wentAway = true;
    else if (wentAway) { wentAway = false; ask(); }
  });
  /* Backstop for what visibilitychange misses: the tab stayed visible but lost focus, which is
     what happens when the posting opens in a separate window rather than a background tab. */
  window.addEventListener("focus", function () { ask(); });
  /* And the closed-tab case -- feed shut, application filled in elsewhere, feed reopened later.
     Anything still parked is older than the dwell by definition. */
  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", ask);
  else
    ask();
})();
