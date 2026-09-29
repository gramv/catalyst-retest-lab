"""The research kit against the Railway app: HTTPS only off this machine, owner-only token.

Mock HTTP only; no request leaves the process (package cloud).
"""

import httpx
import pytest

from research_agent import context, submit, token

FAKE_TOKEN = "fixture-cloud-agent-token-abcdefghijklmnopqrstuvwxyz"  # noqa: S105 - fixture


@pytest.mark.parametrize("url", ["https://trader-production.up.railway.app",
                                 "https://trader-production.up.railway.app/",
                                 "http://127.0.0.1:8780", "http://localhost:8791"])
def test_https_or_local_loopback_base_urls_are_accepted(url):
    assert submit.checked_base_url(url) == url.rstrip("/")


@pytest.mark.parametrize("url", ["http://trader-production.up.railway.app",
                                 "http://trader.railway.internal:8080",
                                 "https://user:pw@trader.up.railway.app",
                                 "https://trader.up.railway.app/api/v1",
                                 "https://trader.up.railway.app?token=x",
                                 "ftp://trader.up.railway.app", "trader.up.railway.app", ""])
def test_anything_that_could_expose_the_token_is_refused_before_any_request(url):
    sent = []
    client = httpx.Client(transport=httpx.MockTransport(lambda r: sent.append(r) or
                                                        httpx.Response(202, json={})))
    with pytest.raises(submit.SubmitError, match="RESEARCH_AGENT_BASE_URL_REFUSED"):
        submit.submit_report(url, FAKE_TOKEN, {"picks": []}, client=client)
    with pytest.raises(context.ContextError, match="RESEARCH_AGENT_BASE_URL_REFUSED"):
        context.fetch_context(url, FAKE_TOKEN, client=client)
    assert sent == []


def test_submit_to_the_railway_url_sends_the_token_only_in_the_header():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(202, json={"status": "ACCEPTED"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = submit.submit_report("https://trader-production.up.railway.app/", FAKE_TOKEN,
                                  {"picks": []}, client=client)
    assert result.accepted
    assert str(seen[0].url) == ("https://trader-production.up.railway.app"
                                "/api/v1/lab/research-reports")
    assert seen[0].headers["authorization"] == "Bearer " + FAKE_TOKEN
    assert FAKE_TOKEN not in result.raw_text


def test_a_token_file_readable_by_others_or_a_symlink_is_refused(tmp_path):
    path = tmp_path / "agent-token"
    path.write_text(FAKE_TOKEN)
    path.chmod(0o644)
    with pytest.raises(token.TokenUnavailable, match="RESEARCH_AGENT_TOKEN_FILE_NOT_PRIVATE"):
        token.load_token(env={token.TOKEN_FILE_ENV: str(path)})
    path.chmod(0o600)
    assert token.load_token(env={token.TOKEN_FILE_ENV: str(path)}) == FAKE_TOKEN
    link = tmp_path / "linked-token"
    link.symlink_to(path)
    with pytest.raises(token.TokenUnavailable, match="RESEARCH_AGENT_TOKEN_FILE_UNREADABLE"):
        token.load_token(env={token.TOKEN_FILE_ENV: str(link)})
    with pytest.raises(token.TokenUnavailable) as refused:
        token.load_token(env={token.TOKEN_FILE_ENV: str(tmp_path)})  # A directory.
    assert FAKE_TOKEN not in str(refused.value)
