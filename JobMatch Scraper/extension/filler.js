// filler.js — application form auto-fill, injected into the apply page in the MAIN world
// (so React's controlled inputs accept programmatic values). One self-contained function,
// jmFillApplication(payload), passed to chrome.scripting.executeScript({func: jmFillApplication}).
// It must reference NO outer scope (it's serialized via toString and run in the page).
//
// payload = { fields: <normalized profile map from /api/ext/profile_fields>,
//             file: {name, mime, b64} | null, defaults: {questionHash: answer} }
// returns  = { found, ats, filled, total, unfilled:[{label,reason}], fileAttached, submitSelector }
//
// Milestone 1 ships the Greenhouse adapter; the engine + fuzzy label matcher are ATS-agnostic so
// Lever/Ashby/SmartRecruiters are added by extending ADAPTERS only.
function jmFillApplication(payload) {
  payload = payload || {};
  var F = payload.fields || {};
  var file = payload.file || null;

  // ----------------------------- low-level DOM helpers -----------------------------
  function vis(el) {
    if (!el) return false;
    if (el.disabled || el.readOnly) return false;
    if (el.type === "hidden") return el.closest("[data-react-class],form") != null; // hidden file inputs ok
    var r = el.getBoundingClientRect();
    var s = getComputedStyle(el);
    return s.display !== "none" && s.visibility !== "hidden" && (r.width > 1 || r.height > 1 || el.type === "file");
  }
  function setNativeValue(el, value) {
    var proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    var setter = Object.getOwnPropertyDescriptor(proto, "value");
    if (setter && setter.set) setter.set.call(el, value); else el.value = value;
    ["input", "change", "blur"].forEach(function (t) {
      el.dispatchEvent(new Event(t, { bubbles: true }));
    });
  }
  function setText(el, value) {
    if (!el || value == null || value === "") return false;
    if (String(el.value || "").trim()) return true; // don't clobber what the user already typed
    setNativeValue(el, String(value));
    return true;
  }
  function firstSel(selectors, root) {
    root = root || document;
    for (var i = 0; i < selectors.length; i++) {
      var nodes = root.querySelectorAll(selectors[i]);
      for (var j = 0; j < nodes.length; j++) if (vis(nodes[j])) return nodes[j];
    }
    return null;
  }
  function labelText(el) {
    var parts = [];
    if (el.id) {
      var lab = document.querySelector('label[for="' + (window.CSS && CSS.escape ? CSS.escape(el.id) : el.id) + '"]');
      if (lab) parts.push(lab.textContent);
    }
    var wrap = el.closest("label");
    if (wrap) parts.push(wrap.textContent);
    if (el.getAttribute("aria-label")) parts.push(el.getAttribute("aria-label"));
    if (el.getAttribute("placeholder")) parts.push(el.getAttribute("placeholder"));
    if (el.name) parts.push(el.name);
    // climb a couple of containers for grouped/legend labels
    var c = el.closest("fieldset, .field, [class*=field], [class*=question]");
    if (c) {
      var lg = c.querySelector("legend, label, .label, [class*=label]");
      if (lg) parts.push(lg.textContent);
    }
    return parts.join(" ").replace(/\s+/g, " ").trim().toLowerCase();
  }
  function isRequired(el) {
    if (el.required || el.getAttribute("aria-required") === "true") return true;
    var t = labelText(el);
    return /\*|\(required\)|required/.test(t);
  }
  function attachFile(input, f) {
    try {
      var bin = atob(f.b64), arr = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
      var file = new File([arr], f.name || "resume.pdf", { type: f.mime || "application/pdf" });
      var dt = new DataTransfer();
      dt.items.add(file);
      input.files = dt.files;
      ["input", "change"].forEach(function (t) { input.dispatchEvent(new Event(t, { bubbles: true })); });
      return true;
    } catch (e) { return false; }
  }
  function setSelectByText(sel, value) {
    if (!value) return false;
    var want = String(value).toLowerCase();
    var opts = sel.options, exact = -1, partial = -1;
    for (var i = 0; i < opts.length; i++) {
      var t = (opts[i].textContent || "").trim().toLowerCase();
      var v = (opts[i].value || "").toLowerCase();
      if (t === want || v === want) { exact = i; break; }
      if (partial < 0 && t && (t.indexOf(want) >= 0 || want.indexOf(t) >= 0) && t !== "") partial = i;
    }
    var idx = exact >= 0 ? exact : partial;
    if (idx < 0) return false;
    sel.selectedIndex = idx;
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }
  function clickRadioByText(groupEl, value) {
    if (!value) return false;
    var want = String(value).toLowerCase();
    var radios = groupEl.querySelectorAll('input[type=radio], input[type=checkbox]');
    for (var i = 0; i < radios.length; i++) {
      var t = labelText(radios[i]);
      if (t.indexOf(want) >= 0 || (want === "yes" && /\byes\b/.test(t)) || (want === "no" && /\bno\b/.test(t))) {
        radios[i].click();
        return true;
      }
    }
    return false;
  }

  // ----------------------------- ATS adapters -----------------------------
  var ADAPTERS = {
    greenhouse: {
      test: function () {
        return /greenhouse/.test(location.hostname) ||
          document.querySelector('#first_name, #s3_upload_for_resume, form[action*="greenhouse"], #application_form');
      },
      core: [
        { key: "first_name", label: "First name", sel: ['#first_name', 'input[name="job_application[first_name]"]', 'input[autocomplete="given-name"]'] },
        { key: "last_name", label: "Last name", sel: ['#last_name', 'input[name="job_application[last_name]"]', 'input[autocomplete="family-name"]'] },
        { key: "email", label: "Email", sel: ['#email', 'input[type=email]', 'input[autocomplete=email]'] },
        { key: "phone", label: "Phone", sel: ['#phone', 'input[type=tel]', 'input[autocomplete=tel]'] }
      ],
      resumeFile: ['input[type=file][id*="resume" i]', 'input[type=file][name*="resume" i]', '#s3_upload_for_resume', 'input[type=file]'],
      submit: '#submit_app, button[type=submit], input[type=submit], button[aria-label*="Submit" i]'
    }
  };

  // ----------------------------- pick adapter -----------------------------
  var ats = null, A = null;
  for (var name in ADAPTERS) {
    try { if (ADAPTERS[name].test()) { ats = name; A = ADAPTERS[name]; break; } } catch (e) {}
  }
  if (!A) return { found: false, ats: null };

  var filled = 0, total = 0, unfilled = [], fileAttached = false;
  function track(ok, label, required) {
    total++;
    if (ok) filled++;
    else if (required) unfilled.push({ label: label, reason: "empty" });
  }

  // 1) core identity/contact fields
  A.core.forEach(function (m) {
    var el = firstSel(m.sel);
    track(el ? setText(el, F[m.key]) : false, m.label, true);
  });

  // 2) résumé file
  if (file && file.b64) {
    var fi = firstSel(A.resumeFile);
    if (fi) fileAttached = attachFile(fi, file);
  }

  // 3) fuzzy label matching for links + common custom questions + EEO
  var links = F.links || {}, work = F.work_auth || {}, eeo = F.eeo || {}, comp = F.comp || {}, addr = F.address || {};
  var RULES = [
    { re: /linkedin/, val: links.linkedin },
    { re: /github/, val: links.github },
    { re: /portfolio|personal (web)?site|website/, val: links.portfolio || links.website },
    { re: /how did you (hear|find)/, val: F.how_did_you_hear },
    { re: /desired (salary|compensation|pay)|salary expectation/, val: comp.desired_salary },
    { re: /start date|available|availability/, val: F.start_date },
    { re: /willing to relocate|open to relocat|relocat/, val: F.relocate ? "Yes" : "No", onlyIf: F.relocate !== "" && F.relocate != null },
    { re: /authoriz|legally (eligible|able) to work|work authorization/, val: work.status_label || (work.authorized ? "Yes" : "No") },
    { re: /require.*(sponsor|visa)|sponsorship/, val: work.requires_sponsorship ? "Yes" : "No" },
    { re: /gender/, val: eeo.gender },
    { re: /hispanic|latino/, val: eeo.hispanic_latino },
    { re: /race|ethnic/, val: eeo.race },
    { re: /veteran/, val: eeo.veteran },
    { re: /disab/, val: eeo.disability },
    { re: /city/, val: addr.city },
    { re: /\bstate\b|province/, val: addr.state },
    { re: /zip|postal/, val: addr.postal },
    { re: /country/, val: addr.country },
    { re: /address/, val: addr.line1 }
  ];
  function applyRule(el, t) {
    for (var i = 0; i < RULES.length; i++) {
      var r = RULES[i];
      if (r.onlyIf === false) continue;
      if (!r.val) continue;
      if (!r.re.test(t)) continue;
      if (el.tagName === "SELECT") return setSelectByText(el, r.val) ? r : null;
      if (el.tagName === "INPUT" && (el.type === "text" || el.type === "url" || el.type === "" || el.type === "number" || el.type === "tel")) return setText(el, r.val) ? r : null;
      if (el.tagName === "TEXTAREA") return setText(el, r.val) ? r : null;
      return null;
    }
    return null;
  }
  var seen = new Set();
  document.querySelectorAll("input, select, textarea").forEach(function (el) {
    if (!vis(el) || el.type === "hidden" || el.type === "file" || el.type === "submit" || el.type === "button") return;
    if (el.type === "radio" || el.type === "checkbox") return; // handled as groups below
    if (A.core.some(function (m) { return m.sel.some(function (s) { try { return el.matches(s); } catch (e) { return false; } }); })) return;
    var t = labelText(el);
    if (!t) return;
    if (applyRule(el, t)) seen.add(el);
  });

  // radio/checkbox groups (work auth, sponsorship, EEO, relocate) by container label
  document.querySelectorAll("fieldset, [role=radiogroup], .field, [class*=question]").forEach(function (g) {
    var t = (g.textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
    if (!t || !g.querySelector("input[type=radio], input[type=checkbox]")) return;
    var val = null;
    if (/authoriz|legally.*work/.test(t)) val = work.authorized ? "Yes" : (work.status_label ? "Yes" : "");
    else if (/sponsor|visa/.test(t)) val = work.requires_sponsorship ? "Yes" : "No";
    else if (/relocat/.test(t) && F.relocate != null && F.relocate !== "") val = F.relocate ? "Yes" : "No";
    else if (/veteran/.test(t)) val = eeo.veteran;
    else if (/disab/.test(t)) val = eeo.disability;
    else if (/gender/.test(t)) val = eeo.gender;
    else if (/hispanic|latino/.test(t)) val = eeo.hispanic_latino;
    if (val) clickRadioByText(g, val);
  });

  // 4) scan remaining required-but-empty fields for the review panel
  document.querySelectorAll("input, select, textarea").forEach(function (el) {
    if (!vis(el) || el.type === "hidden" || el.type === "submit" || el.type === "button" || el.type === "search") return;
    if (el.type === "file") {
      if (isRequired(el) && (!el.files || !el.files.length) && !fileAttached) unfilled.push({ label: labelText(el) || "Résumé", reason: "no file" });
      return;
    }
    var empty = el.tagName === "SELECT" ? (el.selectedIndex <= 0 || el.value === "") : !String(el.value || "").trim();
    if (empty && isRequired(el)) {
      var lab = labelText(el) || el.name || "field";
      if (!unfilled.some(function (u) { return u.label === lab; })) unfilled.push({ label: lab, reason: "required" });
    }
  });

  return {
    found: true, ats: ats, filled: filled, total: total,
    unfilled: unfilled.slice(0, 25), fileAttached: fileAttached, submitSelector: A.submit
  };
}
