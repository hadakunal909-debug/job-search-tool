// Resume Brain — small progressive-enhancement layer (no inline handlers; CSP-friendly).
(function () {
  "use strict";

  // ---- theme toggle ----
  var toggle = document.getElementById("themeToggle");
  function syncIcon() {
    if (toggle) toggle.textContent =
      document.documentElement.getAttribute("data-theme") === "dark" ? "☀️" : "🌙";
  }
  syncIcon();
  if (toggle) {
    toggle.addEventListener("click", function () {
      var next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("rb_theme", next); } catch (e) {}
      syncIcon();
    });
  }

  // ---- copy buttons (data-copy="#id") ----
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy]");
    if (!btn) return;
    var el = document.querySelector(btn.getAttribute("data-copy"));
    if (!el) return;
    el.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e2) {}
    if (navigator.clipboard) { navigator.clipboard.writeText(el.value).catch(function () {}); ok = true; }
    var old = btn.textContent;
    btn.textContent = ok ? "✓ Copied" : "Press Ctrl+C";
    setTimeout(function () { btn.textContent = old; }, 1600);
  });

  // ---- confirm-delete forms (data-confirm="...") ----
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (f.matches("[data-confirm]") && !window.confirm(f.getAttribute("data-confirm"))) {
      e.preventDefault();
    }
  });

  // ---- tailor: show "thinking" + disable button on submit ----
  var form = document.getElementById("tailorForm");
  if (form) {
    form.addEventListener("submit", function () {
      var btn = document.getElementById("tailorBtn");
      var t = document.getElementById("thinking");
      if (btn) { btn.disabled = true; btn.textContent = "🧠 Thinking…"; }
      if (t) t.hidden = false;
    });
  }

  // ---- AI rewrite: disable button (the call takes ~10-20s) ----
  var rform = document.getElementById("rewriteForm");
  if (rform) {
    rform.addEventListener("submit", function () {
      var b = document.getElementById("rewriteBtn");
      if (b) { b.disabled = true; b.textContent = "✍️ Writing… (10–20s)"; }
    });
  }
})();
