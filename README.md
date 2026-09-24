# MCC Machine Shift Monitor

Real-time machine downtime monitoring for a manufacturing line, running on a Raspberry Pi 5. It polls the MCC (Machine Condition/Control) API every minute, derives the current shift from IST, and alerts operators **on Telegram with a spoken voice message** — and can make a **real phone call** through a 4G cellular module (A7670E).

## Features

- **Shift-aware monitoring** — machine shift autodetected from IST:
  - Shift 1: 06:00–13:59 IST
  - Shift 2: 14:00–21:59 IST
  - Shift 3: 22:00–05:59 IST
- **Breakdown alerts** — Telegram message after the machine is down for 10 minutes (configurable).
- **Real phone call** — after a 30-minute breakdown (configurable), dials an operator through an A7670E LTE module and plays a TTS audio message into the call.
  - Falls back to a free Telegram voice note if the SIM call is unavailable.
- **Shift-end production report** — sends a report a few minutes before each shift ends.
- **Auto-recovery** — if the 4G module hangs, the program power-cycles it over USB and retries the call automatically.
- **TTS via Piper** — offline neural TTS (en_US-ryan) with Google TTS fallback.

## Architecture

```
Machine status API  ⇄  Raspberry Pi 5  ⇄  Telegram bot
                                |
                                └─ A7670E LTE module (SIM voice call)
                                     └─ USB audio dongle → module MIC
```

- Single process (`mcc_monitor.py`), one polling loop, run as a systemd service.
- All config via environment file (`mcc_monitor.env`), loaded by systemd.
- Logs to `mcc_monitor.log` and journald.

## Getting Started

### Prerequisites

- Python 3.11+
- A Telegram bot (create one with [@BotFather](https://t.me/BotFather))
- Optional (phone-call feature): A7670E LTE module, a SIM with voice, USB audio dongle

### Install

```bash
sudo bash install.sh
```

Edit `mcc_monitor.env`:

```ini
MCC_API_BASE=https://your-machine-api.example.com/  # REQUIRED: machine status endpoint
MCC_BOT_TOKEN=123456:your-bot-token                  # REQUIRED
MCC_CALL_PROVIDER=auto                               # auto|module|telegram
BREAKDOWN_CALL_TO=+91xxxxxxxxxx                      # optional: real SIM call
```

Install configures the systemd service `mcc_monitor.service` and starts it.

### Manual run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
MCC_BOT_TOKEN=<token> .venv/bin/python mcc_monitor.py
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `MCC_BOT_TOKEN` | *(required)* | Telegram bot token |
| `MCC_API_BASE` | *(required)* | Machine status API endpoint |
| `MCC_CALL_PROVIDER` | `auto` | `auto`/`module`/`telegram` call backend |
| `MCC_BREAKDOWN_ALERT_MIN` | `10` | Minutes down before Telegram alert |
| `MCC_CALL_ALERT_MIN` | `30` | Minutes down before phone call |
| `MCC_SHIFT_REPORT` | `1` | Enable shift-end report |
| `A7670_AT_PORT` | `/dev/ttyUSB1` | 4G module serial port |
| `A7670_AUDIO_DEVICE` | `default` | ALSA playback device into module MIC |
| `BREAKDOWN_CALL_TO` | empty | Phone number for SIM call (E.164) |
| `BREAKDOWN_CALL_TEXT` | — | Message to speak/play on the call |

## Telegram Setup

1. Create a bot with @BotFather and put the token in `mcc_monitor.env`.
2. Message the bot once — the monitor auto-discovers your chat ID.
3. Multiple recipients are supported (each is stored on first contact).

## Operation

```bash
systemctl status mcc_monitor     # status
journalctl -u mcc_monitor -f     # live logs
sudo systemctl restart mcc_monitor
```

## Roadmap

- Multi-machine support (machine list driven by config/API)
- Per-machine recipient routing
- Web dashboard for downtime stats

## License

Proprietary — © 2026 Moses Netto. All rights reserved.