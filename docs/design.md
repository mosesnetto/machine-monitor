# MCC Machine Shift Monitor — Design & Architecture

## Problem

Manufacturing lines need real-time downtime visibility with escalation. Requirements:
- Poll machine status API every minute
- Shift-aware alerts (3 shifts in IST)
- Telegram alerts after 10 min breakdown
- **Real phone call** via 4G module (A7670E) after 30 min
- Runs unattended on Raspberry Pi 5
- Auto-recovery if 4G module hangs

## Solution

A single Python process (`mcc_monitor.py`) running as a systemd service on Raspberry Pi 5.

## Architecture

```
┌──────────────────┐     HTTPS/JSON      ┌──────────────────┐
│  Machine API     │ ◄─────────────────► │  Raspberry Pi 5  │
│  (MCC endpoint)  │   Status polling    │  (mcc_monitor)   │
└──────────────────┘   Every 60s         └────────┬─────────┘
                                                   │
                        ┌──────────────────────────┼──────────────────────────┐
                        ▼                          ▼                          ▼
               ┌─────────────────┐        ┌─────────────────┐        ┌─────────────────┐
               │  Telegram Bot   │        │  A7670E LTE     │        │  Piper TTS      │
               │  (Alerts,       │        │  Module         │        │  (Offline       │
               │   Reports,      │        │  (Voice calls)  │        │   Neural TTS)   │
               │   Voice notes)  │        │                 │        │                 │
               └─────────────────┘        └─────────────────┘        └─────────────────┘
                        ▲                          ▲                          ▲
                        │                          │                          │
                        └──────────────────────────┴──────────────────────────┘
                                              Single process
```

## Core Logic

### Shift Detection (IST)
```
Shift 1: 06:00–13:59  (Morning)
Shift 2: 14:00–21:59  (Afternoon)
Shift 3: 22:00–05:59  (Night)
```
- Auto-detected from system timezone (IST)
- Shift-end report sent 5 min before shift change

### Breakdown Detection
1. Poll machine status API → `is_running` boolean
2. If `False` for `BREAKDOWN_ALERT_MIN` (default 10) consecutive polls → **Telegram alert**
3. If `False` for `CALL_ALERT_MIN` (default 30) consecutive polls → **Phone call escalation**

### Phone Call Flow
```
Breakdown > 30 min
      │
      ▼
┌─────────────────┐
│ MCC_CALL_PROVIDER=auto    │──► Try A7670E module first
└────────┬────────┘
         │ fails
         ▼
┌─────────────────┐
│ Fallback to     │──► Telegram voice note (Piper TTS)
│ Telegram voice  │
└─────────────────┘
```

### 4G Module Auto-Recovery
- If AT commands fail → power-cycle USB port via `uhubctl` or `usb_modeswitch`
- Retry call up to 3 times
- Log all attempts to journald

## Configuration (`mcc_monitor.env`)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MCC_API_BASE` | ✅ | — | Machine status endpoint (returns JSON with `is_running`) |
| `MCC_BOT_TOKEN` | ✅ | — | Telegram bot token from @BotFather |
| `MCC_CALL_PROVIDER` | | `auto` | `auto` \| `module` \| `telegram` |
| `BREAKDOWN_CALL_TO` | | — | Phone number for SIM call (E.164: `+91xxxxxxxxxx`) |
| `MCC_BREAKDOWN_ALERT_MIN` | | `10` | Minutes down before Telegram alert |
| `MCC_CALL_ALERT_MIN` | | `30` | Minutes down before phone call |
| `MCC_SHIFT_REPORT` | | `1` | Enable shift-end production report |
| `A7670_AT_PORT` | | `/dev/ttyUSB1` | 4G module AT command serial port |
| `A7670_AUDIO_DEVICE` | | `default` | ALSA device for audio into module MIC |
| `BREAKDOWN_CALL_TEXT` | | — | Custom message for call/TTS |

## Systemd Service

```ini
# /etc/systemd/system/mcc_monitor.service
[Unit]
Description=MCC Machine Shift Monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/opt/machine-monitor
EnvironmentFile=/opt/machine-monitor/mcc_monitor.env
ExecStart=/opt/machine-monitor/.venv/bin/python mcc_monitor.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
# USB device access for 4G module
SupplementaryGroups=dialout audio

[Install]
WantedBy=multi-user.target
```

## Telegram Bot Setup

1. Create bot with @BotFather → get token
2. Add token to `mcc_monitor.env`
3. Start service → bot auto-discovers chat ID on first message
4. Multiple recipients supported (each stored on first contact)

## Operations

```bash
# Status
systemctl status mcc_monitor

# Live logs
journalctl -u mcc_monitor -f

# Restart
sudo systemctl restart mcc_monitor

# View config
cat /opt/machine-monitor/mcc_monitor.env
```

## Hardware

| Component | Spec | Notes |
|-----------|------|-------|
| Raspberry Pi | Pi 5 (4GB or 8GB) | USB 3.0 for 4G module |
| 4G Module | Quectel A7670E | LTE Cat 4, voice capable |
| SIM | Any with voice + data | Airtel/Jio/Vi work in India |
| Audio | USB audio dongle | Connect to module MIC input |
| Power | 5V 5A (Pi 5) + module power | Module may need separate 5V |

## File Structure

```
machine-monitor/
├── mcc_monitor.py          # Main monitor (single file, ~33KB)
├── install.sh              # Systemd + venv + deps installer
├── requirements.txt        # python-telegram-bot, piper-tts, pyserial, uhubctl
├── mcc_monitor.env         # Configuration (gitignored)
├── docs/
│   └── design.md           # This file
├── LICENSE
├── README.md
└── .github/workflows/ci.yml
```

## Testing

```bash
# Dry run (no network calls)
MCC_BOT_TOKEN=test MCC_API_BASE=http://localhost MCC_CALL_PROVIDER=telegram \
  python mcc_monitor.py --dry-run

# Test Telegram
python -c "
import asyncio
from telegram import Bot
bot = Bot(token='YOUR_TOKEN')
asyncio.run(bot.send_message(chat_id=YOUR_CHAT_ID, text='Test from Pi'))
"

# Test 4G module
python -c "
import serial
ser = serial.Serial('/dev/ttyUSB1', 115200, timeout=2)
ser.write(b'AT\r\n')
print(ser.read_all().decode())
"
```

## Roadmap

- [ ] Multi-machine support (config-driven machine list)
- [ ] Per-machine recipient routing
- [ ] Web dashboard for downtime stats (Grafana + InfluxDB)
- [ ] MQTT telemetry output for SCADA integration
- [ ] OTA updates via GitHub Actions + systemd

## License

MIT — see [LICENSE](../LICENSE)