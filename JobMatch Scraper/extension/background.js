"use strict";
// Background service worker: runs the Tesla auto-import on a DAILY alarm, so the feed
// gets Tesla jobs without anyone clicking anything. The fetch rides the browser's own
// tesla.com cookies; if Akamai still challenges a cold session, the run records a note
// and the tesla_auto.js content script covers it the next time a Tesla page is open.
importScripts("tesla_shared.js", "filler.js", "claude.js", "cdp.js");  // filler.js: jmFillApplication etc; claude.js: jmClaude / jmParseJson / jmClaudeTools; cdp.js: computer-use input

function schedule() {
  chrome.alarms.create("tesla-auto", { delayInMinutes: 3, periodInMinutes: 24 * 60 });
}
chrome.runtime.onInstalled.addListener(schedule);
chrome.runtime.onStartup.addListener(schedule);

chrome.alarms.onAlarm.addListener((a) => {
  // canUseTabs: when Akamai 403s the worker's own fetch, the run transparently
  // retries through a real tesla.com tab (existing or throwaway-inactive).
  if (a.name === "tesla-auto") jmRunTeslaImport({ trigger: "alarm", canUseTabs: true });
  // Keepalive: a no-op storage touch resets the MV3 service-worker idle timer so a long computer-use
  // run isn't evicted between steps. Created while a queue is running, cleared when it ends.
  else if (a.name === "jm-keepalive") { try { chrome.storage.local.get("jm_queue", () => { void chrome.runtime.lastError; }); } catch (e) {} }
});
function jmKeepAlive(on) {
  try { if (on) chrome.alarms.create("jm-keepalive", { periodInMinutes: 0.4 }); else chrome.alarms.clear("jm-keepalive"); } catch (e) {}
}

// The popup's "Run Tesla import now" button (works from any page).
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "run-tesla-now") {
    jmRunTeslaImport({ trigger: "manual", canUseTabs: true }).then(sendResponse);
    return true;                                   // keep the channel open for the async reply
  }
  // The review overlay (in the page, isolated world) asks us to log a submitted application.
  // We do it here because the background holds the backend host permission, so the POST isn't
  // subject to the apply page's CSP.
  if (msg && msg.type === "jm_log_application") {
    fetch(msg.apibase + "/api/ext/save", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token: msg.token, title: msg.title, company: msg.company,
        url: msg.url, resume_name: msg.resume_name || ""
      })
    }).then((r) => r.json()).then(sendResponse).catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }
  // Passive "training" capture (autocapture.js): a submitted/advanced application form's answers.
  // We POST from here so the request isn't subject to the apply page's CSP, and we read the token
  // from storage (the content script never sees it). Dropped silently when not signed in.
  if (msg && msg.type === "jm_autocapture") {
    const fields = msg.fields || [];
    const sig = fields.length + "|" + fields.map((f) => f.label + "=" + f.value).join("|");
    const now = Date.now();
    if (sig === JM_AC.sig && now - JM_AC.at < 20000) return false;   // de-dupe frames / re-clicks
    JM_AC.sig = sig; JM_AC.at = now;
    chrome.storage.local.get(["token", "apibase"], (st) => {
      const token = st && st.token; if (!token || !fields.length) return;
      const apibase = (st && st.apibase) || "https://stemjobs.astrochakra.co";
      fetch(apibase + "/api/ext/learn", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, company: msg.company || "", fields })
      }).catch(() => {});                                            // fire-and-forget
    });
    return false;
  }
});
// Last passive-capture signature/time, so the same answers from multiple frames or a double-click
// aren't saved repeatedly.
const JM_AC = { sig: "", at: 0 };


// ===================== Batch auto-apply runner =====================
// Sequentially opens each queued job in a BACKGROUND tab, tailors + fills via /api/ext/tailor +
// jmFillApplication, then auto-submits ONLY when the form is cleanly filled (résumé attached, no
// required gaps) and there's no CAPTCHA/login wall. dryRun skips the real submit. Live progress is
// written to chrome.storage.local("jm_queue") so the popup can render it. Keep the inter-job delay
// under ~25s so the MV3 service worker isn't evicted between jobs (activity keeps it alive).
const JM_Q = { stop: false, running: false, currentTabId: null, lastFields: [] };
// jmSleep is already defined in tesla_shared.js (imported above) — reuse it (don't redeclare,
// or the shared worker scope throws "jmSleep already declared" → SW registration fails, code 15).
const jmSaveQueue = (state) => new Promise((r) => chrome.storage.local.set({ jm_queue: state }, r));
// Interruptible sleep — bails the instant a hard-stop is requested.
async function jmStoppableSleep(ms) {
  const step = 250;
  for (let t = 0; t < ms && !JM_Q.stop; t += step) await jmSleep(Math.min(step, ms - t));
}

function jmWaitForLoad(tabId, timeout) {
  return new Promise((resolve) => {
    let done = false;
    const finish = () => { if (done) return; done = true; clearTimeout(to); chrome.tabs.onUpdated.removeListener(onUpd); resolve(); };
    const onUpd = (id, info) => { if (id === tabId && info.status === "complete") finish(); };
    const to = setTimeout(finish, timeout || 25000);
    chrome.tabs.onUpdated.addListener(onUpd);
  });
}

// ===================== AI brain: Claude in-extension (hybrid) + backend fallback =====================
// When a Claude key is configured (Settings), the extension calls Claude DIRECTLY (claude.js) for the
// answer + vision steps, building the prompts here from the profile/résumé/learned context fetched once
// per run. With no key it falls back to the backend's /api/ext/answer|vision (Gemini). Tailoring (PDF)
// always stays on the backend.

// mirror db.normalize_label — stable key for matching the same question across forms.
function jmNormLabel(s) {
  s = String(s || "").toLowerCase();
  s = s.replace(/\([^()]*\b(?:required|optional)\b[^()]*\)/g, " ");
  s = s.replace(/[^a-z0-9 ]+/g, " ").replace(/\s+/g, " ").trim();
  return s.slice(0, 200);
}
// flatten the normalized profile field map into "key: value" lines for the prompt
function jmProfileSummary(ctx) {
  const f = (ctx && ctx.fields) || {};
  const a = f.address || {}, l = f.links || {}, w = f.work_auth || {}, e = f.eeo || {}, c = f.comp || {};
  const rows = [
    ["full name", f.full_name], ["first name", f.first_name], ["last name", f.last_name],
    ["email", f.email], ["phone", f.phone], ["pronouns", f.pronouns],
    ["city", a.city], ["state", a.state], ["postal code", a.postal], ["country", a.country], ["location", a.location],
    ["linkedin", l.linkedin], ["github", l.github], ["portfolio/website", l.portfolio || l.website],
    ["authorized to work", w.authorized ? "Yes" : "No"],
    ["requires visa sponsorship (now or in the future)", w.requires_sponsorship ? "Yes" : "No"],
    ["work authorization status", w.status_label],
    ["gender", e.gender], ["race/ethnicity", e.race], ["hispanic or latino", e.hispanic_latino],
    ["veteran status", e.veteran], ["disability status", e.disability],
    ["desired salary", c.desired_salary], ["salary currency", c.currency],
    ["available start date", f.start_date], ["willing to relocate", f.relocate ? "Yes" : "No"],
    ["how did you hear", f.how_did_you_hear]
  ];
  return rows.filter((x) => x[1] !== "" && x[1] != null).map((x) => x[0] + ": " + x[1]).join("\n");
}
function jmAnswerPrompt(profileSummary, resume, company, fields) {
  const lines = fields.slice(0, 35).map((f) => {
    const opts = (f.options || []).slice(0, 40);
    const o = opts.length ? "\n    OPTIONS: " + opts.join(" | ") : "";
    return "- key=" + f.key + " | type=" + (f.type || "text") + " | label=" + String(f.label || "").slice(0, 200) + o;
  }).join("\n");
  const system = "You fill out job application form fields for a candidate. Use ONLY the candidate's real " +
    "data below. If a field lists OPTIONS, answer with EXACTLY one of those option strings (verbatim), or \"\" " +
    "if none truly fit. For free-text fields, give the value, or \"\" if the data doesn't contain it. NEVER " +
    "invent employers, titles, dates, degrees, numbers, salaries, clearances, or any fact not present in the " +
    "data. Do NOT guess protected demographics unless the profile provides them. Return ONLY a JSON object " +
    "mapping each field key to its answer string. No prose.";
  const prompt = "=== CANDIDATE PROFILE ===\n" + (profileSummary || "(none)") +
    "\n\n=== RÉSUMÉ ===\n" + String(resume || "").slice(0, 4000) +
    "\n\n=== TARGET COMPANY ===\n" + (company || "(unknown)") +
    "\n\n=== FIELDS TO ANSWER ===\n" + lines + "\n\n=== JSON ===";
  return { system, prompt };
}
function jmVisionPrompt(profileSummary, resume, company, elements) {
  const lines = elements.slice(0, 60).map((e) => {
    const opts = (e.options || []).slice(0, 40);
    const o = opts.length ? "\n    OPTIONS: " + opts.map((x) => String(x).slice(0, 60)).join(" | ") : "";
    const cur = e.value ? " | current=" + JSON.stringify(e.value) : "";
    return "- index=" + e.index + " | type=" + (e.type || "text") + " | label=" + String(e.label || "").slice(0, 160) + cur + o;
  }).join("\n");
  const system = "You are completing a job application form. You see a SCREENSHOT of the page and an indexed " +
    "list of its interactive elements. Use ONLY the candidate's real data. Decide a value for each element that " +
    "still needs one to submit; skip elements already correctly filled (current shown). For OPTION elements the " +
    "value MUST be exactly one option string (verbatim) or omit it. NEVER invent facts. Do NOT fill voluntary " +
    "demographic fields unless the profile gives them. Identify submit_index = the index of the button that " +
    "ADVANCES/SUBMITS the form (Submit/Continue/Next/Review), NOT Back/Cancel/Save-draft (null if none). Return " +
    "ONLY JSON: {\"sets\":[{\"index\":<int>,\"value\":\"<string>\"}],\"submit_index\":<int or null>}.";
  const prompt = "=== CANDIDATE PROFILE ===\n" + (profileSummary || "(none)") +
    "\n\n=== RÉSUMÉ ===\n" + String(resume || "").slice(0, 3000) +
    "\n\n=== TARGET COMPANY ===\n" + (company || "(unknown)") +
    "\n\n=== ELEMENTS ===\n" + lines + "\n\n=== JSON ===";
  return { system, prompt };
}
function jmAddCost(usage) {
  if (!usage || !JM_Q.cost) return;
  JM_Q.cost.in += usage.input_tokens || 0;
  JM_Q.cost.out += usage.output_tokens || 0;
}
// Answer the snapshot's empty fields. Claude path applies the user's learned answers FIRST (client-side),
// then asks Claude only for the unknowns. Returns { answers:{key:value}, noKey }.
async function jmAiAnswer(cfg, item, fields) {
  if (cfg.useClaude) {
    const ctx = cfg.ctx || {}, learned = ctx.learned || {};
    const answers = {}, unknown = [];
    fields.forEach((f) => {
      const rec = learned[jmNormLabel(f.label)];
      const val = rec && rec.value;
      if (val) {
        const opts = f.options || [];
        if (!opts.length) { answers[f.key] = val; return; }
        const vl = String(val).toLowerCase().trim();
        if (opts.some((o) => { const ol = String(o).toLowerCase().trim(); return vl === ol || vl.indexOf(ol) >= 0 || ol.indexOf(vl) >= 0; })) { answers[f.key] = val; return; }
      }
      unknown.push(f);
    });
    if (unknown.length) {
      const p = jmAnswerPrompt(jmProfileSummary(ctx), ctx.resume, item.company, unknown);
      const res = await jmClaude(cfg, { system: p.system, prompt: p.prompt, max_tokens: 2048 });
      jmAddCost(res.usage);
      if (res.ok) { const obj = jmParseJson(res.text) || {}; Object.keys(obj).forEach((k) => { if (obj[k] != null && String(obj[k]).trim() !== "") answers[k] = String(obj[k]); }); }
    }
    return { answers, noKey: false };
  }
  const a = await fetch(cfg.apibase + "/api/ext/answer", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: cfg.token, company: item.company, fields: fields.slice(0, 30) })
  }).then((rr) => rr.json()).catch(() => null);
  if (a && a.ok && a.answers) return { answers: a.answers, noKey: false };
  return { answers: {}, noKey: !!(a && a.error === "no_ai_key") };
}
// Vision fallback plan {sets, submit_index} from a screenshot + elements. Claude path or backend.
async function jmAiVision(cfg, item, elements, screenshot) {
  if (cfg.useClaude) {
    const ctx = cfg.ctx || {};
    const shot = String(screenshot || "").replace(/^data:image\/[a-z]+;base64,/, "");
    const p = jmVisionPrompt(jmProfileSummary(ctx), ctx.resume, item.company, elements);
    const res = await jmClaude(cfg, { system: p.system, prompt: p.prompt, image_b64: shot, max_tokens: 2048, thinking: true });
    jmAddCost(res.usage);
    if (!res.ok) return null;
    const plan = jmParseJson(res.text);
    return (plan && typeof plan === "object") ? plan : null;
  }
  const v = await fetch(cfg.apibase + "/api/ext/vision", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: cfg.token, company: item.company, elements: elements.slice(0, 50), screenshot })
  }).then((r) => r.json()).catch(() => null);
  return (v && v.ok) ? v.plan : null;
}
// AGENTIC step (Claude only): given a screenshot + elements + the actions taken so far, return the
// SINGLE next action {action:"set"|"click"|"submit"|"done"|"stuck", index, value}. Looped by agenticPass.
async function jmAgentStep(cfg, item, elements, screenshot, history) {
  const ctx = cfg.ctx || {};
  const shot = String(screenshot || "").replace(/^data:image\/[a-z]+;base64,/, "");
  const lines = elements.slice(0, 60).map((e) => {
    const opts = (e.options || []).slice(0, 40);
    const o = opts.length ? "\n    OPTIONS: " + opts.map((x) => String(x).slice(0, 60)).join(" | ") : "";
    const cur = e.value ? " | current=" + JSON.stringify(e.value) : "";
    return "- index=" + e.index + " | type=" + (e.type || "text") + " | label=" + String(e.label || "").slice(0, 160) + cur + o;
  }).join("\n");
  const system = "You are an agent completing a job application form ONE action at a time. You see a " +
    "SCREENSHOT and an indexed list of interactive elements. Use ONLY the candidate's real data; never " +
    "invent. Choose the single next action that makes progress. Actions (return exactly one as JSON): " +
    "{\"action\":\"set\",\"index\":<int>,\"value\":\"<string>\"} fill a field (value must be an exact OPTION for " +
    "option fields); {\"action\":\"click\",\"index\":<int>} click a button/expander; " +
    "{\"action\":\"submit\",\"index\":<int>} click the final submit/continue button ONLY when every required " +
    "field is filled; {\"action\":\"done\"} when complete; {\"action\":\"stuck\"} if blocked (CAPTCHA/login/" +
    "unknown). Return ONLY the JSON for one action.";
  const prompt = "=== CANDIDATE PROFILE ===\n" + jmProfileSummary(ctx) +
    "\n\n=== RÉSUMÉ ===\n" + String(ctx.resume || "").slice(0, 2500) +
    "\n\n=== TARGET COMPANY ===\n" + (item.company || "(unknown)") +
    "\n\n=== ACTIONS SO FAR ===\n" + (history.join("\n") || "(none yet)") +
    "\n\n=== ELEMENTS ===\n" + lines + "\n\n=== NEXT ACTION (JSON) ===";
  const res = await jmClaude(cfg, { system, prompt, image_b64: shot, max_tokens: 1024, thinking: true });
  jmAddCost(res.usage);
  if (!res.ok) return null;
  const a = jmParseJson(res.text);
  return (a && typeof a === "object") ? a : null;
}
// Tailor (rewrite + Tectonic PDF) on the backend, with PREFETCH: a per-run promise cache so the next
// job's tailor can run while the current job's tab loads/fills (the biggest per-job latency).
function prefetchTailor(item, cfg) {
  if (!item || !item.url) return Promise.resolve(null);
  if (JM_Q.tailorPromises && JM_Q.tailorPromises.has(item.url)) return JM_Q.tailorPromises.get(item.url);
  const p = (async () => {
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        return await fetch(cfg.apibase + "/api/ext/tailor", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token: cfg.token, job_url: item.url, company: item.company, format: "pdf" })
        }).then((r) => r.json());
      } catch (e) { await jmSleep(1000); }
    }
    return null;
  })();
  if (JM_Q.tailorPromises) JM_Q.tailorPromises.set(item.url, p);
  return p;
}

// ===================== Computer-use agent (Claude controls the mouse via CDP) =====================
// The honest "full vision agent". Claude sees a screenshot and drives the page with TRUSTED coordinate
// input (cdp.js) — clicking a dropdown open and picking the visible option the way a human does, which is
// exactly what the index→DOM path (jmAgentStep/jmApplyVision) can't do. Reuses the same profile/résumé/
// learned context (cfg.ctx) the other AI passes use. Needs a FOCUSED tab: CDP Input.* only reaches the
// active tab of a focused window, so we move the job tab into its own window for the duration.

function jmCuLearnedLines(ctx) {
  const l = (ctx && ctx.learned) || {};
  const rows = Object.keys(l).map((k) => {
    const v = l[k] && l[k].value; return v ? ("- " + k + " -> " + String(v).slice(0, 120)) : "";
  }).filter(Boolean).slice(0, 40);
  return rows.length ? rows.join("\n") : "(none yet)";
}

function jmCuSystem(cfg, item, fileAttached) {
  const ctx = cfg.ctx || {};
  let gate;
  if (cfg.dryRun) gate = "This is a DRY RUN: fill EVERYTHING but DO NOT click the final Submit/Apply button. When the form is fully filled and only submission remains, stop and reply with exactly: READY: filled";
  else if (!cfg.autosubmit) gate = "Fill EVERYTHING but DO NOT click the final Submit/Apply button. When the form is fully filled, stop and reply with exactly: READY: filled";
  else gate = "Fill the whole form, then click the final Submit/Apply button. Once you see a confirmation / 'thank you' / 'application received' page, stop and reply with exactly: DONE: submitted";
  return "You are operating a web browser to complete a job application for ONE candidate. You see a " +
    "SCREENSHOT of the current page and control a mouse and keyboard. Move and click, type, open dropdowns " +
    "(click to open, then click the correct option — for searchable dropdowns type to filter, then click or " +
    "press Enter), check boxes/radios, scroll to reach fields below the fold, and advance multi-step forms.\n\n" +
    "RULES:\n" +
    "- Use ONLY the candidate's real data below. NEVER invent employers, titles, dates, degrees, numbers, " +
    "salaries, clearances, or demographics. If the data doesn't cover a field, leave it blank.\n" +
    "- For voluntary EEO/demographic questions, answer only if the profile provides the value; otherwise pick " +
    "'Decline to self-identify' if offered, else leave blank.\n" +
    "- Prefer the candidate's LEARNED ANSWERS verbatim when a question matches one.\n" +
    "- The résumé file is " + (fileAttached ? "ALREADY ATTACHED — do not click any Browse/Attach/Upload control." :
      "NOT attached, and you CANNOT operate the native file-picker dialog. If the form requires a résumé upload " +
      "you cannot complete, stop and reply: HANDOFF: résumé upload required.") + "\n" +
    "- Do NOT solve CAPTCHAs, create an account, or log in. On any CAPTCHA / login / account wall, stop and " +
    "reply: HANDOFF: <reason>.\n" +
    "- After each action, take a screenshot and verify the result before the next action.\n" +
    "- " + gate + "\n" +
    "- If you get stuck or cannot make progress, reply: HANDOFF: <reason>.\n\n" +
    "=== CANDIDATE PROFILE ===\n" + (jmProfileSummary(ctx) || "(none)") +
    "\n\n=== RÉSUMÉ ===\n" + String(ctx.resume || "").slice(0, 3500) +
    "\n\n=== LEARNED ANSWERS (prefer verbatim) ===\n" + jmCuLearnedLines(ctx) +
    "\n\n=== TARGET JOB ===\n" + (item.title || "(unknown role)") + " at " + (item.company || "(unknown company)");
}

// Pull plain text out of a Claude content array and look for our control sentinels.
function jmCuSentinel(content) {
  const txt = (content || []).filter((b) => b && b.type === "text").map((b) => b.text || "").join("\n");
  const m = txt.match(/\b(HANDOFF|READY|DONE)\b\s*[:\-]?\s*([^\n]*)/i);
  return m ? { kind: m[1].toUpperCase(), detail: (m[2] || "").trim().slice(0, 120) } : null;
}

// Drive ONE application end-to-end with the computer-use agent. fillPass (preRes) has already attached the
// résumé + trivial fields; here Claude handles the rest by sight. Returns {status, reason, keepTab}.
async function computerUsePass(tab, cfg, item, t, preRes) {
  const maxSteps = cfg.cuMaxSteps || 18;
  const capJob = cfg.cuMaxUsdJob || 0.5, capRun = cfg.cuMaxUsdRun || 5.0;
  const fileAttached = !!(preRes && preRes.fileAttached);
  const job0 = { in: JM_Q.cost.in, out: JM_Q.cost.out };
  let verIdx = 0, triedLegacy = false;

  // Move the job tab into its own FOCUSED window so CDP Input.* lands. Closing the tab later
  // (jmProcessOne's finally) closes this window with it.
  try { await chrome.windows.create({ tabId: tab.id, focused: true, width: 1320, height: 940 }); }
  catch (e) { try { await chrome.tabs.update(tab.id, { active: true }); } catch (e2) {} }
  await jmStoppableSleep(400);

  let dbg = null, detached = false;
  const onDetach = (src) => { if (src && src.tabId === tab.id) detached = true; };
  try {
    chrome.debugger.onDetach.addListener(onDetach);
    dbg = await jmDbgAttach(tab.id);
    await jmCuSetup(dbg);
  } catch (e) {
    chrome.debugger.onDetach.removeListener(onDetach);
    return { status: "check", reason: ("computer-use attach failed: " + String((e && e.message) || e)).slice(0, 140), keepTab: true };
  }

  try {
    const system = jmCuSystem(cfg, item, fileAttached);
    let shot = await jmCuShot(dbg);
    if (!shot) return { status: "check", reason: "computer-use: no screenshot", keepTab: true };
    const messages = [{ role: "user", content: [
      { type: "text", text: "Here is the application page. Complete it per your instructions." },
      { type: "image", source: { type: "base64", media_type: "image/png", data: shot } }
    ] }];

    for (let step = 0; step < maxSteps; step++) {
      if (JM_Q.stop) return { status: "stopped", reason: "stopped" };
      if (detached) return { status: "check", reason: "computer-use: debugger detached (tab closed / DevTools opened)", keepTab: true };

      const ver = JM_CU_VERSIONS[verIdx];
      const res = await jmClaudeTools(cfg, {
        system, messages, tools: jmCuToolDef(ver), beta: ver.beta,
        max_tokens: 3000, thinking: { type: "enabled", budget_tokens: 1024 }, cacheSystem: true
      });
      if (!res.ok) {
        // First-call fallback: an older model may not know the newest tool/beta — retry once on the legacy pair.
        if (!triedLegacy && verIdx === 0 && /beta|computer_20|unsupported|not.*support|tool/i.test(res.error || "")) {
          triedLegacy = true; verIdx = 1; step--; continue;
        }
        return { status: "check", reason: ("computer-use API: " + (res.error || "failed")).slice(0, 140), keepTab: true };
      }
      jmAddCost(res.usage);
      const runUsd = (JM_Q.cost.in * JM_CLAUDE_PRICE.input) + (JM_Q.cost.out * JM_CLAUDE_PRICE.output);
      const jobUsd = ((JM_Q.cost.in - job0.in) * JM_CLAUDE_PRICE.input) + ((JM_Q.cost.out - job0.out) * JM_CLAUDE_PRICE.output);
      if (jobUsd > capJob) return { status: "check", reason: "computer-use: per-job cost cap (~$" + jobUsd.toFixed(2) + ") — verify", keepTab: true };
      if (runUsd > capRun) { JM_Q.stop = true; return { status: "check", reason: "computer-use: run cost cap (~$" + runUsd.toFixed(2) + ") — stopped", keepTab: true }; }

      messages.push({ role: "assistant", content: res.content });

      // A control sentinel ends the run regardless of stop_reason.
      const sent = jmCuSentinel(res.content);
      if (sent) {
        if (sent.kind === "DONE") {
          fetch(cfg.apibase + "/api/ext/save", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ token: cfg.token, title: item.title, company: item.company, url: item.url }) }).catch(() => {});
          return { status: "submitted", reason: "computer-use: " + (sent.detail || "submitted"), keepTab: false };
        }
        if (sent.kind === "READY") return { status: "ready", reason: "computer-use: filled (" + (sent.detail || "not submitted") + ")", keepTab: true };
        return { status: "needs_you", reason: ("computer-use: " + (sent.detail || "handoff")).slice(0, 140), keepTab: true };
      }

      const toolUses = (res.content || []).filter((b) => b && b.type === "tool_use");
      if (!toolUses.length) return { status: "check", reason: "computer-use: agent stopped — verify in the open window", keepTab: true };

      const results = [];
      for (let u = 0; u < toolUses.length; u++) {
        const tu = toolUses[u];
        try { await jmCuDoAction(dbg, tu.input || {}); } catch (e3) {}
        await jmStoppableSleep(550);                   // let the page/widget settle before the screenshot
        let snap = "";
        try { snap = await jmCuShot(dbg); } catch (e4) {}
        results.push({ type: "tool_result", tool_use_id: tu.id,
          content: snap ? [{ type: "image", source: { type: "base64", media_type: "image/png", data: snap } }] : "screenshot unavailable",
          is_error: !snap });
      }
      messages.push({ role: "user", content: results });
    }
    return { status: "check", reason: "computer-use: ran " + maxSteps + " steps, no confirmation — verify", keepTab: true };
  } catch (e) {
    if (JM_Q.stop) return { status: "stopped", reason: "stopped" };
    return { status: "check", reason: ("computer-use error: " + String((e && e.message) || e)).slice(0, 140), keepTab: true };
  } finally {
    chrome.debugger.onDetach.removeListener(onDetach);
    if (dbg && !detached) { try { await jmCuTeardown(dbg); } catch (e5) {} }
  }
}

async function jmProcessOne(item, cfg) {
  let t = await prefetchTailor(item, cfg);            // per-run prefetch cache (may already be running)
  if (!t) return { status: "error", reason: "tailor network error" };
  if (!t.ok) return { status: "error", reason: "tailor: " + (t.error || "failed") };

  let keepTab = false;                                 // leave the tab open if a human must finish it
  const tab = await chrome.tabs.create({ url: item.url, active: false });
  JM_Q.currentTabId = tab.id;
  try {
    await jmWaitForLoad(tab.id, 25000);
    if (JM_Q.stop) return { status: "stopped", reason: "stopped" };
    // Wait for the form to actually render (SPAs: Ashby / SmartRecruiters / EU Greenhouse), and
    // click "Apply" once if the fields haven't appeared after ~3s.
    let ready = false;
    for (let k = 0; k < 9 && !ready && !JM_Q.stop; k++) {
      await jmStoppableSleep(1000);
      try {
        const rr = await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, world: "MAIN", func: jmFormReady });
        ready = (rr || []).some((o) => o && o.result);
      } catch (e) {}
      if (!ready && k === 2) {
        try { await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, world: "MAIN", func: jmClickApply }); } catch (e) {}
      }
    }
    if (JM_Q.stop) return { status: "stopped", reason: "stopped" };
    // Fill the CURRENT page: deterministic pass, then AI pass for whatever it left empty.
    async function fillPass() {
      const o1 = await chrome.scripting.executeScript({
        target: { tabId: tab.id, allFrames: true }, world: "MAIN",
        func: jmFillApplication, args: [{ fields: t.fields, file: t.file, defaults: t.defaults }]
      });
      let r = (o1 || []).map((o) => o && o.result).filter(Boolean).find((x) => x && x.found) || { found: false };
      if (r.found && ((r.unfilled && r.unfilled.length) || !r.fileAttached)) {
        let aiF = 0, aiN = 0, aiM = 0;
        try {
          const snap = await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, world: "MAIN", func: jmSnapshotForm });
          let fields = [];
          (snap || []).forEach((o) => { if (o && Array.isArray(o.result)) fields = fields.concat(o.result); });
          aiF = fields.length;
          JM_Q.lastFields = fields;                       // captured for auto-diagnostics on failure
          if (fields.length) {
            const aiRes = await jmAiAnswer(cfg, item, fields);   // Claude in-extension, or backend fallback
            const answers = aiRes.answers || {};
            if (aiRes.noKey) r._aiNoKey = true;
            aiN = Object.keys(answers).length;
            if (aiN) {
              const ap = await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, world: "MAIN", func: jmApplyAnswers, args: [answers] });
              aiM = (ap || []).reduce((s2, o) => s2 + ((o && o.result && o.result.applied) || 0), 0);
              const o2 = await chrome.scripting.executeScript({
                target: { tabId: tab.id, allFrames: true }, world: "MAIN",
                func: jmFillApplication, args: [{ fields: t.fields, file: t.file, defaults: t.defaults }]
              });
              r = (o2 || []).map((o) => o && o.result).filter(Boolean).find((x) => x && x.found) || r;
            }
          }
        } catch (e) {}
        r._ai = "AI f" + aiF + " a" + aiN + " ok" + aiM;   // diagnostic: snapshot / answered / applied
      }
      return r;
    }

    // VISION FALLBACK (opt-in): if the deterministic + AI-answer pass still left required fields
    // empty and there's no wall, let a vision model take a pass — it sees a screenshot of the page
    // plus every interactive element and decides values the label heuristics missed. Needs a VISIBLE
    // tab (captureVisibleTab), so we briefly foreground it; that's why it's a last resort, not default.
    async function visionPass() {
      try { await chrome.tabs.update(tab.id, { active: true }); } catch (e) {}
      await jmStoppableSleep(500);
      let shot = "";
      try { shot = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" }); } catch (e) {}
      if (!shot) return;
      let elements = [];
      try {
        const sn = await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: "MAIN", func: jmVisionSnapshot });
        elements = (sn && sn[0] && sn[0].result) || [];
      } catch (e) {}
      if (!elements.length) return;
      const plan = await jmAiVision(cfg, item, elements.slice(0, 50), shot);   // Claude in-extension, or backend
      if (!plan) return;
      try { await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: "MAIN", func: jmApplyVision, args: [plan] }); } catch (e) {}
    }

    // AGENTIC mode (opt-in, Claude-only): last-resort loop driving the FOREGROUND tab — screenshot →
    // Claude picks the next action → apply → re-screenshot. Respects the submit gate (won't click submit
    // in dry-run / when auto-submit is off). Same constraints as the vision pass (visible tab, no upload).
    async function agenticPass() {
      try { await chrome.tabs.update(tab.id, { active: true }); } catch (e) {}
      const history = [];
      for (let step = 0; step < 6 && !JM_Q.stop; step++) {
        await jmStoppableSleep(600);
        let shot = "";
        try { shot = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" }); } catch (e) {}
        if (!shot) break;
        let elements = [];
        try { const sn = await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: "MAIN", func: jmVisionSnapshot }); elements = (sn && sn[0] && sn[0].result) || []; } catch (e) {}
        if (!elements.length) break;
        const act = await jmAgentStep(cfg, item, elements, shot, history);
        if (!act || act.action === "done" || act.action === "stuck") break;
        history.push(act.action + (act.index != null ? " #" + act.index : "") + (act.value ? "=" + String(act.value).slice(0, 30) : ""));
        if (act.action === "set" && act.index != null) {
          try { await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: "MAIN", func: jmApplyVision, args: [{ sets: [{ index: act.index, value: act.value }], submit_index: null }] }); } catch (e) {}
        } else if (act.action === "submit" || act.action === "click") {
          if (act.action === "submit" && (cfg.dryRun || !cfg.autosubmit)) break;   // respect the submit gate
          try { await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: "MAIN", func: jmClickSubmit, args: ['[data-jmv="' + act.index + '"]'] }); } catch (e) {}
          await jmStoppableSleep(1500);
        }
      }
    }

    let res = await fillPass();
    // COMPUTER-USE agent (Claude controls the mouse) — the primary path when enabled. fillPass has already
    // attached the résumé + trivial fields; the agent does the rest (dropdowns, custom Qs, multi-step,
    // submit) by clicking like a human. Runs even on unrecognized ATS, and takes over the deterministic
    // submit pipeline below.
    if (cfg.computerUse && cfg.useClaude) {
      if (res.captcha) return { status: "needs_you", reason: "CAPTCHA on page" };
      if (res.login) return { status: "needs_you", reason: "login / account wall" };
      const cu = await computerUsePass(tab, cfg, item, t, res);
      keepTab = !!cu.keepTab;
      return { status: cu.status, reason: cu.reason };
    }
    if (!res.found) return { status: "skipped", reason: "no supported form on page" };
    if (res.captcha) return { status: "needs_you", reason: "CAPTCHA on page" };
    if (res.login) return { status: "needs_you", reason: "login / account wall" };
    if (cfg.vision && res.fileAttached && res.unfilled && res.unfilled.length && !JM_Q.stop) {
      try { await visionPass(); } catch (e) {}
      res = await fillPass();                            // re-check what the vision pass landed
    }
    if (cfg.agentic && cfg.useClaude && res.fileAttached && res.unfilled && res.unfilled.length
        && !res.captcha && !res.login && !JM_Q.stop) {
      try { await agenticPass(); } catch (e) {}
      res = await fillPass();                            // re-check what the agentic loop landed
    }
    if (!res.fileAttached) return { status: "needs_you", reason: ("resume not attached | " + (res._ai || "")).slice(0, 120) };
    if (res.unfilled && res.unfilled.length)
      return { status: "needs_you", reason: ((res._aiNoKey ? "(set GEMINI_API_KEY) " : "") + res.unfilled.length + " req: " + res.unfilled.slice(0, 2).map((u) => u.label).join("; ") + " | " + (res._ai || "")).slice(0, 150) };

    if (cfg.dryRun) return { status: "ready", reason: "dry run — would submit (" + res.filled + "/" + res.total + " filled)" };
    if (!cfg.autosubmit) return { status: "ready", reason: "filled (auto-submit off)" };

    // Submit, advancing through MULTI-STEP forms (click submit/next → re-fill the new step) until a
    // real confirmation page, or until we hit a wall / can't progress.
    const logApp = () => fetch(cfg.apibase + "/api/ext/save", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: cfg.token, title: item.title, company: item.company, url: item.url })
    }).catch(() => {});
    let prevHref = item.url;
    for (let step = 0; step < 6; step++) {
      if (JM_Q.stop) return { status: "stopped", reason: "stopped" };
      const sub = await chrome.scripting.executeScript({
        target: { tabId: tab.id }, world: "MAIN", func: jmClickSubmit, args: [res.submitSelector]
      });
      if (!(sub && sub[0] && sub[0].result && sub[0].result.clicked)) {
        keepTab = true; logApp();
        return { status: "check", reason: "submit/next button not found — VERIFY" };
      }
      await jmStoppableSleep(4000);
      const st = await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: "MAIN", func: jmApplyState, args: [prevHref] });
      const s = (st && st[0] && st[0].result) || {};
      if (s.confirmed) { logApp(); return { status: "submitted", reason: "confirmation page detected" + (step ? " (step " + (step + 1) + ")" : "") }; }
      if (s.captcha) { keepTab = true; try { await chrome.tabs.update(tab.id, { active: true }); } catch (e) {} return { status: "needs_you", reason: "CAPTCHA on submit — tab open; solve it & submit" }; }
      if (!s.changed) {
        // Clicked but no navigation and no recognized confirmation. Re-check the form: if a required
        // field is now flagged, validation blocked the submit; otherwise it may have AJAX-submitted.
        const chk = await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, world: "MAIN", func: jmFillApplication, args: [{ fields: t.fields, file: t.file, defaults: t.defaults }] });
        const cr = (chk || []).map((o) => o && o.result).filter(Boolean).find((x) => x && x.found) || {};
        keepTab = true; logApp();
        if (cr.unfilled && cr.unfilled.length)
          return { status: "check", reason: ("submit blocked — " + cr.unfilled.length + " field(s): " + cr.unfilled.slice(0, 2).map((u) => u.label).join("; ")).slice(0, 150) };
        return { status: "check", reason: "submit clicked — verify in the open tab (no confirmation detected)" };
      }
      // Page advanced to a new step — re-fill it and loop to submit again.
      prevHref = s.href;
      await jmStoppableSleep(1200);
      res = await fillPass();
      if (res.login) { keepTab = true; return { status: "needs_you", reason: "login wall at step " + (step + 2) }; }
      if (res.captcha) { keepTab = true; try { await chrome.tabs.update(tab.id, { active: true }); } catch (e) {} return { status: "needs_you", reason: "CAPTCHA at step " + (step + 2) + " — tab open; solve it" }; }
      if (res.unfilled && res.unfilled.length) { keepTab = true; logApp(); return { status: "check", reason: ("step " + (step + 2) + ": " + res.unfilled.length + " req: " + res.unfilled.slice(0, 2).map((u) => u.label).join("; ") + " | " + (res._ai || "")).slice(0, 150) }; }
    }
    keepTab = true; logApp();
    return { status: "check", reason: "advanced through 6 steps, no confirmation — VERIFY" };
  } catch (e) {
    if (JM_Q.stop) return { status: "stopped", reason: "stopped" };   // tab was killed by hard-stop
    return { status: "error", reason: String((e && e.message) || e).slice(0, 140) };
  } finally {
    JM_Q.currentTabId = null;
    if (!keepTab) { try { await chrome.tabs.remove(tab.id); } catch (e) {} }
  }
}

async function jmRunQueue(cfg) {
  if (JM_Q.running) return { ok: false, error: "already running" };
  JM_Q.running = true; JM_Q.stop = false;
  JM_Q.tailorPromises = new Map(); JM_Q.cost = { in: 0, out: 0 };
  jmKeepAlive(true);                                                // survive the MV3 SW idle timer during long runs
  cfg.useClaude = !!cfg.claudeKey && cfg.provider !== "backend";   // Claude in-extension when a key is set
  // Fetch profile + résumé + learned bank ONCE — used to build Claude prompts and match the user's own
  // past answers client-side. (On the no-key fallback the backend does this server-side instead.)
  try {
    cfg.ctx = await fetch(cfg.apibase + "/api/ext/profile_fields?token=" + encodeURIComponent(cfg.token)).then((r) => r.json());
  } catch (e) { cfg.ctx = {}; }
  const state = {
    running: true, dryRun: cfg.dryRun, autosubmit: cfg.autosubmit, useClaude: cfg.useClaude,
    total: cfg.items.length, idx: 0, startedAt: Date.now(), costUsd: 0,
    items: cfg.items.map((it) => ({ url: it.url, title: it.title || "", company: it.company || "", status: "queued", reason: "" }))
  };
  await jmSaveQueue(state);
  for (let i = 0; i < state.items.length; i++) {
    if (JM_Q.stop) { state.items[i].status = "stopped"; break; }
    state.idx = i; state.items[i].status = "running"; await jmSaveQueue(state);
    prefetchTailor(state.items[i], cfg);                          // ensure current job's tailor is running
    if (i + 1 < state.items.length) prefetchTailor(state.items[i + 1], cfg);   // overlap NEXT job's tailor with this one
    const r = await jmProcessOne(state.items[i], cfg);
    state.items[i].status = r.status; state.items[i].reason = r.reason;
    state.costUsd = +((JM_Q.cost.in * JM_CLAUDE_PRICE.input) + (JM_Q.cost.out * JM_CLAUDE_PRICE.output)).toFixed(4);
    await jmSaveQueue(state);
    // Auto-report failing forms (structure only) so they can be fixed without manual error-relay.
    if (["check", "needs_you", "error", "skipped"].indexOf(r.status) >= 0) {
      fetch(cfg.apibase + "/api/ext/debug", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: cfg.token, url: state.items[i].url, company: state.items[i].company, status: r.status, reason: r.reason, fields: (JM_Q.lastFields || []).slice(0, 40) })
      }).catch(() => {});
    }
    if (i < state.items.length - 1 && !JM_Q.stop) await jmStoppableSleep(Math.min(cfg.delayMs || 8000, 20000));
  }
  if (JM_Q.stop) state.items.forEach((it) => { if (it.status === "queued" || it.status === "running") it.status = "stopped"; });
  state.running = false; JM_Q.running = false; JM_Q.currentTabId = null;
  jmKeepAlive(false);
  await jmSaveQueue(state);
  return { ok: true };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return;
  if (msg.type === "jm_queue_start") {
    // Fire-and-forget: a run can take many minutes. Acknowledge immediately so the popup's message
    // channel closes cleanly (progress is read from chrome.storage). Previously we returned true and
    // awaited the whole queue, which logged "message channel closed before a response" once the popup
    // closed — harmless, but noisy.
    jmRunQueue({
      items: msg.items || [], apibase: msg.apibase, token: msg.token,
      dryRun: msg.dryRun !== false, autosubmit: !!msg.autosubmit, delayMs: msg.delayMs || 8000,
      vision: !!msg.vision, agentic: !!msg.agentic, computerUse: !!msg.computerUse,
      claudeKey: msg.claudeKey || "", claudeModel: msg.claudeModel || "", provider: msg.provider || ""
    });
    sendResponse({ ok: true, started: true });
    return;                                            // synchronous reply — don't hold the channel
  }
  if (msg.type === "jm_queue_stop") {
    // Hard stop: flag it AND kill the in-flight tab so the current job's executeScript/waits abort
    // immediately instead of finishing first.
    JM_Q.stop = true;
    if (JM_Q.currentTabId) { try { chrome.tabs.remove(JM_Q.currentTabId); } catch (e) {} JM_Q.currentTabId = null; }
    sendResponse({ ok: true });
    return;
  }
});
