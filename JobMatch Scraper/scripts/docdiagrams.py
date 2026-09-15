#!/usr/bin/env python3
"""
docdiagrams.py: the four diagrams, described once and rendered twice.

Imported by scripts/build_docs.py. Nothing here reads the filesystem except through the facts
dict it is handed, so the renderers stay testable and the extraction stays in one place.

WHY TWO RENDERERS FOR ONE DESCRIPTION. Mermaid renders free wherever markdown is read -- GitHub,
VS Code -- and diffs as text, so it belongs in ARCHITECTURE.md next to the prose. But it needs a
mermaid-aware viewer, and it will not give you a struck-through dead-end arrow or a funnel. SVG
gives total control and works offline in any browser by double-click, which is what a handover
artifact has to do. Neither is a superset of the other, so both are emitted from the same
structural data and cannot disagree.

WHY THE NUMBERS ARE NOT TYPED IN. docs/ARCHITECTURE.md described a Streamlit app reading
jobs.csv for three months. Every count, label and line number in these diagrams is extracted
from the source by build_docs.py, so the failure mode that produced that document is not
available. The funnel's gate labels in particular are the literal keys of the tally dict in
scraper/__init__.py, which is also what the run prints -- so the picture is a decoder ring for
the log, and the two cannot drift.

THE ONE TRICK. Every SVG shape carries BOTH a CSS class and a literal presentation attribute:

    <rect class="s-web" fill="#e0e4fe" stroke="#4f46e5"/>

Presentation attributes are the lowest-priority style source in CSS. Inlined into map.html,
doc.css wins, so the diagram themes, follows dark mode, and prints. Referenced as a bare
<img src="docs/img/surfaces.svg"> from README.md on GitHub, the stylesheet is stripped and the
literal hexes still render correctly. One artifact, two contexts, no duplicate maintenance.
"""

# Literal fallbacks, kept in step with docs/doc.css by scripts/test_doc_contrast.py. These are
# what a bare .svg shows when doc.css is not loaded.
HEX = {
    "web":    ("#e0e4fe", "#4f46e5", "#3730a3"),
    "sched":  ("#cfe8e3", "#0f766e", "#0b5c56"),
    "client": ("#e4e7ec", "#5f6573", "#3c414d"),
    "trap":   ("#f9edcd", "#a8781a", "#8a5a08"),
    "plain":  ("#ffffff", "#d3d7df", "#101319"),
}
INK = "#101319"
DIM = "#4e5462"
DEAD = "#b3261e"
LINE = "#d3d7df"

GLYPH = {"web": "▣ request", "sched": "◷ scheduled", "client": "◻ browser",
         "trap": "⚠ trap", "plain": ""}

MONO = ('"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace')
SANS = ('"InterVariable",Inter,-apple-system,"Segoe UI",Roboto,sans-serif')


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


class Canvas:
    """A very small SVG builder: rounded boxes, wrapped text, elbow arrows. No dependencies."""

    def __init__(self, w, h, title):
        self.w, self.h, self.title = w, h, title
        self.parts = []

    def box(self, x, y, w, h, kind, head, lines=(), mono_from=0, dashed=False):
        fill, stroke, ink = HEX[kind]
        # Grow rather than clip. A box with a glyph line, a heading and three detail lines needs
        # 22 + 14 + 19 + 3*16 + 10 = 113px; passing a smaller h used to cut the descenders off
        # the last line, which is invisible in the source and obvious on screen.
        need = 22 + (14 if GLYPH[kind] else 0) + (19 if head else 0) + len(lines) * 16 + 8
        h = max(h, need)
        self.parts.append(
            '<rect class="s-%s" x="%d" y="%d" width="%d" height="%d" rx="10" '
            'fill="%s" stroke="%s" stroke-width="1.5"%s/>'
            % (kind, x, y, w, h, fill, stroke,
               ' stroke-dasharray="6 4"' if dashed else ""))
        ty = y + 22
        if GLYPH[kind]:
            self.parts.append(
                '<text class="t-%s" x="%d" y="%d" font-family=%s font-size="10.5" '
                'letter-spacing="0.08em" fill="%s">%s</text>'
                % (kind, x + 14, ty - 6, '"%s"' % MONO, ink, esc(GLYPH[kind].upper())))
            ty += 14
        if head:
            self.parts.append(
                '<text class="t-ink" x="%d" y="%d" font-family=%s font-size="14.5" '
                'font-weight="600" fill="%s">%s</text>'
                % (x + 14, ty, '"%s"' % SANS, INK, esc(head)))
            ty += 19
        for i, ln in enumerate(lines):
            m = i >= mono_from
            self.parts.append(
                '<text class="t-dim" x="%d" y="%d" font-family=%s font-size="%s" fill="%s">%s'
                '</text>' % (x + 14, ty, '"%s"' % (MONO if m else SANS),
                             "11.5" if m else "12.5", DIM, esc(ln)))
            ty += 16
        return (x + w // 2, y + h)

    def text(self, x, y, s, size=12.5, cls="t-dim", colour=None, mono=False,
             anchor="start", weight="400"):
        self.parts.append(
            '<text class="%s" x="%d" y="%d" font-family=%s font-size="%s" fill="%s" '
            'text-anchor="%s" font-weight="%s">%s</text>'
            % (cls, x, y, '"%s"' % (MONO if mono else SANS), size, colour or DIM,
               anchor, weight, esc(s)))

    def arrow(self, x1, y1, x2, y2, dead=False, dashed=False):
        c = DEAD if dead else LINE
        cls = "edge-dead" if dead else "edge"
        d = ("M%d %d L%d %d" % (x1, y1, x2, y2) if x1 == x2 or y1 == y2 else
             "M%d %d L%d %d L%d %d" % (x1, y1, x1, (y1 + y2) // 2, x2, (y1 + y2) // 2))
        if not (x1 == x2 or y1 == y2):
            d += " L%d %d" % (x2, y2)
        self.parts.append('<path class="%s" d="%s" stroke="%s" stroke-width="1.5" fill="none"%s/>'
                          % (cls, d, c, ' stroke-dasharray="5 4"' if dashed else ""))
        # arrowhead, pointing whichever way the last segment ran
        if y2 != y1:
            up = -1 if y2 < y1 else 1
            self.parts.append('<path class="arrowhead" d="M%d %d L%d %d L%d %d Z" fill="%s"/>'
                              % (x2 - 4, y2 - 6 * up, x2 + 4, y2 - 6 * up, x2, y2, c))
        else:
            rt = 1 if x2 > x1 else -1
            self.parts.append('<path class="arrowhead" d="M%d %d L%d %d L%d %d Z" fill="%s"/>'
                              % (x2 - 6 * rt, y2 - 4, x2 - 6 * rt, y2 + 4, x2, y2, c))

    def cross(self, x, y):
        for dx, dy in ((-7, -7, ), (-7, 7)):
            self.parts.append(
                '<path class="edge-dead" d="M%d %d L%d %d" stroke="%s" stroke-width="2.5"/>'
                % (x + dx, y + dy, x - dx, y - dy, DEAD))

    def rule(self, x1, y, x2):
        self.parts.append('<path class="edge" d="M%d %d L%d %d" stroke="%s" stroke-width="1"/>'
                          % (x1, y, x2, y, LINE))

    def render(self):
        return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" '
                'height="%d" role="img" aria-label="%s">\n<title>%s</title>\n%s\n</svg>'
                % (self.w, self.h, self.w, self.h, esc(self.title), esc(self.title),
                   "\n".join(self.parts)))


# =============================================================================================
# 1. THE THREE SURFACES  -- what are the moving parts, and what do they all share?
# =============================================================================================
def surfaces(f):
    # Declared up here because the canvas HEIGHT is derived from it. Fixed at 560, the closing
    # line's baseline sat at 562 and the viewBox clipped it -- a sentence that is present in the
    # source, absent on screen, and reported by nothing.
    rows = [("PG_DSN set", "pgrest.Session -- direct psycopg", "this is the cPanel app", "sched"),
            ("DB_PROXY_URL + DB_PROXY_SECRET", "dbproxy.Session -- HMAC over HTTPS",
             "GitHub Actions, and your laptop", "sched"),
            ("only one of that pair", "RuntimeError", "refuses rather than guessing", "trap"),
            ("neither", "the local CSV fallback", "a laptop with no credentials", "client")]
    c = Canvas(980, 418 + len(rows) * 36 + 14,
               "The three runtime surfaces and the shared spine")
    cols = [(0, "web", "FLASK APP", [
                "Serves every page and API",
                "web.py  %s lines" % f["web_lines"],
                "%s routes / %s handlers" % (f["routes"], f["handlers"]),
                "NO blueprints -- one module",
                "templates/  %s files" % f["templates"]], 1),
            (330, "sched", "SCRAPE PIPELINE", [
                "Runs on a runner or a cron",
                "python -m scraper",
                "__init__.py  %s lines" % f["scraper_lines"],
                "%s ATS adapters, %s boards" % (f["adapters"], f["sources"]),
                "score_jobs.py  %s lines" % f["score_lines"]], 1),
            (660, "client", "CHROME EXTENSION", [
                "Runs in your own browser",
                "extension/  %s files" % f["ext_files"],
                "%s /api/ext/* routes" % f["ext_routes"],
                "bearer token, not a cookie",
                "fills forms, never submits"], 1)]
    for x, kind, head, lines, mf in cols:
        c.box(x, 44, 300, 150, kind, head, lines, mono_from=mf)
        c.arrow(x + 150, 194, 490, 232)
    c.text(0, 26, "Three places code runs. Colour means which.", 13, colour=DIM)

    c.box(0, 246, 960, 104, "plain", "THE SPINE  --  imported by all three", [
        "core.py  %s lines, %s sections  --  filtering, scoring, sponsorship, visa tags,"
        % (f["core_lines"], f["core_sections"]),
        "   location, salary, role tracks, saved-search prefs, work-auth timelines",
        "db.py  %s lines  --  one interface, two transports + a CSV fallback"
        % (f["db_lines"]),
    ], mono_from=0)
    c.arrow(480, 350, 480, 386)

    c.text(0, 380, "Which database you get  --  db.py::_LazyHTTP, line %s" % f["lazyhttp_line"],
           12.5, colour=INK, weight="600")
    y = 400
    for cond, got, why, kind in rows:
        _f, stroke, ink = HEX[kind]
        c.parts.append('<rect class="s-%s" x="0" y="%d" width="960" height="30" rx="6" '
                       'fill="%s" stroke="%s" stroke-width="1"/>'
                       % (kind, y, HEX[kind][0], stroke))
        c.text(14, y + 20, cond, 12, mono=True, colour=ink, weight="600")
        c.text(330, y + 20, "->  " + got, 12, mono=True, colour=INK)
        c.text(660, y + 20, why, 12, colour=DIM)
        y += 36
    c.text(0, y + 18, "has_remote_db() answers True for the first two. backend_name() is the "
           "one that tells you which. The Supabase default was deleted 2026-09-01.", 12.5,
           colour=HEX["trap"][2], weight="600")
    return c.render()


def surfaces_mermaid(f):
    return "\n".join([
        "flowchart TB",
        '  subgraph REQ["&#9635; request-scoped"]',
        '    W["<b>web.py</b><br/>%s lines · %s routes / %s handlers<br/>no blueprints"]'
        % (f["web_lines"], f["routes"], f["handlers"]),
        '    T["templates/ · %s files"]' % f["templates"],
        "  end",
        '  subgraph SCH["&#9719; scheduled"]',
        '    S["<b>scraper/__init__.py</b><br/>%s lines · %s ATS adapters<br/>%s boards"]'
        % (f["scraper_lines"], f["adapters"], f["sources"]),
        '    J["score_jobs.py · %s lines"]' % f["score_lines"],
        "  end",
        '  subgraph CLI["&#9723; browser"]',
        '    E["<b>extension/</b><br/>%s files · %s /api/ext/* routes"]'
        % (f["ext_files"], f["ext_routes"]),
        '    A["static/app.js<br/>the client feed"]',
        "  end",
        '  SPINE["<b>THE SPINE</b> — imported by all three<br/>'
        'core.py · %s lines · %s sections<br/>db.py · %s lines · two transports + CSV"]'
        % (f["core_lines"], f["core_sections"], f["db_lines"]),
        "  REQ --> SPINE",
        "  SCH --> SPINE",
        "  CLI --> SPINE",
        '  SPINE --> D{"db.py::_LazyHTTP<br/>line %s"}' % f["lazyhttp_line"],
        '  D -->|"PG_DSN"| P["pgrest — direct psycopg<br/><i>the cPanel app</i>"]',
        '  D -->|"DB_PROXY_URL + SECRET"| X["dbproxy — HMAC HTTPS<br/><i>Actions, your laptop</i>"]',
        '  D -->|"half a pair"| R["RuntimeError<br/><i>refuses rather than guessing</i>"]',
        '  D -->|"neither"| V["local CSV<br/><i>no credentials: the laptop fallback</i>"]',
        "  classDef web fill:#e0e4fe,stroke:#4f46e5,color:#101319",
        "  classDef sched fill:#cfe8e3,stroke:#0f766e,color:#101319",
        "  classDef client fill:#e4e7ec,stroke:#5f6573,color:#101319",
        "  classDef trap fill:#f9edcd,stroke:#a8781a,color:#101319",
        "  classDef plain fill:#ffffff,stroke:#d3d7df,color:#101319",
        "  class W,T web", "  class S,J sched", "  class E,A client",
        "  class R trap", "  class SPINE,D,P,X,V plain",
    ])


# =============================================================================================
# 2. THE FUNNEL  -- this job exists on the board but is not in my feed. Where did it die?
# =============================================================================================
def funnel(f):
    gates = f["gates"]
    h = 262 + len(gates) * 40 + 190
    c = Canvas(980, h, "Where a scraped posting gets dropped, and what the run prints")
    c.text(0, 22, "Every gate the intake loop applies, in order. The label on each chute is the "
           "exact string the run prints.", 13, colour=DIM)
    c.box(180, 40, 460, 86, "sched", "%s boards -> scrape_all" % f["sources"], [
        "%s ATS adapters, thread pool, per-host caps" % f["adapters"],
        "then fill_missing_jds() buys descriptions BEFORE the gates,",
        "so a JD-rescued row takes the same path as a title match"], mono_from=3)
    c.arrow(410, 126, 410, 162)

    y = 162
    wide, narrow = 460, 300
    for i, (label, line) in enumerate(gates):
        w = int(wide - (wide - narrow) * (i / max(1, len(gates) - 1)))
        x = 410 - w // 2
        c.parts.append('<rect class="s-plain" x="%d" y="%d" width="%d" height="30" rx="6" '
                       'fill="#ffffff" stroke="%s" stroke-width="1.3"/>' % (x, y, w, LINE))
        c.text(410, y + 20, label if len(label) < 46 else label[:44] + "…", 12,
               colour=INK, anchor="middle")
        c.arrow(x + w, y + 15, 690, y + 15)
        c.text(700, y + 12, "dropped", 10, mono=True, colour=HEX["trap"][2])
        c.text(700, y + 24, "scraper/__init__.py:%s" % line, 10, mono=True, colour=DIM)
        y += 40

    c.arrow(410, y, 410, y + 34)
    c.box(150, y + 34, 520, 82, "sched", "kept", [
        "written to last_new_jobs.json BEFORE any database call, so a DB",
        "hiccup cannot lose the scrape -- then insert, listing JDs, prune,",
        "liveness reconcile, board health, status handoff"], mono_from=3)
    # Full width and BELOW the funnel. An earlier version put this in the right-hand gutter at
    # the same coordinates as the first two chute labels and drew straight over them.
    ny = y + 140
    c.rule(0, ny, 960)
    c.text(0, ny + 22, "A job whose TITLE matched nothing can still be admitted on what its "
           "DESCRIPTION says.", 12.5, colour=HEX["sched"][2], weight="600")
    c.text(0, ny + 40, "That is a keep, not a drop, so it has no chute here -- and because the "
           "descriptions are bought before the gates,", 12.5, colour=DIM)
    c.text(0, ny + 56, "a rescued row takes exactly the same path as a title match rather than a "
           "special one.", 12.5, colour=DIM)
    return c.render()


def funnel_mermaid(f):
    L = ["flowchart TB",
         '  SRC["%s boards → scrape_all<br/>%s ATS adapters"]' % (f["sources"], f["adapters"]),
         '  JD["fill_missing_jds()<br/><i>descriptions bought before the gates</i>"]',
         "  SRC --> JD"]
    prev = "JD"
    for i, (label, line) in enumerate(f["gates"]):
        node = "G%d" % i
        drop = "D%d" % i
        L.append('  %s{"%s"}' % (node, label.replace('"', "'")))
        L.append('  %s["%s<br/><i>:%s</i>"]' % (drop, label.replace('"', "'"), line))
        L.append("  %s --> %s" % (prev, node))
        L.append("  %s -->|dropped| %s" % (node, drop))
        L.append("  class %s trap" % drop)
        prev = node
    L += ['  KEEP["<b>kept</b><br/>last_new_jobs.json, then insert"]',
          "  %s -->|survives| KEEP" % prev,
          '  RESCUE["admitted on DESCRIPTION alone<br/><i>a keep, not a drop</i>"]',
          "  JD -.-> RESCUE", "  RESCUE -.-> KEEP",
          "  classDef trap fill:#f9edcd,stroke:#a8781a,color:#101319",
          "  classDef sched fill:#cfe8e3,stroke:#0f766e,color:#101319",
          "  class SRC,JD,KEEP,RESCUE sched"]
    return "\n".join(L)


# =============================================================================================
# 3. DEPLOY REALITY  -- I pushed. Why is the site unchanged?
# =============================================================================================
def deploy(f):
    # Height is DERIVED from the schedule, not typed: the rows below grew from three to seven
    # and a fixed 520 drew the closing note off the bottom of the canvas, where it is invisible
    # in the source and simply missing on screen.
    h = 328 + len(f["schedule"]) * 40 + 62
    c = Canvas(980, h, "How this app actually deploys, and the path that looks like it does")
    c.text(0, 20, "THE PATH THAT LOOKS LIKE A DEPLOY AND IS NOT", 12, mono=True,
           colour=DEAD, weight="600")
    c.box(0, 32, 190, 56, "plain", "your laptop", ["git push"], mono_from=0)
    c.arrow(190, 60, 300, 60)
    c.box(300, 32, 170, 56, "plain", "GitHub", ["origin"], mono_from=0)
    c.arrow(470, 60, 600, 60, dead=True, dashed=True)
    c.cross(535, 60)
    c.box(600, 32, 240, 56, "trap", ".cpanel.yml", ["never runs"], mono_from=0)
    c.text(0, 108, "cPanel executes .cpanel.yml only for a repository HOSTED ON CPANEL, and this "
           "origin is GitHub.", 12.5, colour=DEAD)
    c.text(0, 124, "Nothing on the server ever hears about a push. Verified 2026-08-09 by "
           "pushing a release and polling the served CSS.", 12.5, colour=DIM)
    c.rule(0, 146, 960)

    c.text(0, 172, "THE PATH THAT IS THE DEPLOY", 12, mono=True, colour=HEX["sched"][2],
           weight="600")
    # FIVE steps, not four. A restart empties every per-process cache, so without the last
    # one the first visitor after a deploy rebuilds the corpus, the rows and the 29 MB IDF
    # table on their own request -- measured at several seconds. It is part of the deploy.
    steps = [("build_deploy_zip.py", ["%s modules + %s dirs" % (f["zip_files"], f["zip_dirs"])]),
             ("stemjobs1_deploy.zip", ["flat archive"]),
             ("upload + extract", ["cPanel File Manager"]),
             ("touch tmp/restart.txt", ["Passenger reloads"]),
             ("curl /warm?t=...", ["else visitor 1 pays"])]
    x = 0
    for i, (head, lines) in enumerate(steps):
        w = 184
        c.box(x, 186, w, 62, "sched", None, [head] + lines, mono_from=0)
        if i < len(steps) - 1:
            c.arrow(x + w, 217, x + w + 10, 217)
        x += w + 10
    c.text(0, 268, "The script refuses to build when its own file list and .cpanel.yml disagree "
           "about a module web.py imports.", 12.5, colour=DIM)
    c.rule(0, 288, 960)

    c.text(0, 314, "AND THE PIPELINE RUNS ITSELF ON A SCHEDULE, ACROSS TWO RUNNERS", 12,
           mono=True, colour=INK, weight="600")
    y = 328
    for when, runner, what, kind in f["schedule"]:
        _fl, stroke, ink = HEX[kind]
        c.parts.append('<rect class="s-%s" x="0" y="%d" width="960" height="34" rx="6" '
                       'fill="%s" stroke="%s" stroke-width="1"/>' % (kind, y, HEX[kind][0],
                                                                     stroke))
        c.text(14, y + 22, when, 12, mono=True, colour=ink, weight="600")
        c.text(120, y + 22, runner, 12, mono=True, colour=INK)
        c.text(400, y + 22, what, 12, colour=DIM)
        y += 40
    c.text(0, y + 18, "The cron rows live in the live crontab and nowhere else. The repo "
           "cannot enforce them, so they are", 12.5, colour=HEX["trap"][2])
    c.text(0, y + 34, "recorded in the header of bin/cron_scrape.sh -- and a scheduled Actions "
           "event is not a guarantee, which is what the watchdog is for.", 12.5,
           colour=HEX["trap"][2])
    return c.render()


def deploy_mermaid(f):
    L = ["flowchart LR",
         '  subgraph BAD["&#9888; looks like a deploy, is not"]',
         '    L["your laptop<br/>git push"] --> G["GitHub<br/><i>origin</i>"]',
         '    G -.->|"NEVER RUNS"| C[".cpanel.yml"]',
         "  end",
         '  subgraph GOOD["&#9635; the actual deploy"]',
         '    B["build_deploy_zip.py<br/>%s modules + %s dirs"] --> Z["stemjobs1_deploy.zip"]'
         % (f["zip_files"], f["zip_dirs"]),
         '    Z --> U["File Manager<br/>upload + extract"]',
         '    U --> T["touch tmp/restart.txt"] --> W["curl /warm?t=…<br/>'
         '<i>or the first visitor rebuilds every cache</i>"] --> LIVE'
         '["stemjobs1.astrochakra.co"]',
         "  end",
         "  classDef trap fill:#f9edcd,stroke:#a8781a,color:#101319",
         "  classDef sched fill:#cfe8e3,stroke:#0f766e,color:#101319",
         "  classDef plain fill:#ffffff,stroke:#d3d7df,color:#101319",
         "  class C trap", "  class B,Z,U,T,W,LIVE sched", "  class L,G plain"]
    return "\n".join(L)


# =============================================================================================
# 4. THE TRIPLET  -- I changed how the feed filters. What else must change?
# =============================================================================================
def triplet(f):
    c = Canvas(980, 380, "The three implementations of 'does this job match this search'")
    c.text(0, 22, "One question, three answers that must agree.", 13, colour=DIM)
    c.box(0, 40, 380, 92, "web", "the server feed", [
        "web.py::_filter_rows", "line %s" % f["filter_rows_line"],
        "docstring: \"Server-side mirror of app.js\""], mono_from=1)
    c.box(580, 40, 380, 92, "client", "the client feed", [
        "static/app.js::matches()", "line %s" % f["matches_line"],
        "runs in the visitor's browser"], mono_from=1)
    c.arrow(380, 86, 575, 86)
    c.text(480, 70, "%s = %s" % (f["inline_max_name"], f["inline_max"]), 11.5, mono=True,
           colour=INK, anchor="middle", weight="600")
    c.text(480, 108, "at or below -> the BROWSER filters", 11, colour=DIM, anchor="middle")
    c.text(480, 122, "above -> the SERVER filters", 11, colour=DIM, anchor="middle")

    c.box(230, 176, 500, 62, "trap", None, [
        "The only thing keeping these two in step:  scripts/feed_parity.py",
        "It lifts the JS functions out of app.js BY SOURCE TEXT and runs them in node,",
        "so a copy cannot drift from what ships. A helper must stay a top-level function."],
        mono_from=0)
    c.arrow(190, 132, 300, 176)
    c.arrow(770, 132, 660, 176)

    c.arrow(480, 238, 480, 274, dashed=True)
    c.box(230, 274, 500, 78, "sched", "the email digest", [
        "core.py::prefs_match  --  line %s" % f["prefs_match_line"],
        "Shares the filters, not the whole thing: it skips \"posted within\",",
        "because every candidate in a digest is new. _filter_rows stays the authority."],
        mono_from=1)
    return c.render()


def triplet_mermaid(f):
    return "\n".join([
        "flowchart TB",
        '  S["<b>the server feed</b><br/>web.py::_filter_rows<br/><i>line %s</i>"]'
        % f["filter_rows_line"],
        '  C["<b>the client feed</b><br/>static/app.js::matches()<br/><i>line %s</i>"]'
        % f["matches_line"],
        '  S <-->|"%s = %s<br/>below → browser filters<br/>above → server filters"| C'
        % (f["inline_max_name"], f["inline_max"]),
        '  GUARD["&#128274; scripts/feed_parity.py<br/><i>lifts the JS by source text and runs '
        'it in node<br/>— the only thing keeping these two in step</i>"]',
        "  S --- GUARD", "  C --- GUARD",
        '  D["<b>the email digest</b><br/>core.py::prefs_match<br/><i>line %s — shares the '
        'filters, skips \\"posted within\\"</i>"]' % f["prefs_match_line"],
        "  GUARD -.-> D",
        "  classDef web fill:#e0e4fe,stroke:#4f46e5,color:#101319",
        "  classDef client fill:#e4e7ec,stroke:#5f6573,color:#101319",
        "  classDef sched fill:#cfe8e3,stroke:#0f766e,color:#101319",
        "  classDef trap fill:#f9edcd,stroke:#a8781a,color:#101319",
        "  class S web", "  class C client", "  class D sched", "  class GUARD trap",
    ])


DIAGRAMS = (
    ("surfaces", "The three surfaces", surfaces, surfaces_mermaid,
     "What the moving parts are, and what all three of them share."),
    ("funnel", "The intake funnel", funnel, funnel_mermaid,
     "Where a scraped posting gets dropped — and the label on each chute is the exact string "
     "the run prints, so this doubles as a decoder ring for the log."),
    ("deploy", "Deploy reality", deploy, deploy_mermaid,
     "Why pushing to GitHub changes nothing, and what does."),
    ("triplet", "The filter triplet", triplet, triplet_mermaid,
     "The highest-consequence, lowest-visibility coupling in the codebase."),
)
