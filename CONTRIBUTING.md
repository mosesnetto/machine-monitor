# Contributing to MCC Machine Shift Monitor

Thanks for considering a contribution! This project monitors a manufacturing
line in production, so reliability and care matter.

## Getting started

```bash
# Local environment (no root needed except for install.sh / systemd)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

To run the monitor against a real machine API you need a valid `mcc_monitor.env`
(see `README.md`). Note: `mcc_monitor.env`, logs, and chat state are gitignored
and must never be committed.

## Development workflow

1. **Branch.** Create a topic branch off `main`.
2. **Code.** Keep changes focused and well-commented. Verify syntax:
   ```bash
   python -m py_compile mcc_monitor.py
   bash -n install.sh
   ```
3. **Test on a real/Pi device.** Call/alert paths depend on a physical A7670E
   module and SIM — test behaviour changes against the real hardware before
   opening a PR.
4. **Open a pull request.** Use the PR template. CI runs syntax checks on
   Python 3.11–3.13 and `bash -n` automatically.

## Style & safety notes

- Never put phone numbers, bot tokens, or API secrets in code or docs.
- Config stays in `mcc_monitor.env` (loaded by systemd), not in source.
- Treat the module's USB reset (`_a7670_recover`) as a last resort; test
  recovery paths on hardware.
- Log lines should be human-readable and useful via `journalctl`.

## Reporting issues

Use the issue templates. Include your OS/Pi environment, relevant
`journalctl -u mcc_monitor` output (redact secrets), and reproduction steps.
Security issues go through `SECURITY.md`, not the public tracker.