"use strict";
const $ = (id) => document.getElementById(id);
const get = (keys) => new Promise((r) => chrome.storage.local.get(keys, r));
const set = (obj) => new Promise((r) => chrome.storage.local.set(obj, r));
const activeTab = () => new Promise((r) => chrome.tabs.query({ active: true, currentWindow: true }, (t) => r(t[0])));

let cfg = { token: "", apibase: "https://stemjobs.astrochakra.co" };

function cleanTitle(t) { return (t || "").split(/\s[|\-–—]\s/)[0].trim(); }

async function init() {
  cfg = Object.assign(cfg, await get(["token", "apibase"]));
  if (cfg.apibase) $("apibase").value = cfg.apibase;
  if (cfg.token) {
    $("setup").style.display = "none";
    $("main").style.display = "block";
    const tab = await activeTab();
    $("title").value = cleanTitle(tab && tab.title);
    $("company").value = "";
  } else {
    $("main").style.display = "none";
    $("setup").style.display = "block";
  }
}

$("savetok").onclick = async () => {
  const token = $("token").value.trim();
  const apibase = ($("apibase").value.trim() || "https://stemjobs.astrochakra.co").replace(/\/+$/, "");
  if (!token) { $("setupmsg").textContent = "Paste your token first."; return; }
  await set({ token, apibase });
  cfg.token = token; cfg.apibase = apibase;
  init();
};

$("reset").onclick = async () => { await set({ token: "" }); cfg.token = ""; init(); };

$("save").onclick = async () => {
  const tab = await activeTab();
  $("msg").style.color = "#0b7a52"; $("msg").textContent = "Saving…";
  try {
    const r = await fetch(cfg.apibase + "/api/ext/save", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: cfg.token, title: $("title").value.trim(),
                             company: $("company").value.trim(), url: tab && tab.url })
    });
    const j = await r.json();
    if (j.ok) { $("msg").textContent = j.dup ? "Already in your tracker ✓" : "Saved to JobMatch ✓"; }
    else { $("msg").style.color = "#c0392b"; $("msg").textContent = "Error: " + (j.error || "failed"); }
  } catch (e) {
    $("msg").style.color = "#c0392b"; $("msg").textContent = "Network error — check the App URL.";
  }
};

$("fill").onclick = async () => {
  $("msg").style.color = "#0b7a52"; $("msg").textContent = "Loading profile…";
  let prof;
  try {
    const r = await fetch(cfg.apibase + "/api/ext/profile?token=" + encodeURIComponent(cfg.token));
    const j = await r.json();
    if (!j.ok) { $("msg").style.color = "#c0392b"; $("msg").textContent = "Error: " + (j.error || "failed"); return; }
    prof = j.profile;
  } catch (e) { $("msg").style.color = "#c0392b"; $("msg").textContent = "Network error."; return; }
  const tab = await activeTab();
  try {
    const out = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: autofill, args: [prof] });
    const n = (out && out[0] && out[0].result) || 0;
    $("msg").textContent = "Filled " + n + " field(s). Upload your résumé file manually.";
  } catch (e) { $("msg").style.color = "#c0392b"; $("msg").textContent = "Can't autofill this page."; }
};

// Injected into the page. Best-effort: fills text fields + yes/no selects by matching labels.
function autofill(p) {
  function setVal(el, val) {
    if (val == null || val === "") return false;
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter.call(el, val);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }
  function label(el) {
    let t = (el.name || "") + " " + (el.id || "") + " " + (el.placeholder || "") + " " + (el.getAttribute("aria-label") || "");
    if (el.labels && el.labels.length) t += " " + el.labels[0].innerText;
    const al = el.closest("label"); if (al) t += " " + al.innerText;
    return t.toLowerCase();
  }
  const parts = (p.name || "").trim().split(/\s+/);
  const first = parts[0] || "", last = parts.length > 1 ? parts[parts.length - 1] : "";
  let n = 0;
  document.querySelectorAll("input, textarea").forEach((el) => {
    const ty = (el.type || "text").toLowerCase();
    if (["hidden", "file", "password", "submit", "button", "checkbox", "radio", "search"].indexOf(ty) !== -1) return;
    if (el.value && el.value.trim()) return;                 // don't overwrite what's there
    const L = label(el); let v = null;
    if (/e-?mail/.test(L)) v = p.email;
    else if (/phone|mobile|\btel\b/.test(L)) v = p.phone;
    else if (/first\s*name|given name/.test(L)) v = first;
    else if (/last\s*name|surname|family name/.test(L)) v = last;
    else if (/full\s*name|legal name|your name|^name/.test(L) && !/user|company|file/.test(L)) v = p.name;
    else if (/linkedin/.test(L)) v = p.linkedin;
    else if (/city|location|address/.test(L)) v = p.location;
    else if (/sponsor/.test(L)) v = p.needs_sponsorship;
    else if (/authoriz|eligible to work|work authorization|legally/.test(L)) v = p.work_authorized;
    if (v && setVal(el, v)) n++;
  });
  document.querySelectorAll("select").forEach((sel) => {
    const al = sel.closest("label");
    const L = ((sel.name || "") + " " + (sel.id || "") + " " + (al ? al.innerText : "")).toLowerCase();
    let want = null;
    if (/sponsor/.test(L)) want = p.needs_sponsorship;
    else if (/authoriz|eligible to work|legally/.test(L)) want = p.work_authorized;
    if (want) {
      for (const o of sel.options) {
        if (o.text.trim().toLowerCase() === want.toLowerCase()) {
          sel.value = o.value; sel.dispatchEvent(new Event("change", { bubbles: true })); n++; break;
        }
      }
    }
  });
  return n;
}

init();
