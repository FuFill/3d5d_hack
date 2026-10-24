# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Text of attached specifications (DOCX, TXT, MD).

DOCX uses the canonical core's read_docx_text, so the planner receives exactly the
text its own tests use for the organizer examples.
"""

import re
import sys
import zipfile

MAX_CHARS = 100000


def _read_docx(path):
    try:
        from .core_worker import core_path
        if core_path() not in sys.path:
            sys.path.insert(0, core_path())
        from ste_planner.requirements import read_docx_text
        return read_docx_text(path)
    except (ImportError, RuntimeError):
        pass
    # Same algorithm when the core is not bundled.
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    lines = []
    for paragraph in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S):
        text = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", paragraph))
        text = (text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
                .replace("&apos;", "'").replace("&amp;", "&"))
        if text.strip():
            lines.append(text)
    return "\n".join(lines)


def _read_text(path):
    with open(path, "rb") as f:
        data = f.read()
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def extract_text(path):
    """Returns the specification text; raises ValueError("unreadable"/"empty")."""
    try:
        if path.lower().endswith(".docx"):
            text = _read_docx(path)
        else:
            text = _read_text(path)
    except (OSError, KeyError, zipfile.BadZipFile, UnicodeDecodeError):
        raise ValueError("unreadable")
    text = text.replace("\r\n", "\n").strip()
    if not text:
        raise ValueError("empty")
    return text[:MAX_CHARS]
