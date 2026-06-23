"""
latex.py — render a tailored résumé to LaTeX and compile it to a PDF with Tectonic.

Why this exists: ATS submissions want a PDF, and the user authors résumés in LaTeX. The brain
(and its optional Gemini rewrite) produce a clean PLAIN-TEXT résumé; rather than force the model
into a brittle JSON schema, we parse that text into a header + sections (reusing export.py's
heading/bullet heuristics) and inject it into resume.tex. The contact header prefers the user's
profile (always correct); if there's no profile it falls back to the name/contact lines the
résumé text itself starts with — so a freshly-rewritten résumé still gets a proper header.

Dependency-isolated like export.py: if Tectonic is missing or a compile fails, build_pdf raises
RuntimeError and the caller falls back to .docx — the apply flow never hard-crashes.
"""
import os
import re
import shutil
import tempfile
import subprocess

from .export import _is_heading, _BULLET   # reuse the same plain-text heuristics

PDF_MIME = "application/pdf"

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_TEMPLATE = os.path.join(_HERE, "templates", "resume.tex")
_CACHE_DIR = os.path.join(_REPO_ROOT, ".tectonic-cache")

# Contact-ish lines (the header block) we drop from the body / use to detect the contact line.
_CONTACT_RE = re.compile(r"@|https?://|linkedin\.com|github\.com|\d{3}[)\s.\-]\s*\d{3}[\s.\-]\d{4}",
                         re.IGNORECASE)

# Words that mark a real résumé section heading (vs. the name, which may also be ALL CAPS).
_SECTION_KW = (
    "summary", "objective", "profile", "education", "experience", "employment", "work history",
    "professional", "skills", "projects", "project", "certification", "certifications", "ventures",
    "leadership", "awards", "publications", "activities", "technical", "interests", "volunteer",
    "achievements", "training", "languages", "courses", "coursework",
)


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
def _is_section_heading(line):
    s = line.strip()
    if not _is_heading(line):
        return False
    low = s.lower().rstrip(":")
    return any(kw in low for kw in _SECTION_KW)


def parse_resume_text(text):
    """Plain-text résumé -> (header, sections).

    header  = {"name": str, "contact": str}   (from the block before the first real section)
    sections = [{"title": str, "items": [{"text": str, "bullet": bool}]}]
    """
    lines = (text or "").splitlines()
    header = {"name": "", "contact": ""}

    start = next((i for i, raw in enumerate(lines) if _is_section_heading(raw)), None)
    if start is not None:
        head = [l.strip() for l in lines[:start] if l.strip()]
        if head:
            header["name"] = head[0]
        header["contact"] = next((h for h in head[1:] if _CONTACT_RE.search(h) or "|" in h),
                                 head[1] if len(head) > 1 else "")
        rest = lines[start:]
    else:
        nonempty = [l.strip() for l in lines if l.strip()]
        header["name"] = nonempty[0] if nonempty else ""
        rest = lines

    sections, cur = [], None
    for raw in rest:
        line = raw.rstrip()
        s = line.strip()
        if not s:
            continue
        # In the fallback path, drop the leading name/contact lines (no profile header then).
        if start is None and cur is None and (s == header["name"] or _CONTACT_RE.search(s)):
            continue
        if _is_heading(line):
            cur = {"title": s.rstrip(":").strip(), "items": []}
            sections.append(cur)
            continue
        item = {"text": (_BULLET.match(line).group(1) if _BULLET.match(line) else s).strip(),
                "bullet": bool(_BULLET.match(line))}
        if cur is None:
            cur = {"title": "", "items": []}
            sections.append(cur)
        cur["items"].append(item)
    return header, sections


# ----------------------------- rendering -----------------------------
def _name(profile):
    p = profile or {}
    nm = " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x).strip()
    return nm or (p.get("name") or "")


def _contact_line(profile):
    p = profile or {}
    parts = [
        p.get("email"), p.get("phone"),
        ", ".join(x for x in (p.get("city"), p.get("state")) if x),
        p.get("linkedin"), p.get("github"), p.get("portfolio") or p.get("website"),
    ]
    esc = [_tex_escape(x) for x in parts if x and str(x).strip()]
    return r" \textbar{} ".join(esc)


def _contact_from_string(s):
    """Build a LaTeX contact line from a raw '... | ... | ...' string (header fallback)."""
    parts = [_tex_escape(p.strip()) for p in str(s).split("|") if p.strip()]
    return r" \textbar{} ".join(parts)


def _render_items(items):
    """Bullets grouped into itemize; short non-bullet lines bolded (entry headers); long ones
    as normal paragraphs (e.g. a summary)."""
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
            out.append(r"\textbf{%s}\\" % txt)
        else:
            out.append(txt + r"\par")
    flush()
    return "\n".join(out)


def render(resume_text, profile):
    """Return a full .tex document string for `resume_text`, header from profile (or the text)."""
    with open(_TEMPLATE, "r", encoding="utf-8") as fh:
        tpl = fh.read()
    header, sections = parse_resume_text(resume_text)

    name = _name(profile) or header["name"] or "Your Name"
    contact = _contact_line(profile) or _contact_from_string(header["contact"])

    body = []
    for sec in sections:
        if sec["title"]:
            body.append(r"\section{%s}" % _tex_escape(sec["title"]))
        body.append(_render_items(sec["items"]))

    return (tpl
            .replace("<<NAME>>", _tex_escape(name))
            .replace("<<CONTACT>>", contact)
            .replace("<<BODY>>", "\n\n".join(body)))


# ----------------------------- compilation -----------------------------
def _bootstrap_tectonic():
    """Download the Tectonic binary into bin/ on first use (Linux/macOS hosts) so deploying by a
    plain `git pull` + restart needs NO manual step — the ~50 MB binary is gitignored and can't be
    pulled, and shared-host pip/Terminal access is flaky. Best-effort: returns the binary path, or
    None if it can't fetch it (then build_pdf falls back to .docx/.txt, same as before). Windows is
    not auto-fetched — dev vendors bin/tectonic.exe via scripts/get_tectonic.ps1. Mirrors the URL
    scheme in scripts/get_tectonic.sh."""
    import platform, tarfile, io
    if os.name == "nt":
        return None
    target = {"Linux": "x86_64-unknown-linux-gnu", "Darwin": "x86_64-apple-darwin"}.get(platform.system())
    if not target:
        return None
    ver = os.environ.get("TECTONIC_VERSION", "0.16.9")
    url = ("https://github.com/tectonic-typesetting/tectonic/releases/download/"
           "tectonic@{v}/tectonic-{v}-{t}.tar.gz").format(v=ver, t=target).replace("@", "%40")
    bindir = os.path.join(_REPO_ROOT, "bin")
    dest = os.path.join(bindir, "tectonic")
    tmp = dest + ".tmp.%d" % os.getpid()                 # pid-unique so concurrent workers don't clash
    try:
        import requests
        os.makedirs(bindir, exist_ok=True)
        r = requests.get(url, timeout=180)
        r.raise_for_status()
        with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tf:
            with open(tmp, "wb") as fh:
                shutil.copyfileobj(tf.extractfile("tectonic"), fh)
        os.chmod(tmp, 0o755)
        os.replace(tmp, dest)                            # atomic; last writer wins, all fine
        return dest
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return None


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
    auto = _bootstrap_tectonic()                         # fresh host: fetch it once, then cached in bin/
    if auto and os.path.exists(auto):
        return auto
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
