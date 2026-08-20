#!/usr/bin/env python3
"""
test_doc_contrast.py: hold docs/doc.css to the same contrast bar as the app.

The app's palette is contrast-tested in CI by scripts/test_contrast.py -- 4.5:1 for text, 3:1
for anything whose boundary carries meaning. The doc layer introduces new colours, so it owes
the same floor, in both themes. A diagram nobody can read is not documentation.

IT IMPORTS test_contrast's helpers RATHER THAN COPYING THEM. That is the feed_parity principle:
run the same code, byte for byte, so a divergence cannot hide behind a copy. If the app's
luminance maths is ever corrected, this file inherits the correction for free.

Two places where the doc layer diverges from the app's numbers ON PURPOSE, both noted below:
the doc BORDERS are much stronger than the app's route borders, because in a diagram the border
IS the shape and therefore owes the 3:1 non-text minimum; and the fills are deliberately kept
inside the app's own tint band so a doc panel reads no louder than a job card.

    python scripts/test_doc_contrast.py
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from test_contrast import block, tokens, resolve, ratio      # noqa: E402  (see docstring)

CSS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "doc.css")

TEXT_MIN = 4.5      # WCAG AA for body text
UI_MIN = 3.0        # WCAG AA for a boundary that carries meaning

# (foreground, background, floor, what it is)
PAIRS = (
    # Panel label on its own panel. The whole map is unreadable if any of these fail.
    ("--doc-web-ink", "--doc-web-fill", TEXT_MIN, "request-scoped label on its panel"),
    ("--doc-sched-ink", "--doc-sched-fill", TEXT_MIN, "scheduled label on its panel"),
    ("--doc-client-ink", "--doc-client-fill", TEXT_MIN, "browser label on its panel"),
    ("--doc-trap-ink", "--doc-trap-fill", TEXT_MIN, "trap text on the amber callout"),
    # Body ink on a panel: diagrams put plain text inside coloured boxes.
    ("--text-primary", "--doc-web-fill", TEXT_MIN, "body ink on a request panel"),
    ("--text-primary", "--doc-sched-fill", TEXT_MIN, "body ink on a scheduled panel"),
    ("--text-primary", "--doc-client-fill", TEXT_MIN, "body ink on a browser panel"),
    ("--text-primary", "--doc-trap-fill", TEXT_MIN, "body ink on a trap callout"),
    # This pair is here because it FAILED. doc.css had no dark --doc-plain-fill, so the SVG's
    # presentation attribute kept those boxes white while --text-primary followed the theme to
    # near-white: the funnel's slabs and the spine bar were unreadable in dark mode. Caught by
    # looking at the rendered page, which is why this file is not the only check that matters.
    ("--text-primary", "--doc-plain-fill", TEXT_MIN, "body ink on an uncoloured box"),
    ("--text-secondary", "--doc-plain-fill", TEXT_MIN, "detail text on an uncoloured box"),
    # The one red, as text on the page.
    ("--doc-dead", "--bg-canvas", TEXT_MIN, "the dead-path red on canvas"),
    # Borders. In a diagram the border IS the shape, so it owes the non-text minimum -- this is
    # the deliberate divergence from the app, whose route borders sit near 1.6:1 next to a
    # filled, text-bearing surface and do not carry the shape on their own.
    ("--doc-web-line", "--bg-canvas", UI_MIN, "request panel border on canvas"),
    ("--doc-sched-line", "--bg-canvas", UI_MIN, "scheduled panel border on canvas"),
    ("--doc-client-line", "--bg-canvas", UI_MIN, "browser panel border on canvas"),
    ("--doc-trap-line", "--bg-canvas", UI_MIN, "trap border on canvas"),
    ("--doc-plain-line", "--bg-canvas", UI_MIN, "uncoloured box border on canvas"),
    # Ordinary page text.
    ("--text-primary", "--bg-canvas", TEXT_MIN, "body text on canvas"),
    ("--text-secondary", "--bg-canvas", TEXT_MIN, "secondary text on canvas"),
    ("--text-secondary", "--bg-surface", TEXT_MIN, "caption text on a figure"),
    ("--doc-web-ink", "--bg-surface", TEXT_MIN, "link colour on a figure"),
)

# Fills must stay a TINT, not a block of colour: a doc panel should read no louder than a job
# card does. Measured band of the app's own route fills against canvas is 1.04-1.23:1, so the
# doc fills are allowed a little more and no more.
FILL_MAX = 1.45


def main():
    css = io.open(CSS, encoding="utf-8").read()
    light = tokens(block(css, ":root"))
    dark = dict(light)
    dark.update(tokens(block(css, '[data-theme="dark"]')))

    fails, checked = [], 0
    for theme, table in (("light", light), ("dark", dark)):
        print("\n%s" % theme.upper())
        for fg, bg, floor, what in PAIRS:
            a, b = resolve(fg, table), resolve(bg, table)
            if not a or not b:
                fails.append("%s: %s or %s does not resolve to a colour" % (theme, fg, bg))
                print("  FAIL %-44s unresolved" % what)
                continue
            r = ratio(a, b)
            checked += 1
            ok = r >= floor
            if not ok:
                fails.append("%s: %s on %s is %.2f, needs %.1f (%s)"
                             % (theme, fg, bg, r, floor, what))
            print("  %s %-44s %5.2f  (min %.1f)" % ("ok  " if ok else "FAIL", what, r, floor))

        for name in ("--doc-web-fill", "--doc-sched-fill", "--doc-client-fill",
                     "--doc-trap-fill"):
            a, b = resolve(name, table), resolve("--bg-canvas", table)
            if not a or not b:
                continue
            r = ratio(a, b)
            checked += 1
            ok = r <= FILL_MAX
            if not ok:
                fails.append("%s: %s is %.2f against canvas, louder than the %.2f tint ceiling"
                             % (theme, name, r, FILL_MAX))
            print("  %s %-44s %5.2f  (max %.2f)"
                  % ("ok  " if ok else "FAIL", name + " is a tint", r, FILL_MAX))

    # The doc layer must not steal a ROUTE IDENTITY hue -- the blue, green and violet that tell
    # H-1B from Green Card from STEM-OPT. If a doc panel wore the H-1B blue, a reader trained on
    # the product would mis-read the map, and the docs would contradict style.css's own rule.
    #
    # Deliberately NOT checked: the neutral greys, the warn amber and the danger red. Those are
    # shared vocabulary, not identity -- amber means "careful" and red means "this is dead or
    # destructive" in both places, and reusing them is the point. An earlier version of this
    # check compared against every --route-/--visa-/--match- token and flagged four colours that
    # resolve to plain --gray-200, --amber-700 and --red-700, which is a false positive: it was
    # measuring "is this hex used anywhere in the app" instead of "does this hex carry a meaning
    # I am about to overwrite".
    IDENTITY = ("--route-h1b-", "--route-gc-", "--route-stem-",
                "--visa-h1b-", "--visa-gc-", "--visa-stem-")
    app_css_path = os.path.join(os.path.dirname(CSS), os.pardir, "static", "style.css")
    if os.path.exists(app_css_path):
        app = io.open(app_css_path, encoding="utf-8", errors="replace").read()
        app_tokens = tokens(block(app, ":root"))
        identity = {}
        for k in app_tokens:
            if k.startswith(IDENTITY):
                v = (resolve(k, app_tokens) or "").lower()
                if v:
                    identity.setdefault(v, k)
        clash = []
        for k in sorted(light):
            if not k.startswith("--doc-"):
                continue
            v = (resolve(k, light) or "").lower()
            if v and v in identity:
                clash.append("%s (%s) is the app's %s" % (k, v, identity[v]))
        checked += 1
        if clash:
            fails.extend(clash)
            print("\n  FAIL doc layer steals a route-identity hue: %s" % "; ".join(clash))
        else:
            print("\n  ok   no doc colour steals a route-identity hue (%d app hues checked)"
                  % len(identity))

    print("\n%s  (%d checks)"
          % ("ALL DOC CONTRAST CHECKS PASS" if not fails
             else "%d FAILED: %s" % (len(fails), fails), checked))
    if fails:
        raise SystemExit(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
