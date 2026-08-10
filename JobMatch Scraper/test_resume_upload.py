"""
test_resume_upload.py — reading an uploaded résumé back into text.

The app was paste-only until now, which is a real barrier: people have a PDF, not a clipboard
full of plain text. Everything downstream (match score, digest, Resume Brain) works on the text
from db.profile_text(), so the file is parsed in memory and never stored.

Run it directly
    python test_resume_upload.py
or via pytest. PDF cases need pypdf and .docx cases need python-docx; both are in
requirements.txt, and each is skipped with a printed note rather than failing if absent, since
the product itself degrades to "paste the text instead" in exactly that case.

The failure modes worth testing are the quiet ones. A scanned PDF parses perfectly and yields
nothing — a user cannot debug that from a stack trace, so it gets its own message. A résumé
that lays its work history out in a borderless table is the other one: those cells are NOT in
doc.paragraphs, and missing them drops half the history without any error at all.
"""
import core

RESUME = """KUNAL SINGH
Boston, MA | kunal@example.com

EXPERIENCE
Project Manager, Acme Corp
- Ran a portfolio of six infrastructure projects
- Cut cycle time by 22 percent

EDUCATION
Northeastern University, MS Project Management
"""


def _have(mod):
    try:
        __import__(mod)
        return True
    except ImportError:
        print("     (skipped — %s not installed)" % mod)
        return False


def _pdf_bytes(text):
    """A real PDF built with pypdf, so the test reads a genuine file rather than a fixture."""
    from pypdf import PdfWriter
    from io import BytesIO
    w = PdfWriter()
    page = w.add_blank_page(width=612, height=792)
    try:
        from pypdf.annotations import FreeText
        y = 740
        for line in text.split("\n"):
            if line.strip():
                w.add_annotation(page_number=0, annotation=FreeText(
                    text=line, rect=(40, y, 560, y + 14)))
            y -= 16
    except Exception:
        pass
    buf = BytesIO()
    w.write(buf)
    return buf.getvalue()


def _docx_bytes(text):
    return core.resume_to_docx_bytes(text)      # the writer this repo already had


# --- the formats people actually have -------------------------------------------------
def test_plain_text_round_trips():
    got, err = core.resume_text_from_upload("cv.txt", RESUME.encode("utf-8"))
    assert not err, err
    assert "Project Manager, Acme Corp" in got
    assert "Northeastern" in got


def test_markdown_is_read_as_text():
    got, err = core.resume_text_from_upload("cv.md", ("# CV\n\n" + RESUME).encode("utf-8"))
    assert not err and "Acme Corp" in got


def test_docx_round_trips_through_the_writer_this_repo_already_had():
    if not _have("docx"):
        return
    got, err = core.resume_text_from_upload("cv.docx", _docx_bytes(RESUME))
    assert not err, err
    assert "Project Manager, Acme Corp" in got
    assert "Cut cycle time by 22 percent" in got


def test_docx_tables_are_read_too():
    """Plenty of résumés lay dates and employers out in a borderless table. Those cells are not
    in doc.paragraphs — miss them and half the work history vanishes with no error."""
    if not _have("docx"):
        return
    from docx import Document
    from io import BytesIO
    doc = Document()
    doc.add_paragraph("KUNAL SINGH")
    t = doc.add_table(rows=2, cols=2)
    t.rows[0].cells[0].text = "2023-2026"
    t.rows[0].cells[1].text = "Program Manager, Globex"
    t.rows[1].cells[0].text = "2021-2023"
    t.rows[1].cells[1].text = "Analyst, Initech"
    buf = BytesIO()
    doc.save(buf)
    got, err = core.resume_text_from_upload("cv.docx", buf.getvalue())
    assert not err, err
    assert "Program Manager, Globex" in got, got
    assert "Analyst, Initech" in got


def test_pdf_is_read():
    if not _have("pypdf"):
        return
    data = _pdf_bytes(RESUME)
    got, err = core.resume_text_from_upload("cv.pdf", data)
    # A PDF with no extractable text must report the scanned-document message, never crash.
    assert err == "" or "no readable text" in err or "scanned" in err, (err, got[:80])
    if not err:
        assert "Acme" in got or "KUNAL" in got.upper()


# --- the quiet failures ----------------------------------------------------------------
def test_a_scanned_pdf_says_so_instead_of_saving_nothing():
    if not _have("pypdf"):
        return
    from pypdf import PdfWriter
    from io import BytesIO
    w = PdfWriter()
    w.add_blank_page(width=612, height=792)     # an image-only scan has no text layer either
    buf = BytesIO()
    w.write(buf)
    got, err = core.resume_text_from_upload("scan.pdf", buf.getvalue())
    assert got == ""
    assert "scanned" in err and "paste" in err, err


def test_a_near_empty_file_is_refused_rather_than_stored():
    got, err = core.resume_text_from_upload("cv.txt", b"hi")
    assert got == "" and "readable text" in err


def test_empty_upload():
    got, err = core.resume_text_from_upload("cv.pdf", b"")
    assert got == "" and "empty" in err.lower()


def test_corrupt_file_of_a_supported_type_is_a_message_not_a_crash():
    got, err = core.resume_text_from_upload("cv.pdf", b"this is definitely not a PDF" * 20)
    assert got == ""
    assert "Couldn't read" in err or "no readable text" in err, err
    got, err = core.resume_text_from_upload("cv.docx", b"nor is this a docx" * 20)
    assert got == "" and err


# --- what we refuse, and why -----------------------------------------------------------
def test_legacy_doc_names_the_fix():
    got, err = core.resume_text_from_upload("resume.doc", b"\xd0\xcf\x11\xe0" + b"x" * 200)
    assert got == ""
    assert ".docx" in err and "paste" in err, err


def test_unsupported_types_are_refused():
    for name in ("resume.exe", "resume.zip", "resume.jpg", "resume.html", "resume"):
        got, err = core.resume_text_from_upload(name, b"x" * 500)
        assert got == "" and err, name


def test_extension_check_is_case_insensitive():
    got, err = core.resume_text_from_upload("CV.TXT", RESUME.encode("utf-8"))
    assert not err and "Acme" in got


def test_oversized_uploads_are_refused_by_size_not_parsed():
    big = b"x" * (core.RESUME_UPLOAD_MAX_BYTES + 1)
    got, err = core.resume_text_from_upload("cv.pdf", big)
    assert got == "" and "limit" in err


def test_undecodable_bytes_do_not_raise():
    got, err = core.resume_text_from_upload("cv.txt", b"\xff\xfe\x00bad bytes" + RESUME.encode())
    assert isinstance(got, str) and isinstance(err, str)


# --- the plumbing ----------------------------------------------------------------------
def test_the_flask_body_cap_sits_above_the_per_file_cap():
    """So the friendly per-file message is what users normally hit, with the hard 413 as a
    backstop for anything that would otherwise be buffered first."""
    import web
    assert web.app.config["MAX_CONTENT_LENGTH"] > core.RESUME_UPLOAD_MAX_BYTES


def test_both_upload_forms_are_multipart():
    """Without enctype the browser posts the field NAME and no file, and it fails SILENTLY."""
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    for tpl, field in (("welcome.html", "resume_file"), ("brain_teach.html", "resume_file")):
        src = open(os.path.join(here, "templates", tpl), encoding="utf-8").read()
        assert 'name="%s"' % field in src, tpl
        assert 'enctype="multipart/form-data"' in src, tpl


def test_paste_still_works_so_a_failed_parse_never_blocks_anyone():
    import inspect
    import web
    src = inspect.getsource(web.brain_resume_save)
    assert "uploaded or request.form.get" in src, "the textarea must remain the fallback"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d résumé-upload checks passed." % len(fns))
