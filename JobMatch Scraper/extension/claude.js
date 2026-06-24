// claude.js — direct Anthropic Claude API client for the extension's background worker.
//
// HYBRID architecture: the extension calls api.anthropic.com ITSELF for the AI brain (answering
// novel questions, the vision fallback, the agentic loop), instead of routing through the Flask
// backend. The backend still does résumé tailoring/PDF and serves the profile + learned bank.
//
// The user's Anthropic key lives in chrome.storage (entered in the popup Settings) — NEVER committed,
// NEVER baked into the shipped extension (a client-side key is extractable; this is for personal use).
// Browser-origin calls to Anthropic require the `anthropic-dangerous-direct-browser-access` header.
// importScripts()'d by background.js (service-worker context — no ES modules).

var JM_CLAUDE_MODEL_DEFAULT = "claude-sonnet-4-6";
// Sonnet 4.6 list price, $/token — used only for the popup's rough running-cost counter.
var JM_CLAUDE_PRICE = { input: 3.0 / 1e6, output: 15.0 / 1e6 };

// Robust JSON extractor: pulls the first balanced {...} or [...] out of model text, tolerating
// ```json fences and surrounding prose. Mirrors resume_brain/ai.py parse_json.
function jmParseJson(text) {
  if (text == null) return null;
  try { return JSON.parse(text); } catch (e) {}
  var s = String(text);
  var fence = s.match(/```(?:json)?\s*([\s\S]*?)```/i);
  if (fence) { try { return JSON.parse(fence[1]); } catch (e) {} }
  var start = s.search(/[{\[]/);
  if (start < 0) return null;
  var open = s[start], close = open === "{" ? "}" : "]";
  var depth = 0, inStr = false, esc = false;
  for (var i = start; i < s.length; i++) {
    var ch = s[i];
    if (inStr) { if (esc) esc = false; else if (ch === "\\") esc = true; else if (ch === '"') inStr = false; continue; }
    if (ch === '"') inStr = true;
    else if (ch === open) depth++;
    else if (ch === close) { depth--; if (depth === 0) { try { return JSON.parse(s.slice(start, i + 1)); } catch (e) { return null; } } }
  }
  return null;
}

// Call the Claude Messages API once. opts: {system, prompt, image_b64, max_tokens, thinking}.
// Returns { ok, text, usage:{input_tokens, output_tokens}, error }.
async function jmClaude(cfg, opts) {
  opts = opts || {};
  if (!cfg || !cfg.claudeKey) return { ok: false, error: "no_claude_key", text: "", usage: {} };
  var content = [];
  if (opts.image_b64) content.push({ type: "image", source: { type: "base64", media_type: "image/png", data: opts.image_b64 } });
  content.push({ type: "text", text: opts.prompt || "" });
  var body = {
    model: cfg.claudeModel || JM_CLAUDE_MODEL_DEFAULT,
    max_tokens: opts.max_tokens || 2048,
    messages: [{ role: "user", content: content }]
  };
  if (opts.system) body.system = opts.system;
  if (opts.thinking) body.thinking = { type: "adaptive" };   // off by default (faster/cheaper for structured extraction)
  try {
    var r = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-api-key": cfg.claudeKey,
        "anthropic-version": "2023-06-01",
        "anthropic-dangerous-direct-browser-access": "true"
      },
      body: JSON.stringify(body)
    });
    if (!r.ok) {
      var errtxt = "";
      try { var ej = await r.json(); errtxt = (ej && ej.error && ej.error.message) || JSON.stringify(ej); }
      catch (e) { try { errtxt = await r.text(); } catch (e2) {} }
      return { ok: false, error: ("HTTP " + r.status + (errtxt ? ": " + String(errtxt).slice(0, 160) : "")), text: "", usage: {} };
    }
    var j = await r.json();
    var text = (j.content || []).filter(function (b) { return b && b.type === "text"; })
      .map(function (b) { return b.text || ""; }).join("");
    var u = j.usage || {};
    return { ok: true, text: text, usage: { input_tokens: u.input_tokens || 0, output_tokens: u.output_tokens || 0 } };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e).slice(0, 140), text: "", usage: {} };
  }
}

// Multi-turn Messages call WITH tools — used by the computer-use agent (cdp.js / computerUsePass).
// Unlike jmClaude (single user turn, text-only return) this sends a full `messages` array, a `tools`
// list, an extra `anthropic-beta` header, optional prompt-caching of the static system prefix, and
// returns the RAW content array + stop_reason so the caller can run a screenshot→action→screenshot loop.
// opts: { system, messages, tools, beta, max_tokens, thinking, cacheSystem }
async function jmClaudeTools(cfg, opts) {
  opts = opts || {};
  if (!cfg || !cfg.claudeKey) return { ok: false, error: "no_claude_key", content: [], stop_reason: null, usage: {} };
  var headers = {
    "content-type": "application/json",
    "x-api-key": cfg.claudeKey,
    "anthropic-version": "2023-06-01",
    "anthropic-dangerous-direct-browser-access": "true"
  };
  if (opts.beta) headers["anthropic-beta"] = opts.beta;
  // cache_control on the system block caches the (large, static) tools + profile + résumé prefix so
  // only the growing screenshot tail is re-billed each turn.
  var system = opts.system;
  if (opts.cacheSystem && typeof opts.system === "string" && opts.system) {
    system = [{ type: "text", text: opts.system, cache_control: { type: "ephemeral" } }];
  }
  var body = {
    model: cfg.claudeModel || JM_CLAUDE_MODEL_DEFAULT,
    max_tokens: opts.max_tokens || 3000,
    messages: opts.messages || []
  };
  if (system) body.system = system;
  if (opts.tools) body.tools = opts.tools;
  if (opts.thinking) body.thinking = opts.thinking;   // {type:"enabled", budget_tokens:N} — boosts click accuracy
  try {
    var r = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST", headers: headers, body: JSON.stringify(body)
    });
    if (!r.ok) {
      var errtxt = "";
      try { var ej = await r.json(); errtxt = (ej && ej.error && ej.error.message) || JSON.stringify(ej); }
      catch (e) { try { errtxt = await r.text(); } catch (e2) {} }
      return { ok: false, error: ("HTTP " + r.status + (errtxt ? ": " + String(errtxt).slice(0, 200) : "")), content: [], stop_reason: null, usage: {} };
    }
    var j = await r.json();
    var u = j.usage || {};
    return {
      ok: true, content: j.content || [], stop_reason: j.stop_reason || null,
      usage: { input_tokens: u.input_tokens || 0, output_tokens: u.output_tokens || 0 }
    };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e).slice(0, 160), content: [], stop_reason: null, usage: {} };
  }
}
