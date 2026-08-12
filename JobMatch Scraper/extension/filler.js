// filler.js — application form auto-fill, injected into the apply page in the MAIN world
// (so React's controlled inputs accept programmatic values). Self-contained functions passed to
// chrome.scripting.executeScript({func}) — they must reference NO outer scope (serialized via
// toString and run in the page). Also importScripts()'d by the background batch runner.
//
// jmFillApplication(payload): payload = { fields:<profile map>, file:{name,mime,b64}|null, defaults }
//   returns { found, ats, filled, total, unfilled:[{label,reason}], fileAttached, submitSelector,
//             captcha, login }
//
// Adapters cover Workday / Oracle Cloud / iCIMS / Greenhouse / Lever / Ashby / SmartRecruiters
// precisely; a GENERIC adapter then fills ANY standard application form (file upload + email +
// submit) via autocomplete/type/label heuristics, so it works far beyond the named ATS. Workday,
// Oracle and iCIMS are account-walled multi-step wizards: we fill the CURRENT step and never
// advance it — jmWizardStep reports where you are, and the overlay re-fills when you click Next.

// Which ATS is this page? Fingerprints the PAGE, not the hostname — 29% of our corpus sits on
// employer vanity domains (careers.airbnb.com, jobs.sap.com, careers-inc.nttdata.com) that front a
// stock ATS, so a host allow-list can never enumerate them. Each marker is a DOM/script tell the
// platform emits on every tenant; ordered most-specific first. Also reads same-origin iframe SRCs
// (iCIMS/Greenhouse/Workable embed the form that way) without touching their contents.
// Self-contained (injected on its own). Returns "" when nothing matches.
function jmDetectAts() {
  var host = (location.hostname || "").toLowerCase();
  var href = location.href || "";
  // iframe srcs + script srcs: a vanity page that embeds its ATS names it here even when the
  // surrounding DOM is all marketing markup.
  var refs = "";
  try {
    var nodes = document.querySelectorAll("iframe[src], script[src], link[href]");
    for (var i = 0; i < nodes.length && i < 400; i++) {
      refs += " " + (nodes[i].getAttribute("src") || nodes[i].getAttribute("href") || "");
    }
    refs = refs.toLowerCase();
  } catch (e) {}
  function has(sel) { try { return !!document.querySelector(sel); } catch (e) { return false; } }
  // [name, host/url regex, DOM selector, iframe/script src substring]
  var M = [
    // Workday's data-automation-id is unique to it and present on every tenant, incl. vanity hosts.
    ["workday", /myworkdayjobs\.com|myworkdaysite\.com|wd\d+\.myworkday/,
      '[data-automation-id="jobPostingHeader"], [data-automation-id="applyButton"], [data-automation-id="progressBar"], [data-automation-id="legalNameSection_firstName"], [data-automation-id="bottom-navigation-next-button"], [data-automation-id="adventureButton"]', "myworkdayjobs.com"],
    ["icims", /icims\.com/, '.iCIMS_MainWrapper, #icims_content_iframe, .iCIMS_ApplyOnline, [id^="icims_"]', "icims.com"],
    // Oracle Recruiting (ORC): Oracle JET custom elements + the candidate-experience path.
    ["oracle", /oraclecloud\.com|\/hcmui\/candidateexperience/i,
      'oj-input-text, .oj-inputtext-input, .job-details__apply-button, #apply-flow, [data-ojkey]', "oraclecloud.com"],
    ["greenhouse", /greenhouse\.io/, '#grnhse_app, #application_form, #s3_upload_for_resume, [id^="job_application_"]', "greenhouse.io"],
    ["lever", /lever\.co/, '.application-form, [data-qa="btn-submit"], form[action*="lever"]', "lever.co"],
    ["ashby", /ashbyhq\.com/, 'input[name^="_systemfield_"]', "ashbyhq.com"],
    ["smartrecruiters", /smartrecruiters\.com/, '[data-test*="application"], form[action*="smartrecruiters"]', "smartrecruiters.com"],
    // data-ph-at-id is Phenom's signature attribute.
    ["phenom", /phenompeople\.com/, '[data-ph-at-id], .phenom-widget', "phenompeople.com"],
    ["successfactors", /successfactors\.|jobs2web/, '[id*="careersection"], tr.data-row, .jobDescriptionTable', "rmkcdn.successfactors.com"],
    ["taleo", /taleo\.net/, '#requisitionDescriptionInterface, [id*="requisitionDescription"]', "taleo.net"],
    ["jibe", /jibeapply\.com|talemetry/, '[class*="jibe" i]', "jibeapply.com"],
    ["avature", /avature\.net/, "#atsForm, li.listSingleColumnItem", "avature.net"],
    ["jobdiva", /jobdiva\.com/, "", "jobdiva.com"],
    ["ultipro", /recruiting\d*\.ultipro\.com/, ".opportunity-container, #Opportunity", "ultipro.com"],
    ["workable", /workable\.com/, '[data-ui="application-form"]', "workable.com"],
    ["breezy", /breezy\.hr/, ".application-form", "breezy.hr"],
    ["bamboohr", /bamboohr\.com/, ".ApplicantForm", "bamboohr.com"],
    ["pinpoint", /pinpointhq\.com/, "", "pinpointhq.com"],
    ["rippling", /ats\.rippling\.com/, "", "rippling.com"],
    ["recruitee", /recruitee\.com/, "", "recruitee.com"],
    ["personio", /personio\./, "", "personio."],
    ["jobvite", /jobvite\.com/, ".jv-page, [class^='jv-']", "jobvite.com"],
    ["peoplesoft", /HRS_HRAM_FL/, '[id^="HRS_"]', ""],
    ["brassring", /brassring\.com/, "", "brassring.com"],
    ["adp", /workforcenow\.adp\.com|myjobs\.adp\.com/, "", "adp.com"],
    ["dayforce", /dayforcehcm\.com/, "", "dayforcehcm.com"],
    ["paylocity", /paylocity\.com/, "", "paylocity.com"],
    ["paycom", /paycomonline\.net/, "", "paycomonline.net"],
    ["eightfold", /eightfold\.ai/, "", "eightfold.ai"],
    ["workatastartup", /workatastartup\.com/, "", ""]
  ];
  for (var m = 0; m < M.length; m++) {
    var name = M[m][0], hre = M[m][1], sel = M[m][2], src = M[m][3];
    if (hre && hre.test(host)) return name;
    if (hre && hre.test(href)) return name;
    if (sel && has(sel)) return name;
    if (src && refs.indexOf(src) >= 0) return name;
  }
  // Workday fallback: a tenant whose markers above are all absent still sprays data-automation-id
  // across the page. Require several so one stray attribute on an unrelated site can't win.
  try { if (document.querySelectorAll("[data-automation-id]").length >= 4) return "workday"; } catch (e) {}
  return "";
}

async function jmFillApplication(payload) {
  payload = payload || {};
  var F = payload.fields || {};
  var file = payload.file || null;
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  // ----------------------------- low-level DOM helpers -----------------------------
  function vis(el) {
    if (!el) return false;
    if (el.disabled || el.readOnly) return false;
    // react-select renders a hidden value-mirror <input required aria-hidden=true tabindex=-1> for
    // native validation — never a user-fillable field, so don't find/fill/flag it.
    if (el.getAttribute && el.getAttribute("aria-hidden") === "true") return false;
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
    // Workday names every field with a data-automation-id ("phone-device-type", "addressSection_city")
    // and often gives the visible <label> no `for=`. Humanising the id recovers the question when
    // nothing else does, and it's the SAME id on every tenant.
    var aid = el.getAttribute("data-automation-id");
    if (aid) parts.push(aid.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/[-_]+/g, " "));
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
    },
    // Workday — 35% of our feed. Every tenant emits the SAME data-automation-id attributes, so these
    // selectors are tenant-independent and work on vanity hosts too. Multi-step wizard behind an
    // account: `submit` is deliberately the final Submit only, never bottom-navigation-next-button,
    // so the review panel can't advance a step on your behalf.
    workday: {
      test: function () {
        return /myworkdayjobs\.com|myworkdaysite\.com/.test(location.hostname) ||
          document.querySelectorAll("[data-automation-id]").length >= 4;
      },
      name: {
        first: ['[data-automation-id="legalNameSection_firstName"]', '[data-automation-id="firstName"]', 'input[data-automation-id*="firstName" i]'],
        last: ['[data-automation-id="legalNameSection_lastName"]', '[data-automation-id="lastName"]', 'input[data-automation-id*="lastName" i]'],
        full: []
      },
      email: ['[data-automation-id="email"]', '[data-automation-id="userName"]', 'input[type=email]'],
      phone: ['[data-automation-id="phone-number"]', '[data-automation-id="phoneNumber"]', 'input[data-automation-id*="phone" i]', 'input[type=tel]'],
      resumeFile: ['[data-automation-id="file-upload-input-ref"]', 'input[type=file][data-automation-id]', 'input[type=file]'],
      submit: '[data-automation-id="bottom-navigation-submit-button"]'
    },
    // Oracle Cloud Recruiting (ORC) — 11.5%. Oracle JET widgets (oj-*) wrap real inputs, so text
    // fields fill normally once found; the dropdowns need the oj-listbox handler below. Reached both
    // on *.oraclecloud.com and behind employer vanity domains (careers.americanexpress.com).
    oracle: {
      test: function () {
        return /oraclecloud\.com/.test(location.hostname) || /\/hcmUI\/CandidateExperience/i.test(location.href) ||
          !!document.querySelector("oj-input-text, .oj-inputtext-input, [data-ojkey]");
      },
      name: {
        first: ['input[id*="firstName" i]', 'input[name*="firstName" i]', 'oj-input-text[id*="first" i] input'],
        last: ['input[id*="lastName" i]', 'input[name*="lastName" i]', 'oj-input-text[id*="last" i] input'],
        full: ['input[id*="fullName" i]', 'input[id*="candidateName" i]']
      },
      email: ['input[id*="email" i]', 'input[name*="email" i]', 'input[type=email]'],
      phone: ['input[id*="phoneNumber" i]', 'input[id*="phone" i]', 'input[type=tel]'],
      resumeFile: ['input[type=file]'],
      submit: 'button[data-affordance="submit"], .apply-flow__submit, button[title="Submit" i]'
    },
    // iCIMS — 4.5%, and the #2 host in the corpus. Two generations live side by side: the classic
    // portal (lowercase ids, often inside the #icims_content_iframe on an employer domain — we run in
    // all frames so the inner frame gets its own pass) and the newer camelCase Talent Cloud form.
    icims: {
      test: function () {
        return /icims\.com/.test(location.hostname) ||
          !!document.querySelector('.iCIMS_MainWrapper, #icims_content_iframe, .iCIMS_ApplyOnline, [id^="icims_"]');
      },
      name: {
        first: ['#firstname', 'input[name="firstname"]', 'input[name="firstName"]', 'input[id*="firstname" i]'],
        last: ['#lastname', 'input[name="lastname"]', 'input[name="lastName"]', 'input[id*="lastname" i]'],
        full: ['input[name="fullname"]']
      },
      email: ['#email', 'input[name="email"]', 'input[type=email]'],
      phone: ['#phone', '#mobilephone', '#homephone', 'input[name*="phone" i]', 'input[type=tel]'],
      resumeFile: ['#icims_addResumeSection input[type=file]', 'input[type=file][name*="resume" i]', 'input[type=file]'],
      submit: '#icims_button_apply, .iCIMS_ActionButton, input[name="submit"]'
    }
  };
  // Most-specific FIRST, and explicitly ordered rather than relying on object key order: several of
  // the older tests are loose enough to steal a page they don't own (ashby matches any
  // form[class*=application], greenhouse any #first_name), which would shadow the wizard adapters.
  var ORDER = ["workday", "oracle", "icims", "greenhouse", "lever", "ashby", "smartrecruiters"];

  // pick the most specific adapter; else GENERIC if the page looks like an application form
  function looksLikeForm() {
    return !!(firstSel(GENERIC.resumeFile) && firstSel(GENERIC.email));
  }
  var ats = null, A = null;
  for (var oi = 0; oi < ORDER.length; oi++) {
    var nm = ORDER[oi];
    try { if (ADAPTERS[nm] && ADAPTERS[nm].test()) { ats = nm; A = ADAPTERS[nm]; break; } } catch (e) {}
  }
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
    // ORDER IS THE DISAMBIGUATION — ruleValues takes the first hit, so anything whose wording overlaps
    // a looser rule has to sit above it. Four Workday fields forced this layout:
    //   phone-device-type / country-phone-code   both contain "phone" -> must beat the phone rule,
    //                                            or the phone NUMBER gets stuffed into a dropdown
    //   "...authorized to work in this country?"  contains "country"  -> must beat the country rule,
    //                                            or a Yes/No question gets answered "United States"
    //   addressSection_countryRegion             contains "country"   -> must beat the country rule:
    //                                            in Workday countryRegion is the STATE/province field,
    //                                            while the actual country field is countryDropdown.
    { re: /phone (device )?type|device type/, val: ["Mobile", "Cell Phone", "Cell", "Home"] },
    { re: /country (phone )?code|phone code|dial code/, val: ["United States of America (+1)", "United States (+1)", "+1"] },
    { re: /authoriz|legally (eligible|able) to work|work authorization|eligible to work/, val: work.authorized ? "Yes" : "No" },
    { re: /sponsor|work permit|need.*visa|require.*visa|visa.*(need|require|sponsor)/, val: work.requires_sponsorship ? "Yes" : "No" },
    // "country ?region" matches Workday's humanised countryRegion id but NOT a plain "Country/Region"
    // label (slash, no space), which really does mean country and falls through to the rule below.
    { re: /\bstate\b|province|country ?region/, val: addr.state },
    { re: /country|nationality/, val: addr.country },
    { re: /\bphone\b|mobile number|cell( phone)?/, val: F.phone },
    { re: /preferred (first )?name/, val: F.first_name }, { re: /preferred last name/, val: F.last_name },
    { re: /linkedin/, val: links.linkedin },
    { re: /github/, val: links.github },
    { re: /portfolio|personal (web)?site|website/, val: links.portfolio || links.website },
    // "^source$" catches Workday's source dropdown on tenants that give it no visible label (labelText
    // then falls back to the humanised data-automation-id). Anchored so an "open source" question can't match.
    { re: /how did you (hear|find)|^source$|referral source/, val: [F.how_did_you_hear, "LinkedIn", "Job board", "Company website", "Other"] },
    { re: /desired (salary|compensation|pay)|salary expectation/, val: comp.desired_salary },
    { re: /start date|available|availability/, val: F.start_date },
    { re: /willing to relocate|open to relocat|relocat/, val: F.relocate ? "Yes" : "No", onlyIf: F.relocate !== "" && F.relocate != null },
    { re: /gender/, val: eeo.gender }, { re: /hispanic|latino/, val: eeo.hispanic_latino },
    { re: /race|ethnic/, val: eeo.race }, { re: /veteran/, val: eeo.veteran }, { re: /disab/, val: eeo.disability },
    { re: /city/, val: addr.city },
    { re: /zip|postal/, val: addr.postal }, { re: /address/, val: addr.line1 }
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
      var cur = comboSelected(el);                                  // already set (e.g. 2nd pass)? skip
      if (cur && (cur === want || cur.indexOf(want) >= 0 || want.indexOf(cur) >= 0)) return true;
      try { el.focus(); } catch (e) {}
      control.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, button: 0 }));
      control.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, button: 0 }));
      await sleep(100);
      try { comboType(el, vals[c]); } catch (e) {}
      var pick = null, sawOpts = false;
      for (var t = 0; t < 9 && !pick; t++) {                        // poll ~1.5s, but bail fast if no menu
        await sleep(160);
        var opts = document.querySelectorAll('.select__option, [class*="__option"], [id*="-option-"], [role="option"]');
        if (opts.length) sawOpts = true;
        var exact = null, starts = null, partial = null;
        for (var k = 0; k < opts.length; k++) {
          var ot = (opts[k].textContent || "").toLowerCase().trim(); if (!ot) continue;
          if (ot === want) { exact = opts[k]; break; }
          if (!starts && ot.indexOf(want) === 0) starts = opts[k];
          if (!partial && (ot.indexOf(want) >= 0 || want.indexOf(ot) >= 0)) partial = opts[k];
        }
        pick = exact || starts || partial;
        if (!pick && !sawOpts && t >= 2) break;                     // menu never opened — don't burn the full budget
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

  // Workday dropdown: a <button aria-haspopup="listbox"> whose options render in a PORTAL at body
  // level (NOT inside the button's container, so a scoped query finds nothing). The chosen value
  // becomes the button's own text — that's both how we skip an already-answered field and how we
  // verify the pick landed, so a miss stays honestly empty instead of silently wrong.
  function listboxSelected(btn) {
    var sel = btn.querySelector('[data-automation-id="selectedItem"]');
    var t = ((sel ? sel.textContent : btn.textContent) || "").replace(/\s+/g, " ").trim().toLowerCase();
    return /^(select one|select\.\.\.|select|search|choose one|choose)?$/.test(t) ? "" : t;
  }
  // Closing matters more than it looks. These menus render in a portal at BODY level, so a menu left
  // open from the previous field is indistinguishable from the current field's menu — optionNodes()
  // would hand back the stale options and we'd answer this dropdown from the last one's list. So:
  // Escape on the control, Escape on the document, and if something is still open, toggle the control
  // to shut it. Verified, not assumed.
  function menuOpen() {
    return optionNodes().length > 0 || !!document.querySelector(".oj-listbox-drop");
  }
  async function closeMenu(el) {
    for (var a = 0; a < 2 && menuOpen(); a++) {
      try { el.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", keyCode: 27, bubbles: true })); } catch (e) {}
      try { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", keyCode: 27, bubbles: true })); } catch (e) {}
      await sleep(90);
      if (menuOpen()) { try { el.click(); } catch (e) {} await sleep(90); }
    }
  }
  // The open menu's option nodes, by MOST-SPECIFIC selector first. One combined selector would return
  // matches in DOCUMENT order across all of them, so on a menu where an <li role=option> wraps the real
  // clickable [data-automation-id=promptOption] the wrapper sorts first and the click lands on the
  // wrapper — missing the handler and silently leaving the field empty. Taking the first selector that
  // matches anything keeps us on the platform's own option node.
  function optionNodes() {
    var SELS = ['[data-automation-id="promptOption"]', 'ul[role="listbox"] li[role="option"]',
                '[role="listbox"] [role="option"]', '[role="option"]'];
    for (var i = 0; i < SELS.length; i++) {
      var n = document.querySelectorAll(SELS[i]);
      if (n.length) return n;
    }
    return [];
  }
  async function fillListbox(btn, vals) {
    for (var c = 0; c < vals.length; c++) {
      var want = String(vals[c] || "").toLowerCase().trim();
      if (!want) continue;
      var cur = listboxSelected(btn);
      if (cur && (cur === want || cur.indexOf(want) >= 0 || want.indexOf(cur) >= 0)) return true;
      try { btn.click(); } catch (e) {}
      var pick = null, sawOpts = false;
      for (var t = 0; t < 10 && !pick; t++) {
        await sleep(150);
        var opts = optionNodes();
        if (opts.length) sawOpts = true;
        var exact = null, starts = null, partial = null;
        for (var k = 0; k < opts.length; k++) {
          var ot = ((opts[k].getAttribute && opts[k].getAttribute("data-automation-label")) ||
                    opts[k].textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
          if (!ot) continue;
          if (ot === want) { exact = opts[k]; break; }
          if (!starts && ot.indexOf(want) === 0) starts = opts[k];
          if (!partial && (ot.indexOf(want) >= 0 || want.indexOf(ot) >= 0)) partial = opts[k];
        }
        pick = exact || starts || partial;
        if (!pick && !sawOpts && t >= 3) break;              // menu never opened — don't burn the budget
      }
      if (pick) {
        try { pick.scrollIntoView({ block: "nearest" }); } catch (e) {}
        pick.click();
        await sleep(200);
        var got = listboxSelected(btn);
        if (got && (got === want || got.indexOf(want) >= 0 || want.indexOf(got) >= 0)) return true;
      }
      await closeMenu(btn);      // leave no menu open, or it poisons the next field's options
    }
    return false;
  }

  // Oracle JET select (oj-select-single / oj-combobox-one): the control is a div[role=combobox] and
  // the options render in .oj-listbox-drop at body level, filtered by a search box when present.
  function ojSelected(root) {
    var c = root.querySelector(".oj-select-chosen, .oj-combobox-chosen, [class*='chosen']");
    var t = ((c ? c.textContent : "") || "").replace(/\s+/g, " ").trim().toLowerCase();
    return /^(select a value|select\.\.\.|select|choose)?$/.test(t) ? "" : t;
  }
  async function fillOjSelect(root, vals) {
    for (var c = 0; c < vals.length; c++) {
      var want = String(vals[c] || "").toLowerCase().trim();
      if (!want) continue;
      var cur = ojSelected(root);
      if (cur && (cur === want || cur.indexOf(want) >= 0 || want.indexOf(cur) >= 0)) return true;
      var opener = root.querySelector(".oj-select-choice, .oj-combobox-choice, [role=combobox]") || root;
      try { opener.click(); } catch (e) {}
      await sleep(200);
      var search = document.querySelector(".oj-listbox-drop input.oj-listbox-input, .oj-listbox-search input");
      if (search) { try { setNativeValue(search, String(vals[c])); } catch (e) {} }
      var pick = null;
      for (var t = 0; t < 10 && !pick; t++) {
        await sleep(150);
        var opts = [], OSEL = [".oj-listbox-drop .oj-listbox-result-label", ".oj-listbox-drop li[role=option]",
                               ".oj-listbox-result", ".oj-listbox-drop [role=option]"];
        for (var oq = 0; oq < OSEL.length && !opts.length; oq++) opts = document.querySelectorAll(OSEL[oq]);
        var exact = null, partial = null;
        for (var k = 0; k < opts.length; k++) {
          var ot = (opts[k].textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
          if (!ot) continue;
          if (ot === want) { exact = opts[k]; break; }
          if (!partial && (ot.indexOf(want) >= 0 || want.indexOf(ot) >= 0)) partial = opts[k];
        }
        pick = exact || partial;
      }
      if (pick) {
        pick.click();
        await sleep(200);
        var got = ojSelected(root);
        if (got && (got === want || got.indexOf(want) >= 0 || want.indexOf(got) >= 0)) return true;
      }
      await closeMenu(opener);
    }
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
  // 3b) dropdowns that are NOT <input>/<select> at all, so the sweep above can never see them:
  // Workday renders every one as <button aria-haspopup="listbox"> and Oracle as <oj-select-single>.
  // This is not a nicety — on Workday the REQUIRED fields (phone device type, country, source,
  // EEO) are exactly these, so without this pass its first step can't be completed.
  var widgets = Array.prototype.slice.call(document.querySelectorAll(
    'button[aria-haspopup="listbox"], [role=combobox][aria-haspopup="listbox"], oj-select-single, oj-combobox-one'));
  for (var wi = 0; wi < widgets.length; wi++) {
    var wel = widgets[wi];
    if (!vis(wel)) continue;
    var wlbl = labelText(wel); if (!wlbl) continue;
    var wvals = ruleValues(wlbl); if (!wvals) continue;
    var isOj = /^OJ-/.test(wel.tagName);
    if (isOj ? ojSelected(wel) : listboxSelected(wel)) continue;      // already answered — don't clobber
    var wok = isOj ? await fillOjSelect(wel, wvals) : await fillListbox(wel, wvals);
    track(wok, wlbl.slice(0, 60), isRequired(wel), true);
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
  // ...and the same for the button/oj dropdowns, or a required Workday step would report "nothing
  // left for you" while its dropdowns sat empty and Next stayed disabled.
  widgets.forEach(function (w) {
    if (!vis(w) || !isRequired(w)) return;
    if (/^OJ-/.test(w.tagName) ? ojSelected(w) : listboxSelected(w)) return;
    var lab = labelText(w) || "dropdown";
    if (!unfilled.some(function (u) { return u.label === lab; })) unfilled.push({ label: lab, reason: "required" });
  });

  // walls a human must clear (the runner parks the job on any of these)
  var captcha = visibleChallenge();                 // only a VISIBLE challenge, not invisible reCAPTCHA
  var login = !!document.querySelector("input[type=password]") ||
    /\/(login|sign[_-]?in|signin|auth|account\/new|users\/sign)/i.test(location.href);

  // Where are we in a multi-step wizard? Drives the review panel: on a non-final step it must NOT
  // offer "Submit application" (there's nothing to submit yet) — it tells you to click Next instead,
  // and re-fills automatically when you do.
  var wizard = null;
  try {
    var steps = document.querySelectorAll('[data-automation-id="progressBar"] [data-automation-id="progressBarStep"], ' +
      '[data-automation-id="progressBar"] li, .apply-flow__progress li, ol[class*="progress"] li, [role="tablist"] [role="tab"]');
    if (steps.length > 1) {
      var active = -1;
      for (var si = 0; si < steps.length; si++) {
        var s = steps[si], cls = (s.className || "") + " " + (s.getAttribute("data-automation-id") || "");
        if (s.getAttribute("aria-current") || s.getAttribute("aria-selected") === "true" ||
            /active|current|selected/i.test(cls)) { active = si; break; }
      }
      var hasSubmit = !!document.querySelector('[data-automation-id="bottom-navigation-submit-button"], button[data-affordance="submit"]');
      wizard = {
        index: active >= 0 ? active + 1 : 0, total: steps.length,
        step: active >= 0 ? (steps[active].textContent || "").replace(/\s+/g, " ").trim().slice(0, 60) : "",
        isLast: hasSubmit || (active >= 0 && active === steps.length - 1)
      };
    }
  } catch (e) {}

  return {
    found: true, ats: ats, filled: filled, total: total, unfilled: unfilled.slice(0, 25),
    fileAttached: fileAttached, submitSelector: A.submit, captcha: captcha, login: login,
    wizard: wizard, href: location.href
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
// with JS after load, often inside web components — so pierce shadow DOM.) Self-contained.
function jmFormReady() {
  var sel = 'input[type=file], input[type=email], input[autocomplete="email"], #first_name, ' +
            'input[name*="email" i], input[name*="first" i], ' +
            // Workday/Oracle/iCIMS: the wizard steps often carry NO type=email and no name=first —
            // Workday's email input is a plain text field identified only by its automation id, and a
            // later step (Experience, Questions) may have neither. Match the platform's own markers so
            // a mid-wizard page still counts as "form ready".
            '[data-automation-id="legalNameSection_firstName"], [data-automation-id="email"], ' +
            '[data-automation-id="userName"], [data-automation-id="bottom-navigation-next-button"], ' +
            'oj-input-text, .oj-inputtext-input, .iCIMS_ApplyOnline, #icims_addResumeSection, ' +
            'button[aria-haspopup="listbox"]';
  function find(root) {
    if (!root || !root.querySelector) return false;
    if (root.querySelector(sel)) return true;
    var all = root.querySelectorAll("*");
    for (var i = 0; i < all.length; i++) { if (all[i].shadowRoot && find(all[i].shadowRoot)) return true; }
    return false;
  }
  return find(document);
}

// Is a "Start Your Application" chooser / modal dialog open in THIS frame? (e.g. Workday's
// Autofill-with-Resume / Apply-Manually / Use-My-Last-Application.) Callers run this across ALL
// frames before any gate-click, so a modal in one frame stops the "Apply" click in another.
// Self-contained (injected on its own).
function jmChooserOpen() {
  var modal = document.querySelector('[role="dialog"], [aria-modal="true"], dialog[open]');
  if (modal && modal.offsetParent !== null) return true;
  return Array.prototype.some.call(
    document.querySelectorAll('a, button, [role=button], input[type=submit]'),
    function (x) {
      return x.offsetParent !== null &&
        /apply manually|autofill with resume|use my last application/i.test(x.textContent || "");
    });
}

// Click a gate that reveals the real form: "Apply" / "Apply now" / "Apply for this role" /
// "I'm interested" / "Start application". Self-contained. Skips sign-in/filter/share look-alikes.
function jmClickApply() {
  // If a chooser/modal is open (e.g. Workday's "Start Your Application": Autofill with Resume /
  // Apply Manually / Use My Last Application), DON'T click any gate — clicking the "Apply" button
  // behind the modal just dismisses it. Let the user pick their option inside the modal.
  var modal = document.querySelector('[role="dialog"], [aria-modal="true"], dialog[open]');
  if (modal && modal.offsetParent !== null) return false;
  var hasChooser = Array.prototype.some.call(
    document.querySelectorAll('a, button, [role=button], input[type=submit]'),
    function (x) {
      return x.offsetParent !== null &&
        /apply manually|autofill with resume|use my last application/i.test(x.textContent || "");
    });
  if (hasChooser) return false;
  // Platform-specific gates first — these are unambiguous, so we don't have to reason about button
  // text at all. (Workday's "Apply" is an <a data-automation-id="adventureButton">; iCIMS puts its
  // gate in a styled link; Oracle's is a button on the job-details panel.)
  var known = document.querySelector(
    '[data-automation-id="adventureButton"], [data-automation-id="applyButton"], ' +
    '#icims_apply_button, .iCIMS_ApplyOnlineButton, .job-details__apply-button');
  if (known && known.offsetParent !== null) { known.click(); return true; }
  var els = Array.prototype.slice.call(document.querySelectorAll('a, button, [role=button], input[type=submit]'));
  var b = els.filter(function (x) {
    var t = (x.textContent || x.value || "").replace(/\s+/g, " ").trim();
    if (!t || t.length > 40 || x.offsetParent === null) return false;
    var tl = t.toLowerCase();
    // Skip sign-in/filter/share look-alikes AND third-party SSO autofill ("Apply With LinkedIn/Indeed",
    // "Continue with Google") — those open an OAuth popup instead of revealing the on-page form.
    if (/sign|log ?in|filter|search|sort|saved|already applied|share|refer/.test(tl)) return false;
    if (/\bwith\b|linkedin|indeed|google|facebook|\bseek\b|xing/.test(tl)) return false;
    // Skip the options in a "Start Your Application" chooser (e.g. Workday) — "Apply Manually" /
    // "Autofill with Resume" / "Use My Last Application" are account/résumé choices YOU make, not a
    // gate to auto-click (clicking one dismisses the modal / hits a login wall).
    if (/manually|autofill|last application/.test(tl)) return false;
    return /^apply\b/.test(tl) ||                         // Apply / Apply now / Apply for this role
           /^(i'?m |i am )?interested\b/.test(tl) ||       // I'm interested / Interested
           /^(start|begin)\b.*\bapplication\b/.test(tl);   // Start (your) application / Begin application
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
    if (el.getAttribute && el.getAttribute("aria-hidden") === "true") return false;   // react-select value-mirror
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
  // Workday <button aria-haspopup=listbox> / Oracle <oj-select-single> dropdowns. Reported with NO
  // options list on purpose: their options don't exist in the DOM until the menu is opened, and
  // opening every one to enumerate it would be slow and visibly disruptive. jmMatchLearned therefore
  // passes a saved value straight through — safe here because jmApplyAnswers VERIFIES the pick landed
  // and reports failure, so a stale answer ends up honestly empty rather than silently wrong.
  document.querySelectorAll('button[aria-haspopup="listbox"], [role=combobox][aria-haspopup="listbox"], oj-select-single, oj-combobox-one')
    .forEach(function (el) {
      if (!vis(el)) return;
      var oj = /^OJ-/.test(el.tagName);
      var chosen = el.querySelector(oj ? ".oj-select-chosen, .oj-combobox-chosen" : '[data-automation-id="selectedItem"]');
      var ctext = ((chosen ? chosen.textContent : (oj ? "" : el.textContent)) || "").replace(/\s+/g, " ").trim();
      if (ctext && !/^(select one|select a value|select\.\.\.|select|search|choose one|choose)$/i.test(ctext)) return;  // answered
      var t2 = lbl(el);
      // Workday often gives these no <label for=>; the humanised data-automation-id is the fallback.
      var aid = el.getAttribute("data-automation-id");
      if (!t2 && aid) t2 = aid.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/[-_]+/g, " ");
      if (!t2) return;
      var key2 = "jmk" + (i++); el.setAttribute("data-jmk", key2);
      out.push({ key: key2, label: t2.slice(0, 200), type: oj ? "ojselect" : "listbox" });
    });
  // radio/checkbox QUESTIONS — robust to forms with NO <fieldset>/<legend> (e.g. Tesla: a question
  // line + styled Yes/No radios). Group options by `name`/nearest container, SKIP already-answered
  // groups, tag the options' common ancestor (so the apply step's clickRadio can click within it),
  // and recover the question text by walking up from that anchor. Mirrors jmCaptureFilled so the
  // learned-answer + AI pass can actually FILL these, not just learn them.
  (function () {
    function optEl(inp) { return inp.closest("label") || (inp.id && document.querySelector('label[for="' + (window.CSS && CSS.escape ? CSS.escape(inp.id) : inp.id) + '"]')) || inp; }
    function otxt(el) { return ((el && el.getAttribute && el.getAttribute("aria-label")) || (el && el.textContent) || "").replace(/\s+/g, " ").trim(); }
    var groups = {}, gc = 0, cmap = (typeof WeakMap !== "undefined") ? new WeakMap() : null;
    function ckey(c) { if (!c) return "c0"; if (cmap) { if (!cmap.has(c)) cmap.set(c, "c" + (++gc)); return cmap.get(c); } if (!c.__jmd) c.__jmd = "c" + (++gc); return c.__jmd; }
    Array.prototype.forEach.call(document.querySelectorAll("input[type=radio], input[type=checkbox]"), function (inp) {
      var le = optEl(inp); if (!vis(inp) && !vis(le)) return;
      var key = inp.name ? ("n:" + inp.name) : ckey(inp.closest("fieldset, [role=radiogroup], [role=group]") || inp.parentElement);
      var g = groups[key] || (groups[key] = { inputs: [], texts: [], checked: false });
      g.inputs.push(inp); g.texts.push(otxt(le) || String(inp.value || "").trim());
      if (inp.checked) g.checked = true;
    });
    function lca(els) { var a = els[0]; for (var j = 1; j < els.length && a; j++) { while (a && !a.contains(els[j])) a = a.parentElement; } return a; }
    function question(node, texts) {
      for (var hop = 0; node && hop < 6; hop++, node = node.parentElement) {
        var t = node.textContent || "";
        texts.forEach(function (o) { if (o) t = t.split(o).join(" "); });
        t = t.replace(/\s+/g, " ").trim();
        if (t.length >= 8 && /[a-z]/i.test(t)) return t;
      }
      return "";
    }
    Object.keys(groups).forEach(function (k) {
      var g = groups[k]; if (g.checked || !g.inputs.length) return;     // already answered -> don't re-ask
      var anchor = lca(g.inputs); if (!anchor) return;
      var label = question(anchor, g.texts); if (!label) return;
      var key2 = "jmg" + (i++); anchor.setAttribute("data-jmk", key2);
      out.push({ key: key2, label: label.slice(0, 200), type: "radio", options: g.texts.filter(Boolean).slice(0, 20) });
    });
  })();
  // custom TOGGLE / segmented-button choice questions: Ashby, Vanta and many design systems render
  // Yes/No (etc.) as <button> or role= toggles with NO <input> at all. Group the option controls by
  // their shared parent, skip already-chosen ones, tag the container so clickRadio clicks within it,
  // and recover the question via ancestor-walk. Generic — not tied to any one ATS.
  (function () {
    if (typeof Map === "undefined") return;
    var NAV = /\b(submit|continue|next|back|previous|prev|apply|save|cancel|add|remove|delete|upload|browse|edit|search|close|menu|skip|sign in|log in|login)\b/;
    var cand = [];
    document.querySelectorAll('button, [role=radio], [role=button], [role=tab], [role=option], [role=switch]').forEach(function (el) {
      if (el.getAttribute("aria-haspopup") || el.hasAttribute("data-jmk") ||
              (el.closest && el.closest("oj-select-single, oj-combobox-one"))) return;   // a popup DROPDOWN, not a toggle option
      if (!vis(el) || el.querySelector("input")) return;
      var t = (el.textContent || el.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim();
      if (!t || t.length > 30 || NAV.test(t.toLowerCase())) return;
      cand.push({ el: el, t: t });
    });
    if (cand.length < 2) return;
    function group(el) {                                             // smallest ancestor holding 2-5 options
      var node = el.parentElement;
      for (var hop = 0; node && hop < 6; hop++, node = node.parentElement) {
        var n = 0; for (var c = 0; c < cand.length; c++) if (node.contains(cand[c].el)) n++;
        if (n >= 2 && n <= 5) return node;
        if (n > 5) return null;
      }
      return null;
    }
    var groups = new Map();
    cand.forEach(function (o) { var g = group(o.el); if (!g) return; var arr = groups.get(g) || []; arr.push(o.t); groups.set(g, arr); });
    groups.forEach(function (texts, p) {
      if (texts.length < 2 || texts.length > 5) return;              // a Yes/No or small choice set
      if (p.querySelector("input[type=radio], input[type=checkbox], select")) return;
      if (p.closest("[data-jmk]")) return;                           // already handled above
      if (p.querySelector('[aria-checked="true"], [aria-pressed="true"], [aria-selected="true"], [class*="selected" i], [class*="active" i], [class*="checked" i]')) return; // already chosen
      var node = p, label = "";
      for (var hop = 0; node && hop < 6; hop++, node = node.parentElement) {
        var tx = node.textContent || "";
        texts.forEach(function (o) { if (o) tx = tx.split(o).join(" "); });
        tx = tx.replace(/\s+/g, " ").trim();
        if (tx.length >= 8 && /[a-z]/i.test(tx)) { label = tx; break; }
      }
      if (!label) return;
      var key = "jmg" + (i++); p.setAttribute("data-jmk", key);
      out.push({ key: key, label: label.slice(0, 200), type: "radio", options: texts.slice(0, 20) });
    });
  })();
  return out.slice(0, 30);
}

// ---- Learned-answer matching (NO AI) — shared by popup.js + background.js (both load filler.js) ----
// Stable normalized label key, mirroring db.normalize_label, so the same question matches across forms.
function jmNormLabel(s) {
  s = String(s || "").toLowerCase();
  s = s.replace(/\([^()]*\b(?:required|optional)\b[^()]*\)/g, " ");
  s = s.replace(/[^a-z0-9 ]+/g, " ").replace(/\s+/g, " ").trim();
  return s.slice(0, 200);
}
// Map snapshot fields -> {key: value} using ONLY the user's saved answers (no model). learned is
// {normLabel: {value}}. For option fields the saved value must match an offered option, so a stale
// answer is never forced onto a dropdown that no longer lists it.
function jmMatchLearned(fields, learned) {
  learned = learned || {};
  var answers = {};
  (fields || []).forEach(function (f) {
    var rec = learned[jmNormLabel(f.label)];
    var val = rec && rec.value;
    if (!val) return;
    var opts = f.options || [];
    if (!opts.length) { answers[f.key] = val; return; }
    var vl = String(val).toLowerCase().trim();
    if (opts.some(function (o) { var ol = String(o).toLowerCase().trim(); return vl === ol || vl.indexOf(ol) >= 0 || ol.indexOf(vl) >= 0; })) answers[f.key] = val;
  });
  return answers;
}

// Apply saved/learned answers (keyed by data-jmk) to the form. Self-contained, async (combobox needs waits).
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
    var cur = comboSelected(el);                                  // already correct? skip the work
    if (cur && (cur === want || cur.indexOf(want) >= 0 || want.indexOf(cur) >= 0)) return true;
    var control = el.closest(".select__control") || el.closest(".select__container") ||
      el.closest("[class*='select']") || el.parentElement || el;
    try { el.focus(); } catch (e) {}
    control.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, button: 0 }));
    control.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, button: 0 }));
    await sleep(100);
    try { comboType(el, v); } catch (e) {}
    var pick = null, sawOpts = false;
    for (var t = 0; t < 9 && !pick; t++) {
      await sleep(160);
      var opts = document.querySelectorAll('.select__option, [class*="__option"], [id*="-option-"], [role="option"]');
      if (opts.length) sawOpts = true;
      var exact = null, starts = null, partial = null;
      for (var k = 0; k < opts.length; k++) { var ot = (opts[k].textContent || "").toLowerCase().trim(); if (!ot) continue; if (ot === want) { exact = opts[k]; break; } if (!starts && ot.indexOf(want) === 0) starts = opts[k]; if (!partial && (ot.indexOf(want) >= 0 || want.indexOf(ot) >= 0)) partial = opts[k]; }
      pick = exact || starts || partial;
      if (!pick && !sawOpts && t >= 2) break;                     // menu never opened — bail fast
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
    var w = String(v).toLowerCase().trim(), radios = g.querySelectorAll("input[type=radio], input[type=checkbox]");
    for (var i = 0; i < radios.length; i++) { var rl = radios[i].closest("label") || (radios[i].id && document.querySelector('label[for="' + radios[i].id + '"]')); var t = ((rl ? rl.textContent : radios[i].value) || "").toLowerCase(); if (t.indexOf(w) >= 0 || (w === "yes" && /\byes\b/.test(t)) || (w === "no" && /\bno\b/.test(t))) { radios[i].click(); return true; } }
    // custom toggle/segmented buttons (no <input>): click the option whose text matches the value.
    var opts = g.querySelectorAll('button, [role=radio], [role=button], [role=tab], [role=option], [role=switch]');
    for (var j = 0; j < opts.length; j++) { if (opts[j].querySelector("input")) continue; var ot = (opts[j].textContent || opts[j].getAttribute("aria-label") || "").toLowerCase().replace(/\s+/g, " ").trim(); if (!ot) continue; if (ot === w || (w && ot.indexOf(w) >= 0) || (w === "yes" && /\byes\b/.test(ot)) || (w === "no" && /\bno\b/.test(ot))) { opts[j].click(); return true; } }
    return false;
  }
  // Workday listbox button / Oracle oj-select — same mechanics as jmFillApplication (options render in
  // a portal at body level, so they can't be queried through the control). Verify-then-report, so a
  // saved answer the dropdown no longer offers fails visibly instead of landing on the wrong option.
  function pickedText(el, oj) {
    var c = el.querySelector(oj ? ".oj-select-chosen, .oj-combobox-chosen" : '[data-automation-id="selectedItem"]');
    var t = ((c ? c.textContent : (oj ? "" : el.textContent)) || "").replace(/\s+/g, " ").trim().toLowerCase();
    return /^(select one|select a value|select\.\.\.|select|search|choose one|choose)?$/.test(t) ? "" : t;
  }
  async function fillPopupSelect(el, v, oj) {
    var want = String(v).toLowerCase().trim(); if (!want) return false;
    var cur = pickedText(el, oj);
    if (cur && (cur === want || cur.indexOf(want) >= 0 || want.indexOf(cur) >= 0)) return true;
    var opener = oj ? (el.querySelector(".oj-select-choice, .oj-combobox-choice, [role=combobox]") || el) : el;
    try { opener.click(); } catch (e) {}
    await sleep(200);
    if (oj) {
      var sb = document.querySelector(".oj-listbox-drop input.oj-listbox-input, .oj-listbox-search input");
      if (sb) { try { setNativeValue(sb, String(v)); } catch (e) {} }
    }
    // Most-specific selector first — see optionNodes() in jmFillApplication: a combined selector
    // returns document order, so an <li role=option> wrapping the real clickable node would win and
    // the click would miss the handler.
    var SELS = oj
      ? [".oj-listbox-drop .oj-listbox-result-label", ".oj-listbox-drop li[role=option]", ".oj-listbox-result"]
      : ['[data-automation-id="promptOption"]', 'ul[role="listbox"] li[role="option"]', '[role="option"]'];
    var pick = null;
    for (var t = 0; t < 10 && !pick; t++) {
      await sleep(150);
      var opts = [];
      for (var sq = 0; sq < SELS.length && !opts.length; sq++) opts = document.querySelectorAll(SELS[sq]);
      var exact = null, partial = null;
      for (var k2 = 0; k2 < opts.length; k2++) {
        var ot = ((opts[k2].getAttribute && opts[k2].getAttribute("data-automation-label")) || opts[k2].textContent || "")
          .replace(/\s+/g, " ").trim().toLowerCase();
        if (!ot) continue;
        if (ot === want) { exact = opts[k2]; break; }
        if (!partial && (ot.indexOf(want) >= 0 || want.indexOf(ot) >= 0)) partial = opts[k2];
      }
      pick = exact || partial;
    }
    if (pick) { try { pick.scrollIntoView({ block: "nearest" }); } catch (e) {} pick.click(); await sleep(200); }
    var got = pickedText(el, oj);
    if (got && (got === want || got.indexOf(want) >= 0 || want.indexOf(got) >= 0)) return true;
    // Shut the menu before the next answer is applied. These render in a body-level portal, so one
    // left open would have its options read as the NEXT dropdown's options. Escape, then toggle.
    for (var a = 0; a < 2; a++) {
      var stillOpen = false;
      for (var sq2 = 0; sq2 < SELS.length && !stillOpen; sq2++) stillOpen = document.querySelectorAll(SELS[sq2]).length > 0;
      if (!stillOpen) break;
      try { opener.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", keyCode: 27, bubbles: true })); } catch (e) {}
      try { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", keyCode: 27, bubbles: true })); } catch (e) {}
      await sleep(90);
      try { opener.click(); } catch (e) {}
      await sleep(90);
    }
    return false;
  }
  var applied = 0, keys = Object.keys(answers || {});
  for (var i = 0; i < keys.length; i++) {
    var k = keys[i], v = answers[k];
    if (v == null || String(v).trim() === "") continue;
    var el = document.querySelector('[data-jmk="' + k + '"]'); if (!el) continue;
    var ok = false;
    // The popup-select checks come FIRST: a Workday control can carry role=combobox too, and the
    // react-select path below would then try to drive it with the wrong mechanics and fail.
    if (/^OJ-(SELECT|COMBOBOX)/.test(el.tagName)) ok = await fillPopupSelect(el, v, true);
    else if (el.matches && el.matches('button[aria-haspopup="listbox"], [aria-haspopup="listbox"]')) ok = await fillPopupSelect(el, v, false);
    else if (el.tagName === "SELECT") ok = setSelect(el, v);
    else if (el.getAttribute("role") === "combobox" || el.getAttribute("aria-autocomplete") === "list" || (el.closest && el.closest(".select__container, [class*=select__]"))) ok = await fillCombo(el, v);
    else if (el.querySelector && el.querySelector('input[type=radio], input[type=checkbox], button, [role=radio], [role=button], [role=tab], [role=option], [role=switch]')) ok = clickRadio(el, v);
    else if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") ok = setText(el, v);
    if (ok) applied++;
  }
  return { applied: applied };
}

// ===================== VISION FALLBACK (used only when the normal pass parks) =====================
// jmVisionSnapshot: enumerate EVERY visible interactive element (index + label + type + options +
// current value + on-screen rect) and tag each with data-jmv. Paired with a screenshot, this lets a
// vision model decide values even when label-detection failed. Self-contained.
function jmVisionSnapshot() {
  function vis(el) {
    if (!el || el.disabled || el.readOnly) return false;
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
    return p.join(" ").replace(/\s+/g, " ").trim().slice(0, 180);
  }
  function isCombo(el) {
    return el.getAttribute("role") === "combobox" || el.getAttribute("aria-autocomplete") === "list" || !!el.closest(".select__container, [class*=select__]");
  }
  function comboVal(el) {
    var c = el.closest(".select__control") || el.closest(".select__container") || el.closest("[class*='select']");
    var sv = c && c.querySelector(".select__single-value, [class*='singleValue'], [class*='single-value']");
    return sv ? (sv.textContent || "").replace(/\s+/g, " ").trim() : "";
  }
  function rect(el) { var r = el.getBoundingClientRect(); return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) }; }
  var out = [], i = 0;
  document.querySelectorAll("input, select, textarea").forEach(function (el) {
    if (/hidden|submit|button|password/.test(el.type)) return;
    if (el.type === "radio" || el.type === "checkbox") return;       // handled as groups below
    if (!vis(el)) return;
    var rec = { index: i, type: "text", value: "" };
    if (el.tagName === "SELECT") { rec.type = "select"; rec.options = Array.prototype.map.call(el.options, function (o) { return (o.textContent || "").trim(); }).filter(Boolean).slice(0, 40); rec.value = el.selectedIndex > 0 ? el.value : ""; }
    else if (el.type === "file") { rec.type = "file"; rec.value = (el.files && el.files.length) ? el.files[0].name : ""; }
    else if (isCombo(el)) { rec.type = "combobox"; rec.value = comboVal(el); }
    else { rec.value = String(el.value || ""); }
    rec.label = lbl(el);
    if (!rec.label && !(rec.options && rec.options.length)) return;
    el.setAttribute("data-jmv", i); rec.rect = rect(el); out.push(rec); i++;
  });
  // radio/checkbox QUESTIONS — same fieldset-less grouping as jmDetectFields (group by name/nearest
  // container, anchor on the options' common ancestor, recover the question via ancestor-walk) so the
  // vision fallback also sees Tesla-style questions. Records current checked value + rect.
  (function () {
    function optEl(inp) { return inp.closest("label") || (inp.id && document.querySelector('label[for="' + (window.CSS && CSS.escape ? CSS.escape(inp.id) : inp.id) + '"]')) || inp; }
    function otxt(el) { return ((el && el.getAttribute && el.getAttribute("aria-label")) || (el && el.textContent) || "").replace(/\s+/g, " ").trim(); }
    var groups = {}, gc = 0, cmap = (typeof WeakMap !== "undefined") ? new WeakMap() : null;
    function ckey(c) { if (!c) return "c0"; if (cmap) { if (!cmap.has(c)) cmap.set(c, "c" + (++gc)); return cmap.get(c); } if (!c.__jmv) c.__jmv = "c" + (++gc); return c.__jmv; }
    Array.prototype.forEach.call(document.querySelectorAll("input[type=radio], input[type=checkbox]"), function (inp) {
      var le = optEl(inp); if (!vis(inp) && !vis(le)) return;
      var key = inp.name ? ("n:" + inp.name) : ckey(inp.closest("fieldset, [role=radiogroup], [role=group]") || inp.parentElement);
      var g = groups[key] || (groups[key] = { inputs: [], texts: [], checked: "" });
      g.inputs.push(inp); var tt = otxt(le) || String(inp.value || "").trim(); g.texts.push(tt);
      if (inp.checked) g.checked = tt;
    });
    function lca(els) { var a = els[0]; for (var j = 1; j < els.length && a; j++) { while (a && !a.contains(els[j])) a = a.parentElement; } return a; }
    function question(node, texts) {
      for (var hop = 0; node && hop < 6; hop++, node = node.parentElement) {
        var t = node.textContent || "";
        texts.forEach(function (o) { if (o) t = t.split(o).join(" "); });
        t = t.replace(/\s+/g, " ").trim();
        if (t.length >= 8 && /[a-z]/i.test(t)) return t;
      }
      return "";
    }
    Object.keys(groups).forEach(function (k) {
      var g = groups[k]; if (!g.inputs.length) return;
      var anchor = lca(g.inputs); if (!anchor) return;
      var label = question(anchor, g.texts); if (!label) return;
      anchor.setAttribute("data-jmv", i);
      out.push({ index: i, type: "radio", label: label.slice(0, 180), options: g.texts.filter(Boolean).slice(0, 20), value: g.checked, rect: rect(anchor) }); i++;
    });
  })();
  document.querySelectorAll("button, input[type=submit], [role=button]").forEach(function (b) {
    if (!vis(b)) return;
    var t = (b.textContent || b.value || b.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim(); if (!t) return;
    b.setAttribute("data-jmv", i);
    out.push({ index: i, type: "button", label: t.slice(0, 60), rect: rect(b) }); i++;
  });
  return out.slice(0, 60);
}

// jmApplyVision: apply a vision plan {sets:[{index,value}], submit_index} by data-jmv index, reusing
// the same robust setters. Returns {applied, submitSelector} — the caller decides whether to click
// submit (so the dry-run / required-gate logic stays in the runner). Self-contained, async.
async function jmApplyVision(plan) {
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function setNativeValue(el, value) { var proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype; var s = Object.getOwnPropertyDescriptor(proto, "value"); if (s && s.set) s.set.call(el, value); else el.value = value;["input", "change", "blur"].forEach(function (t) { el.dispatchEvent(new Event(t, { bubbles: true })); }); }
  function setText(el, v) { if (String(el.value || "").trim()) return true; setNativeValue(el, v); if (!String(el.value || "").trim()) { try { el.focus(); if (el.select) el.select(); if (document.execCommand) document.execCommand("insertText", false, v); el.dispatchEvent(new Event("change", { bubbles: true })); el.blur(); } catch (e) {} } return !!String(el.value || "").trim(); }
  function setSelect(sel, v) { var w = String(v).toLowerCase(); for (var i = 0; i < sel.options.length; i++) { var o = sel.options[i]; if ((o.textContent || "").trim().toLowerCase() === w || (o.value || "").toLowerCase() === w) { sel.selectedIndex = i; sel.dispatchEvent(new Event("change", { bubbles: true })); return true; } } for (var j = 0; j < sel.options.length; j++) { var ot = (sel.options[j].textContent || "").trim().toLowerCase(); if (ot && (ot.indexOf(w) >= 0 || w.indexOf(ot) >= 0)) { sel.selectedIndex = j; sel.dispatchEvent(new Event("change", { bubbles: true })); return true; } } return false; }
  function comboSelected(el) { var c = el.closest(".select__control") || el.closest(".select__container") || el.closest("[class*='select']"); if (!c) return ""; var sv = c.querySelector(".select__single-value, [class*='singleValue'], [class*='single-value'], .select__multi-value, [class*='multiValue']"); return sv ? (sv.textContent || "").replace(/\s+/g, " ").trim().toLowerCase() : ""; }
  function comboType(node, text) { var setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value"); if (setter && setter.set) setter.set.call(node, text); else node.value = text; node.dispatchEvent(new Event("input", { bubbles: true })); }
  async function fillCombo(el, v) {
    var want = String(v).toLowerCase().trim(); if (!want) return false;
    var cur = comboSelected(el); if (cur && (cur === want || cur.indexOf(want) >= 0 || want.indexOf(cur) >= 0)) return true;
    var control = el.closest(".select__control") || el.closest(".select__container") || el.closest("[class*='select']") || el.parentElement || el;
    try { el.focus(); } catch (e) {}
    control.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, button: 0 }));
    control.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, button: 0 }));
    await sleep(100); try { comboType(el, v); } catch (e) {}
    var pick = null, saw = false;
    for (var t = 0; t < 9 && !pick; t++) {
      await sleep(160);
      var opts = document.querySelectorAll('.select__option, [class*="__option"], [id*="-option-"], [role="option"]');
      if (opts.length) saw = true;
      var ex = null, st = null, pa = null;
      for (var k = 0; k < opts.length; k++) { var ot = (opts[k].textContent || "").toLowerCase().trim(); if (!ot) continue; if (ot === want) { ex = opts[k]; break; } if (!st && ot.indexOf(want) === 0) st = opts[k]; if (!pa && (ot.indexOf(want) >= 0 || want.indexOf(ot) >= 0)) pa = opts[k]; }
      pick = ex || st || pa; if (!pick && !saw && t >= 2) break;
    }
    if (pick) { try { pick.scrollIntoView({ block: "nearest" }); } catch (e) {} pick.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, button: 0 })); pick.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, button: 0 })); pick.click(); await sleep(150); }
    var got = comboSelected(el); return !!(got && (got === want || got.indexOf(want) >= 0 || want.indexOf(got) >= 0));
  }
  function clickRadio(g, v) { var w = String(v).toLowerCase(), radios = g.querySelectorAll("input[type=radio], input[type=checkbox]"); for (var i = 0; i < radios.length; i++) { var rl = radios[i].closest("label") || (radios[i].id && document.querySelector('label[for="' + radios[i].id + '"]')); var t = ((rl ? rl.textContent : radios[i].value) || "").toLowerCase(); if (t.indexOf(w) >= 0 || (w === "yes" && /\byes\b/.test(t)) || (w === "no" && /\bno\b/.test(t))) { radios[i].click(); return true; } } return false; }
  var applied = 0, sets = (plan && plan.sets) || [];
  for (var i = 0; i < sets.length; i++) {
    var s = sets[i]; if (s.value == null || String(s.value).trim() === "") continue;
    var el = document.querySelector('[data-jmv="' + s.index + '"]'); if (!el) continue;
    var ok = false;
    if (el.tagName === "SELECT") ok = setSelect(el, s.value);
    else if (el.getAttribute("role") === "combobox" || el.getAttribute("aria-autocomplete") === "list" || (el.closest && el.closest(".select__container, [class*=select__]"))) ok = await fillCombo(el, s.value);
    else if (el.matches("fieldset, [role=radiogroup]") || (el.querySelector && el.querySelector("input[type=radio], input[type=checkbox]"))) ok = clickRadio(el, s.value);
    else if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") ok = setText(el, s.value);
    if (ok) applied++;
  }
  var submitSelector = (plan && plan.submit_index != null) ? ('[data-jmv="' + plan.submit_index + '"]') : null;
  return { applied: applied, submitSelector: submitSelector };
}

// ===================== TRAINING CAPTURE: read how the user filled this form =====================
// jmCaptureFilled: return [{label,type,value,options?}] for every FILLED field on the page, so the
// backend can learn how this user answers each question. Sensitive fields are skipped. Self-contained.
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
    if (!value) return;                                  // only learn from FILLED fields
    if (seen[label]) return; seen[label] = 1;
    out.push({ label: label, type: type, value: value, options: options });
  });
  // Workday / Oracle popup dropdowns (a <button aria-haspopup=listbox> or <oj-select-single>, never an
  // <input>) — without this the answer bank could never learn the fields that make up most of a
  // Workday application, so every new Workday form would start from scratch.
  document.querySelectorAll('button[aria-haspopup="listbox"], [role=combobox][aria-haspopup="listbox"], oj-select-single, oj-combobox-one')
    .forEach(function (el) {
      if (!vis(el)) return;
      var oj = /^OJ-/.test(el.tagName);
      var c = el.querySelector(oj ? ".oj-select-chosen, .oj-combobox-chosen" : '[data-automation-id="selectedItem"]');
      var value = ((c ? c.textContent : (oj ? "" : el.textContent)) || "").replace(/\s+/g, " ").trim();
      if (!value || /^(select one|select a value|select\.\.\.|select|search|choose one|choose)$/i.test(value)) return;
      var label = lbl(el);
      var aid = el.getAttribute("data-automation-id");
      if (!label && aid) label = aid.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/[-_]+/g, " ");
      if (!label || SENSITIVE.test(label) || seen[label]) return;
      seen[label] = 1;
      out.push({ label: label.slice(0, 200), type: oj ? "ojselect" : "listbox", value: value, options: null });
    });
  // radio / checkbox / ARIA-choice QUESTIONS. Robust to forms with NO <fieldset>/<legend> (e.g.
  // Tesla renders each question as a plain <div>: a question line followed by styled Yes/No radios).
  // Group options by the radio `name` (or nearest group container / ARIA radiogroup), then recover
  // the QUESTION text as the nearest ancestor of the options whose text — minus the option words —
  // reads as a real sentence. Also tolerates custom-styled radios whose real <input> is size-0 but
  // whose label is visible. Without this, only <fieldset>/<legend> forms were ever learned.
  (function () {
    var groups = {}, gid = 0, cmap = (typeof WeakMap !== "undefined") ? new WeakMap() : null;
    function ckey(c) { if (!c) return "c0"; if (cmap) { if (!cmap.has(c)) cmap.set(c, "c" + (++gid)); return cmap.get(c); } if (!c.__jmg) c.__jmg = "c" + (++gid); return c.__jmg; }
    function optEl(inp) { return inp.closest("label") || (inp.id && document.querySelector('label[for="' + (window.CSS && CSS.escape ? CSS.escape(inp.id) : inp.id) + '"]')) || inp; }
    function txt(el) { return ((el && el.getAttribute && el.getAttribute("aria-label")) || (el && el.textContent) || "").replace(/\s+/g, " ").trim(); }
    function add(key, el, t, on) { var g = groups[key] || (groups[key] = { els: [], texts: [], on: [] }); if (el) g.els.push(el); if (t) g.texts.push(t); if (on && t) g.on.push(t); }
    Array.prototype.forEach.call(document.querySelectorAll("input[type=radio], input[type=checkbox]"), function (inp) {
      var le = optEl(inp); if (!vis(inp) && !vis(le)) return;           // accept hidden input if its label shows
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
  // custom TOGGLE / segmented-button answers (button/role toggles with no <input> — Ashby/Vanta etc.):
  // learn the option the user chose, marked via aria-checked/pressed/selected or a selected-ish class.
  (function () {
    if (typeof Map === "undefined") return;
    var NAV = /\b(submit|continue|next|back|previous|prev|apply|save|cancel|add|remove|delete|upload|browse|edit|search|close|menu|skip|sign in|log in|login)\b/;
    var cand = [];
    document.querySelectorAll('button, [role=radio], [role=button], [role=tab], [role=option], [role=switch]').forEach(function (el) {
      if (el.getAttribute("aria-haspopup") || el.hasAttribute("data-jmk") ||
              (el.closest && el.closest("oj-select-single, oj-combobox-one"))) return;   // a popup DROPDOWN, not a toggle option
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
      if (rec.texts.length < 2 || rec.texts.length > 5 || !rec.sel) return;       // need a chosen value
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
