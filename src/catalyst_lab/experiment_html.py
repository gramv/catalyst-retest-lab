"""The live page's HTML shell (EXPERIMENT_DASHBOARD_V3; V2 package public-page): the title, the
fixture banner, the status line's band, empty sections and the first data.

Two views share one shell: the live page (``/``) and a trade's own page (``/trade/{n}``).
``/experiment.js`` renders every section from the JSON document with DOM calls only (never
``innerHTML``), draws the charts as inline SVG and polls ``/api/public/experiment`` every 30
seconds. The first document is
embedded as an inert ``application/json`` block, so the page renders at once without a second
request; the Content-Security-Policy still forbids every inline script.
"""

import hashlib
import json
from functools import lru_cache
from html import escape
from pathlib import Path

STATIC = Path(__file__).with_name("static")
BRAND = "Catalyst"
# The live page, top to bottom (EXPERIMENT_DASHBOARD_V3, package public-page-v3): the status
# band is above <main>; then the account (equity and its KPIs), the equity curve with the BTC
# benchmark and the drawdown, performance (daily P&L, the R distribution and the statistics),
# open positions, today vs the limits and the market, the research agent and Jev, closed today
# and past days.
SECTIONS = (
    ("account", None),
    ("equity", "Trading P&L"),
    ("performance", "Performance"),
    ("open", "Open positions"),
    ("tiles", None),
    ("agents", None),
    ("closed", "Closed today"),
    ("past", "Past days"),
)
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
    head = (f'<div class="section-head"><h2 id="{key}-title">{e(title)}</h2>'
            f'<span class="section-meta" id="{key}-meta"></span></div>' if title else "")
    label = f' aria-labelledby="{key}-title"' if title else ""
    return (f'<section class="section" id="{key}"{label}>{head}'
            f'<div class="body" id="{key}-body"></div></section>')


def sections():
    return "\n".join(section(key, title) for key, title in SECTIONS)


def render_page(document, trade_no=None):
    """The live page, or with ``trade_no`` that trade's own page (its data is in the
    document; the service answers 404 for a trade the document does not list)."""
    fixture = ""
    if document["fixture_data"]:
        fixture = f'<div class="fixture-banner" role="alert">{e(document["data_label"])}</div>'
    preload = "\n".join(f'<link rel="preload" href="/fonts/{name}" as="font" type="font/woff2" '
                        'crossorigin>' for name in PRELOAD_FONTS)
    title = document["title"]
    if trade_no is None:
        view = 'data-view="main"'
        head = (f'<div class="brand"><span class="kicker">{BRAND}</span>'
                f'<h1 id="title">{e(title)}</h1></div>')
        body = sections()
        page_title = title
    else:
        view = f'data-view="trade" data-trade="{int(trade_no)}"'
        head = ('<div class="brand"><a class="back" href="/">&larr; Live page</a>'
                '<span class="crumb" id="crumb"></span></div>')
        body = '<div id="trade-body"></div>'
        page_title = f"Trade #{int(trade_no)} · {title}"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<title>{e(page_title)}</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
{preload}
<link rel="stylesheet" href="/experiment.css?v={asset_version()}">
<script defer src="/experiment.js?v={asset_version()}"></script>
</head>
<body {view}>
{fixture}<header class="masthead"><div class="wrap mast">
{head}
<div class="updated" id="updated">Paper account</div>
</div></header>
<div class="status-band" id="status" role="status" aria-live="polite"><div class="wrap status-row"
id="status-body"></div></div>
<main class="wrap" id="app">
<noscript><p class="empty">This page updates itself with JavaScript. The same figures as JSON:
<a href="/api/public/experiment">/api/public/experiment</a>.</p></noscript>
{body}
</main>
<script type="application/json" id="initial-data">{embedded_json(document)}</script>
</body>
</html>
"""


__all__ = ["BRAND", "PRELOAD_FONTS", "SECTIONS", "asset_version", "embedded_json",
           "render_page", "section"]
