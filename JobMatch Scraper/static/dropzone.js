/* Makes a .dropzone label behave like the control it looks like.
 *
 * Three jobs, none of which the markup can do alone:
 *
 *   1. Echo the chosen filename into [data-dz-label]. The native "Choose File" button used to
 *      show this, and hiding the input to get a designed control silently took it away -- so
 *      after picking a file the zone still read "Choose a File", which looks like a failure.
 *   2. Accept an actual drop. The input is clipped to 1px off-screen (style.css, so the label
 *      can open the picker), which also removes it from hit testing -- so the "or Drop One
 *      Here" in the label was a promise nothing kept. Dropping on the LABEL and assigning to
 *      input.files is what makes it true.
 *   3. Paint the drag state, because a drop target that doesn't react reads as inert.
 *
 * Delegated and idempotent: any .dropzone on the page is handled, including one added later,
 * and loading this file twice costs three more listeners and changes no behaviour.
 */
(function () {
  "use strict";

  function zoneOf(el) { return el && el.closest ? el.closest(".dropzone") : null; }
  function inputOf(z) { return z ? z.querySelector('input[type="file"]') : null; }

  /* The label's own text is the fallback, so a cleared input reverts to whatever the template
     said rather than to a hardcoded string this file would have to keep in step. */
  function label(z) {
    var el = z.querySelector("[data-dz-label]");
    if (el && !el.hasAttribute("data-dz-idle")) el.setAttribute("data-dz-idle", el.textContent);
    return el;
  }

  function show(z) {
    var el = label(z), inp = inputOf(z);
    if (!el || !inp) return;
    var f = inp.files && inp.files[0];
    if (!f) { el.textContent = el.getAttribute("data-dz-idle") || el.textContent; return; }
    /* Size alongside the name: a 0-byte file is the one failure the browser reports as success,
       and it is worth seeing before you press Upload. */
    var kb = f.size < 1024 ? f.size + " B" : Math.round(f.size / 1024) + " KB";
    el.textContent = f.name + " (" + kb + ")";
  }

  document.addEventListener("change", function (e) {
    if (e.target && e.target.type === "file") {
      var z = zoneOf(e.target);
      if (z) show(z);
    }
  });

  /* dragover has to be cancelled on every tick or the browser navigates to the file instead. */
  ["dragenter", "dragover"].forEach(function (evt) {
    document.addEventListener(evt, function (e) {
      var z = zoneOf(e.target);
      if (!z) return;
      e.preventDefault();
      z.classList.add("dz-over");
    });
  });

  document.addEventListener("dragleave", function (e) {
    var z = zoneOf(e.target);
    /* relatedTarget is where the pointer went. Moving between the zone's own children fires a
       dragleave on each one, so without this the highlight flickers off mid-drag. */
    if (z && !(e.relatedTarget && z.contains(e.relatedTarget))) z.classList.remove("dz-over");
  });

  document.addEventListener("drop", function (e) {
    var z = zoneOf(e.target);
    if (!z) return;
    e.preventDefault();
    z.classList.remove("dz-over");
    var inp = inputOf(z), dt = e.dataTransfer;
    if (!inp || !dt || !dt.files || !dt.files.length) return;
    try {
      inp.files = dt.files;
    } catch (err) {
      return;                 /* Older engines refuse the assignment; the picker still works. */
    }
    /* The assignment above does not fire change, and something else may be listening for it
       (welcome.tsx reads the file to echo a parse preview), so say it out loud. */
    inp.dispatchEvent(new Event("change", { bubbles: true }));
  });

  /* A form restored from history keeps its file selection, so paint it on load too. */
  document.addEventListener("DOMContentLoaded", function () {
    var zs = document.querySelectorAll(".dropzone");
    for (var i = 0; i < zs.length; i++) show(zs[i]);
  });
})();
