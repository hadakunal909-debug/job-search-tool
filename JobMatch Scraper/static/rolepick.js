/* Role picker behaviour — search, the six-pick cap, the live counter, and the feed's modal.
 *
 * Its own file because the picker renders on TWO pages and app.js only loads on one of them.
 * The first version put this inside app.js, so on the onboarding wizard nothing ran: the
 * counter sat at "0 of 6" while four boxes were ticked, and the roles submitted were empty.
 *
 * The markup works with this file absent. On the wizard the checkboxes carry name="roles" and
 * post themselves; on the feed the hidden #roles input is the state and this keeps it in step,
 * then fires a "roles:change" event for app.js to re-render on. No direct call into app.js, so
 * neither file has to exist for the other to load. */
(function () {
  var pick = document.querySelector(".rolepick");
  if (!pick) return;

  var hidden = document.getElementById("roles"),          // feed only; absent on the wizard
      list = document.getElementById("rolelist"),
      find = document.getElementById("rolefind"),
      none = document.getElementById("rolenone"),
      nEl = document.getElementById("rolen"),
      btnT = document.getElementById("rolebtn-t"),
      badge = document.getElementById("rolebadge"),
      openBtn = document.getElementById("roleopen"),
      modal = document.getElementById("rolemodal"),
      MAX = parseInt(pick.getAttribute("data-max"), 10) || 6;

  function tiles() { return pick.querySelectorAll(".roletile"); }

  function sync(quiet) {
    var on = [], t = tiles(), i;
    for (i = 0; i < t.length; i++) {
      var cb = t[i].querySelector("input");
      t[i].classList.toggle("on", cb.checked);
      if (cb.checked) on.push(t[i].getAttribute("data-role"));
    }
    if (hidden) hidden.value = on.join(",");
    // At the cap the unticked tiles go quiet rather than vanishing, so the limit reads as a
    // state instead of options mysteriously disappearing.
    pick.classList.toggle("full", on.length >= MAX);
    for (i = 0; i < t.length; i++) {
      var b = t[i].querySelector("input");
      b.disabled = !b.checked && on.length >= MAX;
    }
    if (nEl) nEl.textContent = on.length;
    // The rail button reads as the CURRENT SELECTION, so the rail says what is on without
    // anyone having to open the dialog.
    if (btnT) {
      var labs = [];
      for (i = 0; i < t.length; i++)
        if (t[i].querySelector("input").checked)
          labs.push(t[i].querySelector(".rolelab").textContent.trim());
      btnT.textContent = !labs.length ? "All roles"
        : (labs.length <= 2 ? labs.join(", ") : labs[0] + " +" + (labs.length - 1) + " more");
    }
    if (badge) { badge.textContent = on.length; badge.hidden = !on.length; }
    if (openBtn) openBtn.classList.toggle("fset", !!on.length);
    if (!quiet && hidden)
      document.dispatchEvent(new CustomEvent("roles:change", { detail: { roles: on } }));
  }

  if (list) list.addEventListener("change", function (e) {
    if (!e.target || e.target.type !== "checkbox") return;
    // In the modal the feed re-renders on "Show these roles", so don't thrash it per tick.
    sync(!!modal);
  });

  if (find) find.addEventListener("input", function () {
    var q = find.value.trim().toLowerCase(), t = tiles(), shown = 0, i;
    for (i = 0; i < t.length; i++) {
      // data-find carries the family's phrases as well as its label, so "sde" or "tpm" finds
      // the right tile even though neither word is in the visible text.
      var hit = !q || (t[i].getAttribute("data-find") || "").indexOf(q) >= 0;
      t[i].hidden = !hit;
      if (hit) shown++;
    }
    // Hide a whole section once everything inside it is filtered out, or the headings stack up
    // over nothing.
    var groups = pick.querySelectorAll(".rolegroup");
    for (i = 0; i < groups.length; i++)
      groups[i].hidden = !groups[i].querySelector(".roletile:not([hidden])");
    if (none) none.hidden = !!shown;
  });

  function open() {
    if (!modal) return;
    modal.classList.add("open");
    document.body.style.overflow = "hidden";
    if (find) find.focus();
  }
  function close(apply) {
    if (!modal) return;
    modal.classList.remove("open");
    document.body.style.overflow = "";
    if (apply) sync();
  }
  if (openBtn) openBtn.addEventListener("click", open);
  var x = document.getElementById("roleclose");
  if (x) x.addEventListener("click", function () { close(true); });
  var done = document.getElementById("roledone");
  if (done) done.addEventListener("click", function () { close(true); });
  var clear = document.getElementById("roleclear");
  if (clear) clear.addEventListener("click", function () {
    var t = tiles(), i;
    for (i = 0; i < t.length; i++) t[i].querySelector("input").checked = false;
    if (find) { find.value = ""; find.dispatchEvent(new Event("input")); }
    sync(!!modal);
  });
  if (modal) modal.addEventListener("click", function (e) {
    if (e.target === modal) close(true);
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && modal && modal.classList.contains("open")) close(true);
  });

  // Seed from whatever the server rendered as checked, without firing a re-render on load.
  sync(true);
})();
