"""WCAG contrast gate over the design tokens, in BOTH themes.

Why this exists: the token set carries two full themes, and a colour pair can only be
checked by eye on the one screen someone happened to look at. Every edit to style.css
silently risks the other theme. This asserts the pairs programmatically so a regression
fails a build instead of shipping.

Thresholds, and the distinction that matters:
  * TEXT on its background must clear 4.5:1 (WCAG 1.4.3 AA, normal-size text).
  * INTERACTIVE boundaries must clear 3:1 (WCAG 1.4.11 non-text contrast).
  * Decorative hairlines are EXEMPT. --border-subtle is a divider between rows of the
    same surface, not the boundary of a control. Forcing 3:1 on it would mean a visibly
    grey rule everywhere, which is the opposite of what the design calls for. It is
    listed under EXEMPT below rather than left out, so the exemption is a decision on
    the record rather than an omission.

No dependencies. Parses the two token blocks out of static/style.css and resolves
var() chains itself, so it runs anywhere Python does.

    python scripts/test_contrast.py        # exit 0 = every checked pair passes
"""
import io
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(ROOT, "static", "style.css")

TEXT_MIN = 4.5
UI_MIN = 3.0

# (text token, background token). Checked in both themes.
TEXT_PAIRS = [
    ("--text-primary", "--bg-canvas"),
    ("--text-primary", "--bg-surface"),
    ("--text-primary", "--bg-raised"),
    ("--text-primary", "--bg-sunken"),
    ("--text-secondary", "--bg-canvas"),
    ("--text-secondary", "--bg-surface"),
    ("--text-secondary", "--bg-sunken"),
    ("--text-tertiary", "--bg-surface"),
    ("--text-on-accent", "--accent"),
    ("--accent-text", "--accent-bg"),
    ("--status-success-text", "--status-success-bg"),
    ("--status-warn-text", "--status-warn-bg"),
    ("--status-danger-text", "--status-danger-bg"),
    ("--status-info-text", "--status-info-bg"),
    ("--visa-h1b-text", "--visa-h1b-bg"),
    ("--visa-gc-text", "--visa-gc-bg"),
    ("--visa-stem-text", "--visa-stem-bg"),
    ("--visa-blocked-text", "--visa-blocked-bg"),
    ("--visa-unknown-text", "--visa-unknown-bg"),
    ("--posting-agency-text", "--posting-agency-bg"),
    # THE MATCH METER. All three, against the opaque disc they are actually drawn on rather
    # than against --bg-surface in the abstract. --match-weak used to be exempt as a
    # "quantitative ramp end, never used as body text"; that stopped being true on 2026-08-11,
    # when the ramp became a traffic light and the arc colour started painting the percentage
    # INSIDE the ring. It is 10px type on a 46px disc, so it is text and it is small.
    ("--match-strong", "--match-pill"),
    ("--match-good", "--match-pill"),
    ("--match-weak", "--match-pill"),
    # APPLY, ON EVERY ROUTE. Five button backgrounds that carried a hardcoded color:#fff and
    # were therefore invisible to this file until --route-on-ink existed. 14px label on a
    # filled button, so it is held to the text minimum.
    ("--route-on-ink", "--route-h1b-ink"),
    ("--route-on-ink", "--route-gc-ink"),
    ("--route-on-ink", "--route-stem-ink"),
    ("--route-on-ink", "--route-blocked-ink"),
    ("--route-on-ink", "--route-none-ink"),
    # Keyword marks inside a description. Real body text, so the text minimum, and both now carry
    # a fill (green / red) rather than one being an underline on the page surface.
    ("--kw-have-text", "--kw-have-bg"),
    ("--kw-miss-text", "--kw-miss-bg"),
    # The tooltip is an INVERTED bubble: it paints --text-primary as its background and
    # --bg-surface as its text, so the pair has to be checked in that order or the one
    # surface in the app that reverses the ramp goes unchecked.
    # THE LOGO PLATE'S MONOGRAM. Roughly a third of /companies tiles have no harvestable brand
    # logo, so two ink letters on a neutral plate is a first-class state and not a fallback.
    # Both sides are mode-stable, declared in :root and deliberately NOT redeclared in dark,
    # because a harvested logo is drawn for paper and most are solid black, so the surface is a
    # property of the ARTWORK rather than of the theme. That is also why the ink cannot resolve
    # to --text-primary, which on dark is near-white and would vanish on a near-white plate.
    # 14px, so it is held to the text minimum.
    ("--logo-plate-ink", "--logo-plate"),
    ("--bg-surface", "--text-primary"),
]

# Boundaries of real controls: inputs, buttons, the focus ring's companion border.
UI_PAIRS = [
    ("--border-strong", "--bg-surface"),
    ("--border-strong", "--bg-canvas"),
    ("--accent", "--bg-surface"),
]

EXEMPT = {
    "--border-subtle": "row divider on one surface, not a control boundary",
    "--border-default": "field hairline, paired with a 3:1 --border-strong on focus",
    # Considered, not overlooked. This one is a CARD boundary, so the temptation is to hold it
    # to UI_MIN -- but a panel is not a control: you cannot focus it, click it or type into it,
    # and at 3:1 a page of them reads as a wireframe. It is held at 2.0:1 against the canvas by
    # its comment in style.css, which is a deliberate value rather than whatever grey was handy.
    "--border-card": "boundary of a panel/tile, not a control you can operate",
    "--logo-plate": "the surface an IMAGE sits on, not text and not a control you can "
                    "operate, so the same reasoning as --border-card. It stays lighter than "
                    "the card in dark mode because most brand marks are drawn in dark ink for "
                    "white paper and would otherwise vanish -- but it is no longer a near-white "
                    "#f1f3f6 block, which read as a sticker stuck onto the card rather than "
                    "part of it. Hugging the artwork was not enough on its own; the plate is "
                    "dimmed to the composite it would have had at 82% alpha, and kept opaque "
                    "so the ink pair below stays resolvable here.",
    "--logo-plate-edge": "inset rim on the logo plate, 1.30:1 against it in both themes",
    "--match-none": "empty-track fill, never used as text",
    "--match-track": "unfilled arc, never used as text",
}


def block(css, selector):
    """The declaration body for `selector`, or ''. Brace-counted, not regex-matched."""
    i = css.find(selector)
    if i < 0:
        return ""
    i = css.find("{", i)
    depth, j = 0, i
    while j < len(css):
        if css[j] == "{":
            depth += 1
        elif css[j] == "}":
            depth -= 1
            if depth == 0:
                return css[i + 1:j]
        j += 1
    return ""


def tokens(body):
    out = {}
    for name, val in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;}]+)", body, re.I):
        out[name] = val.strip()
    return out


def resolve(name, table, seen=None):
    """Follow var() chains to a literal. None if it never reaches a colour."""
    seen = seen or set()
    if name in seen or name not in table:
        return None
    seen.add(name)
    val = table[name]
    m = re.match(r"^var\(\s*(--[a-z0-9-]+)\s*\)$", val, re.I)
    if m:
        return resolve(m.group(1), table, seen)
    return val if val.startswith("#") else None


def rgb(hex_str):
    h = hex_str.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def luminance(c):
    def chan(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (chan(x) for x in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(fg, bg):
    a, b = luminance(rgb(fg)), luminance(rgb(bg))
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def main():
    css = io.open(CSS, encoding="utf-8").read()
    light = tokens(block(css, ":root"))
    dark = dict(light)
    dark.update(tokens(block(css, '[data-theme="dark"]')))

    fails, checked, skipped = [], 0, []
    for theme, table in (("light", light), ("dark", dark)):
        print("=" * 70)
        print("%s theme" % theme)
        print("=" * 70)
        for pairs, floor, kind in ((TEXT_PAIRS, TEXT_MIN, "text"), (UI_PAIRS, UI_MIN, "ui")):
            for fg_name, bg_name in pairs:
                fg, bg = resolve(fg_name, table), resolve(bg_name, table)
                if not fg or not bg:
                    skipped.append("%s: %s on %s" % (theme, fg_name, bg_name))
                    continue
                checked += 1
                r = ratio(fg, bg)
                ok = r >= floor
                if not ok:
                    fails.append("%s %s on %s = %.2f:1 (needs %.1f)"
                                 % (theme, fg_name, bg_name, r, floor))
                print("  %s %-24s on %-22s %5.2f:1  (%s min %.1f)"
                      % ("ok " if ok else "FAIL", fg_name, bg_name, r, kind, floor))
        print()

    print("=" * 70)
    print("exempt by decision")
    print("=" * 70)
    for name, why in sorted(EXEMPT.items()):
        print("  -   %-18s %s" % (name, why))

    print()
    if skipped:
        print("UNRESOLVED (token missing or not a literal colour):")
        for s in skipped:
            print("   ", s)
        print()
    if fails:
        print("CONTRAST FAILURES (%d):" % len(fails))
        for f in fails:
            print("   ", f)
        raise SystemExit(1)
    print("ALL %d CONTRAST PAIRS PASS in both themes." % checked)


if __name__ == "__main__":
    main()
