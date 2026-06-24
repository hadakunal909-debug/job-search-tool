// overlay.js — the review-before-submit panel. Injected as a CONTENT SCRIPT (isolated world)
// via chrome.scripting.executeScript({files:["overlay.js"]}), so it has chrome.runtime/storage
// (to log the application through the background, which holds the backend host permissions and
// thus bypasses the page's CSP) while still sharing the page DOM (to click the native Submit).
//
// It reads what to show from chrome.storage.local "jm_review", written by the popup/background right
// before injection: { result, token, apibase, title, company, url, fileName }.
(function () {
  "use strict";
  var HOST_ID = "jm-review-overlay-host";
  var existing = document.getElementById(HOST_ID);
  if (existing) existing.remove();

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
    return null;
  }

  function render(R) {
    var res = R.result || {};
    var unfilled = res.unfilled || [];
    var captcha = !!document.querySelector(
      'iframe[src*="recaptcha"],iframe[src*="hcaptcha"],iframe[src*="turnstile"],.g-recaptcha,[class*="captcha" i]');

    var host = document.createElement("div");
    host.id = HOST_ID;
    host.style.cssText = "position:fixed;top:14px;right:14px;z-index:2147483647;";
    var root = host.attachShadow({ mode: "open" });
    (document.body || document.documentElement).appendChild(host);

    root.innerHTML =
      '<style>' +
      ':host{all:initial}' +
      '.card{font:13px/1.45 Inter,system-ui,Arial,sans-serif;width:320px;background:#fff;color:#0b1220;' +
      'border:1px solid #d7deea;border-radius:12px;box-shadow:0 10px 30px rgba(2,12,40,.22);overflow:hidden}' +
      '.hd{background:#0e8a5f;color:#fff;padding:10px 13px;font-weight:700;display:flex;justify-content:space-between;align-items:center}' +
      '.hd .x{cursor:pointer;opacity:.85;font-weight:400}' +
      '.bd{padding:12px 13px}' +
      '.stat{font-weight:600;margin:0 0 6px}' +
      '.dim{color:#5b6675;font-size:12px}' +
      '.warn{background:#fff5e6;border:1px solid #ffd591;color:#9a5b00;border-radius:8px;padding:7px 9px;margin:8px 0;font-size:12px}' +
      '.list{margin:8px 0 0;padding:0;list-style:none;max-height:150px;overflow:auto}' +
      '.list li{padding:5px 7px;border:1px solid #eef1f6;border-radius:7px;margin:4px 0;cursor:pointer;color:#b4232a;font-size:12px}' +
      '.list li:hover{background:#fef2f2}' +
      '.btns{display:flex;gap:8px;margin-top:12px}' +
      'button{flex:1;padding:9px;border:0;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer}' +
      '.primary{background:#0e8a5f;color:#fff}.ghost{background:#eef2f7;color:#222}' +
      '.msg{font-size:12px;margin-top:8px;min-height:14px;color:#0b7a52}' +
      '</style>' +
      '<div class="card">' +
      '<div class="hd"><span>JobMatch — review &amp; submit</span><span class="x" id="x">✕</span></div>' +
      '<div class="bd">' +
      '<p class="stat">Filled ' + res.filled + ' of ' + res.total + ' fields</p>' +
      '<div class="warn">📎 Upload your résumé yourself, then submit. This fills fields only — it never attaches files or auto-submits.</div>' +
      (captcha ? '<div class="warn">A verification (CAPTCHA) is on this page — solve it yourself before submitting.</div>' : '') +
      (unfilled.length
        ? '<p class="dim" style="margin-top:8px">Still needs you (click to jump):</p><ul class="list" id="uf">' +
          unfilled.map(function (u) { var up = (u.reason === "no file"); return '<li data-l="' + esc(u.label) + '">' + (up ? "📎 " : "✍️ ") + esc(u.label.slice(0, 70)) + (up ? " — upload" : "") + '</li>'; }).join("") + '</ul>'
        : '<p class="dim" style="margin-top:8px;color:#0e8a5f">No fields left for the tool. Upload your résumé, then submit.</p>') +
      '<div class="btns"><button class="primary" id="submit">Submit application</button>' +
      '<button class="ghost" id="dismiss">Dismiss</button></div>' +
      '<div class="msg" id="msg"></div>' +
      '</div></div>';

    function $(id) { return root.getElementById(id); }
    function msg(t, err) { var m = $("msg"); m.textContent = t; m.style.color = err ? "#b4232a" : "#0b7a52"; }

    $("x").onclick = $("dismiss").onclick = function () { host.remove(); };

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

    var confirmed = false;
    $("submit").onclick = function () {
      if (unfilled.length && !confirmed) {
        confirmed = true;
        $("submit").textContent = "Submit anyway";
        msg(unfilled.length + " required field(s) still empty — click again to submit anyway.", true);
        return;
      }
      // log to the tracker via the background (has backend host permission), then click native submit
      try {
        chrome.runtime.sendMessage({
          type: "jm_log_application", apibase: R.apibase, token: R.token,
          title: R.title, company: R.company, url: R.url, resume_name: R.fileName || ""
        }, function () { /* best-effort */ });
      } catch (e) {}
      var btn = res.submitSelector ? document.querySelector(res.submitSelector) : null;
      if (btn) {
        btn.scrollIntoView({ block: "center" });
        btn.click();
        msg("Submitted ✓ — confirm on the page. Logged to your tracker.");
        setTimeout(function () { host.remove(); }, 2500);
      } else {
        msg("Couldn't find the Submit button — please click it on the page.", true);
      }
    };
  }
})();
