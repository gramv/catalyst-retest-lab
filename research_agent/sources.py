"""Source excerpts: fetch a page, cut an exact excerpt, verify it against the live page,
and read its own publish-time metadata. Never invents a publish time or paraphrases text.

Round 3 of the 2026-09-27 real-Jev runs found the failure mode this module exists to
prevent (``catalyst-research-agent-method.md``): a research subagent invented future
``retrieved_at`` times and paraphrased about half its excerpts. The fix is mechanical,
not a reminder to be careful: cut excerpts from the page's own visible text
(``cut_excerpt``), take publish times only from the page's own metadata
(``extract_published_at``, never the fetch time), and re-fetch and check every excerpt
before it reaches a report (``verify_excerpt``). Contract rules other than this (HTTPS,
excerpt length, no 0x address or e-mail) are left to the app's own ``SourceExcerpt``
model and privacy screen, which ``build`` calls before anything is sent — this module
does not re-implement them, so they can never drift out of step with the app's.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_TAG_BLOCK = re.compile(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>")
_BLOCK_BREAK = re.compile(r"(?i)</?(p|div|li|h[1-6]|br|tr|section|article|blockquote)[^>]*>")
_ANY_TAG = re.compile(r"(?s)<[^>]+>")
_WHITESPACE = re.compile(r"\s+")
_QUOTE_MAP = (("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'),
              ("–", "-"), ("—", "-"), (" ", " "), ("…", "..."))
_META_TAG = re.compile(r"(?is)<meta\b([^>]*)>")
_TIME_TAG = re.compile(r"(?is)<time\b([^>]*)>")
_LD_JSON = re.compile(
    r'(?is)<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>'
)
_ATTR = re.compile(
    r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)')"""
)
_PUBLISHED_META_KEYS = frozenset({
    "article:published_time", "og:article:published_time", "datepublished",
    "publish-date", "publishdate", "date",
})


class SourceError(Exception):
    """Sanitized: never a page body, header value, or full URL in the message."""


@dataclass(frozen=True)
class Verification:
    """The result of re-checking one excerpt against its live page."""

    status: str  # VERIFIED, NEAR_MATCH, NOT_FOUND, UNVERIFIABLE
    overlap: float
    detail: str | None = None

    @property
    def ok(self):
        return self.status == "VERIFIED"


# --- Fetching and visible text -----------------------------------------------------------

def fetch_page(url, *, client=None, timeout=25.0):
    """The page's decoded text, or raises ``SourceError``. HTTPS only."""
    if not url.startswith("https://"):
        raise SourceError("PUBLIC_HTTPS_SOURCE_REQUIRED")
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        try:
            response = client.get(url, headers=UA)
        except httpx.HTTPError as exc:
            raise SourceError(f"SOURCE_CONNECTION_ERROR: {type(exc).__name__}") from None
    finally:
        if owns_client:
            client.close()
    if response.status_code != 200:
        raise SourceError(f"SOURCE_HTTP_{response.status_code}")
    return response.text


def visible_text(raw_html):
    """Visible text, one non-empty line per block element, in document order.

    Ports ``research3/quote.py``'s ``visible()``: strip script/style, break on block
    tags, unescape entities and NFKC-normalize (so full-width or accented look-alikes do
    not silently change what an excerpt search matches).
    """
    body = _TAG_BLOCK.sub("\n", raw_html)
    body = _BLOCK_BREAK.sub("\n", body)
    text = html.unescape(_ANY_TAG.sub("", body))
    text = unicodedata.normalize("NFKC", text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return [line for line in lines if line]


def cut_excerpt(lines, start, end):
    """The exact text from the first occurrence of ``start`` through the end of the next
    ``end`` on the same line — case, punctuation and curly quotes exactly as the page
    has them (ports ``research3/build_curation.py``'s ``cut``)."""
    for line in lines:
        i = line.find(start)
        if i == -1:
            continue
        j = line.find(end, i)
        if j != -1:
            return line[i:j + len(end)]
    raise SourceError("EXCERPT_NOT_FOUND_ON_PAGE")


# --- Verification (re-fetch and check) ---------------------------------------------------

def normalize(text):
    """Case/whitespace/curly-quote-insensitive form used only to *compare* text, never
    to build the excerpt actually cited (ports ``research3/verify_sources.py``'s ``norm``)."""
    text = unicodedata.normalize("NFKC", html.unescape(text))
    for curly, straight in _QUOTE_MAP:
        text = text.replace(curly, straight)
    return _WHITESPACE.sub(" ", text).strip().lower()


def _searchable_texts(raw_html):
    """The page's texts an excerpt may be found in, each normalized and searched on its own.

    1. The visible text exactly as ``visible_text`` gives it, the text ``cut_excerpt`` cuts
       from. Inline tags are removed without a space there, as a browser shows them
       ("rose <b>12%</b>," reads "rose 12%,"), so an excerpt the kit cut always verifies
       against the same page. (Until 2026-09-28 only text 2 was searched, where that reads
       "rose 12% ,": an exact cut across an inline tag next to punctuation came back
       NEAR_MATCH.)
    2. The same text with every tag read as a space, as before: an excerpt copied from a
       rendered page where two elements sit side by side (table cells) still verifies.
    3. The raw markup: a JSON API body or JSON-LD block is not visible text, but it is
       still a legitimate excerpt source.

    Each is searched separately, never concatenated, so no match can span the seam
    between two of them. The comparison itself is unchanged (``normalize``).
    """
    return (normalize("\n".join(visible_text(raw_html))),
            normalize(_ANY_TAG.sub(" ", _TAG_BLOCK.sub(" ", raw_html))),
            normalize(raw_html))


def verify_excerpt(url, excerpt, *, client=None, timeout=25.0):
    """Re-fetch ``url`` and check that ``excerpt`` is a verbatim (case/quote-insensitive)
    substring of the page now. Never raises for a fetch failure: returns
    ``UNVERIFIABLE`` with the sanitized reason, so a caller can drop the item and record
    why rather than crash a whole build."""
    try:
        raw = fetch_page(url, client=client, timeout=timeout)
    except SourceError as exc:
        return Verification("UNVERIFIABLE", 0.0, str(exc))
    return check_excerpt(raw, excerpt)


def check_excerpt(raw_html, excerpt):
    """``verify_excerpt``'s check on an already fetched page: VERIFIED only when the
    normalized excerpt is a substring of one of ``_searchable_texts``; else NEAR_MATCH
    (at least 80% of its 4-word runs found) or NOT_FOUND. Neither of those is ``ok``."""
    texts = _searchable_texts(raw_html)
    needle = normalize(excerpt)
    if needle and any(needle in text for text in texts):
        return Verification("VERIFIED", 1.0)
    words = needle.split()
    grams = [" ".join(words[k:k + 4]) for k in range(max(1, len(words) - 3))] if words else []
    found = sum(any(gram in text for text in texts) for gram in grams)
    overlap = (found / len(grams)) if grams else 0.0
    status = "NEAR_MATCH" if overlap >= 0.8 else "NOT_FOUND"
    return Verification(status, overlap)


# --- Publish-time metadata (never invented) -----------------------------------------------

def _attrs(tag_text):
    found = {}
    for match in _ATTR.finditer(tag_text):
        name, dq, sq = match.groups()
        found[name.lower()] = html.unescape(dq if dq is not None else sq)
    return found


def _meta_published_at(raw_html):
    for tag in _META_TAG.finditer(raw_html):
        attrs = _attrs(tag.group(1))
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        content = attrs.get("content")
        if key in _PUBLISHED_META_KEYS and content:
            return content.strip()
    return None


def _flatten_ld(data):
    if isinstance(data, list):
        for item in data:
            yield from _flatten_ld(item)
    elif isinstance(data, dict):
        yield data
        graph = data.get("@graph")
        if isinstance(graph, list):
            yield from _flatten_ld(graph)


def _ld_json_published_at(raw_html):
    for block in _LD_JSON.finditer(raw_html):
        try:
            data = json.loads(block.group(1).strip())
        except (ValueError, TypeError):
            continue
        for node in _flatten_ld(data):
            value = node.get("datePublished")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _time_tag_published_at(raw_html):
    for tag in _TIME_TAG.finditer(raw_html):
        value = _attrs(tag.group(1)).get("datetime")
        if value and value.strip():
            return value.strip()
    return None


def _rfc3339_or_none(value):
    """``value`` parsed as an aware timestamp, or ``None`` — never a guessed offset."""
    text = value.strip()
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None  # A bare date or an offset-less time; never invent one.
    return parsed.astimezone(UTC).isoformat()


def extract_published_at(raw_html):
    """RFC3339 publish time from the page's own metadata, or ``None``.

    Checked in order: ``article:published_time``/``og:article:published_time`` meta
    tags, JSON-LD ``datePublished`` (including inside ``@graph``), then a ``<time
    datetime=...>`` tag. Returns ``None`` — never an invented time — when nothing is
    found or the found value has no explicit UTC offset.
    """
    for finder in (_meta_published_at, _ld_json_published_at, _time_tag_published_at):
        value = finder(raw_html)
        if value:
            parsed = _rfc3339_or_none(value)
            if parsed:
                return parsed
    return None


def page_published_times(raw_html):
    """Every publish time the page's own metadata gives, in UTC, first found first: each
    publish-time meta tag, each JSON-LD ``datePublished``, and the first ``<time
    datetime=...>`` tag (the one ``extract_published_at`` would read). Values without an
    explicit offset are left out, never completed."""
    found = []
    for tag in _META_TAG.finditer(raw_html):
        attrs = _attrs(tag.group(1))
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key in _PUBLISHED_META_KEYS and attrs.get("content"):
            found.append(attrs["content"])
    for block in _LD_JSON.finditer(raw_html):
        try:
            data = json.loads(block.group(1).strip())
        except (ValueError, TypeError):
            continue
        found.extend(node["datePublished"] for node in _flatten_ld(data)
                     if isinstance(node.get("datePublished"), str))
    first_time = _time_tag_published_at(raw_html)
    if first_time:
        found.append(first_time)
    times = []
    for value in found:
        parsed = _rfc3339_or_none(value)
        if parsed and parsed not in times:
            times.append(parsed)
    return tuple(times)


def same_instant(declared, page_times):
    """Whether ``declared`` (RFC3339 with an offset) is one of ``page_times`` (to the
    second: a page time of 05:30:38.536 matches a declared 05:30:38)."""
    try:
        moment = datetime.fromisoformat(declared.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return False
    if moment.tzinfo is None:
        return False
    return any(abs((moment - datetime.fromisoformat(value)).total_seconds()) < 1
               for value in page_times)


@dataclass(frozen=True)
class SourceCheck:
    """One cited source checked against its live page, fetched once."""

    verification: Verification
    page_times: tuple  # every publish time the page's own metadata gives (UTC RFC3339)
    retrieved_at: datetime | None  # when the page was fetched; None when it could not be
    problem: str | None  # why the source cannot be sent, or None

    @property
    def ok(self):
        return self.problem is None


def check_source(url, excerpt, published_at, *, client=None, timeout=25.0, clock=None):
    """Re-fetch ``url`` once and check the citation: the excerpt must verify exactly, and a
    non-null ``published_at`` must be one of the page's own metadata publish times (the
    rule "from the page's own metadata, or null", checked rather than trusted). The fetch
    time becomes the source's ``retrieved_at``: a real read, never an invented one."""
    return check_sources([(url, excerpt, published_at)], client=client, timeout=timeout,
                         clock=clock)[0]


def check_sources(citations, *, client=None, timeout=25.0, clock=None):
    """``check_source`` for each ``(url, excerpt, published_at)``, in order, fetching each
    page once however many excerpts cite it."""
    pages, results = {}, []
    for url, excerpt, published_at in citations:
        if url not in pages:
            try:
                pages[url] = (fetch_page(url, client=client, timeout=timeout),
                              (clock or (lambda: datetime.now(UTC)))())
            except SourceError as exc:
                pages[url] = exc
        page = pages[url]
        if isinstance(page, SourceError):
            results.append(SourceCheck(Verification("UNVERIFIABLE", 0.0, str(page)), (), None,
                                       f"UNVERIFIABLE ({page})"))
        else:
            results.append(_check_page(*page, excerpt, published_at))
    return results


def _check_page(raw, retrieved_at, excerpt, published_at):
    verification = check_excerpt(raw, excerpt)
    times = page_published_times(raw)
    problem = None
    if not verification.ok:
        problem = f"EXCERPT_{verification.status} (overlap {verification.overlap:.2f})"
    elif published_at is not None and not same_instant(published_at, times):
        problem = ("PUBLISHED_AT_NOT_FROM_PAGE_METADATA: the page's own metadata gives "
                   + (", ".join(times) if times else "no publish time") + "; use one of "
                   "those, or null")
    elif published_at is not None and datetime.fromisoformat(
            published_at.replace("Z", "+00:00")) > retrieved_at:
        problem = "PUBLISHED_AT_AFTER_RETRIEVAL"
    return SourceCheck(verification, times, retrieved_at, problem)
