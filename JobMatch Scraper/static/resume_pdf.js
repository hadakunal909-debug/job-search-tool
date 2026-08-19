/* resume_pdf.js — render the stored résumé PDF into the page, as pages rather than as a viewer.
 *
 * WHY NOT AN <iframe>. An iframe hands the file to the browser's own PDF viewer, and that viewer is
 * a window: a dark toolbar with zoom, rotate, download and print, a thumbnail rail, a grey gutter
 * around the sheet, and a title captioned from the URL. `#toolbar=0&navpanes=0` removes the first
 * two and nothing removes the rest, because all of it lives in a document we do not own and cannot
 * style. Inside a panel that has its own controls it reads as a second application bolted into the
 * first. So the pages are drawn to <canvas> here and the surround is ours.
 *
 * pdf.js is vendored at static/vendor/pdfjs (see VERSION.txt) rather than loaded from a CDN, because
 * script-src is 'self' and because a résumé is not a reason to talk to a third party.
 *
 * THREE THINGS THIS HAS TO GET RIGHT, each learned the hard way:
 *
 * 1. Render when the panel is VISIBLE, not on page load. The Original tab starts hidden, so a
 *    container measured at load time reports clientWidth 0 — the first attempt sized every page
 *    from an 800px fallback and then stretched it, which is blurry on purpose-built hardware.
 *    An IntersectionObserver waits for the tab to be shown.
 * 2. Canvas rendering is driven by requestAnimationFrame, so it never completes in a document that
 *    is not compositing — a background tab, a hidden pane, some headless setups. Without a deadline
 *    that shows as "Rendering your résumé…" forever.
 * 3. It must always degrade to something usable. The "open in a new tab" link is in the markup
 *    unconditionally, works with JavaScript off, and every failure path below points at it.
 */
(function () {
  "use strict";

  var host = document.querySelector("[data-pdf]");
  if (!host) { return; }

  var url = host.getAttribute("data-pdf");
  var base = host.getAttribute("data-pdfjs");          // .../static/vendor/pdfjs/
  var status = host.querySelector("[data-pdf-status]");
  var started = false;

  // Rendering is expensive per page and a résumé is one or two, so this is well clear of any real
  // document while still bounding what a crafted file can ask for.
  var MAX_PAGES = 12;
  // Long enough for a slow machine on a big page, short enough that nobody stares at a spinner.
  var DEADLINE_MS = 12000;
  // Device pixels, or the page looks soft on any modern display. Capped at 2 because beyond that
  // the canvases cost more memory than the sharpness is worth.
  var DPR = Math.min(window.devicePixelRatio || 1, 2);

  function say(text) {
    if (!status) { return; }
    status.hidden = false;
    status.textContent = text;
  }

  function fail(why) {
    host.classList.remove("is-ready");
    host.classList.add("is-failed");
    say(why + " Use “Open in a new tab” above to read it in your browser's own viewer.");
  }

  function deadline(promise, ms, label) {
    return new Promise(function (resolve, reject) {
      var timer = setTimeout(function () {
        reject(new Error(label + " did not finish in " + Math.round(ms / 1000) + "s"));
      }, ms);
      promise.then(function (v) { clearTimeout(timer); resolve(v); },
                   function (e) { clearTimeout(timer); reject(e); });
    });
  }

  function renderPage(pdfjs, doc, num) {
    return doc.getPage(num).then(function (page) {
      var wrap = document.createElement("div");
      wrap.className = "rvpage";
      host.appendChild(wrap);

      // Measured now, with the panel on screen, so the two-column layout and a phone each get a
      // page sized to the space they actually have.
      var avail = wrap.clientWidth || host.clientWidth;
      if (!avail) { throw new Error("the preview area has no width yet"); }

      var unscaled = page.getViewport({ scale: 1 });
      var viewport = page.getViewport({ scale: (avail / unscaled.width) * DPR });
      var canvas = document.createElement("canvas");
      canvas.width = Math.floor(viewport.width);
      canvas.height = Math.floor(viewport.height);
      // Bitmap in device pixels, CSS box in layout pixels: that pair is what makes it sharp.
      canvas.style.width = "100%";
      canvas.style.height = "auto";
      canvas.setAttribute("role", "img");
      canvas.setAttribute("aria-label", "Page " + num + " of your résumé");
      wrap.appendChild(canvas);

      return page.render({
        canvasContext: canvas.getContext("2d", { alpha: false }),
        viewport: viewport
      }).promise;
    });
  }

  function start() {
    if (started) { return; }
    started = true;

    import(base + "pdf.min.js").then(function (pdfjs) {
      pdfjs.GlobalWorkerOptions.workerSrc = base + "pdf.worker.min.js";
      return pdfjs.getDocument({
        url: url,
        // Needed by any PDF that names one of the base-14 fonts without embedding it — common from
        // simple generators. Without it those glyphs have no data to draw.
        standardFontDataUrl: base + "standard_fonts/"
      }).promise.then(function (doc) { return { pdfjs: pdfjs, doc: doc }; });
    }).then(function (ctx) {
      var doc = ctx.doc;
      var n = Math.min(doc.numPages, MAX_PAGES);
      if (doc.numPages > n) {
        say("Showing the first " + n + " of " + doc.numPages + " pages.");
      }
      var chain = Promise.resolve();
      for (var i = 1; i <= n; i++) {
        chain = chain.then(renderPage.bind(null, ctx.pdfjs, doc, i));
      }
      return deadline(chain, DEADLINE_MS, "Rendering");
    }).then(function () {
      host.classList.add("is-ready");
      if (status && status.textContent.indexOf("Showing the first") !== 0) {
        status.hidden = true;
      }
    }).catch(function (err) {
      fail("This PDF could not be displayed here (" +
           ((err && err.message) || "unknown error") + ").");
    });
  }

  // Wait until the panel is laid out, then render. TWO triggers, deliberately.
  //
  // IntersectionObserver is the fast, correct one — it fires the moment the tab is shown. But it is
  // part of the same rendering lifecycle as requestAnimationFrame, so in a document that is not
  // compositing it never delivers an entry and the preview would not merely stall, it would never
  // begin: no canvases, no error, no status. A poll on layout properties has no such dependency —
  // offsetParent and clientWidth are answers about layout, which happens regardless — so it starts
  // the work, and the deadline above then turns a stalled render into a message and a link.
  function laidOut() {
    return host.offsetParent !== null && host.clientWidth > 0;
  }

  if (typeof IntersectionObserver === "function") {
    var io = new IntersectionObserver(function (entries) {
      for (var i = 0; i < entries.length; i++) {
        if (entries[i].isIntersecting) { io.disconnect(); start(); return; }
      }
    });
    io.observe(host);
  }

  var tries = 0;
  var poll = setInterval(function () {
    // ~40s of patience: the tab may simply never be opened, and that must cost nothing.
    if (started || ++tries > 200) { clearInterval(poll); return; }
    if (laidOut()) { clearInterval(poll); start(); }
  }, 200);
}());
