# Design: polish-supporter-mail

## Pinned decisions (D1–D6)

- **D1. Plaintext stays the source of truth; HTML is an alternative, not a
  replacement.** `build_credential_mail` keeps producing today's plaintext
  `body` byte-for-byte (the existing tests on it must keep passing
  unchanged) and additionally fills a new `Mail.html`. `smtp_sender` builds
  the message with `EmailMessage.set_content(body)` followed by
  `add_alternative(html, subtype="html")` when `html` is not `None` —
  which yields `multipart/alternative` with `text/plain` first, the order
  mail clients expect. When `html` is `None` (the notify mail, and every
  existing test that constructs a `Mail` directly) the message is exactly
  today's single-part `text/plain`.
- **D2. The interpolation invariant is unchanged and now spec-pinned.**
  The HTML template interpolates only `credential` (`username:password`),
  `public_endpoint`, and the three static constants (`_DOCS_URL`,
  `_DEMO_URL`, `_INSTALL_URL`). No other argument is added to
  `build_credential_mail`'s signature — the type checker and the existing
  "never echoes a donor-supplied field" test then make it structurally
  impossible to leak a BMC field. All interpolated values pass through
  `html.escape(..., quote=True)` before insertion. This is defence in
  depth, not the control: the values are operator/facade-generated, but a
  future `public_endpoint` with `&` in a query string must still not
  produce broken markup.
- **D3. Mail-client HTML, not web HTML.** Hard constraints on the template,
  each of them tested (see "Testing constraints"):
  - No external resource of any kind: no `<img>`, `<link>`, `@import`,
    `url(...)`, web fonts, or tracking pixel. The only URLs anywhere in the
    HTML are the three static constants and `public_endpoint`. A donor
    opening this mail must generate zero network requests to anyone.
  - No `<script>`, no `<form>`, no `on*=` attributes.
  - Inline CSS only (`style="..."`), plus one `<style>` block in `<head>`
    for the `@media (prefers-color-scheme: dark)` overrides that cannot be
    expressed inline. Layout via nested `<table>` — Outlook still needs it.
  - Single column, `max-width: 600px`, centred. System font stack
    (`-apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif`);
    monospace stack for the credential and the code blocks.
  - `<meta name="color-scheme" content="light dark">` and
    `<meta name="supported-color-schemes" content="light dark">`; colours
    chosen to read on both a light and a dark background without the media
    query, the media query only improves them.
  - The credential appears in **exactly one** monospace block, on one line,
    with `white-space: pre` / `word-break: keep-all` so it never wraps
    mid-string; long enough for `supporter-<hex>:<password>` (~80 chars) —
    horizontal scroll inside the block is acceptable, a broken credential
    is not.
  - The GitHub Actions snippet and the CLI snippet are reproduced as `<pre>`
    blocks with the same content as the plaintext part (YAML indentation
    preserved). They reference "the credential above", never a second copy
    of the password — same rule as the plaintext part.
  - `lang="en"`, semantic headings (`<h1>` once), `role="presentation"` on
    layout tables, sufficient contrast (≥ 4.5:1 for body text in both
    schemes). Screen-reader users get the plaintext part anyway, but the
    HTML must not be worse than it.
- **D4. Tone and structure mirror the plaintext.** Sections in the same
  order: thanks → the credential → what it is (best-effort, no SLA, fair
  use) → keep it safe → how to use it (Actions, then terminal) → links.
  Plain English, no marketing copy, no emoji (project convention: output
  runs in CI, and this project's voice is the same in mail). The
  restraint *is* the design: a quiet, well-typeset letter that makes the
  credential the obvious centre of the page. A builder equipped with a
  frontend/design skill should use it for typographic hierarchy, spacing,
  and the code-block treatment — not to add imagery or motion, which D3
  forbids.
- **D5. `NETNL_SUPPORTER_MIN_AMOUNT` default `0` → `2`.** Owner decision
  2026-09-04, resolving `add-supporter-issuance` O3. Only the default in
  `netnl.settings._load_supporter` changes (`Decimal("2")`); `qualifies`
  already implements "at or above" (`amount < min_amount` → ignore), so
  `2.00` qualifies and `1.99` does not. The currency is whatever BMC
  reports for the account — `NETNL_SUPPORTER_CURRENCY` stays optional and
  unset by default. The `add-supporter-issuance` change's own `design.md` is
  historical and is **not** edited; the spec delta in this change is where
  the new default lives. Its `tasks.md` gets exactly one edit: O3 ticked,
  with a pointer to this change.
- **D6. Evidence includes the rendered mail.** Because habitat's reviewer
  and the owner cannot open a mail client on a K8s Job, the builder's
  run output must include the full rendered HTML of one sample credential
  mail (built with a dummy `username`/`password`/`public_endpoint`), so it
  can be pasted into a browser or an HTML-mail preview tool. No file is
  committed for this — it is evidence, not an artefact of the repo.

## Configuration

One row changes; nothing is added.

| Variable | Old default | New default | Notes |
|---|---|---|---|
| `NETNL_SUPPORTER_MIN_AMOUNT` | `0` | `2` | Decimal string, `Decimal` never `float`; "at or above"; account currency. |

## `mail.py` shape after this change

```python
@dataclass(frozen=True)
class Mail:
    to: str
    subject: str
    body: str            # plaintext, unchanged
    html: str | None = None

_CREDENTIAL_HTML_TEMPLATE = """..."""   # static; {credential} {public_endpoint} {docs_url} {demo_url} {install_url}

def build_credential_mail(*, to, username, password, public_endpoint) -> Mail:
    credential = f"{username}:{password}"
    body = _CREDENTIAL_BODY_TEMPLATE.format(...)          # as today
    html = _CREDENTIAL_HTML_TEMPLATE.format(
        credential=html.escape(credential, quote=True),
        public_endpoint=html.escape(public_endpoint, quote=True),
        docs_url=..., demo_url=..., install_url=...,      # escaped too, cheap
    )
    return Mail(to=to, subject=_CREDENTIAL_SUBJECT, body=body, html=html)
```

`smtp_sender._send`: after `message.set_content(mail.body)`, add
`if mail.html is not None: message.add_alternative(mail.html, subtype="html")`.
Nothing else in `_send` changes — the `except Exception → DeliveryError`
root-fix, the `[mail.to]` envelope, and the starttls-before-login order are
untouched. Note the stdlib module `html` collides with a local name `html`
— import as `import html as _html` or name the variable `html_body`.

## Testing constraints

Extend `tests/netnl/test_netnl_mail.py`; do not weaken any existing test.

- Existing plaintext tests pass unchanged (D1).
- `Mail(...)` without `html` still produces a single-part `text/plain`
  message through `smtp_sender` (assert on the message the fake SMTP
  captured: `not is_multipart()`, content type `text/plain`).
- Credential mail through `smtp_sender` is `multipart/alternative` with
  parts `[text/plain, text/html]` in that order; the plaintext part equals
  `mail.body`.
- HTML contains the credential string **exactly once**, and the password
  substring exactly once (no second copy in the snippets).
- HTML never contains any donor-supplied field: reuse the existing
  "never echoes a donor-supplied field" approach — build with sentinel
  values and assert none of the not-passed sentinels appear in `html`.
- HTML contains no `<script`, `<img`, `<link`, `<form`, `@import`,
  `url(`, or ` on[a-z]+=` (case-insensitive) — the "zero network
  requests" guarantee (D3).
- Every `http://`/`https://` occurrence in the HTML is one of
  `_DOCS_URL`, `_DEMO_URL`, or `public_endpoint` (`_INSTALL_URL` is a
  `git+https://` string; include it in the allowlist).
- Escaping: a `public_endpoint` of `https://x.example/?a=1&b=<2>` appears
  in the HTML as `&amp;`/`&lt;`/`&gt;`, and appears raw in the plaintext
  part.
- `build_notify_mail` still has `html is None`.
- `tests/netnl/test_netnl_supporter_settings.py`: the default assertion at
  the current `== Decimal("0")` becomes `== Decimal("2")`; add one test
  that with the variable unset, a live `donation.created` of `1.99` is
  `IGNORE_AMOUNT` and `2.00`/`"2"`/`2` is `ISSUE` through `bmc.qualifies`
  with a `QualifyConfig` built from real `Settings` (not a hand-made
  config) — this is the test that proves the default actually reaches
  the decision.
- `tests/netnl/test_netnl_supporter.py`: any fixture that relies on the
  old default to get a delivery issued must set an amount ≥ 2 in its
  payload (or set `NETNL_SUPPORTER_MIN_AMOUNT=0` explicitly in
  `supporter_env`) — prefer the former so the suite exercises the real
  default. `test_netnl_leak.py`'s supporter cases must keep asserting no
  password/secret/supporter address leaks, now across both MIME parts.

## Reused, not reimplemented

- `EmailMessage.add_alternative` — stdlib; no `MIMEMultipart` hand-assembly.
- `html.escape` — stdlib.
- The `Sender` seam and `RecordingSender` — unchanged; they already carry
  the whole `Mail`.
- `_resolve_decimal` in settings — unchanged; only the default argument
  changes.

## Exit criteria for this build

- `sh scripts/verify.sh` green (full `uv run pytest -q`).
- `openspec validate polish-supporter-mail --strict` and
  `openspec validate --all --strict` pass.
- Run output contains the rendered sample HTML (D6).
- `git diff --stat` touches only: `src/netnl/mail.py`, `src/netnl/settings.py`,
  `tests/netnl/test_netnl_mail.py`, `tests/netnl/test_netnl_supporter_settings.py`,
  `tests/netnl/test_netnl_supporter.py` (if fixtures need amounts),
  `tests/netnl/test_netnl_leak.py` (if extended), `deploy/.env.example`,
  `docs/how-to/supporter-webhook.md`, `docs/how-to/supporter-key.md`,
  `CHANGELOG.md`, and `openspec/changes/polish-supporter-mail/tasks.md`
  (ticking). Anything else is a deviation to report, not to do.
