"use strict";
// autocapture.js — PASSIVE "training" capture. Runs as a content script on every page (isolated
// world, all frames). When you (or the auto-apply runner) submit / advance an application form, it
// reads how the fields were filled and hands them to the background worker, which POSTs them to
// /api/ext/learn — so the answer bank fills itself with NO button. It is strictly read-only (never
// changes the page), skips sensitive fields (password/SSN/card/DOB), and only fires when a form was
// actually filled (>= 2 fields) and a JobMatch token is configured (the background drops it
// otherwise). Lightweight on idle pages: it just attaches two listeners and does nothing until a
// submit-like action happens.
(function () {
  if (window.__jmAutoCaptureLoaded) return;            // guard against double-injection in a frame
  window.__jmAutoCaptureLoaded = true;
  // Don't capture the JobMatch app's own forms (login/profile) — only real application sites.
  if (/(^|\.)stemjobs\.astrochakra\.co$/.test(location.hostname) ||
      location.hostname === "localhost" || location.hostname === "127.0.0.1" ||
      /(^|\.)anthropic\.com$/.test(location.hostname)) return;

  // ----- capture: MUST stay in sync with filler.js jmCaptureFilled (read-only copy; isolated world
  // can read .value/.checked/.textContent directly, so no MAIN-world injection is needed here) -----
  function jmCaptureFilled() {
    function vis(el) {
      if (!el || el.disabled) return false;
      if (el.getAttribute && el.getAttribute("aria-hidden") === "true") return false;
      var r = el.getBoundingClientRect(), s = getComputedStyle(el);
      return s.display !== "none" && s.visibility !== "hidden" && r.width > 1 && r.height > 1;
    }
    function lbl(el) {
      var p = [];
      if (el.id) { var l = document.querySelector('label[for="' + (window.CSS && CSS.escape ? CSS.escape(el.id) : el.id) + '"]'); if (l) p.push(l.textContent); }
      var w = el.closest("label"); if (w) p.push(w.textContent);
      if (el.getAttribute("aria-label")) p.push(el.getAttribute("aria-label"));
      var alby = el.getAttribute("aria-labelledby");
      if (alby) alby.split(/\s+/).forEach(function (id) { var n = document.getElementById(id); if (n) p.push(n.textContent); });
      if (el.getAttribute("placeholder")) p.push(el.getAttribute("placeholder"));
      var c = el.closest("fieldset, .field, [class*=field], [class*=question], .select__container, .select");
      if (c) { var lg = c.querySelector("legend, label, .label, [class*=label]"); if (lg) p.push(lg.textContent); }
      return p.join(" ").replace(/\s+/g, " ").trim().slice(0, 200);
    }
    function isCombo(el) {
      return el.getAttribute("role") === "combobox" || el.getAttribute("aria-autocomplete") === "list" || !!el.closest(".select__container, [class*=select__]");
    }
    function comboVal(el) {
      var c = el.closest(".select__control") || el.closest(".select__container") || el.closest("[class*='select']");
      var sv = c && c.querySelector(".select__single-value, [class*='singleValue'], [class*='single-value']");
      return sv ? (sv.textContent || "").replace(/\s+/g, " ").trim() : "";
    }
    var SENSITIVE = /password|social security|\bssn\b|card number|cvv|cvc|routing|account number|date of birth|\bdob\b/i;
    var out = [], seen = {};
    document.querySelectorAll("input, select, textarea").forEach(function (el) {
      if (/hidden|submit|button|password|file/.test(el.type)) return;
      if (el.type === "radio" || el.type === "checkbox") return;
      if (!vis(el)) return;
      var label = lbl(el); if (!label || SENSITIVE.test(label)) return;
      var value = "", type = "text", options = null;
      if (el.tagName === "SELECT") { type = "select"; if (el.selectedIndex > 0) value = (el.options[el.selectedIndex].textContent || "").trim(); options = Array.prototype.map.call(el.options, function (o) { return (o.textContent || "").trim(); }).filter(Boolean).slice(0, 40); }
      else if (isCombo(el)) { type = "combobox"; value = comboVal(el); }
      else { value = String(el.value || "").trim(); }
      if (!value) return;
      if (seen[label]) return; seen[label] = 1;
      out.push({ label: label, type: type, value: value, options: options });
    });
    // radio / checkbox / ARIA-choice QUESTIONS — robust to forms with no <fieldset>/<legend>.
    (function () {
      var groups = {}, gid = 0, cmap = (typeof WeakMap !== "undefined") ? new WeakMap() : null;
      function ckey(c) { if (!c) return "c0"; if (cmap) { if (!cmap.has(c)) cmap.set(c, "c" + (++gid)); return cmap.get(c); } if (!c.__jmg) c.__jmg = "c" + (++gid); return c.__jmg; }
      function optEl(inp) { return inp.closest("label") || (inp.id && document.querySelector('label[for="' + (window.CSS && CSS.escape ? CSS.escape(inp.id) : inp.id) + '"]')) || inp; }
      function txt(el) { return ((el && el.getAttribute && el.getAttribute("aria-label")) || (el && el.textContent) || "").replace(/\s+/g, " ").trim(); }
      function add(key, el, t, on) { var g = groups[key] || (groups[key] = { els: [], texts: [], on: [] }); if (el) g.els.push(el); if (t) g.texts.push(t); if (on && t) g.on.push(t); }
      Array.prototype.forEach.call(document.querySelectorAll("input[type=radio], input[type=checkbox]"), function (inp) {
        var le = optEl(inp); if (!vis(inp) && !vis(le)) return;
        var key = inp.name ? ("n:" + inp.name) : ckey(inp.closest("fieldset, [role=radiogroup], [role=group]") || inp.parentElement);
        add(key, le, txt(le) || String(inp.value || "").trim(), inp.checked);
      });
      Array.prototype.forEach.call(document.querySelectorAll('[role=radio], [role=checkbox], [role=switch]'), function (el) {
        if (!vis(el)) return;
        add(ckey(el.closest("[role=radiogroup], [role=group]") || el.parentElement), el, txt(el),
            el.getAttribute("aria-checked") === "true" || el.getAttribute("aria-selected") === "true");
      });
      function lca(els) { var a = els[0]; for (var i = 1; i < els.length && a; i++) { while (a && !a.contains(els[i])) a = a.parentElement; } return a; }
      function question(g) {
        var node = g.els.length ? lca(g.els) : null;
        for (var hop = 0; node && hop < 6; hop++, node = node.parentElement) {
          var t = node.textContent || "";
          g.texts.forEach(function (o) { if (o) t = t.split(o).join(" "); });
          t = t.replace(/\s+/g, " ").trim();
          if (t.length >= 8 && /[a-z]/i.test(t)) return t.slice(0, 200);
        }
        return "";
      }
      Object.keys(groups).forEach(function (k) {
        var g = groups[k]; if (!g.on.length) return;
        var label = question(g);
        if (!label || SENSITIVE.test(label) || seen[label]) return;
        seen[label] = 1;
        out.push({ label: label, type: "radio", value: g.on.join(", "), options: g.texts.filter(Boolean).slice(0, 20) });
      });
    })();
    // custom TOGGLE / segmented-button answers (button/role toggles, no <input> — Ashby/Vanta etc.).
    (function () {
      if (typeof Map === "undefined") return;
      var NAV = /\b(submit|continue|next|back|previous|prev|apply|save|cancel|add|remove|delete|upload|browse|edit|search|close|menu|skip|sign in|log in|login)\b/;
      var cand = [];
      document.querySelectorAll('button, [role=radio], [role=button], [role=tab], [role=option], [role=switch]').forEach(function (el) {
        if (!vis(el) || el.querySelector("input")) return;
        var t = (el.textContent || el.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim();
        if (!t || t.length > 30 || NAV.test(t.toLowerCase())) return;
        var on = el.getAttribute("aria-checked") === "true" || el.getAttribute("aria-pressed") === "true" ||
                 el.getAttribute("aria-selected") === "true" || /(selected|active|checked|isselected|--on)/i.test(el.getAttribute("class") || "");
        cand.push({ el: el, t: t, on: on });
      });
      if (cand.length < 2) return;
      function group(el) {
        var node = el.parentElement;
        for (var hop = 0; node && hop < 6; hop++, node = node.parentElement) {
          var n = 0; for (var c = 0; c < cand.length; c++) if (node.contains(cand[c].el)) n++;
          if (n >= 2 && n <= 5) return node;
          if (n > 5) return null;
        }
        return null;
      }
      var groups = new Map();
      cand.forEach(function (o) { var g = group(o.el); if (!g) return; var rec = groups.get(g) || { texts: [], sel: "" }; rec.texts.push(o.t); if (o.on) rec.sel = o.t; groups.set(g, rec); });
      groups.forEach(function (rec, p) {
        if (rec.texts.length < 2 || rec.texts.length > 5 || !rec.sel) return;
        if (p.querySelector("input[type=radio], input[type=checkbox], select")) return;
        var node = p, label = "";
        for (var hop = 0; node && hop < 6; hop++, node = node.parentElement) {
          var tx = node.textContent || "";
          rec.texts.forEach(function (o) { if (o) tx = tx.split(o).join(" "); });
          tx = tx.replace(/\s+/g, " ").trim();
          if (tx.length >= 8 && /[a-z]/i.test(tx)) { label = tx; break; }
        }
        if (!label || SENSITIVE.test(label) || seen[label]) return;
        seen[label] = 1;
        out.push({ label: label, type: "radio", value: rec.sel, options: rec.texts.slice(0, 20) });
      });
    })();
    return out.slice(0, 50);
  }

  // Best-effort company from the ATS URL / page, so the "manage learned answers" UI has context.
  function guessCompany() {
    try {
      var h = location.hostname, seg = location.pathname.split("/").filter(Boolean);
      if (/(greenhouse\.io|lever\.co|ashbyhq\.com|smartrecruiters\.com)$/.test(h)) return (seg[0] || "").slice(0, 60);
      if (/myworkdayjobs\.com$/.test(h)) return (h.split(".")[0] || "").slice(0, 60);
      if (/tesla\.com$/.test(h)) return "Tesla";
      var og = document.querySelector('meta[property="og:site_name"]');
      if (og && og.content) return og.content.slice(0, 60);
      return (h.replace(/^www\./, "").split(".")[0] || "").slice(0, 60);
    } catch (e) { return ""; }
  }

  var lastAt = 0, lastSig = "";
  function captureAndSend() {
    var now = Date.now();
    if (now - lastAt < 1200) return;                   // debounce a single click's burst
    var fields;
    try { fields = jmCaptureFilled(); } catch (e) { return; }
    if (!fields || fields.length < 2) return;          // a real application step has several fields
    var sig = fields.length + "|" + fields.map(function (f) { return f.label + "=" + f.value; }).join("|");
    if (sig === lastSig && now - lastAt < 15000) return;
    lastAt = now; lastSig = sig;
    try { chrome.runtime.sendMessage({ type: "jm_autocapture", fields: fields, company: guessCompany(), url: location.href }); } catch (e) {}
  }

  // Triggers: native form submit, and clicks on submit / continue / next / apply / save controls
  // (SPA buttons that never fire a real submit). Capture phase so we read the filled state BEFORE
  // the form clears or the page navigates.
  function isSubmitish(el) {
    var b = el && el.closest && el.closest('button, input[type=submit], input[type=button], [role=button], a[href]');
    if (!b) return false;
    if (b.type === "submit") return true;
    var t = ((b.textContent || b.value || (b.getAttribute && b.getAttribute("aria-label")) || "") + "").toLowerCase();
    return /\b(submit|apply|continue|next|save|finish|review|agree)\b/.test(t);
  }
  document.addEventListener("submit", captureAndSend, true);
  document.addEventListener("click", function (e) {
    try { if (isSubmitish(e.target)) captureAndSend(); } catch (x) {}
  }, true);
})();
