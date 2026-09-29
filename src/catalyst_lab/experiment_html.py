"""The live dashboard's HTML shell: title, the fixture banner, empty sections and the first data.

``/experiment.js`` renders every section from the JSON document with DOM calls only (never
``innerHTML``) and polls ``/api/public/experiment`` every few seconds. The first document is
embedded as an inert ``application/json`` block, so the page renders at once without a second
request; the Content-Security-Policy still forbids every inline script.
"""

import hashlib
import json
from functools import lru_cache
from html import escape
from pathlib import Path

STATIC = Path(__file__).with_name("static")
SECTIONS = (
    ("live", "Positions"),
    ("closed", "Closed trades"),
    ("research", "Research"),
    ("jev", "Jev"),
    ("picks", "Latest picks"),
    ("past", "Performance"),
)
SIDE_BY_SIDE = ("research", "jev")  # One row on wide screens: research beside Jev.
PRELOAD_FONTS = ("IBMPlexSans-Regular.woff2", "IBMPlexMono-Regular.woff2")


def e(value):
    return escape("" if value is None else str(value), quote=True)


@lru_cache(maxsize=1)
def asset_version():
    """A short digest of the page's stylesheet and script, added to their URLs: a new release
    changes the URLs, so no browser pairs a new page with an older cached copy."""
    digest = hashlib.sha256()
    for name in ("experiment.css", "experiment.js"):
        digest.update((STATIC / name).read_bytes())
    return digest.hexdigest()[:12]


def embedded_json(document):
    """JSON safe inside a ``<script type="application/json">`` element."""
    text = json.dumps(document, separators=(",", ":"), ensure_ascii=True)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def section(key, title):
    return (f'<section class="block" id="{key}" aria-labelledby="{key}-title">'
            f'<div class="block-head"><h2 id="{key}-title">{e(title)}</h2>'
            f'<span class="block-meta" id="{key}-meta"></span></div>'
            f'<div class="body" id="{key}-body"></div></section>')


def sections():
    html = []
    for key, title in SECTIONS:
        if key == SIDE_BY_SIDE[0]:
            html.append('<div class="split">')
        html.append(section(key, title))
        if key == SIDE_BY_SIDE[-1]:
            html.append("</div>")
    return "\n".join(html)


def render_page(document):
    fixture = ""
    if document["fixture_data"]:
        fixture = f'<div class="fixture-banner" role="alert">{e(document["data_label"])}</div>'
    preload = "\n".join(f'<link rel="preload" href="/fonts/{name}" as="font" type="font/woff2" '
                        'crossorigin>' for name in PRELOAD_FONTS)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{e(document["title"])}</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
{preload}
<link rel="stylesheet" href="/experiment.css?v={asset_version()}">
<script defer src="/experiment.js?v={asset_version()}"></script>
</head>
<body>
{fixture}<header class="masthead"><div class="wrap mast">
<h1 id="title">{e(document["title"])}</h1>
<div class="mast-status"><span class="state" id="pill"><span class="dot"
aria-hidden="true"></span><span id="pill-text">&hellip;</span></span>
<span class="updated" id="updated">loading</span>
<span class="account" id="account"></span></div>
</div></header>
<main class="wrap" id="app">
<noscript><p class="empty">This page updates itself with JavaScript. The same figures as JSON:
<a href="/api/public/experiment">/api/public/experiment</a>.</p></noscript>
<section class="summary" id="summary" aria-labelledby="summary-title">
<h2 class="sr-only" id="summary-title">Summary</h2><div class="body" id="summary-body"></div>
</section>
{sections()}
</main>
<script type="application/json" id="initial-data">{embedded_json(document)}</script>
</body>
</html>
"""


__all__ = ["PRELOAD_FONTS", "SECTIONS", "SIDE_BY_SIDE", "asset_version", "embedded_json",
           "render_page", "section"]
