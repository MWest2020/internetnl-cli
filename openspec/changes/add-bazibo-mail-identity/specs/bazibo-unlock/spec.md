# Spec Delta: bazibo-unlock (add-bazibo-mail-identity)

## ADDED Requirements

### Requirement: The code mail has its own sender identity

The facade SHALL send the buyer's code mail with the Bazibo sender
identity and every other mail of the bridge (the netnl supporter-key mail,
operator notifications, watch alerts) with the bridge's `NETNL_SMTP_*`
settings, unchanged. The Bazibo identity is: the relay
`NETNL_BAZIBO_SMTP_HOST` (with `_PORT`, default 587, `_USERNAME`,
`_PASSWORD` and `_MODE`, `starttls` or `ssl`) when a host is set, else the
bridge's relay; the From `<NETNL_BAZIBO_SMTP_FROM_NAME> <address>` with
the name defaulting to `Bazibo` and the address to
`NETNL_BAZIBO_SMTP_FROM`, else the address of `NETNL_SMTP_FROM`; and a
`Reply-To` header with `NETNL_BAZIBO_MAIL_REPLY_TO` when that is set.

#### Scenario: Own relay

- WHEN `NETNL_BAZIBO_SMTP_HOST`, `_FROM`, `_USERNAME` and `_PASSWORD` are set
- THEN the code mail is sent through that host, logged in with that
  username, with `From: Bazibo <FROM>` and the envelope sender `FROM`
- AND the supporter-key mail is still sent through `NETNL_SMTP_HOST` with
  `From: NETNL_SMTP_FROM` and no `Reply-To`

#### Scenario: Fallback to the bridge relay

- WHEN `NETNL_BAZIBO_SMTP_HOST` is unset and `NETNL_BAZIBO_SMTP_FROM` and
  `NETNL_BAZIBO_MAIL_REPLY_TO` are set
- THEN the code mail is sent through the bridge's relay and login, with
  `From: Bazibo <NETNL_BAZIBO_SMTP_FROM>` and the `Reply-To` header

### Requirement: A half-configured Bazibo relay refuses startup

The facade SHALL refuse to start, naming the variable, when a Bazibo relay
part (`NETNL_BAZIBO_SMTP_PORT`, `_USERNAME`, `_PASSWORD`, `_MODE`) is set
without `NETNL_BAZIBO_SMTP_HOST`; when a host is set without
`NETNL_BAZIBO_SMTP_FROM`; when only one of username and password is set;
when the mode is not `starttls` or `ssl`; or when the From or Reply-To is
not one plain address. No startup error, log line or delivery error SHALL
contain the relay's username or password.

#### Scenario: Password without a host

- WHEN `NETNL_BAZIBO_SMTP_PASSWORD` is set and `NETNL_BAZIBO_SMTP_HOST` is not
- THEN startup fails naming `NETNL_BAZIBO_SMTP_HOST`, and the message does
  not contain the password

#### Scenario: A failing relay

- WHEN the Bazibo relay refuses the login with a reply that quotes the
  username and password
- THEN the send raises the fixed delivery error and the log carries only
  the exception type
