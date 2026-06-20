// filler.js — application form auto-fill, injected into the apply page in the MAIN world
// (so React's controlled inputs accept programmatic values). Self-contained functions passed to
// chrome.scripting.executeScript({func}) — they must reference NO outer scope (serialized via
// toString and run in the page). Also importScripts()'d by the background batch runner.
//
// jmFillApplication(payload): payload = { fields:<profile map>, file:{name,mime,b64}|null, defaults }
//   returns { found, ats, filled, total, unfilled:[{label,reason}], fileAttached, submitSelector,
//             captcha, login }
//
// Adapters cover Greenhouse / Lever / Ashby / SmartRecruiters precisely; a GENERIC adapter then
// fills ANY standard application form (file upload + email + submit) via autocomplete/type/label
// heuristics, so it works far beyond the named ATS. Login/CAPTCHA-walled flows (Workday, iCIMS,
// Oracle, Taleo) are detected and reported as walls — they can't be auto-filled.
async function jmFillApplication(payload) {
  payload = payload || {};
  var F = payload.fields || {};
  var file = payload.file || null;
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  // ----------------------------- low-level DOM helpers -----------------------------
  function vis(el) {
    if (!el) return false;
    if (el.disabled || el.readOnly) return false;
    if (el.type === "hidden") return el.closest("form,[data-react-class],[class*=application],[class*=apply]") != null;
    var r = el.getBoundingClientRect();
    var s = getComputedStyle(el);
    return s.display !== "none" && s.visibility !== "hidden" && (r.width > 1 || r.height > 1 || el.type === "file");
  }
  function setNativeValue(el, value) {
    var proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    var setter = Object.getOwnPropertyDescriptor(proto, "value");
    if (setter && setter.set) setter.set.call(el, value); else el.value = value;
    ["input", "change", "blur"].forEach(function (t) { el.dispatchEvent(new Event(t, { bubbles: true })); });
  }
  function setText(el, value) {
    if (!el || value == null || value === "") return false;
    if (String(el.value || "").trim()) return true;   // don't clobber what's already there
    setNativeValue(el, String(value));
    if (!String(el.value || "").trim()) {              // controlled widget rejected it — TYPE it
      try {
        el.focus(); if (el.select) el.select();
        if (document.execCommand) document.execCommand("insertText", false, String(value));
        el.dispatchEvent(new Event("change", { bubbles: true })); el.blur();
      } catch (e) {}
    }
    return !!String(el.value || "").trim();
  }
  function firstSel(selectors) {
    for (var i = 0; i < (selectors || []).length; i++) {
      var nodes = document.querySelectorAll(selectors[i]);
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
    var alby = el.getAttribute("aria-labelledby");   // new Greenhouse labels comboboxes this way
    if (alby) alby.split(/\s+/).forEach(function (id) { var n = document.getElementById(id); if (n) parts.push(n.textContent); });
    if (el.getAttribute("placeholder")) parts.push(el.getAttribute("placeholder"));
    if (el.name) parts.push(el.name);
    var c = el.closest("fieldset, .field, [class*=field], [class*=question]");
    if (c) { var lg = c.querySelector("legend, label, .label, [class*=label]"); if (lg) parts.push(lg.textContent); }
    return parts.join(" ").replace(/\s+/g, " ").trim().toLowerCase();
  }
  function isRequired(el) {
    if (el.required || el.getAttribute("aria-required") === "true") return true;
    return /\*|\(required\)|required/.test(labelText(el));
  }
  function attachFile(input, f) {
    try {
      var bin = atob(f.b64), arr = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
      var fl = new File([arr], f.name || "resume.pdf", { type: f.mime || "application/pdf" });
      var dt = new DataTransfer(); dt.items.add(fl);
      input.files = dt.files;
      ["input", "change"].forEach(function (t) { input.dispatchEvent(new Event(t, { bubbles: true })); });
      return true;
    } catch (e) { return false; }
  }
  function setSelectByText(sel, value) {
    if (!value) return false;
    var want = String(value).toLowerCase(), opts = sel.options, exact = -1, partial = -1;
    for (var i = 0; i < opts.length; i++) {
      var t = (opts[i].textContent || "").trim().toLowerCase(), v = (opts[i].value || "").toLowerCase();
      if (t === want || v === want) { exact = i; break; }
      if (partial < 0 && t && (t.indexOf(want) >= 0 || want.indexOf(t) >= 0)) partial = i;
    }
    var idx = exact >= 0 ? exact : partial;
    if (idx < 0) return false;
    sel.selectedIndex = idx;
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }
  function clickRadioByText(groupEl, value) {
    if (!value) return false;
    var want = String(value).toLowerCase(), radios = groupEl.querySelectorAll('input[type=radio], input[type=checkbox]');
    for (var i = 0; i < radios.length; i++) {
      var t = labelText(radios[i]);
      if (t.indexOf(want) >= 0 || (want === "yes" && /\byes\b/.test(t)) || (want === "no" && /\bno\b/.test(t))) {
        radios[i].click(); return true;
      }
    }
    return false;
  }
  // File inputs are usually display:none (a styled "Attach" button fronts them), so DON'T require
  // visibility here — just find an enabled <input type=file>.
  function findFileInput(selectors) {
    for (var i = 0; i < (selectors || []).length; i++) {
      var nodes = document.querySelectorAll(selectors[i]);
      for (var j = 0; j < nodes.length; j++) if (nodes[j].type === "file" && !nodes[j].disabled) return nodes[j];
    }
    // fallback: pierce shadow DOM (SmartRecruiters / Ashby wrap the input in web components)
    var stack = [document];
    while (stack.length) {
      var root = stack.pop();
      var files = root.querySelectorAll ? root.querySelectorAll("input[type=file]") : [];
      for (var k = 0; k < files.length; k++) if (!files[k].disabled) return files[k];
      var all = root.querySelectorAll ? root.querySelectorAll("*") : [];
      for (var m = 0; m < all.length; m++) if (all[m].shadowRoot) stack.push(all[m].shadowRoot);
    }
    return null;
  }
  // Only a REAL, visible challenge is a wall. The invisible reCAPTCHA v3 BADGE (the floating logo,
  // .grecaptcha-badge) is on most Greenhouse/Lever pages and passes silently — it must NOT park the
  // job. A hard challenge = hCaptcha/Turnstile widget, a reCAPTCHA image popup (api2/bframe), or a
  // v2 "I'm not a robot" checkbox that is NOT the badge.
  function visibleChallenge() {
    var hard = document.querySelectorAll(
      'iframe[src*="recaptcha/api2/bframe"], iframe[src*="hcaptcha.com"], iframe[src*="challenges.cloudflare.com"], .h-captcha, .cf-turnstile');
    for (var i = 0; i < hard.length; i++) { var r = hard[i].getBoundingClientRect(); if (r.width > 10 && r.height > 10) return true; }
    var anchors = document.querySelectorAll('iframe[src*="recaptcha/api2/anchor"]');
    for (var j = 0; j < anchors.length; j++) {
      if (anchors[j].closest(".grecaptcha-badge")) continue;     // floating v3 badge — ignore
      var rr = anchors[j].getBoundingClientRect();
      if (rr.width > 10 && rr.height > 10) return true;          // a real v2 checkbox
    }
    return false;
  }

  // ----------------------------- ATS adapters -----------------------------
  // name: {first:[], last:[], full:[]}  — fill first+last, else the single full-name field.
  var GENERIC = {
    name: {
      first: ['input[autocomplete="given-name"]', 'input[name*="first" i]', '#first_name', '#firstName'],
      last: ['input[autocomplete="family-name"]', 'input[name*="last" i]', '#last_name', '#lastName'],
      full: ['input[autocomplete="name"]', 'input[name="name"]', 'input[name="fullName"]', 'input[name*="full" i]', '#name']
    },
    email: ['input[type=email]', 'input[autocomplete=email]', 'input[name*="email" i]', '#email'],
    phone: ['input[type=tel]', 'input[autocomplete=tel]', 'input[name*="phone" i]', '#phone'],
    resumeFile: ['input[type=file][name*="resume" i]', 'input[type=file][id*="resume" i]', 'input[type=file][accept*="pdf"]', 'input[type=file]'],
    submit: 'button[type=submit], input[type=submit], button[aria-label*="submit" i]'
  };
  var ADAPTERS = {
    greenhouse: {
      test: function () { return /greenhouse/.test(location.hostname) || document.querySelector('#first_name, #s3_upload_for_resume, form[action*="greenhouse"], #application_form'); },
      name: { first: ['#first_name', 'input[name="job_application[first_name]"]'], last: ['#last_name', 'input[name="job_application[last_name]"]'], full: [] },
      email: ['#email', 'input[type=email]'],
      phone: ['#phone', 'input[type=tel]', 'input[autocomplete="tel"]', 'input[name*="phone" i]'],
      resumeFile: ['input[type=file][id*="resume" i]', '#s3_upload_for_resume', 'input[type=file]'],
      submit: '#submit_app, button[type=submit], input[type=submit]'
    },
    lever: {
      test: function () { return /lever\.co/.test(location.hostname) || document.querySelector('form[action*="lever"], .application-form'); },
      name: { first: [], last: [], full: ['input[name="name"]', '#name'] },   // Lever uses one Name field
      email: ['input[name="email"]', 'input[type=email]'], phone: ['input[name="phone"]', 'input[type=tel]'],
      resumeFile: ['input[name="resume"]', 'input[type=file]'],
      submit: '#btn-submit, button[type=submit], .postings-btn[type=submit], button[data-qa="btn-submit"]'
    },
    ashby: {
      test: function () { return /ashbyhq\.com/.test(location.hostname) || document.querySelector('[class*="ashby" i], form[class*="application" i] [data-highlight]'); },
      name: { first: ['input[name*="first" i]'], last: ['input[name*="last" i]'], full: ['input[name="_systemfield_name"]', 'input[name*="name" i]', 'input[aria-label*="name" i]'] },
      email: ['input[name="_systemfield_email"]', 'input[type=email]', 'input[aria-label*="email" i]'],
      phone: ['input[name="_systemfield_phone"]', 'input[type=tel]', 'input[aria-label*="phone" i]'],
      resumeFile: ['input[type=file]'],
      submit: 'button[type=submit], button[aria-label*="submit" i]'
    },
    smartrecruiters: {
      test: function () { return /smartrecruiters\.com/.test(location.hostname) || document.querySelector('[data-test*="application"], form[action*="smartrecruiters"]'); },
      name: { first: ['#firstName', 'input[name="firstName"]', '[data-test="field-firstName"] input'], last: ['#lastName', 'input[name="lastName"]', '[data-test="field-lastName"] input'], full: [] },
      email: ['#email', 'input[name="email"]', 'input[type=email]'], phone: ['#phoneNumber', 'input[name="phoneNumber"]', 'input[type=tel]'],
      resumeFile: ['input[type=file]'],
      submit: 'button[type=submit], button[data-test*="submit"], button[aria-label*="submit" i]'
    }
  };

  // pick the most specific adapter; else GENERIC if the page looks like an application form
  function looksLikeForm() {
    return !!(firstSel(GENERIC.resumeFile) && firstSel(GENERIC.email));
  }
  var ats = null, A = null;
  for (var nm in ADAPTERS) { try { if (ADAPTERS[nm].test()) { ats = nm; A = ADAPTERS[nm]; break; } } catch (e) {} }
  if (!A) {
    if (!looksLikeForm()) return { found: false, ats: null };
    ats = "generic"; A = GENERIC;
  }

  var filled = 0, total = 0, unfilled = [], fileAttached = false;
  function track(ok, label, required, found) {
    if (found === false) return;                       // field not on this form -> don't count
    total++;
    if (ok) filled++; else if (required) unfilled.push({ label: label, reason: "empty" });
  }

  // 1) name (first+last, else single full-name), email, phone
  var fe = firstSel(A.name.first), le = firstSel(A.name.last);
  if (fe || le) {
    track(fe ? setText(fe, F.first_name) : false, "First name", true, !!fe);
    track(le ? setText(le, F.last_name) : false, "Last name", true, !!le);
  } else {
    var ne = firstSel(A.name.full);
    track(ne ? setText(ne, F.full_name) : false, "Name", true, !!ne);
  }
  var ee = firstSel(A.email); track(ee ? setText(ee, F.email) : false, "Email", true, !!ee);
  var pe = firstSel(A.phone); track(pe ? setText(pe, F.phone) : false, "Phone", true, !!pe);

  // 2) résumé file (find even when the real input is hidden behind a styled button)
  if (file && file.b64) { var fi = findFileInput(A.resumeFile); if (fi) fileAttached = attachFile(fi, file); }

  // 3) fuzzy label matching: links + common custom questions + EEO
  var links = F.links || {}, work = F.work_auth || {}, eeo = F.eeo || {}, comp = F.comp || {}, addr = F.address || {};
  var RULES = [
    { re: /\bphone\b|mobile number|cell( phone)?/, val: F.phone },
    { re: /preferred (first )?name/, val: F.first_name }, { re: /preferred last name/, val: F.last_name },
    { re: /linkedin/, val: links.linkedin },
    { re: /github/, val: links.github },
    { re: /portfolio|personal (web)?site|website/, val: links.portfolio || links.website },
    { re: /how did you (hear|find)/, val: [F.how_did_you_hear, "LinkedIn", "Job board", "Company website", "Other"] },
    { re: /desired (salary|compensation|pay)|salary expectation/, val: comp.desired_salary },
    { re: /start date|available|availability/, val: F.start_date },
    { re: /willing to relocate|open to relocat|relocat/, val: F.relocate ? "Yes" : "No", onlyIf: F.relocate !== "" && F.relocate != null },
    // Yes/No dropdowns: feed "Yes"/"No", NOT the status label (which never matches Yes/No options).
    { re: /authoriz|legally (eligible|able) to work|work authorization|eligible to work/, val: work.authorized ? "Yes" : "No" },
    { re: /sponsor|work permit|need.*visa|require.*visa|visa.*(need|require|sponsor)/, val: work.requires_sponsorship ? "Yes" : "No" },
    { re: /gender/, val: eeo.gender }, { re: /hispanic|latino/, val: eeo.hispanic_latino },
    { re: /race|ethnic/, val: eeo.race }, { re: /veteran/, val: eeo.veteran }, { re: /disab/, val: eeo.disability },
    { re: /city/, val: addr.city }, { re: /\bstate\b|province/, val: addr.state },
    { re: /zip|postal/, val: addr.postal }, { re: /country/, val: addr.country }, { re: /address/, val: addr.line1 }
  ];
  function coreMatch(el) {
    return [A.name.first, A.name.last, A.name.full, A.email, A.phone].some(function (ss) {
      return (ss || []).some(function (s) { try { return el.matches(s); } catch (e) { return false; } });
    });
  }
  function ruleValues(t) {
    for (var i = 0; i < RULES.length; i++) {
      var r = RULES[i];
      if (r.onlyIf === false || !r.re.test(t)) continue;
      var vals = (Array.isArray(r.val) ? r.val : [r.val]).filter(Boolean);
      if (vals.length) return vals;
    }
    return null;
  }
  function isCombobox(el) {
    return el.getAttribute("role") === "combobox" || el.getAttribute("aria-autocomplete") === "list" ||
      !!el.closest(".select__container, [class*=select__]");
  }
  // A react-select shows its chosen value in .select__single-value (NOT in the input's .value, which
  // it clears after picking). Returns the selected label lowercased, or "" if nothing is selected.
  function comboSelected(el) {
    var control = el.closest(".select__control") || el.closest(".select__container") || el.closest("[class*='select']");
    if (!control) return "";
    var sv = control.querySelector(".select__single-value, [class*='singleValue'], [class*='single-value'], .select__multi-value, [class*='multiValue']");
    return sv ? (sv.textContent || "").replace(/\s+/g, " ").trim().toLowerCase() : "";
  }
  // react-select / Greenhouse combobox. Open the menu, type to filter, click the matching option,
  // then VERIFY the choice landed. Past bugs this avoids: (1) the generic setNativeValue fires a
  // "blur" that CLOSES the menu before options can be read — so we type input-only here; (2) fixed
  // short waits miss late-rendered options in throttled background tabs — so we poll. We only report
  // ok when .select__single-value matches, so a missed Yes/No is left honestly empty, never wrong.
  function comboType(node, text) {
    var setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
    if (setter && setter.set) setter.set.call(node, text); else node.value = text;
    node.dispatchEvent(new Event("input", { bubbles: true }));      // input only — no change/blur
  }
  async function fillCombobox(el, vals) {
    var control = el.closest(".select__control") || el.closest(".select__container") ||
      el.closest("[class*='select']") || el.parentElement || el;
    for (var c = 0; c < vals.length; c++) {
      var want = String(vals[c] || "").toLowerCase().trim();
      if (!want) continue;
      try { el.focus(); } catch (e) {}
      control.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, button: 0 }));
      control.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, button: 0 }));
      await sleep(120);
      try { comboType(el, vals[c]); } catch (e) {}
      var pick = null;
      for (var t = 0; t < 12 && !pick; t++) {                       // poll ~2.4s (background-tab safe)
        await sleep(200);
        var opts = document.querySelectorAll('.select__option, [class*="__option"], [id*="-option-"], [role="option"]');
        var exact = null, starts = null, partial = null;
        for (var k = 0; k < opts.length; k++) {
          var ot = (opts[k].textContent || "").toLowerCase().trim(); if (!ot) continue;
          if (ot === want) { exact = opts[k]; break; }
          if (!starts && ot.indexOf(want) === 0) starts = opts[k];
          if (!partial && (ot.indexOf(want) >= 0 || want.indexOf(ot) >= 0)) partial = opts[k];
        }
        pick = exact || starts || partial;
      }
      if (pick) {
        try { pick.scrollIntoView({ block: "nearest" }); } catch (e) {}
        pick.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, button: 0 }));
        pick.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, button: 0 }));
        pick.click();
        await sleep(150);
        var got = comboSelected(el);
        if (got && (got === want || got.indexOf(want) >= 0 || want.indexOf(got) >= 0)) return true;
      }
    }
    try { el.blur(); } catch (e) {}
    return false;
  }
  var allFields = Array.prototype.slice.call(document.querySelectorAll("input, select, textarea"));
  for (var afi = 0; afi < allFields.length; afi++) {
    var fel = allFields[afi];
    if (!vis(fel) || /hidden|file|submit|button|password/.test(fel.type) || fel.type === "radio" || fel.type === "checkbox") continue;
    if (coreMatch(fel)) continue;
    var flbl = labelText(fel); if (!flbl) continue;
    var fvals = ruleValues(flbl); if (!fvals) continue;
    if (isCombobox(fel)) { await fillCombobox(fel, fvals); }
    else if (fel.tagName === "SELECT") { for (var sv = 0; sv < fvals.length; sv++) if (setSelectByText(fel, fvals[sv])) break; }
    else if (fel.tagName === "TEXTAREA" || (fel.tagName === "INPUT" && /^(text|url|tel|number|search|)$/.test(fel.type))) setText(fel, fvals[0]);
  }
  // radio/checkbox groups (work auth, sponsorship, EEO, relocate)
  document.querySelectorAll("fieldset, [role=radiogroup], .field, [class*=question]").forEach(function (g) {
    var t = (g.textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
    if (!t || !g.querySelector("input[type=radio], input[type=checkbox]")) return;
    var val = null;
    if (/authoriz|legally.*work/.test(t)) val = (work.authorized || work.status_label) ? "Yes" : "";
    else if (/sponsor|visa/.test(t)) val = work.requires_sponsorship ? "Yes" : "No";
    else if (/relocat/.test(t) && F.relocate != null && F.relocate !== "") val = F.relocate ? "Yes" : "No";
    else if (/veteran/.test(t)) val = eeo.veteran; else if (/disab/.test(t)) val = eeo.disability;
    else if (/gender/.test(t)) val = eeo.gender; else if (/hispanic|latino/.test(t)) val = eeo.hispanic_latino;
    if (val) clickRadioByText(g, val);
  });

  // 4) remaining required-but-empty fields (for the report / submit gate)
  document.querySelectorAll("input, select, textarea").forEach(function (el) {
    if (!vis(el) || /hidden|submit|button|search/.test(el.type)) return;
    if (el.type === "file") {
      if (isRequired(el) && (!el.files || !el.files.length) && !fileAttached) unfilled.push({ label: labelText(el) || "Résumé", reason: "no file" });
      return;
    }
    var empty = el.tagName === "SELECT" ? (el.selectedIndex <= 0 || el.value === "")
      : (isCombobox(el) ? !comboSelected(el) : !String(el.value || "").trim());
    if (empty && isRequired(el)) {
      var lab = labelText(el) || el.name || "field";
      if (!unfilled.some(function (u) { return u.label === lab; })) unfilled.push({ label: lab, reason: "required" });
    }
  });

  // walls a human must clear (the runner parks the job on any of these)
  var captcha = visibleChallenge();                 // only a VISIBLE challenge, not invisible reCAPTCHA
  var login = !!document.querySelector("input[type=password]") ||
    /\/(login|sign[_-]?in|signin|auth|account\/new|users\/sign)/i.test(location.href);

  return {
    found: true, ats: ats, filled: filled, total: total, unfilled: unfilled.slice(0, 25),
    fileAttached: fileAttached, submitSelector: A.submit, captcha: captcha, login: login
  };
}

// Click the real submit button (injected by the batch runner / overlay). Self-contained.
function jmClickSubmit(selector) {
  var btn = selector ? document.querySelector(selector) : null;
  if (btn && btn.offsetParent === null) btn = null;        // selector matched a hidden element
  if (!btn) {
    var all = Array.prototype.slice.call(document.querySelectorAll("button, input[type=submit], [role=button]"));
    var cand = all.filter(function (b) {
      var t = (b.textContent || b.value || "").trim();
      if (!t || b.offsetParent === null || b.disabled) return false;
      if (/back|cancel|previous|save draft|save for later|sign ?in|log ?in/i.test(t)) return false;
      return /submit|continue|next|review|finish|^apply/i.test(t);     // intermediate or final step button
    });
    btn = cand.filter(function (b) { return /submit|finish/i.test(b.textContent || b.value || ""); })[0] || cand[0];
  }
  if (!btn) return { clicked: false, href: location.href };
  btn.scrollIntoView({ block: "center" });
  btn.click();
  return { clicked: true, href: location.href };
}

// Has the application form rendered? (SPAs like Ashby / SmartRecruiters / EU Greenhouse render it
// with JS after load.) Self-contained — the runner polls this before filling.
function jmFormReady() {
  return !!document.querySelector(
    'input[type=file], input[type=email], input[autocomplete="email"], #first_name, input[name*="email" i]');
}

// Click an "Apply"/"Apply for this job" button to reveal a collapsed form. Self-contained.
function jmClickApply() {
  var els = Array.prototype.slice.call(document.querySelectorAll('a, button, [role=button], input[type=submit]'));
  var b = els.filter(function (x) {
    var t = (x.textContent || x.value || "").trim();
    return /^apply(\s|$)|apply for this job|apply now/i.test(t) && x.offsetParent !== null && !/sign|login/i.test(t);
  })[0];
  if (b) { b.click(); return true; }
  return false;
}

// Best-effort post-submit check: URL change or a confirmation message. Self-contained.
function jmApplyState(prevHref) {
  var body = document.body ? (document.body.innerText || "").slice(0, 5000) : "";
  function visChallenge() {
    // real challenge = image popup (bframe) / hCaptcha / Turnstile, or a v2 checkbox that is NOT
    // the invisible v3 badge. Ignore the badge (it's on the page even when nothing is required).
    var hard = document.querySelectorAll('iframe[src*="recaptcha/api2/bframe"], iframe[src*="hcaptcha.com"], iframe[src*="challenges.cloudflare.com"], .h-captcha, .cf-turnstile');
    for (var i = 0; i < hard.length; i++) { var r = hard[i].getBoundingClientRect(); if (r.width > 10 && r.height > 10) return true; }
    var anchors = document.querySelectorAll('iframe[src*="recaptcha/api2/anchor"]');
    for (var j = 0; j < anchors.length; j++) {
      if (anchors[j].closest(".grecaptcha-badge")) continue;     // invisible v3 badge — not a wall
      var rr = anchors[j].getBoundingClientRect();
      if (rr.width > 10 && rr.height > 10) return true;
    }
    return false;
  }
  return {
    href: location.href,
    changed: location.href !== prevHref,
    confirmed: /thank you|thanks for applying|application (received|submitted|complete|sent)|received your application|submission received|successfully (applied|submitted)|your application has been|you have (applied|successfully)|we have received/i.test(body),
    captcha: visChallenge()                          // a challenge that POPPED on submit (v2 bframe)
  };
}

// Snapshot the still-EMPTY fields (label, type, options) and tag each with data-jmk so the AI's
// answers can be applied back by key. Self-contained. Returns [{key,label,type,options?}].
function jmSnapshotForm() {
  function vis(el) {
    if (!el || el.disabled || el.readOnly) return false;
    var r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return s.display !== "none" && s.visibility !== "hidden" && (r.width > 1 || r.height > 1);
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
    return p.join(" ").replace(/\s+/g, " ").trim();
  }
  function isCombo(el) {
    return el.getAttribute("role") === "combobox" || el.getAttribute("aria-autocomplete") === "list" || !!el.closest(".select__container, [class*=select__]");
  }
  // react-select keeps its chosen value in .select__single-value, not the input's .value — so check
  // that, otherwise an already-answered dropdown gets re-sent to the AI every pass.
  function comboSel(el) {
    var c = el.closest(".select__control") || el.closest(".select__container") || el.closest("[class*='select']");
    return c ? !!c.querySelector(".select__single-value, [class*='singleValue'], [class*='single-value'], .select__multi-value, [class*='multiValue']") : false;
  }
  var out = [], i = 0;
  document.querySelectorAll("input, select, textarea").forEach(function (el) {
    if (!vis(el) || /hidden|file|submit|button|password/.test(el.type) || el.type === "radio" || el.type === "checkbox") return;
    var filled = el.tagName === "SELECT" ? (el.selectedIndex > 0 && el.value)
      : (isCombo(el) ? comboSel(el) : String(el.value || "").trim());
    if (filled) return;
    var t = lbl(el); if (!t) return;
    var key = "jmk" + (i++); el.setAttribute("data-jmk", key);
    var type = el.tagName === "SELECT" ? "select" : (isCombo(el) ? "combobox" : "text");
    var rec = { key: key, label: t.slice(0, 200), type: type };
    if (type === "select") rec.options = Array.prototype.map.call(el.options, function (o) { return (o.textContent || "").trim(); }).filter(Boolean).slice(0, 40);
    out.push(rec);
  });
  document.querySelectorAll("fieldset, [role=radiogroup]").forEach(function (g) {
    var radios = g.querySelectorAll("input[type=radio], input[type=checkbox]"); if (!radios.length) return;
    for (var r = 0; r < radios.length; r++) if (radios[r].checked) return;
    var lg = g.querySelector("legend, label, .label");
    var t = ((lg ? lg.textContent : g.textContent) || "").replace(/\s+/g, " ").trim(); if (!t) return;
    var key = "jmg" + (i++); g.setAttribute("data-jmk", key);
    var opts = [];
    for (var k = 0; k < radios.length; k++) { var rl = radios[k].closest("label") || (radios[k].id && document.querySelector('label[for="' + radios[k].id + '"]')); opts.push(((rl ? rl.textContent : radios[k].value) || "").replace(/\s+/g, " ").trim()); }
    out.push({ key: key, label: t.slice(0, 200), type: "radio", options: opts.filter(Boolean).slice(0, 20) });
  });
  return out.slice(0, 30);
}

// Apply AI answers (keyed by data-jmk) to the form. Self-contained, async (combobox needs waits).
async function jmApplyAnswers(answers) {
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function setNativeValue(el, value) {
    var proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    var s = Object.getOwnPropertyDescriptor(proto, "value"); if (s && s.set) s.set.call(el, value); else el.value = value;
    ["input", "change", "blur"].forEach(function (t) { el.dispatchEvent(new Event(t, { bubbles: true })); });
  }
  function setText(el, v) {
    if (String(el.value || "").trim()) return true;
    setNativeValue(el, v);
    if (!String(el.value || "").trim()) { try { el.focus(); if (el.select) el.select(); if (document.execCommand) document.execCommand("insertText", false, v); el.dispatchEvent(new Event("change", { bubbles: true })); el.blur(); } catch (e) {} }
    return !!String(el.value || "").trim();
  }
  function setSelect(sel, v) {
    var w = String(v).toLowerCase();
    for (var i = 0; i < sel.options.length; i++) { var o = sel.options[i]; if ((o.textContent || "").trim().toLowerCase() === w || (o.value || "").toLowerCase() === w) { sel.selectedIndex = i; sel.dispatchEvent(new Event("change", { bubbles: true })); return true; } }
    for (var j = 0; j < sel.options.length; j++) { var ot = (sel.options[j].textContent || "").trim().toLowerCase(); if (ot && (ot.indexOf(w) >= 0 || w.indexOf(ot) >= 0)) { sel.selectedIndex = j; sel.dispatchEvent(new Event("change", { bubbles: true })); return true; } }
    return false;
  }
  // react-select. Type input-only (a blur would close the menu), poll for options (background tabs
  // throttle timers), pick by text, then VERIFY via .select__single-value so a missed pick is
  // reported false (honestly left empty) rather than a silent wrong answer.
  function comboSelected(el) {
    var control = el.closest(".select__control") || el.closest(".select__container") || el.closest("[class*='select']");
    if (!control) return "";
    var sv = control.querySelector(".select__single-value, [class*='singleValue'], [class*='single-value'], .select__multi-value, [class*='multiValue']");
    return sv ? (sv.textContent || "").replace(/\s+/g, " ").trim().toLowerCase() : "";
  }
  function comboType(node, text) {
    var setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
    if (setter && setter.set) setter.set.call(node, text); else node.value = text;
    node.dispatchEvent(new Event("input", { bubbles: true }));
  }
  async function fillCombo(el, v) {
    var want = String(v).toLowerCase().trim(); if (!want) return false;
    var control = el.closest(".select__control") || el.closest(".select__container") ||
      el.closest("[class*='select']") || el.parentElement || el;
    try { el.focus(); } catch (e) {}
    control.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, button: 0 }));
    control.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, button: 0 }));
    await sleep(120);
    try { comboType(el, v); } catch (e) {}
    var pick = null;
    for (var t = 0; t < 12 && !pick; t++) {
      await sleep(200);
      var opts = document.querySelectorAll('.select__option, [class*="__option"], [id*="-option-"], [role="option"]');
      var exact = null, starts = null, partial = null;
      for (var k = 0; k < opts.length; k++) { var ot = (opts[k].textContent || "").toLowerCase().trim(); if (!ot) continue; if (ot === want) { exact = opts[k]; break; } if (!starts && ot.indexOf(want) === 0) starts = opts[k]; if (!partial && (ot.indexOf(want) >= 0 || want.indexOf(ot) >= 0)) partial = opts[k]; }
      pick = exact || starts || partial;
    }
    if (pick) {
      try { pick.scrollIntoView({ block: "nearest" }); } catch (e) {}
      pick.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, button: 0 }));
      pick.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, button: 0 }));
      pick.click();
      await sleep(150);
    }
    var got = comboSelected(el);
    return !!(got && (got === want || got.indexOf(want) >= 0 || want.indexOf(got) >= 0));
  }
  function clickRadio(g, v) {
    var w = String(v).toLowerCase(), radios = g.querySelectorAll("input[type=radio], input[type=checkbox]");
    for (var i = 0; i < radios.length; i++) { var rl = radios[i].closest("label") || (radios[i].id && document.querySelector('label[for="' + radios[i].id + '"]')); var t = ((rl ? rl.textContent : radios[i].value) || "").toLowerCase(); if (t.indexOf(w) >= 0 || (w === "yes" && /\byes\b/.test(t)) || (w === "no" && /\bno\b/.test(t))) { radios[i].click(); return true; } }
    return false;
  }
  var applied = 0, keys = Object.keys(answers || {});
  for (var i = 0; i < keys.length; i++) {
    var k = keys[i], v = answers[k];
    if (v == null || String(v).trim() === "") continue;
    var el = document.querySelector('[data-jmk="' + k + '"]'); if (!el) continue;
    var ok = false;
    if (el.tagName === "SELECT") ok = setSelect(el, v);
    else if (el.getAttribute("role") === "combobox" || el.getAttribute("aria-autocomplete") === "list" || (el.closest && el.closest(".select__container, [class*=select__]"))) ok = await fillCombo(el, v);
    else if (el.querySelector && el.querySelector("input[type=radio], input[type=checkbox]")) ok = clickRadio(el, v);
    else if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") ok = setText(el, v);
    if (ok) applied++;
  }
  return { applied: applied };
}
