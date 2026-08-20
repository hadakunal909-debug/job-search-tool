/* resume_review.js — tabs and click-to-highlight for the Resume Brain review panel.
 *
 * Everything it needs is already in the DOM. The server rendered the document once with every
 * offending span wrapped in <mark data-checks="key1 key2">, all inert, so selecting a check is a
 * class toggle rather than a re-fetch — instant, and the marks cannot drift out of step with the
 * counts in the rail because both came from the same report.
 *
 * Marks carry a LIST of keys, not one: "Responsible for" is filler and "Responsible" is a weak
 * opener, so their spans genuinely overlap and the server cut the text at every boundary.
 *
 * No framework, no inline handlers (the CSP has no unsafe-inline), and it degrades to a plain
 * scrollable document if the file fails to load.
 */
(function () {
  "use strict";

  var doc = document.getElementById("rvdoc");
  var label = document.getElementById("rvactive");
  var marks = doc ? Array.prototype.slice.call(doc.querySelectorAll("mark[data-checks]")) : [];
  var current = "";

  /* ---------------------------------------------------------------- tabs */
  var tabs = Array.prototype.slice.call(document.querySelectorAll(".rvtab"));
  var panels = Array.prototype.slice.call(document.querySelectorAll(".rvpanel"));

  function showTab(name, focus) {
    tabs.forEach(function (t) {
      var on = t.getAttribute("data-tab") === name;
      t.classList.toggle("on", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
      // Roving tabindex: the tablist is ONE tab stop, and arrow keys move within it. Leaving every
      // tab focusable makes a keyboard user press Tab five times to get past the tabs.
      t.setAttribute("tabindex", on ? "0" : "-1");
      if (on && focus) { t.focus(); }
    });
    panels.forEach(function (p) {
      p.hidden = p.getAttribute("data-panel") !== name;
    });
  }

  tabs.forEach(function (t) {
    t.addEventListener("click", function () { showTab(t.getAttribute("data-tab")); });
  });

  // Cross-tab hand-off. A fix row on the Review tab whose per-line advice lives under Bullets
  // sends the user there rather than restating it. Routed through showTab (with focus) so the
  // roving tabindex and aria-selected stay correct -- flipping `hidden` directly would leave a
  // keyboard user on a tab the tablist no longer thinks is current.
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest("[data-goto-tab]");
    if (!b) { return; }
    ev.preventDefault();
    showTab(b.getAttribute("data-goto-tab"), true);
  });

  // Arrow-key movement, which is what makes role="tablist" mean anything.
  document.addEventListener("keydown", function (ev) {
    var i = tabs.indexOf(document.activeElement);
    if (i < 0) { return; }
    var next = null;
    if (ev.key === "ArrowRight") { next = (i + 1) % tabs.length; }
    else if (ev.key === "ArrowLeft") { next = (i - 1 + tabs.length) % tabs.length; }
    else if (ev.key === "Home") { next = 0; }
    else if (ev.key === "End") { next = tabs.length - 1; }
    if (next === null) { return; }
    ev.preventDefault();
    showTab(tabs[next].getAttribute("data-tab"), true);
  });

  /* ------------------------------------------------------- highlighting */
  function has(mark, key) {
    // Space-separated token match, not indexOf: "spelling" must not match "spelling_x", and
    // "spacing_hygiene".indexOf("spacing") would be a false positive on any prefix.
    var list = (mark.getAttribute("data-checks") || "").split(/\s+/);
    return list.indexOf(key) !== -1;
  }

  function select(key) {
    // Clicking the selected check again clears it, so the document can be read unmarked.
    current = (key === current) ? "" : key;
    marks.forEach(function (m) { m.classList.toggle("on", !!current && has(m, current)); });

    document.querySelectorAll(".rvcheck.sel, .rs-fixes > li.sel").forEach(function (el) {
      el.classList.remove("sel");
    });
    // These are two-state controls. Without aria-pressed they announce as plain buttons and a
    // screen-reader user has no way to know which check is currently marking the document.
    document.querySelectorAll(".rvcheck[aria-pressed]").forEach(function (el) {
      el.setAttribute("aria-pressed", el.getAttribute("data-check") === current ? "true" : "false");
    });

    var n = 0;
    if (current) {
      document.querySelectorAll('[data-check="' + current + '"]').forEach(function (el) {
        el.classList.add("sel");
      });
      // The count comes from the server, not from counting <mark> elements. Overlapping spans were
      // split into separate runs ("Responsible for" is filler AND "Responsible" is a weak opener),
      // so a DOM count reports 4 where there are 2 offences — and a rail that disagrees with its
      // own document is worse than no count at all.
      var btn = document.querySelector('.rvcheck[data-check="' + current + '"]');
      var name = btn ? btn.querySelector(".rvc-name") : null;
      n = btn && btn.getAttribute("data-count")
        ? btn.getAttribute("data-count")
        : marks.filter(function (m) { return has(m, current); }).length;
      if (label) {
        label.textContent = (name ? name.textContent : current) + " · " + n + " marked";
      }
      showTab("review");
      var first = marks.filter(function (m) { return has(m, current); })[0];
      if (first && first.scrollIntoView) {
        // Honour the OS setting. Someone who has asked for less motion has usually asked because
        // smooth scrolling makes them ill, and this fires on every click.
        var calm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        first.scrollIntoView({ block: "center", behavior: calm ? "auto" : "smooth" });
      }
    } else if (label) {
      label.textContent = "nothing selected";
    }
  }

  // One listener on the document rather than one per row: the rail and the fix list both carry
  // data-check, and delegation means neither has to be re-wired if the markup moves.
  document.addEventListener("click", function (ev) {
    // The cross-tab hand-off button lives INSIDE a fix row, which carries data-check. Without this
    // guard the click does both things: showTab("bullets") from the hand-off, then select() ->
    // showTab("review") from this handler, so the user lands back where they started with focus
    // stranded on a tab that is no longer selected. Checked explicitly rather than by
    // stopPropagation, because both listeners are on `document` and would then depend on
    // registration order.
    if (ev.target.closest && ev.target.closest("[data-goto-tab]")) { return; }
    var hit = ev.target.closest ? ev.target.closest("[data-check]") : null;
    if (!hit || hit.classList.contains("flat")) { return; }
    if (hit.tagName === "A") { return; }          // never swallow a real navigation
    ev.preventDefault();
    select(hit.getAttribute("data-check"));
  });

  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" && current) { select(current); }
  });
}());
