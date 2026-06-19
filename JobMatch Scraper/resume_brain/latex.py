"""
latex.py — render a tailored résumé to LaTeX and compile it to a PDF with Tectonic.

Why this exists: ATS submissions want a PDF, and the user authors résumés in LaTeX. The brain
(and its optional Gemini rewrite) produce a clean PLAIN-TEXT résumé; rather than force the model
into a brittle JSON schema, we parse that text into sections (reusing export.py's heading/bullet
heuristics) and inject it into resume.tex. The contact header is built from the user's profile so
it's always correct, regardless of what the model put at the top.

Dependency-isolated like export.py: if Tectonic is missing or a compile fails, build_pdf raises
RuntimeError and the caller falls back to .docx — the apply flow never hard-crashes.

Tectonic binary resolution order: explicit arg -> $TECTONIC_BIN -> vendored bin/tectonic[.exe] ->
"tectonic" on PATH. First compile downloads the support bundle into a project-local cache
(.tectonic-cache), so it stays self-contained and avoids host HOME/permission surprises.
"""
import os
import re
import sys
import shutil
import tempfile
import subprocess

from .export import _is_heading, _BULLET   # reuse the same plain-text heuristics

PDF_MIME = "application/pdf"

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_TEMPLATE = os.path.join(_HERE, "templates", "resume.tex")
_CACHE_DIR = os.path.join(_REPO_ROOT, ".tectonic-cache")

# Contact-ish leading lines we drop from the body (the header is rebuilt from the profile).
_CONTACT_RE = re.compile(r"@|https?://|linkedin\.com|github\.com|\d{3}[)\s.\-]\s*\d{3}[\s.\-]\d{4}",
                         re.IGNORECASE)


# ----------------------------- escaping -----------------------------
_TEX_REPL = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
    "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def _tex_escape(s):
    """Escape LaTeX specials. Char-by-char so inserted backslashes/braces are never re-scanned."""
    if not s:
        return ""
    return "".join(_TEX_REPL.get(ch, ch) for ch in str(s))


# ----------------------------- parsing -----------------------------
def parse_resume_text(text):
    """Plain-text résumé -> (preamble_items, sections).

    sections = [{"title": str, "items": [{"text": str, "bullet": bool}]}].
    Lines before the first heading become preamble items (e.g. a summary); obvious name/contact
    lines are dropped because the header is built from the profile.
    """
    preamble, sections, cur = [], [], None
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        s = line.strip()
        if not s:
            continue
        if _is_heading(line):
            cur = {"title": s.rstrip(":").strip(), "items": []}
            sections.append(cur)
            continue
        m = _BULLET.match(line)
        item = {"text": (m.group(1) if m else s).strip(), "bullet": bool(m)}
        if cur is None:
            preamble.append(item)
        else:
            cur["items"].append(item)

    # Drop a leading name line + any contact-looking lines from the preamble.
    cleaned = []
    for i, it in enumerate(preamble):
        if i == 0 and " " in it["text"] and len(it["text"]) <= 50 and not _CONTACT_RE.search(it["text"]):
            continue                                   # first line is almost always the name
        if _CONTACT_RE.search(it["text"]):
            continue
        cleaned.append(it)
    return cleaned, sections


# ----------------------------- rendering -----------------------------
def _contact_line(profile):
    p = profile or {}
    parts = [
        p.get("email"), p.get("phone"),
        ", ".join(x for x in (p.get("city"), p.get("state")) if x),
        p.get("linkedin"), p.get("github"), p.get("portfolio") or p.get("website"),
    ]
    esc = [_tex_escape(x) for x in parts if x and str(x).strip()]
    return r" \textbar{} ".join(esc)


def _name(profile):
    p = profile or {}
    nm = " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x).strip()
    return nm or p.get("name") or "Your Name"


def _render_items(items):
    """Emit LaTeX for a list of {text,bullet} items: bullets grouped into itemize, short
    non-bullet lines bolded (entry headers), long ones as normal paragraphs."""
    out, bullets = [], []

    def flush():
        if bullets:
            out.append(r"\begin{itemize}")
            out.extend(r"  \item " + _tex_escape(b) for b in bullets)
            out.append(r"\end{itemize}")
            bullets.clear()

    for it in items:
        if it["bullet"]:
            bullets.append(it["text"])
            continue
        flush()
        txt = _tex_escape(it["text"])
        if len(it["text"]) <= 90:
            out.append(r"\textbf{%s}\\" % txt)         # entry header / sub-heading
        else:
            out.append(txt + r"\par")                  # prose paragraph (e.g. summary)
    flush()
    return "\n".join(out)


def render(resume_text, profile):
    """Return a full .tex document string for `resume_text` using the profile for the header."""
    with open(_TEMPLATE, "r", encoding="utf-8") as fh:
        tpl = fh.read()
    preamble, sections = parse_resume_text(resume_text)

    body = []
    if preamble:
        body.append(_render_items(preamble))
    for sec in sections:
        body.append(r"\section{%s}" % _tex_escape(sec["title"]))
        body.append(_render_items(sec["items"]))

    return (tpl
            .replace("<<NAME>>", _tex_escape(_name(profile)))
            .replace("<<CONTACT>>", _contact_line(profile))
            .replace("<<BODY>>", "\n\n".join(body)))


# ----------------------------- compilation -----------------------------
def _tectonic_bin(explicit=None):
    cand = explicit or os.environ.get("TECTONIC_BIN")
    if cand and os.path.exists(cand):
        return cand
    name = "tectonic.exe" if os.name == "nt" else "tectonic"
    vendored = os.path.join(_REPO_ROOT, "bin", name)
    if os.path.exists(vendored):
        return vendored
    found = shutil.which("tectonic")
    if found:
        return found
    raise RuntimeError("Tectonic not found (set TECTONIC_BIN or vendor bin/%s)." % name)


def build_pdf(resume_text, profile, tectonic_bin=None, timeout=180):
    """Compile the tailored résumé to PDF bytes. Raises RuntimeError on any failure so the
    caller can fall back to .docx."""
    binpath = _tectonic_bin(tectonic_bin)
    tex = render(resume_text, profile)

    workdir = tempfile.mkdtemp(prefix="rb_tex_")
    try:
        tex_path = os.path.join(workdir, "resume.tex")
        with open(tex_path, "w", encoding="utf-8") as fh:
            fh.write(tex)

        os.makedirs(_CACHE_DIR, exist_ok=True)
        env = dict(os.environ, TECTONIC_CACHE_DIR=_CACHE_DIR)
        proc = subprocess.run(
            [binpath, tex_path, "--outdir", workdir, "--chatter", "minimal"],
            cwd=workdir, env=env, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        pdf_path = os.path.join(workdir, "resume.pdf")
        if proc.returncode != 0 or not os.path.exists(pdf_path):
            log = (proc.stdout or b"").decode("utf-8", "replace")[-1500:]
            raise RuntimeError("Tectonic compile failed (rc=%s):\n%s" % (proc.returncode, log))
        with open(pdf_path, "rb") as fh:
            return fh.read()
    except subprocess.TimeoutExpired:
        raise RuntimeError("Tectonic compile timed out after %ss." % timeout)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
