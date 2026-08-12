// test_ats_adapters.js — network-free checks for the extension's ATS form-filler.
//
//   npm install jsdom          (dev-only; NOT a repo dependency, and CI does not run this)
//   node scripts/test_ats_adapters.js
//
// To keep jsdom out of the repo entirely, install it anywhere and point NODE_PATH at it:
//   NODE_PATH=/some/dir/node_modules node scripts/test_ats_adapters.js
//
// Why this exists: the filler has to work on Workday (35% of the corpus), Oracle Cloud (11.5%) and
// iCIMS (4.5%), and there is no way to eyeball those without an account and a live posting. Each
// fixture below reproduces the platform's real markup CONTRACT — the attributes and widget shapes
// that are identical across every tenant — so the adapters can be exercised offline. It caught six
// real bugs on the first run, including two that left Workday's required dropdowns silently empty.
//
// The fixtures encode these hard-won facts:
//   * Workday names every field with a tenant-independent data-automation-id, and renders each
//     dropdown as a <button aria-haspopup=listbox> whose options exist only while open, in a PORTAL
//     at body level. Its "countryRegion" field is the STATE, not the country.
//   * A menu left open from the previous field is indistinguishable from this field's menu, so option
//     lookup must be scoped by specificity and menus must be verified closed.
//   * Oracle wraps real inputs in Oracle JET custom elements (oj-input-text, oj-select-single).
//   * iCIMS ships two generations side by side (lowercase classic ids, camelCase Talent Cloud).
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { JSDOM } = require("jsdom");

const FILLER = fs.readFileSync(path.join(__dirname, "..", "extension", "filler.js"), "utf8");

let pass = 0, fail = 0;
function ok(cond, name, extra) {
  if (cond) { pass++; console.log("  ok    " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "  -> " + extra : "")); }
}
function eq(got, want, name) {
  ok(got === want, name, "got " + JSON.stringify(got) + " want " + JSON.stringify(want));
}

// A jsdom page with filler.js loaded as page globals. jsdom has no layout engine, so every
// getBoundingClientRect() is 0x0 and the filler's vis() would reject the entire form — stub a
// real-looking box for anything not explicitly hidden. That is the only fiction in this harness.
function makePage(url, html) {
  const dom = new JSDOM("<!doctype html><html><body>" + html + "</body></html>",
    { url, pretendToBeVisual: true, runScripts: "outside-only" });
  const w = dom.window;
  w.Element.prototype.getBoundingClientRect = function () {
    const s = w.getComputedStyle(this);
    if (s.display === "none" || s.visibility === "hidden") {
      return { width: 0, height: 0, top: 0, left: 0, bottom: 0, right: 0 };
    }
    return { width: 180, height: 30, top: 10, left: 10, bottom: 40, right: 190 };
  };
  w.Element.prototype.scrollIntoView = function () {};
  vm.runInContext(FILLER, dom.getInternalVMContext());
  return w;
}

// Workday dropdown behaviour. `nested` switches between the two shapes seen in the wild:
//   false — role=option and data-automation-id=promptOption on the SAME node (what Workday ships)
//   true  — an <li role=option> WRAPPING the clickable promptOption. The click handler is on the
//           inner node, so an adapter that clicks the outer wrapper does nothing at all. This is the
//           shape that broke the first implementation, which used one combined selector and therefore
//           got document order (wrapper first) instead of specificity order.
function wireListbox(w, btn, options, nested) {
  btn.addEventListener("click", function () {
    if (w.document.querySelector("ul[role=listbox]")) {          // second click toggles it shut
      w.document.querySelectorAll("ul[role=listbox]").forEach((u) => u.remove());
      return;
    }
    const ul = w.document.createElement("ul");
    ul.setAttribute("role", "listbox");
    options.forEach((label) => {
      const li = w.document.createElement("li");
      li.setAttribute("role", "option");
      let clickable = li;
      if (nested) {
        const d = w.document.createElement("div");
        d.setAttribute("data-automation-id", "promptOption");
        d.setAttribute("data-automation-label", label);
        d.textContent = label;
        li.appendChild(d);
        clickable = d;
      } else {
        li.setAttribute("data-automation-id", "promptOption");
        li.setAttribute("data-automation-label", label);
        li.textContent = label;
      }
      clickable.addEventListener("click", function () {
        btn.innerHTML = '<span data-automation-id="selectedItem">' + label + "</span>";
        w.document.querySelectorAll("ul[role=listbox]").forEach((u) => u.remove());
      });
      ul.appendChild(li);
    });
    w.document.body.appendChild(ul);
  });
}

const PROFILE = {
  fields: {
    first_name: "Kunal", last_name: "Hada", full_name: "Kunal Hada",
    email: "hada.k@northeastern.edu", phone: "+1 617 555 0134",
    address: {
      line1: "12 Fenway", city: "Boston", state: "Massachusetts",
      postal: "02115", country: "United States of America"
    },
    work_auth: { authorized: true, requires_sponsorship: true },
    eeo: { gender: "Male", veteran: "I am not a veteran", disability: "No" },
    links: { linkedin: "https://linkedin.com/in/kunal" },
    comp: { desired_salary: "120000" },
    how_did_you_hear: "LinkedIn"
  },
  defaults: {}
};

async function main() {
  // ------------------------------------------------- 1. fingerprinting (this is what covers vanity hosts)
  console.log("\njmDetectAts — identify the platform from the PAGE, not the hostname");
  const cases = [
    ["workday", "https://ngc.wd1.myworkdayjobs.com/en-US/N/job/x", '<div data-automation-id="jobPostingHeader">PM</div>'],
    ["workday", "https://careers.vanity.com/job/1",
      '<div data-automation-id="a"></div><div data-automation-id="b"></div>' +
      '<div data-automation-id="c"></div><div data-automation-id="d"></div>'],
    ["oracle", "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/2", "<div>x</div>"],
    ["oracle", "https://careers.americanexpress.com/en/sites/CX_1/jobs",
      "<oj-input-text><input class='oj-inputtext-input'></oj-input-text>"],
    ["icims", "https://apply2-republicfinance.icims.com/jobs/123/login", "<div>x</div>"],
    ["icims", "https://jobs.vanity.com/careers", '<div class="iCIMS_MainWrapper"></div>'],
    ["icims", "https://careers.vanity.com/x",
      '<iframe id="icims_content_iframe" src="https://careers-x.icims.com/jobs/1"></iframe>'],
    ["greenhouse", "https://careers.airbnb.com/positions/123", '<div id="grnhse_app"></div>'],
    ["successfactors", "https://jobs.sap.com/job/123", '<script src="https://rmkcdn.successfactors.com/x.js"></script>'],
    ["phenom", "https://jobs.ebayinc.com/us/en/job/1", '<div data-ph-at-id="job-title"></div>'],
    ["taleo", "https://x.taleo.net/careersection/1/jobdetail.ftl", "<div>x</div>"],
    ["peoplesoft", "https://jobs.omni.fsu.edu/psc/x/EMPLOYEE/HRMS/c/HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL", "<div>x</div>"],
    // negatives: an ordinary page must NOT be claimed by any adapter
    ["", "https://www.example.com/about", "<h1>About us</h1><p>We are a company.</p>"],
    ["", "https://news.ycombinator.com/", "<table><tr><td>story</td></tr></table>"]
  ];
  for (const [want, url, html] of cases) {
    eq(makePage(url, html).jmDetectAts(), want, "detect " + (want || "(nothing)") + " @ " + url.slice(0, 46));
  }

  // ------------------------------------------------- 2. Workday, "My Information" step
  console.log("\nWorkday adapter — My Information step");
  const w = makePage("https://ngc.wd1.myworkdayjobs.com/en-US/N/job/PM/apply", `
    <div data-automation-id="progressBar">
      <li data-automation-id="progressBarStep" aria-current="step">My Information</li>
      <li data-automation-id="progressBarStep">My Experience</li>
      <li data-automation-id="progressBarStep">Application Questions</li>
      <li data-automation-id="progressBarStep">Voluntary Disclosures</li>
    </div>
    <label for="f">First Name</label><input id="f" data-automation-id="legalNameSection_firstName" required>
    <label for="l">Last Name</label><input id="l" data-automation-id="legalNameSection_lastName" required>
    <label for="e">Email Address</label><input id="e" data-automation-id="email" required>
    <label for="p">Phone Number</label><input id="p" data-automation-id="phone-number" required>
    <label for="c">City</label><input id="c" data-automation-id="addressSection_city">
    <label for="z">Postal Code</label><input id="z" data-automation-id="addressSection_postalCode">
    <label for="dev">Phone Device Type</label>
    <button id="dev" data-automation-id="phone-device-type" aria-haspopup="listbox" aria-required="true">Select One</button>
    <label for="ctry">Country</label>
    <button id="ctry" data-automation-id="countryDropdown" aria-haspopup="listbox" aria-required="true">Select One</button>
    <label for="reg">State</label>
    <button id="reg" data-automation-id="addressSection_countryRegion" aria-haspopup="listbox">Select One</button>
    <label for="auth">Are you legally authorized to work in this country?</label>
    <button id="auth" data-automation-id="workAuthQuestion" aria-haspopup="listbox">Select One</button>
    <button data-automation-id="bottom-navigation-next-button">Save and Continue</button>
  `);
  const D = w.document;
  wireListbox(w, D.getElementById("dev"), ["Home", "Mobile", "Work", "Fax"]);
  wireListbox(w, D.getElementById("ctry"), ["Canada", "Mexico", "United States of America"]);
  wireListbox(w, D.getElementById("reg"), ["Maine", "Maryland", "Massachusetts", "Michigan"]);
  wireListbox(w, D.getElementById("auth"), ["Yes", "No"]);

  const res = await w.jmFillApplication(PROFILE);
  eq(res.ats, "workday", "workday adapter selected");
  eq(res.found, true, "form found");
  eq(D.getElementById("f").value, "Kunal", "first name");
  eq(D.getElementById("l").value, "Hada", "last name");
  eq(D.getElementById("e").value, "hada.k@northeastern.edu", "email");
  eq(D.getElementById("p").value, "+1 617 555 0134", "phone");
  eq(D.getElementById("c").value, "Boston", "city (label recovered from data-automation-id)");
  eq(D.getElementById("z").value, "02115", "postal code");
  // The point of the widget sweep: these are <button>s, invisible to an input/select/textarea scan,
  // and on Workday they are the REQUIRED fields.
  eq(D.getElementById("dev").textContent.trim(), "Mobile", "phone device type dropdown");
  eq(D.getElementById("ctry").textContent.trim(), "United States of America", "country dropdown");
  eq(D.getElementById("reg").textContent.trim(), "Massachusetts", "countryRegion is the STATE, not the country");
  eq(D.getElementById("auth").textContent.trim(), "Yes",
     '"authorized to work in this country" answered Yes, not with a country name');
  ok(res.wizard && res.wizard.total === 4, "wizard step count", JSON.stringify(res.wizard));
  ok(res.wizard && res.wizard.index === 1, "wizard current step", JSON.stringify(res.wizard));
  eq(res.wizard && res.wizard.step, "My Information", "wizard step name");
  eq(res.wizard && res.wizard.isLast, false, "not the last step, so the overlay must not offer Submit");
  eq(res.submitSelector, '[data-automation-id="bottom-navigation-submit-button"]',
     "submit selector is Submit — never the Next button");

  // ------------------------------------------------- 3. the wrapped-option shape + menu hygiene
  console.log("\nWorkday — hostile option shape and stale-menu hygiene");
  const nst = makePage("https://y.wd1.myworkdayjobs.com/apply", `
    <label for="n1">Phone Device Type</label>
    <button id="n1" data-automation-id="phone-device-type" aria-haspopup="listbox" aria-required="true">Select One</button>
    <label for="n2">Country</label>
    <button id="n2" data-automation-id="countryDropdown" aria-haspopup="listbox">Select One</button>
    <input data-automation-id="email">
  `);
  wireListbox(nst, nst.document.getElementById("n1"), ["Home", "Mobile", "Work"], true);
  wireListbox(nst, nst.document.getElementById("n2"), ["Canada", "United States of America"], true);
  await nst.jmFillApplication(PROFILE);
  eq(nst.document.getElementById("n1").textContent.trim(), "Mobile", "click reaches the INNER promptOption");
  eq(nst.document.getElementById("n2").textContent.trim(), "United States of America",
     "a menu left open by the previous field does not poison this one");

  // ------------------------------------------------- 4. Oracle Cloud Recruiting
  console.log("\nOracle Cloud (ORC) adapter");
  const o = makePage("https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1/apply", `
    <label for="ofn">First Name</label><oj-input-text><input id="ofn" class="oj-inputtext-input" name="firstName" required></oj-input-text>
    <label for="oln">Last Name</label><oj-input-text><input id="oln" class="oj-inputtext-input" name="lastName" required></oj-input-text>
    <label for="oem">Email</label><oj-input-text><input id="oem" class="oj-inputtext-input" name="email" type="email" required></oj-input-text>
    <label for="oph">Phone Number</label><oj-input-text><input id="oph" class="oj-inputtext-input" name="phoneNumber"></oj-input-text>
    <label for="ocity">City</label><oj-input-text><input id="ocity" class="oj-inputtext-input" name="city"></oj-input-text>
    <input type="file" name="resume">
    <button data-affordance="submit">Submit</button>
  `);
  const ores = await o.jmFillApplication(PROFILE);
  eq(ores.ats, "oracle", "oracle adapter selected");
  eq(o.document.getElementById("ofn").value, "Kunal", "first name inside oj-input-text");
  eq(o.document.getElementById("oln").value, "Hada", "last name");
  eq(o.document.getElementById("oem").value, "hada.k@northeastern.edu", "email");
  eq(o.document.getElementById("oph").value, "+1 617 555 0134", "phone");
  eq(o.document.getElementById("ocity").value, "Boston", "city");

  // ------------------------------------------------- 5. iCIMS
  console.log("\niCIMS adapter — classic portal ids");
  const c = makePage("https://apply2-republicfinance.icims.com/jobs/9/candidate", `
    <div class="iCIMS_MainWrapper">
      <label for="firstname">First Name</label><input id="firstname" name="firstname" required>
      <label for="lastname">Last Name</label><input id="lastname" name="lastname" required>
      <label for="email">Email Address</label><input id="email" name="email" type="email" required>
      <label for="phone">Phone</label><input id="phone" name="phone">
      <div id="icims_addResumeSection"><input type="file" name="resume"></div>
      <label for="ad">Address</label><input id="ad" name="addr1">
      <label for="ct">City</label><input id="ct" name="city">
      <input type="submit" name="submit" value="Submit">
    </div>
  `);
  const cres = await c.jmFillApplication(PROFILE);
  eq(cres.ats, "icims", "icims adapter selected");
  eq(c.document.getElementById("firstname").value, "Kunal", "first name");
  eq(c.document.getElementById("lastname").value, "Hada", "last name");
  eq(c.document.getElementById("email").value, "hada.k@northeastern.edu", "email");
  eq(c.document.getElementById("phone").value, "+1 617 555 0134", "phone");
  eq(c.document.getElementById("ct").value, "Boston", "city");
  eq(c.document.getElementById("ad").value, "12 Fenway", "address line 1");

  // ------------------------------------------------- 6. the older adapters must be unaffected
  console.log("\nregression — existing adapters still win their own pages");
  const g = makePage("https://job-boards.greenhouse.io/acme/jobs/1", `
    <form id="application_form">
      <label for="first_name">First Name</label><input id="first_name" required>
      <label for="last_name">Last Name</label><input id="last_name" required>
      <label for="email">Email</label><input id="email" type="email" required>
      <label for="phone">Phone</label><input id="phone" type="tel">
      <input type="file" id="resume">
      <button id="submit_app" type="submit">Submit application</button>
    </form>
  `);
  const gres = await g.jmFillApplication(PROFILE);
  eq(gres.ats, "greenhouse", "greenhouse not shadowed by the new adapters");
  eq(g.document.getElementById("first_name").value, "Kunal", "greenhouse first name");
  eq(gres.wizard, null, "a single-page form reports no wizard, so the overlay watcher stays disarmed");

  // ------------------------------------------------- 7. answer bank round-trip on Workday dropdowns
  console.log("\nlearned-answer bank — capture, snapshot and replay a Workday dropdown");
  const s = makePage("https://x.wd1.myworkdayjobs.com/job/apply", `
    <label for="q1">Have you previously worked for Northrop Grumman?</label>
    <button id="q1" data-automation-id="previousWorker" aria-haspopup="listbox">Select One</button>
    <label for="q2">How did you hear about us?</label>
    <button id="q2" data-automation-id="source" aria-haspopup="listbox"><span data-automation-id="selectedItem">LinkedIn</span></button>
    <input data-automation-id="email" value="x@y.com">
  `);
  const snap = s.jmSnapshotForm();
  const q1 = snap.filter((f) => /previously worked/i.test(f.label))[0];
  ok(!!q1, "an unanswered dropdown appears in the snapshot", JSON.stringify(snap));
  eq(q1 && q1.type, "listbox", "reported as a listbox");
  ok(!snap.some((f) => /how did you hear/i.test(f.label)),
     "an already-answered dropdown is not re-asked (and is not mistaken for a toggle group)");
  const capt = s.jmCaptureFilled();
  const learned = capt.filter((f) => /how did you hear/i.test(f.label))[0];
  ok(!!learned, "an answered dropdown is captured for the bank", JSON.stringify(capt));
  eq(learned && learned.value, "LinkedIn", "captured value");

  wireListbox(s, s.document.getElementById("q1"), ["Yes", "No"]);
  const answers = s.jmMatchLearned(snap, { "have you previously worked for northrop grumman": { value: "No" } });
  ok(Object.keys(answers).length === 1, "matched by normalised label", JSON.stringify(answers));
  await s.jmApplyAnswers(answers);
  eq(s.document.getElementById("q1").textContent.trim(), "No", "learned answer replayed onto the dropdown");

  // A saved answer the dropdown no longer offers must fail VISIBLY, never land on a near-miss option.
  const s2 = makePage("https://x.wd1.myworkdayjobs.com/job/apply", `
    <label for="q3">Preferred pronouns</label>
    <button id="q3" data-automation-id="pronouns" aria-haspopup="listbox">Select One</button>
    <input data-automation-id="email" value="x@y.com">
  `);
  const snap2 = s2.jmSnapshotForm();
  wireListbox(s2, s2.document.getElementById("q3"), ["He/Him", "She/Her", "They/Them"]);
  const r2 = await s2.jmApplyAnswers(s2.jmMatchLearned(snap2, { "preferred pronouns": { value: "Ze/Zir" } }));
  eq(r2.applied, 0, "a stale saved option is reported as NOT applied");
  eq(s2.document.getElementById("q3").textContent.trim(), "Select One", "and the dropdown is left untouched");

  console.log("\n" + (fail ? "FAILED " + fail + " of " + (pass + fail) : "All " + pass + " ATS-adapter checks passed"));
  process.exit(fail ? 1 : 0);
}

main().catch((e) => { console.error("HARNESS ERROR", e); process.exit(2); });
