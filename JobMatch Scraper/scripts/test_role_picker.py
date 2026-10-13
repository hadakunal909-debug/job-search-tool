#!/usr/bin/env python3
"""Role picker restoration against the entire shipped JavaScript, offline.

The server's checked tiles seed the picker before app.js restores per-user filter
memory. A small DOM fixture supplies controls/events, with no copy of the picker
implementation. No Flask imports, database, browser, network, or personal data.
"""
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
if not NODE:
    raise SystemExit("Node is required for the offline role picker regression.")

DRIVER = r"""
const vm = require("vm");
const source = __SOURCE__;
const passed = [];
function check(name, condition) {
  if (!condition) throw new Error(name);
  passed.push(name);
}
function element() {
  const listeners = {}, classes = new Set();
  return {
    listeners, textContent: "", hidden: false,
    classList: {
      add: k => classes.add(k), remove: k => classes.delete(k),
      toggle: (k, on) => { if (on) classes.add(k); else classes.delete(k); },
      contains: k => classes.has(k)
    },
    addEventListener: (type, fn) => (listeners[type] ||= []).push(fn),
    emit(type, event = {}) { for (const fn of listeners[type] || []) fn(event); }
  };
}
function fixture(wizard = false) {
  const events = [], docEvents = {};
  const tiles = ["A", "B", "C"].map(key => {
    const tile = element(), input = {type: "checkbox", checked: key === "A", disabled: false};
    const label = {textContent: "Role " + key};
    tile.getAttribute = name => name === "data-role" ? key : null;
    tile.querySelector = selector => selector === "input" ? input : selector === ".rolelab" ? label : null;
    return {tile, input, key};
  });
  const pick = element();
  pick.getAttribute = name => name === "data-max" ? "0" : null;
  pick.querySelectorAll = selector => selector === ".roletile" ? tiles.map(t => t.tile) : [];
  const hidden = wizard ? null : {value: "A"}, list = element(), number = element();
  const label = wizard ? null : element(), badge = wizard ? null : element();
  const done = wizard ? null : element(), modal = wizard ? null : element();
  const ids = {roles: hidden, rolelist: list, rolen: number,
    "rolebtn-t": label, rolebadge: badge, roleopen: wizard ? null : element(),
    rolemodal: modal, roledone: done};
  const document = {
    body: {style: {}},
    querySelector: selector => selector === ".rolepick" ? pick : null,
    getElementById: id => ids[id] || null,
    addEventListener: (type, fn) => (docEvents[type] ||= []).push(fn),
    dispatchEvent(event) {
      events.push({type: event.type, detail: event.detail});
      for (const fn of docEvents[event.type] || []) fn(event);
      return true;
    }
  };
  function CustomEvent(type, options = {}) { this.type = type; this.detail = options.detail; }
  vm.runInNewContext(source, {document, CustomEvent}, {filename: "static/rolepick.js"});
  return {
    hidden, tiles, done, list, document,
    restore(value) { hidden.value = value; document.dispatchEvent(new CustomEvent("roles:sync")); },
    changes() { return events.filter(e => e.type === "roles:change"); },
    state() { return {hidden: hidden && hidden.value,
      selected: tiles.filter(t => t.input.checked).map(t => t.key).join(","),
      label: label && label.textContent, count: String(number.textContent),
      badgeHidden: badge && badge.hidden}; },
    syncListeners: () => (docEvents["roles:sync"] || []).length
  };
}
const restored = fixture();
check("initial server A seeds hidden selection", restored.state().hidden === "A");
check("initial server A labels and selects A", restored.state().label === "Role A" && restored.state().selected === "A");
restored.restore("B");
check("restored B replaces checkbox A", restored.state().selected === "B");
check("restored B updates the label and counter", restored.state().label === "Role B" && restored.state().count === "1");
check("restoration is quiet", restored.changes().length === 0);
restored.done.emit("click");
check("Show These Roles retains restored B without edits", restored.state().hidden === "B" && restored.state().selected === "B");
check("Show These Roles emits one actual B selection", restored.changes().length === 1 && restored.changes()[0].detail.roles.join(",") === "B");

const empty = fixture();
empty.restore("");
check("empty restoration means no checked tiles", empty.state().hidden === "" && empty.state().selected === "");
check("empty restoration labels All roles with zero count", empty.state().label === "All roles" && empty.state().count === "0" && empty.state().badgeHidden);
empty.done.emit("click");
check("Show These Roles preserves empty all-role selection", empty.state().hidden === "" && empty.changes()[0].detail.roles.length === 0);

const cleared = fixture();
cleared.restore("B,C");
check("multiple restored roles remain selected and labelled", cleared.state().selected === "B,C" && cleared.state().label === "Role B, Role C");
cleared.restore("");
check("clear resets hidden, checkboxes, count and label", cleared.state().hidden === "" && cleared.state().selected === "" && cleared.state().label === "All roles" && cleared.state().count === "0");
cleared.restore("");
check("clear and repeated sync never emit roles:change", cleared.changes().length === 0);
cleared.done.emit("click");
check("accepting after clear cannot resurrect old selection", cleared.state().hidden === "" && cleared.state().selected === "");

const wizard = fixture(true);
check("wizard has no hidden-input synchronization listener", wizard.syncListeners() === 0);
wizard.document.dispatchEvent({type: "roles:sync"});
check("irrelevant sync preserves named wizard checkboxes", wizard.state().selected === "A");
wizard.tiles[0].input.checked = false;
wizard.tiles[1].input.checked = true;
wizard.list.emit("change", {target: wizard.tiles[1].input});
check("wizard checkbox changes still update the counter", wizard.state().selected === "B" && wizard.state().count === "1");
check("wizard cannot emit feed role-change loops", wizard.changes().length === 0);
console.log(JSON.stringify({passed, count: passed.length}));
"""

driver = DRIVER.replace("__SOURCE__", json.dumps((ROOT / "static/rolepick.js").read_text(encoding="utf-8")))
result = subprocess.run([NODE, "-"], input=driver, text=True, encoding="utf-8", capture_output=True)
if result.returncode:
    print(result.stderr or result.stdout)
    raise SystemExit(result.returncode)
receipt = json.loads(result.stdout)
for name in receipt["passed"]:
    print("  ok  " + name)
print("%d role picker checks passed" % receipt["count"])
