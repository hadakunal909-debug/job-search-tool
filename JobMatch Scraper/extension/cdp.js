"use strict";
// cdp.js — Chrome DevTools Protocol layer for the computer-use agent.
//
// This is what makes "Claude controls the mouse" real. Claude's computer-use tool returns COORDINATE
// actions (left_click [x,y], type, scroll, open a dropdown and click the visible option). We execute
// them here as TRUSTED input via chrome.debugger (Input.dispatchMouseEvent / dispatchKeyEvent) — events
// with isTrusted===true, so native + custom (react-select) dropdowns, React controlled inputs, date
// pickers, and form validation react exactly as they do for a human. That's the whole reason this beats
// the synthetic MouseEvent/execCommand/value-setter hacks in filler.js, which sites can and do ignore.
//
// Requires the "debugger" permission (shows a "started debugging this browser" infobar — fine for a
// single personal user). importScripts()'d by background.js (service-worker context — no ES modules).

// Viewport == Claude's coordinate space. deviceScaleFactor:1 means screenshot pixels map 1:1 to the
// coordinates Claude returns, so we never scale clicks (the #1 cause of consistent click offset).
// 1280x800 = 1.024 MP — under the API image limit, so we capture natively with zero downscale.
var JM_CU_W = 1280, JM_CU_H = 800;

// Computer-use tool/beta pairs, newest first. computerUsePass tries [0]; if the API rejects the tool
// type or beta (e.g. running an older model), it falls back to [1]. Keep newest first.
var JM_CU_VERSIONS = [
  { tool: "computer_20251124", beta: "computer-use-2025-11-24" },
  { tool: "computer_20250124", beta: "computer-use-2025-01-24" }
];
function jmCuToolDef(ver) {
  return [{ type: ver.tool, name: "computer", display_width_px: JM_CU_W, display_height_px: JM_CU_H }];
}

// ---- low-level debugger plumbing (promisified chrome.debugger) ----
function jmDbgSend(dbg, method, params) {
  return new Promise(function (resolve, reject) {
    try {
      chrome.debugger.sendCommand(dbg, method, params || {}, function (res) {
        var e = chrome.runtime.lastError;
        if (e) reject(new Error(method + ": " + e.message)); else resolve(res || {});
      });
    } catch (e) { reject(e); }
  });
}
function jmDbgAttach(tabId) {
  return new Promise(function (resolve, reject) {
    chrome.debugger.attach({ tabId: tabId }, "1.3", function () {
      var e = chrome.runtime.lastError;
      if (e) reject(new Error("attach: " + e.message)); else resolve({ tabId: tabId });
    });
  });
}
function jmDbgDetach(dbg) {
  return new Promise(function (resolve) {
    try { chrome.debugger.detach(dbg, function () { void chrome.runtime.lastError; resolve(); }); }
    catch (e) { resolve(); }
  });
}

// Attach + enable + pin the viewport so screenshots match the coordinate space.
async function jmCuSetup(dbg) {
  await jmDbgSend(dbg, "Page.enable");
  await jmDbgSend(dbg, "Runtime.enable");
  await jmDbgSend(dbg, "Emulation.setDeviceMetricsOverride",
    { width: JM_CU_W, height: JM_CU_H, deviceScaleFactor: 1, mobile: false });
}
async function jmCuTeardown(dbg) {
  try { await jmDbgSend(dbg, "Emulation.clearDeviceMetricsOverride"); } catch (e) {}
  await jmDbgDetach(dbg);
}

// Screenshot of the (focused) tab — works without captureVisibleTab's "must be the active window"
// dance; captureBeyondViewport:false keeps coordinates viewport-relative (the agent scrolls to reach
// off-screen fields). Returns raw base64 (no data: prefix), ready for an image content block.
async function jmCuShot(dbg) {
  var r = await jmDbgSend(dbg, "Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  return (r && r.data) ? r.data : "";
}

// ---- coordinate helpers ----
function jmClampX(x) { return Math.max(0, Math.min(JM_CU_W - 1, Math.round(x || 0))); }
function jmClampY(y) { return Math.max(0, Math.min(JM_CU_H - 1, Math.round(y || 0))); }

// ---- trusted mouse input ----
async function jmCuMove(dbg, x, y, mods) {
  await jmDbgSend(dbg, "Input.dispatchMouseEvent", { type: "mouseMoved", x: jmClampX(x), y: jmClampY(y), modifiers: mods || 0 });
}
async function jmCuClick(dbg, x, y, button, count, mods) {
  button = button || "left"; count = count || 1; mods = mods || 0;
  var mask = button === "right" ? 2 : button === "middle" ? 4 : 1;
  x = jmClampX(x); y = jmClampY(y);
  await jmDbgSend(dbg, "Input.dispatchMouseEvent", { type: "mouseMoved", x: x, y: y, modifiers: mods });
  await jmDbgSend(dbg, "Input.dispatchMouseEvent", { type: "mousePressed", x: x, y: y, button: button, buttons: mask, clickCount: count, modifiers: mods });
  await jmDbgSend(dbg, "Input.dispatchMouseEvent", { type: "mouseReleased", x: x, y: y, button: button, buttons: 0, clickCount: count, modifiers: mods });
}
async function jmCuScroll(dbg, x, y, dx, dy, mods) {
  await jmDbgSend(dbg, "Input.dispatchMouseEvent", { type: "mouseWheel", x: jmClampX(x), y: jmClampY(y), deltaX: dx || 0, deltaY: dy || 0, modifiers: mods || 0 });
}
async function jmCuDrag(dbg, x1, y1, x2, y2, mods) {
  mods = mods || 0;
  x1 = jmClampX(x1); y1 = jmClampY(y1); x2 = jmClampX(x2); y2 = jmClampY(y2);
  await jmDbgSend(dbg, "Input.dispatchMouseEvent", { type: "mouseMoved", x: x1, y: y1, modifiers: mods });
  await jmDbgSend(dbg, "Input.dispatchMouseEvent", { type: "mousePressed", x: x1, y: y1, button: "left", buttons: 1, clickCount: 1, modifiers: mods });
  await jmDbgSend(dbg, "Input.dispatchMouseEvent", { type: "mouseMoved", x: x2, y: y2, button: "left", buttons: 1, modifiers: mods });
  await jmDbgSend(dbg, "Input.dispatchMouseEvent", { type: "mouseReleased", x: x2, y: y2, button: "left", buttons: 0, clickCount: 1, modifiers: mods });
}

// ---- trusted keyboard input ----
// Plain text -> Input.insertText (most reliable into a focused field). Named keys / chords (Enter, Tab,
// ctrl+a, ...) -> Input.dispatchKeyEvent with a small keysym map (Claude uses xdotool-style names).
var JM_KEYMAP = {
  "return": { key: "Enter", code: "Enter", vk: 13 }, "enter": { key: "Enter", code: "Enter", vk: 13 },
  "tab": { key: "Tab", code: "Tab", vk: 9 },
  "escape": { key: "Escape", code: "Escape", vk: 27 }, "esc": { key: "Escape", code: "Escape", vk: 27 },
  "backspace": { key: "Backspace", code: "Backspace", vk: 8 },
  "delete": { key: "Delete", code: "Delete", vk: 46 },
  "space": { key: " ", code: "Space", vk: 32 },
  "up": { key: "ArrowUp", code: "ArrowUp", vk: 38 }, "down": { key: "ArrowDown", code: "ArrowDown", vk: 40 },
  "left": { key: "ArrowLeft", code: "ArrowLeft", vk: 37 }, "right": { key: "ArrowRight", code: "ArrowRight", vk: 39 },
  "page_up": { key: "PageUp", code: "PageUp", vk: 33 }, "page_down": { key: "PageDown", code: "PageDown", vk: 34 },
  "home": { key: "Home", code: "Home", vk: 36 }, "end": { key: "End", code: "End", vk: 35 }
};
var JM_MODMASK = { ctrl: 2, control: 2, alt: 1, shift: 8, super: 4, meta: 4, cmd: 4, command: 4 };
function jmResolveKey(token) {
  var raw = String(token || ""), t = raw.toLowerCase();
  if (JM_KEYMAP[t]) return JM_KEYMAP[t];
  if (raw.length === 1) {
    if (/[a-z]/i.test(raw)) return { key: raw, code: "Key" + raw.toUpperCase(), vk: raw.toUpperCase().charCodeAt(0) };
    if (/[0-9]/.test(raw)) return { key: raw, code: "Digit" + raw, vk: raw.charCodeAt(0) };
    return { key: raw, code: "", vk: raw.charCodeAt(0) };
  }
  var fm = t.match(/^f([1-9]|1[0-2])$/);
  if (fm) { var n = parseInt(fm[1], 10); return { key: "F" + n, code: "F" + n, vk: 111 + n }; }
  return { key: raw, code: "", vk: 0 };
}
async function jmCuType(dbg, text) {
  await jmDbgSend(dbg, "Input.insertText", { text: String(text == null ? "" : text) });
}
async function jmCuKey(dbg, combo) {
  var parts = String(combo || "").split("+").map(function (s) { return s.trim(); }).filter(Boolean);
  if (!parts.length) return;
  var mods = 0;
  for (var i = 0; i < parts.length - 1; i++) { var m = JM_MODMASK[parts[i].toLowerCase()]; if (m) mods |= m; }
  var k = jmResolveKey(parts[parts.length - 1]);
  var base = { modifiers: mods, key: k.key, code: k.code, windowsVirtualKeyCode: k.vk, nativeVirtualKeyCode: k.vk };
  await jmDbgSend(dbg, "Input.dispatchKeyEvent", Object.assign({ type: "keyDown" }, base));
  await jmDbgSend(dbg, "Input.dispatchKeyEvent", Object.assign({ type: "keyUp" }, base));
}

// Map ONE Claude computer-tool action -> CDP calls. The caller takes a fresh screenshot afterward, so
// we don't return one here. Unknown/no-op actions (wait, cursor_position) just fall through.
async function jmCuDoAction(dbg, a) {
  a = a || {};
  var act = a.action || "screenshot";
  var c = a.coordinate || [];
  var mods = 0;
  if (a.text && /_click$/.test(act)) { var mm = JM_MODMASK[String(a.text).toLowerCase()]; if (mm) mods = mm; }
  switch (act) {
    case "screenshot": break;
    case "mouse_move": await jmCuMove(dbg, c[0], c[1]); break;
    case "left_click": await jmCuClick(dbg, c[0], c[1], "left", 1, mods); break;
    case "right_click": await jmCuClick(dbg, c[0], c[1], "right", 1, mods); break;
    case "middle_click": await jmCuClick(dbg, c[0], c[1], "middle", 1, mods); break;
    case "double_click": await jmCuClick(dbg, c[0], c[1], "left", 2, mods); break;
    case "triple_click": await jmCuClick(dbg, c[0], c[1], "left", 3, mods); break;
    case "left_click_drag": { var s = a.start_coordinate || []; await jmCuDrag(dbg, s[0], s[1], c[0], c[1]); break; }
    case "left_mouse_down": await jmDbgSend(dbg, "Input.dispatchMouseEvent", { type: "mousePressed", x: jmClampX(c[0]), y: jmClampY(c[1]), button: "left", buttons: 1, clickCount: 1 }); break;
    case "left_mouse_up": await jmDbgSend(dbg, "Input.dispatchMouseEvent", { type: "mouseReleased", x: jmClampX(c[0]), y: jmClampY(c[1]), button: "left", buttons: 0, clickCount: 1 }); break;
    case "type": await jmCuType(dbg, a.text); break;
    case "key": await jmCuKey(dbg, a.text); break;
    case "hold_key": await jmCuKey(dbg, a.text); break;   // tap approximation; duration not held
    case "scroll": {
      var dir = a.scroll_direction || "down";
      var amt = (a.scroll_amount != null ? a.scroll_amount : 3) * 100;
      var dx = 0, dy = 0;
      if (dir === "down") dy = amt; else if (dir === "up") dy = -amt;
      else if (dir === "right") dx = amt; else if (dir === "left") dx = -amt;
      var sx = (c[0] != null) ? c[0] : JM_CU_W / 2, sy = (c[1] != null) ? c[1] : JM_CU_H / 2;
      await jmCuScroll(dbg, sx, sy, dx, dy);
      break;
    }
    case "wait": break;       // the loop adds a settle delay regardless
    case "cursor_position": break;
    default: break;
  }
}
