#!/usr/bin/env python3
"""Streaming .xlsx reader — stdlib only (openpyxl isn't installed).

Why this exists: probe_everify_xlsx.read_xlsx() builds a list of every row before returning.
That's fine for a 26k-row E-Verify export, but the DOL LCA disclosure file is 131 MB / ~1M
rows and materializing it costs several GB. iter_rows() yields one dict at a time and only
keeps the columns you ask for, so the same file streams in constant memory.

An xlsx is a zip of XML: xl/sharedStrings.xml holds deduplicated text, each worksheet holds
cells that reference it by index. We iterparse both and clear as we go.

    from scraper.xlsx_stream import iter_rows, header_of
    for row in iter_rows(path, ("EMPLOYER_NAME", "VISA_CLASS", "CASE_STATUS")):
        ...
"""
import re
import xml.etree.ElementTree as ET
import zipfile


def _ln(tag):
    return tag.split("}", 1)[1] if "}" in tag else tag


def _colnum(ref):
    """'BC12' -> zero-based column index."""
    m = re.match(r"[A-Z]+", ref or "")
    if not m:
        return 0
    n = 0
    for ch in m.group(0):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _shared_strings(z):
    out = []
    if "xl/sharedStrings.xml" not in z.namelist():
        return out
    for _, el in ET.iterparse(z.open("xl/sharedStrings.xml"), events=("end",)):
        if _ln(el.tag) == "si":
            out.append("".join((t.text or "") for t in el.iter() if _ln(t.tag) == "t"))
            el.clear()
    return out


def _first_sheet(z):
    names = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
    if not names:
        raise ValueError("no worksheet found in %s" % getattr(z, "filename", "xlsx"))
    return names[0]


def _cells(el, shared):
    cells = {}
    for c in el:
        if _ln(c.tag) != "c":
            continue
        v, t = None, c.get("t")
        for ch in c:
            if _ln(ch.tag) == "v":
                v = ch.text
            elif _ln(ch.tag) == "is":
                v = "".join((x.text or "") for x in ch.iter() if _ln(x.tag) == "t")
        if v is None:
            val = ""
        elif t == "s":
            try:
                val = shared[int(v)]
            except (ValueError, IndexError):
                val = ""
        else:
            val = v
        cells[_colnum(c.get("r"))] = val
    return cells


def header_of(path):
    """Column names from the first row, without reading the rest of the sheet."""
    with zipfile.ZipFile(path) as z:
        shared = _shared_strings(z)
        for _, el in ET.iterparse(z.open(_first_sheet(z)), events=("end",)):
            if _ln(el.tag) != "row":
                continue
            cells = _cells(el, shared)
            el.clear()
            return [str(cells.get(i, "")).strip() for i in range(max(cells) + 1)] if cells else []
    return []


def iter_rows(path, wanted=None, stop_after_blank=500):
    """Yield {column_name: value} per data row.

    `wanted` limits which columns are carried (case-insensitive); None means all.
    `stop_after_blank` ends the scan after that many consecutive empty rows — the DOL sheets
    declare ~1M rows but only ~210k carry data, and without this the tail costs minutes.
    """
    want = {w.strip().lower() for w in wanted} if wanted else None
    with zipfile.ZipFile(path) as z:
        shared = _shared_strings(z)
        idx, blanks = None, 0
        for _, el in ET.iterparse(z.open(_first_sheet(z)), events=("end",)):
            if _ln(el.tag) != "row":
                continue
            cells = _cells(el, shared)
            el.clear()
            if idx is None:                       # header row
                names = [str(cells.get(i, "")).strip() for i in range(max(cells) + 1)] if cells else []
                idx = {n: i for i, n in enumerate(names)
                       if n and (want is None or n.lower() in want)}
                continue
            if not any(str(v).strip() for v in cells.values()):
                blanks += 1
                if stop_after_blank and blanks >= stop_after_blank:
                    return
                continue
            blanks = 0
            yield {n: str(cells.get(i, "") or "").strip() for n, i in idx.items()}
