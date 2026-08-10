/* Tooltips that work on touch.
 *
 * The app carries real explanation in title= attributes: what the pay filter actually reads,
 * what "confirmed posting date" excludes, why a missing sponsorship chip is not a refusal.
 * A native tooltip never appears on a touch device, has a delay nobody can tune, and is
 * announced inconsistently by screen readers. So those three explanations were invisible to
 * anyone on a phone.
 *
 * Progressive enhancement, deliberately: title= stays in the templates and this file MOVES it
 * to data-tip the first time an element is hovered or focused. With JS off you keep the native
 * tooltip; with JS on you get one that works. Nothing had to change in the markup.
 *
 * Promotion happens lazily on the event rather than in a startup sweep, so cards rendered later
 * by app.js are covered without a MutationObserver and without a cost at load.
 */
(function () {
  "use strict";
  var SEL = "[title],[data-tip]";
  var GAP = 8;                 // px between the trigger and the bubble
  var tip = null, current = null, hideT = 0, seq = 0;

  function node() {
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "tip";
      tip.setAttribute("role", "tooltip");
      tip.id = "tip-bubble";
      document.body.appendChild(tip);
    }
    return tip;
  }

  /* title= would render the NATIVE tooltip on top of ours, so it has to move rather than be
     copied. Kept in data-tip so a second hover still has the text. */
  function promote(el) {
    var t = el.getAttribute("title");
    if (t && t.trim()) {
      el.setAttribute("data-tip", t.trim());
      el.removeAttribute("title");
    }
    return el.getAttribute("data-tip");
  }

  function place(el) {
    var b = node(), r = el.getBoundingClientRect();
    b.style.left = "0px";
    b.style.top = "0px";
    var w = b.offsetWidth, h = b.offsetHeight;
    var left = r.left + (r.width - w) / 2;
    var top = r.top - h - GAP;
    if (top < 4) top = r.bottom + GAP;                       // flip below when there is no room
    var max = document.documentElement.clientWidth - w - 4;
    if (left > max) left = max;
    if (left < 4) left = 4;
    b.style.left = Math.round(left) + "px";
    b.style.top = Math.round(top) + "px";
  }

  function show(el) {
    var text = promote(el);
    if (!text) return;
    clearTimeout(hideT);
    current = el;
    var b = node();
    b.textContent = text;
    place(el);
    /* place() reads offsetWidth, which forces the pre-transition state to be computed, so the
       class below animates from it. This deliberately does NOT use requestAnimationFrame: rAF
       is paused whenever the tab is not compositing, and a tooltip that silently never appears
       is worse than one that appears without a fade. */
    b.classList.add("on");
    el.setAttribute("aria-describedby", "tip-bubble");
  }

  function hide() {
    if (!current) return;
    current.removeAttribute("aria-describedby");
    current = null;
    var b = node(), mine = ++seq;
    b.classList.remove("on");
    hideT = setTimeout(function () { if (mine === seq) b.textContent = ""; }, 200);
  }

  function trigger(e) {
    var el = e.target && e.target.closest ? e.target.closest(SEL) : null;
    return el && el !== tip ? el : null;
  }

  document.addEventListener("pointerover", function (e) {
    if (e.pointerType === "touch") return;                   // touch is handled on tap below
    var el = trigger(e);
    if (el && el !== current) show(el); else if (!el) hide();
  });
  document.addEventListener("pointerout", function (e) {
    if (e.pointerType === "touch") return;
    if (trigger(e)) hide();
  });

  /* Touch: tap to open, tap anywhere else to close. Non-passive because a tooltip on a plain
     <span> would otherwise be dismissed by the click that follows it. */
  document.addEventListener("pointerdown", function (e) {
    if (e.pointerType !== "touch") return;
    var el = trigger(e);
    if (!el) { hide(); return; }
    if (el === current) { hide(); return; }
    show(el);
  }, true);

  /* Keyboard reaches the same text. focusin/out rather than focus/blur so it delegates. */
  document.addEventListener("focusin", function (e) {
    var el = trigger(e);
    if (el) show(el);
  });
  document.addEventListener("focusout", hide);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") hide(); });
  /* A fixed bubble does not travel with the page, so anything that moves the trigger kills it. */
  addEventListener("scroll", hide, true);
  addEventListener("resize", hide);
})();
