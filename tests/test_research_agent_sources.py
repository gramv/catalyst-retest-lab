"""research_agent.sources: excerpt cutting, re-verification and publish-time metadata,
on fixture HTML. Offline only: httpx.MockTransport stands in for the live page; no real
network access anywhere in this file.
"""

import httpx
import pytest

from research_agent import sources

HTML_BASIC = """
<html><head><script>var x = 1;</script></head>
<body>
<div>The exchange’s report said <b>“volumes rose 12%.”</b></div>
<p>Second   paragraph   here.</p>
<style>.x{color:red}</style>
</body></html>
"""


def _client(html_text, status=200):
    def handle(request):
        return httpx.Response(status, text=html_text)
    return httpx.Client(transport=httpx.MockTransport(handle))


# --- visible_text: strips tags, keeps curly quotes exactly, drops empty lines --------------

def test_visible_text_strips_tags_and_keeps_curly_quotes():
    lines = sources.visible_text(HTML_BASIC)
    assert "The exchange’s report said “volumes rose 12%.”" in lines
    assert "Second paragraph here." in lines
    assert not any("script" in line.lower() or "color:red" in line for line in lines)


# --- cut_excerpt: the exact text, case and curly quotes untouched -------------------------

def test_cut_excerpt_is_exact_including_curly_quotes():
    lines = sources.visible_text(HTML_BASIC)
    excerpt = sources.cut_excerpt(lines, "The exchange", "12%.”")
    assert excerpt == "The exchange’s report said “volumes rose 12%.”"


def test_cut_excerpt_raises_when_not_found():
    lines = sources.visible_text(HTML_BASIC)
    with pytest.raises(sources.SourceError):
        sources.cut_excerpt(lines, "Not anywhere on", "this page")


def test_cut_excerpt_requires_start_and_end_on_the_same_line():
    lines = sources.visible_text(HTML_BASIC)
    with pytest.raises(sources.SourceError):
        sources.cut_excerpt(lines, "The exchange", "Second paragraph")


# --- normalize: curly and straight quotes compare equal, case-insensitively ----------------

def test_normalize_curly_and_straight_quotes_are_equal():
    curly = "“Volumes rose,” he said—sharply."
    straight = '"volumes ROSE," HE SAID-sharply.'
    assert sources.normalize(curly) == sources.normalize(straight)


# --- verify_excerpt: re-fetch and check, exactly, near, not found, or unverifiable ---------

def test_verify_excerpt_verified_case_and_quote_insensitive():
    client = _client(HTML_BASIC)
    result = sources.verify_excerpt(
        "https://news.example/a",
        'the exchange\'s report said "volumes rose 12%."',
        client=client,
    )
    assert result.ok and result.status == "VERIFIED"


def test_verify_excerpt_not_found():
    client = _client(HTML_BASIC)
    result = sources.verify_excerpt(
        "https://news.example/a",
        "This exact sentence never appeared anywhere on the page at all.",
        client=client,
    )
    assert result.status == "NOT_FOUND" and not result.ok


def test_verify_excerpt_near_match_at_the_0_8_overlap_boundary():
    words = [f"word{i}" for i in range(1, 25)]  # 24 words -> 21 four-word grams.
    page = _client(f"<p>{' '.join(words)}</p>")
    excerpt_words = list(words)
    excerpt_words[11] = "different"  # One wrong word away from either edge: 4/21 grams lost.
    result = sources.verify_excerpt("https://news.example/a", " ".join(excerpt_words),
                                    client=page)
    assert result.status == "NEAR_MATCH"
    assert 0.8 <= result.overlap < 1.0


# --- Cutting and verifying agree (the 2026-09-28 NEAR_MATCH bug) ---------------------------

HTML_INLINE_PUNCTUATION = (
    "<html><body><article><p>The foundation said on-chain activity rose "
    '<a href="https://example.com/data">12%</a>, the largest weekly gain since March '
    "as fees paid by users climbed for a third straight week, according to the weekly "
    "network report published on Monday.</p></article></body></html>"
)


def test_an_excerpt_cut_across_an_inline_tag_next_to_punctuation_verifies():
    """visible_text drops an inline tag without a space ("12%</a>," reads "12%,"), as the
    page shows it; verification used to read every tag as a space ("12% ,"), so this exact
    cut came back NEAR_MATCH (overlap 0.87) and build dropped the catalyst."""
    lines = sources.visible_text(HTML_INLINE_PUNCTUATION)
    excerpt = sources.cut_excerpt(lines, "The foundation said", "published on Monday.")
    assert "rose 12%, the largest" in excerpt  # Exactly as the page prints it.
    result = sources.verify_excerpt("https://news.example/a", excerpt,
                                    client=_client(HTML_INLINE_PUNCTUATION))
    assert result.status == "VERIFIED" and result.ok


@pytest.mark.parametrize("page, start, end", [
    (HTML_INLINE_PUNCTUATION, "on-chain activity", "third straight week,"),
    ("<p>Token <em>unlock</em>: <b>4.2%</b> of supply (<i>vesting</i>) on Oct. 3.</p>",
     "Token unlock", "Oct. 3."),
    ("<p>“We’re <strong>live</strong>,” the team said &amp; added <span>more</span>.</p>",
     "“We’re", "more."),
    ("<div>Price</div><div>closed at <b>$1.25</b>; volume <a href='x'>doubled</a>!</div>",
     "closed at", "doubled!"),
])
def test_every_excerpt_the_kit_cuts_verifies_against_the_same_page(page, start, end):
    excerpt = sources.cut_excerpt(sources.visible_text(page), start, end)
    assert sources.verify_excerpt("https://news.example/p", excerpt, client=_client(page)).ok


def test_the_fix_does_not_loosen_verification():
    lines = sources.visible_text(HTML_INLINE_PUNCTUATION)
    excerpt = sources.cut_excerpt(lines, "The foundation said", "published on Monday.")
    one_word_changed = excerpt.replace("third straight week", "fourth straight week")
    result = sources.verify_excerpt("https://news.example/a", one_word_changed,
                                    client=_client(HTML_INLINE_PUNCTUATION))
    assert result.status == "NEAR_MATCH" and not result.ok
    # A cut that invents a space the page does not print still verifies only through the
    # tag-as-space text it verified through before; nothing new is accepted around it.
    spaced = excerpt.replace("12%,", "12% ,")
    assert sources.verify_excerpt("https://news.example/a", spaced,
                                  client=_client(HTML_INLINE_PUNCTUATION)).ok


def test_no_match_spans_the_seam_between_two_searched_texts():
    """The texts used to be concatenated, so an excerpt running from the end of the
    visible text into the start of the raw markup verified. Each is now searched alone."""
    page = "<p>Alpha beta.</p>"
    result = sources.verify_excerpt("https://news.example/s", "beta. <p>alpha",
                                    client=_client(page))
    assert not result.ok


def test_verify_excerpt_unverifiable_on_http_error():
    client = _client("", status=404)
    result = sources.verify_excerpt("https://news.example/missing", "anything", client=client)
    assert result.status == "UNVERIFIABLE" and "404" in result.detail


def test_verify_excerpt_rejects_non_https_without_any_network_call():
    result = sources.verify_excerpt("http://news.example/a", "anything", client=None)
    assert result.status == "UNVERIFIABLE"


# --- extract_published_at: page metadata only, never invented -----------------------------

def test_extract_published_at_from_meta_tag():
    html_ = ('<html><head><meta property="article:published_time" '
            'content="2026-09-25T21:23:07.000Z"></head></html>')
    assert sources.extract_published_at(html_) == "2026-09-25T21:23:07+00:00"


def test_extract_published_at_from_json_ld_graph():
    html_ = (
        '<html><head><script type="application/ld+json">'
        '{"@context": "https://schema.org", "@graph": [{"@type": "NewsArticle", '
        '"datePublished": "2026-09-27T05:30:38.536+00:00"}]}'
        "</script></head><body><p>Text.</p></body></html>"
    )
    assert sources.extract_published_at(html_) == "2026-09-27T05:30:38.536000+00:00"


def test_extract_published_at_from_time_tag():
    html_ = '<html><body><time datetime="2026-09-26T12:00:00+00:00">Sept 26</time></body></html>'
    assert sources.extract_published_at(html_) == "2026-09-26T12:00:00+00:00"


def test_extract_published_at_prefers_meta_tag_over_json_ld_and_time_tag():
    html_ = (
        '<html><head>'
        '<meta property="article:published_time" content="2026-09-25T21:23:07Z">'
        '<script type="application/ld+json">{"datePublished": "2020-01-01T00:00:00Z"}</script>'
        '</head><body><time datetime="2019-01-01T00:00:00Z"></time></body></html>'
    )
    assert sources.extract_published_at(html_) == "2026-09-25T21:23:07+00:00"


def test_extract_published_at_missing_metadata_is_none():
    html_ = "<html><body><p>No metadata at all.</p></body></html>"
    assert sources.extract_published_at(html_) is None


def test_extract_published_at_never_guesses_an_offset():
    # A publish time with no UTC offset at all: never invented, always null.
    html_ = ('<html><head><meta property="article:published_time" '
            'content="2026-09-25T21:23:07"></head></html>')
    assert sources.extract_published_at(html_) is None


def test_extract_published_at_a_bare_date_is_none():
    # A date with no time-of-day: filling one in would be inventing it.
    html_ = '<html><body><time datetime="2026-09-25">Sept 25</time></body></html>'
    assert sources.extract_published_at(html_) is None


def test_extract_published_at_malformed_content_is_none():
    html_ = ('<html><head><meta property="article:published_time" '
            'content="not-a-real-timestamp"></head></html>')
    assert sources.extract_published_at(html_) is None


# --- check_source: one fetch checks the excerpt and that published_at is the page's own -------

PAGE_WITH_TIMES = (
    '<html><head><meta property="article:published_time" content="2026-09-27T05:30:38.536Z">'
    '<script type="application/ld+json">{"datePublished": "2026-09-27T01:30:38-04:00"}</script>'
    "</head><body><time datetime=\"2026-09-26T09:00:00Z\">x</time><time datetime="
    '"2020-01-01T00:00:00Z">old</time><p>The exchange listed the token today.</p></body></html>'
)
CHECKED_AT = sources.datetime(2026, 9, 28, 12, 0, tzinfo=sources.UTC)


def _check(published_at, excerpt="The exchange listed the token today.", page=PAGE_WITH_TIMES):
    return sources.check_source("https://news.example/a", excerpt, published_at,
                                client=_client(page), clock=lambda: CHECKED_AT)


def test_page_published_times_reads_every_metadata_time_but_only_the_first_time_tag():
    assert sources.page_published_times(PAGE_WITH_TIMES) == (
        "2026-09-27T05:30:38.536000+00:00", "2026-09-27T05:30:38+00:00",
        "2026-09-26T09:00:00+00:00")


@pytest.mark.parametrize("declared", [
    "2026-09-27T05:30:38Z", "2026-09-27T01:30:38.536-04:00", "2026-09-26T09:00:00+00:00", None,
])
def test_check_source_accepts_a_time_from_the_pages_own_metadata_or_null(declared):
    checked = _check(declared)
    assert checked.ok and checked.retrieved_at == CHECKED_AT


def test_check_source_refuses_a_time_the_page_does_not_give():
    checked = _check("2026-09-27T06:00:00Z")
    assert checked.problem.startswith("PUBLISHED_AT_NOT_FROM_PAGE_METADATA")
    assert _check("2020-01-01T00:00:00Z").problem.startswith("PUBLISHED_AT_NOT_FROM")  # 2nd tag.
    bare = "<html><body><p>The exchange listed the token today.</p></body></html>"
    assert "gives no publish time" in _check("2026-09-27T05:30:38Z", page=bare).problem


def test_check_source_refuses_an_excerpt_that_does_not_verify_or_a_page_it_cannot_read():
    assert _check(None, excerpt="The exchange delisted the token today.").problem.startswith(
        "EXCERPT_")
    missing = sources.check_source("https://news.example/gone", "x", None,
                                   client=_client("", status=404))
    assert not missing.ok and missing.retrieved_at is None and "404" in missing.problem
