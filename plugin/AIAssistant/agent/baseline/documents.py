# -*- coding: utf-8 -*-
"""Чтение вложений ТЗ (DOCX/TXT/MD) и длинных путей Windows без сторонних библиотек."""

import os
import re
import zipfile


def long_path(path):
    """На Windows добавляет префикс \\\\?\\ для путей длиннее MAX_PATH (имена кейсов длинные)."""
    path = os.path.abspath(path)
    if os.name == "nt" and len(path) >= 240 and not path.startswith("\\\\?\\"):
        return "\\\\?\\" + path
    return path


def docx_text(path):
    with zipfile.ZipFile(long_path(path)) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    paragraphs = []
    for para in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
        text = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", para, re.S))
        text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&")
        if text.strip():
            paragraphs.append(text)
    return "\n".join(paragraphs)


def read_text(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        return docx_text(path)
    with open(long_path(path), "rb") as f:
        return f.read().decode("utf-8", "replace")


def find_case_files(folder):
    """Возвращает (модель, [документы ТЗ], [изображения]) для папки кейса."""
    folder = long_path(folder)
    model, docs, images = None, [], []
    for name in sorted(os.listdir(folder)):
        ext = os.path.splitext(name)[1].lower()
        full = os.path.join(folder, name)
        if ext in (".stl", ".obj", ".3mf") and model is None:
            model = full
        elif ext in (".docx", ".txt", ".md"):
            docs.append(full)
        elif ext in (".jpg", ".jpeg", ".png"):
            images.append(full)
    return model, docs, images
