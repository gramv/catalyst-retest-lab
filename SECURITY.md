# Security

## Scope

Catalyst Lab trades **paper accounts only**. The broker endpoints are fixed to Alpaca's paper
environment in code and there is no live-order path. Even so, a deployment holds credentials
(an Alpaca paper key, optionally a TypeSafe key, database passwords and API tokens) and an
append-only ledger whose integrity matters.

## Reporting a vulnerability

Please report security problems privately, not in a public issue: use the repository host's
private vulnerability reporting ("Report a vulnerability" under the Security tab). Include what
you found, how to reproduce it, and its impact. We aim to acknowledge reports within a week.

Especially relevant:

- any way to place a broker order without the one-use risk authorization, or to reach a
  non-paper endpoint;
- any way for a research-agent token to set size, trigger execution or read another agent's
  data;
- any way to rewrite or delete ledger history, or to make an application role own schema;
- secrets that leak into logs, receipts, events, images or the public page.

## Handling secrets

- Never commit `.env` files or keys. The local Docker stack generates its own database passwords
  at first start; your broker key lives only in a `.env.paper` you create (mode 0600).
- Use a **paper** key. The engine refuses key ids that do not look like paper keys.
- Rotate any key that was ever pasted into an issue, a log or a chat.
