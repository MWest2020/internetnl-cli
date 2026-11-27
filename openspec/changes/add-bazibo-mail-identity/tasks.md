# Tasks: add-bazibo-mail-identity

## T1. OpenSpec change

- [x] 1.1 proposal.md, tasks.md
- [x] 1.2 Spec delta `bazibo-unlock` (ADDED: the code mail's sender identity)

## T2. Settings

- [x] 2.1 `BaziboRelay`, `BaziboMail`, `BaziboSettings.mail`
- [x] 2.2 Fail-fast on every half configuration; no secret in any error;
      password `repr=False`
- Verify: `uv run pytest tests/netnl/test_netnl_bazibo_mail_identity.py -k "refused or repr or not_read"`

## T3. Sender

- [x] 3.1 `Mail.product`; `build_bazibo_mail` marks `bazibo`
- [x] 3.2 `smtp_sender(reply_to=...)`; envelope sender = bare address of From
- [x] 3.3 `build_sender(settings)` routes on product; fallback to the
      bridge relay with the Bazibo From/Reply-To
- [x] 3.4 App, `bazibo poll`, `bazibo watch`, `bazibo mint --mail` use it
- Verify: `uv run pytest tests/netnl/test_netnl_bazibo_mail_identity.py`

## T4. Docs

- [x] 4.1 Settings table and note in `docs/how-to/supporter-webhook.md`
- [x] 4.2 `deploy/.env.example`, `CHANGELOG.md`

## T5. Deploy (homelab)

- [ ] 5.1 Interim: image bump, `NETNL_BAZIBO_MAIL_REPLY_TO=hallo@bazibo.nl`
      in the ConfigMap (From falls back to the Soverin account address
      under the name "Bazibo"); one hand-issued test code
- [ ] 5.2 Target: once `mail.bazibo.nl:587` is reachable and Plesk accepts
      SMTP auth for hallo@bazibo.nl, the Secret patch from the runbook
      `bazibo-nl` ("Switch Bazibo mail to mail.bazibo.nl")
