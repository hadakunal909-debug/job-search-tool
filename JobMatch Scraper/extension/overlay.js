// overlay.js — the review-before-submit panel. Injected as a CONTENT SCRIPT (isolated world)
// via chrome.scripting.executeScript({files:["overlay.js"]}), so it has chrome.runtime/storage
// (to log the application through the background, which holds the backend host permissions and
// thus bypasses the page's CSP) while still sharing the page DOM (to click the native Submit).
//
// It reads what to show from chrome.storage.local "jm_review", written by the popup/background right
// before injection: { result, token, apibase, title, company, url, fileName }.
//
// On the multi-step ATS (Workday / Oracle / iCIMS — half our feed) one application spans several
// pages. The panel therefore also WATCHES for the step changing and asks the background to fill the
// new step, so you click Next and the next page arrives already filled. It never clicks Next itself,
// and on a non-final step it does not offer Submit — there is nothing to submit yet.
(function () {
  "use strict";
  var HOST_ID = "jm-review-overlay-host";
  // render() re-runs itself after each step fill, so every pass must retire the previous pass's
  // watcher. GEN is the live generation: a stale closure sees GEN move and stops. Without this each
  // step would leave its MutationObserver + interval running and they'd all trigger the next fill.
  var GEN = 0, WATCH = { obs: null, poll: 0 };
  function teardown() {
    try { if (WATCH.obs) WATCH.obs.disconnect(); } catch (e) {}
    if (WATCH.poll) clearInterval(WATCH.poll);
    WATCH.obs = null; WATCH.poll = 0;
  }

  chrome.storage.local.get(["jm_review"], function (st) {
    var R = st && st.jm_review;
    if (R && R.result) render(R);
  });

  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
    return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]; }); }

  function findFieldByLabel(snippet) {
    snippet = String(snippet || "").toLowerCase().replace(/\*|\(required\)|required/g, "").trim().slice(0, 30);
    if (!snippet) return null;
    var labs = document.querySelectorAll("label, legend");
    for (var i = 0; i < labs.length; i++) {
      if ((labs[i].textContent || "").toLowerCase().indexOf(snippet) >= 0) {
        var f = labs[i].getAttribute && labs[i].getAttribute("for");
        var el = f ? document.getElementById(f) : labs[i].querySelector("input,select,textarea");
        return el || labs[i];
      }
    }
    // Workday labels its dropdowns by automation id rather than <label for>, so fall back to that.
    var byAid = document.querySelector('[data-automation-id*="' + snippet.split(" ")[0] + '" i]');
    return byAid || null;
  }

  // What step are we on? Cheap fingerprint of the visible form — URL, the wizard's active step text,
  // and how many controls are on screen. When this changes, the page moved and needs filling again.
  function stepSig() {
    var active = document.querySelector(
      '[data-automation-id="progressBar"] [aria-current], [role="tablist"] [aria-selected="true"], ' +
      '.apply-flow__progress li[class*="active"], ol[class*="progress"] li[class*="active"]');
    var n = 0;
    try {
      n = document.querySelectorAll('input:not([type=hidden]), select, textarea, button[aria-haspopup="listbox"], oj-select-single').length;
    } catch (e) {}
    return location.href + "|" + ((active && active.textContent) || "").replace(/\s+/g, " ").trim() + "|" + n;
  }

  // Click Submit: the adapter's selector first, then a text match, so an ATS whose submit button we
  // don't have a selector for still works instead of dead-ending on "couldn't find it".
  function findSubmit(sel) {
    var btn = sel ? document.querySelector(sel) : null;
    if (btn && btn.offsetParent !== null) return btn;
    var all = Array.prototype.slice.call(document.querySelectorAll('button, input[type=submit], [role=button]'));
    return all.filter(function (b) {
      var t = (b.textContent || b.value || "").replace(/\s+/g, " ").trim();
      if (!t || t.length > 40 || b.offsetParent === null || b.disabled) return false;
      if (/back|cancel|previous|save draft|save for later|sign ?in|log ?in/i.test(t)) return false;
      return /^(submit|submit application|finish|send application)$/i.test(t);
    })[0] || null;
  }

  function render(R) {
    var old = document.getElementById(HOST_ID);
    if (old) old.remove();
    teardown();
    var gen = ++GEN;
    function live() { return gen === GEN; }

    var res = R.result || {};
    var unfilled = res.unfilled || [];
    var wiz = res.wizard || null;
    // A non-final wizard step has nothing to submit yet — offer filling/advancing instead.
    var midWizard = !!(wiz && wiz.total > 1 && !wiz.isLast);
    var captcha = !!document.querySelector(
      'iframe[src*="recaptcha"],iframe[src*="hcaptcha"],iframe[src*="turnstile"],.g-recaptcha,[class*="captcha" i]');

    var host = document.createElement("div");
    host.id = HOST_ID;
    host.style.cssText = "position:fixed;top:14px;right:14px;z-index:2147483647;";
    var root = host.attachShadow({ mode: "open" });
    (document.body || document.documentElement).appendChild(host);

    var stepLine = wiz && wiz.total > 1
      ? '<p class="dim">' + (res.ats ? esc(res.ats) + ' · ' : '') + 'Step ' + (wiz.index || "?") + ' of ' + wiz.total +
        (wiz.step ? ': ' + esc(wiz.step) : '') + '</p>'
      : (res.ats ? '<p class="dim">' + esc(res.ats) + '</p>' : '');

    root.innerHTML =
      '<style>' +
      ':host{all:initial}' +
      '.card{font:13px/1.45 Inter,system-ui,Arial,sans-serif;width:320px;background:#fff;color:#0b1220;' +
      'border:1px solid #d7deea;border-radius:12px;box-shadow:0 10px 30px rgba(2,12,40,.22);overflow:hidden}' +
      '.hd{background:#0e8a5f;color:#fff;padding:10px 13px;font-weight:700;display:flex;justify-content:space-between;align-items:center}' +
      '.hd .x{cursor:pointer;opacity:.85;font-weight:400}' +
      '.bd{padding:12px 13px}' +
      '.stat{font-weight:600;margin:0 0 6px}' +
      '.dim{color:#5b6675;font-size:12px;margin:0 0 6px}' +
      '.warn{background:#fff5e6;border:1px solid #ffd591;color:#9a5b00;border-radius:8px;padding:7px 9px;margin:8px 0;font-size:12px}' +
      '.info{background:#eef6ff;border:1px solid #b6d8ff;color:#14507f;border-radius:8px;padding:7px 9px;margin:8px 0;font-size:12px}' +
      '.list{margin:8px 0 0;padding:0;list-style:none;max-height:150px;overflow:auto}' +
      '.list li{padding:5px 7px;border:1px solid #eef1f6;border-radius:7px;margin:4px 0;cursor:pointer;color:#b4232a;font-size:12px}' +
      '.list li:hover{background:#fef2f2}' +
      '.btns{display:flex;gap:8px;margin-top:12px}' +
      'button{flex:1;padding:9px;border:0;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer}' +
      '.primary{background:#0e8a5f;color:#fff}.ghost{background:#eef2f7;color:#222}' +
      '.msg{font-size:12px;margin-top:8px;min-height:14px;color:#0b7a52}' +
      '</style>' +
      '<div class="card">' +
      '<div class="hd"><span>JobMatch review' + (midWizard ? '' : ' &amp; submit') + '</span><span class="x" id="x">✕</span></div>' +
      '<div class="bd">' +
      '<p class="stat">Filled ' + res.filled + ' of ' + res.total + ' fields</p>' +
      stepLine +
      (midWizard
        ? '<div class="info">This application has ' + wiz.total + ' steps. Click <b>Next</b> on the page when this one looks right — I\'ll fill the next step automatically.</div>'
        : '<div class="warn">📎 Upload your résumé yourself, then submit. This fills fields only. It never attaches files and never submits for you.</div>') +
      (captcha ? '<div class="warn">A verification (CAPTCHA) is on this page. Solve it yourself before submitting.</div>' : '') +
      (unfilled.length
        ? '<p class="dim" style="margin-top:8px">Still needs you (click to jump):</p><ul class="list" id="uf">' +
          unfilled.map(function (u) { var up = (u.reason === "no file"); return '<li data-l="' + esc(u.label) + '">' + (up ? "📎 " : "✍️ ") + esc(u.label.slice(0, 70)) + (up ? ", upload it yourself" : "") + '</li>'; }).join("") + '</ul>'
        : '<p class="dim" style="margin-top:8px;color:#0e8a5f">No fields left for the tool' + (midWizard ? ' on this step.' : '. Upload your résumé, then submit.') + '</p>') +
      '<div class="btns">' +
      (midWizard
        ? '<button class="ghost" id="refill">Fill this step again</button>'
        : '<button class="primary" id="submit">Submit application</button>') +
      '<button class="ghost" id="dismiss">Dismiss</button></div>' +
      '<div class="msg" id="msg"></div>' +
      '</div></div>';

    function $(id) { return root.getElementById(id); }
    function msg(t, err) { var m = $("msg"); if (m) { m.textContent = t; m.style.color = err ? "#b4232a" : "#0b7a52"; } }

    function shutdown() { GEN++; teardown(); host.remove(); }   // GEN++ retires this pass's callbacks
    $("x").onclick = $("dismiss").onclick = shutdown;

    if ($("uf")) {
      Array.prototype.forEach.call($("uf").querySelectorAll("li"), function (li) {
        li.onclick = function () {
          var el = findFieldByLabel(li.getAttribute("data-l"));
          if (!el) { msg("Couldn't locate that field on the page.", true); return; }
          el.scrollIntoView({ behavior: "smooth", block: "center" });
          var prev = el.style.outline;
          el.style.outline = "2px solid #f59e0b";
          setTimeout(function () { el.style.outline = prev; }, 1800);
          if (el.focus) try { el.focus(); } catch (e) {}
        };
      });
    }

    // ---- ask the background to (re)fill the form now on screen, then redraw this panel ----
    var filling = false, sig = stepSig(), timer = null, refills = 0;
    function refill(why) {
      if (filling || !live()) return;
      filling = true;
      msg(why || "Filling this step…");
      try {
        chrome.runtime.sendMessage({ type: "jm_fill_step" }, function (r) {
          filling = false;
          if (!live()) return;
          if (r && r.ok && r.result) {
            // render() bumps GEN, which retires this closure — so nothing below runs twice.
            render({ result: r.result, token: R.token, apibase: R.apibase,
                     title: R.title, company: R.company, url: location.href, fileName: R.fileName });
          } else {
            sig = stepSig();                                   // don't re-ask about a step we can't fill
            msg((r && r.error) ? ("Couldn't fill: " + r.error) : "Nothing to fill on this step.", true);
          }
        });
      } catch (e) { filling = false; }
    }
    if ($("refill")) $("refill").onclick = function () { refill("Filling…"); };

    // ---- step watcher (multi-step ATS only) ----
    // Only armed when the page actually reports a wizard, so a normal single-page form is never
    // re-filled on every DOM mutation. Capped so a page that mutates constantly can't loop forever.
    if (wiz && wiz.total > 1) {
      var onChange = function () {
        if (!live() || filling || refills >= 12) return;
        clearTimeout(timer);
        timer = setTimeout(function () {
          if (!live() || filling) return;
          var now = stepSig();
          if (now === sig) return;
          sig = now; refills++;
          refill("New step — filling…");
        }, 1200);                                              // let the step finish rendering first
      };
      try {
        WATCH.obs = new MutationObserver(onChange);
        WATCH.obs.observe(document.body || document.documentElement, { childList: true, subtree: true });
      } catch (e) {}
      WATCH.poll = setInterval(onChange, 2500);                // SPA route changes that mutate nothing visible
    }

    var confirmed = false;
    if ($("submit")) $("submit").onclick = function () {
      if (unfilled.length && !confirmed) {
        confirmed = true;
        $("submit").textContent = "Submit anyway";
        msg(unfilled.length + " required field(s) still empty. Click again to submit anyway.", true);
        return;
      }
      // log to the tracker via the background (has backend host permission), then click native submit
      try {
        chrome.runtime.sendMessage({
          type: "jm_log_application", apibase: R.apibase, token: R.token,
          title: R.title, company: R.company, url: R.url, resume_name: R.fileName || ""
        }, function () { /* best-effort */ });
      } catch (e) {}
      var btn = findSubmit(res.submitSelector);
      if (btn) {
        btn.scrollIntoView({ block: "center" });
        btn.click();
        msg("Submitted ✓. Confirm on the page. Logged to your tracker.");
        setTimeout(shutdown, 2500);
      } else {
        msg("Couldn't find the Submit button. Please click it on the page.", true);
      }
    };
  }
})();
