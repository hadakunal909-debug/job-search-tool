/* Client-side filter for the admin "By User" list.
 *
 * An external file, not an inline <script>, so CSP script-src 'self' already covers it and it
 * needs no nonce. Same shape as the role picker's filter (static/rolepick.js): the rows are
 * already all in the DOM, so a round trip to filter them would buy nothing but latency.
 *
 * Null-guards everything: this file also loads on admin pages that have no such list.
 */
(function () {
  "use strict";
  var find = document.getElementById("userfind");
  var table = document.getElementById("usertable");
  if (!find || !table) return;
  var count = document.getElementById("usern");
  var none = document.getElementById("usernone");
  var total = null;

  function rows() {
    var out = [], tr = table.tBodies[0] ? table.tBodies[0].rows : [];
    for (var i = 0; i < tr.length; i++)
      if (tr[i].id !== "usernone") out.push(tr[i]);
    return out;
  }

  find.addEventListener("input", function () {
    var q = find.value.trim().toLowerCase(), rs = rows(), shown = 0;
    if (total === null) total = rs.length;
    for (var i = 0; i < rs.length; i++) {
      // data-find is built server-side and already lowercased: it carries the username AND the
      // companies that user acts on, so searching an employer finds whoever acts on it.
      var hit = !q || (rs[i].getAttribute("data-find") || "").indexOf(q) >= 0;
      rs[i].hidden = !hit;
      if (hit) shown++;
    }
    if (none) none.hidden = !!shown;
    // The count reads as a filter result while filtering and as a total when not, which is the
    // only way "3" next to a search box is unambiguous.
    if (count) count.textContent = q ? (shown + " of " + total) : String(total);
  });
})();
