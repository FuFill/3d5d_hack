# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Download the FDM material pages of the official Stereotech wiki (development tool).

Usage: python crawl_stereotech_wiki.py <cache_dir>
Writes <cache_dir>/wiki_pages.json: {path: {"url", "retrieved", "html"}} with the
server-rendered page body only. The cache is an input of build_material_catalog.py.
"""

import datetime
import json
import os
import re
import sys
import urllib.parse
import urllib.request

BASE = "https://wiki.stereotech.org"
ROOT = "/ru/fdm-materials"


def fetch(path):
    req = urllib.request.Request(BASE + urllib.parse.quote(path), headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")


def page_body(html):
    m = re.search(r'<template slot="contents">(.*?)</template>', html, re.S)
    return m.group(1) if m else ""


def main(cache_dir):
    today = datetime.date.today().isoformat()
    seen, queue, pages = set(), [ROOT], {}
    while queue:
        path = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        if path.lower().endswith((".pdf", ".jpg", ".png")):
            continue
        try:
            body = page_body(fetch(path))
        except Exception as e:  # network errors are reported, not fatal
            print("ERROR", path, e)
            continue
        pages[path] = {"url": BASE + path, "retrieved": today, "html": body}
        for href in re.findall(r'href="(/(?:ru/)?fdm-materials[^"#]*)"', body):
            nxt = href if href.startswith("/ru/") else "/ru" + href
            if nxt not in seen:
                queue.append(nxt)
    os.makedirs(cache_dir, exist_ok=True)
    with open(os.path.join(cache_dir, "wiki_pages.json"), "w", encoding="utf-8") as f:
        json.dump(pages, f, ensure_ascii=False, indent=1, sort_keys=True)
    print("pages:", len(pages))


if __name__ == "__main__":
    main(sys.argv[1])
