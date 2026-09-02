"""EVERY TEMPLATE MUST PARSE, AND EVERY JINJA BLOCK MUST BE BALANCED.

This exists because of a specific mistake, made during the 2026-09-02 copy pass and caught by
hand rather than by CI. Shortening a paragraph in brain_home.html replaced a run of lines, and
the first line of that run happened to also carry the tail of an `{% if %}` chain:

    in your own feed{% else %}, from a curated skill list...{% endif %}. A keyword only

The prose was the target; the `{% else %}...{% endif %}` went with it, and the page then failed
with "Encountered unknown tag 'endblock'". Nothing in the suite noticed: no test renders
brain_home, and the pages that ARE rendered elsewhere were untouched.

A template that does not parse is a 500 on a real page, so the cheapest possible guard is worth
having. This is a syntax check, not a render: it needs no database, no session and no fixtures,
which is what lets it cover ALL of them rather than the handful with page tests.

    python test_templates_parse.py
"""
import glob
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import jinja2

HERE = os.path.dirname(os.path.abspath(__file__))
TPL = os.path.join(HERE, "templates")

# The real environment's settings, because a template can parse under one and not another --
# autoescape changes nothing here but line_statement/comment prefixes would.
env = jinja2.Environment(loader=jinja2.FileSystemLoader(TPL), autoescape=True)

files = sorted(glob.glob(os.path.join(TPL, "*.html")))
if not files:
    print("FAIL: no templates found at %s" % TPL)
    sys.exit(1)

bad = []
for path in files:
    name = os.path.basename(path)
    try:
        env.parse(io.open(path, encoding="utf-8").read(), name=name, filename=path)
    except jinja2.TemplateSyntaxError as e:
        bad.append((name, "line %s: %s" % (e.lineno, e.message)))
    except Exception as e:                       # a decode error is just as fatal to the page
        bad.append((name, "%s: %s" % (type(e).__name__, str(e)[:90])))

# ...and the includes/extends they name must exist. A renamed partial is the other way a page
# 500s without any test noticing, and get_or_select_template resolves it without rendering.
missing = []
for path in files:
    name = os.path.basename(path)
    try:
        src = io.open(path, encoding="utf-8").read()
        for ref in jinja2.meta.find_referenced_templates(env.parse(src, name=name)):
            if ref and not os.path.exists(os.path.join(TPL, ref)):
                missing.append((name, ref))
    except Exception:
        pass                                     # already reported by the parse pass above

print("parsed %d template(s)" % len(files))
if bad:
    print("\nFAIL - %d template(s) do not parse:" % len(bad))
    for n, why in bad:
        print("  %-30s %s" % (n, why))
if missing:
    print("\nFAIL - %d missing include/extends target(s):" % len(missing))
    for n, ref in missing:
        print("  %-30s references %s" % (n, ref))
if bad or missing:
    sys.exit(1)
print("ok - every template parses and every include resolves")
