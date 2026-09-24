# Security Policy

## Supported versions

Security fixes are provided for the latest state of the `main` branch.

## Reporting a vulnerability

**Do not** open a public GitHub issue for a security problem.

This system runs on production hardware tied to a real machine-status API and
can place phone calls. If you find something sensitive (secret handling, call
abuse, unauthorized API access), report it privately:

- **Security contact:** mosesnetto@gmail.com

You should receive a reply within 3 business days. Please include a
description of the issue, impact, and reproduction steps (redacting any
secrets).

## Operational guidance

- `mcc_monitor.env` holds the Telegram bot token and API URL — keep it out of
  version control (it is gitignored) and restrict file permissions:
  `chmod 600 mcc_monitor.env`.
- `BREAKDOWN_CALL_TO` must be a trusted number; the program will only ever
  call that configured destination.
- Do not expose the Pi or its API to the public internet without network
  protection.