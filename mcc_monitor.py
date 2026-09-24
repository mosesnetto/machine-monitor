#!/usr/bin/env python3
"""
MCC Machine Shift Monitor for Raspberry Pi 5

Monitors the MCC machine API and reports to Telegram:
  * Shift is derived from current IST time (always IST):
       06:00 - 13:59 IST  -> shift 1
       14:00 - 21:59 IST  -> shift 2
       22:00 - 05:59 IST  -> shift 3
  * While the machine is RUNNING (machine_status true):
       sends an updated report every 30 minutes
  * While the machine is STOPPED (machine_status false):
       sends a MACHINE BREAKDOWN alert if stopped for more than 10 minutes
       (includes the time the breakdown happened)

All times displayed are Indian Standard Time (IST, Asia/Kolkata).
"""

import os
import re
import sys
import time
import json
import struct
import select
import shutil
import logging
import requests
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "mcc_monitor.log")
CHAT_ID_FILE = os.path.join(BASE_DIR, "chat_id.txt")
LAST_DATE_FILE = os.path.join(BASE_DIR, "last_date.txt")
UPDATE_OFFSET_FILE = os.path.join(BASE_DIR, "update_offset.txt")

BOT_TOKEN = os.environ.get("MCC_BOT_TOKEN", "")
API_BASE = os.environ.get("MCC_API_BASE", "")
if not API_BASE:
    log.critical("MCC_API_BASE is not set in mcc_monitor.env - cannot fetch machine status")
MACHINE_ID = int(os.environ.get("MCC_MACHINE_ID", "74"))
POLL_INTERVAL_SECONDS = int(os.environ.get("MCC_POLL_INTERVAL_SECONDS", "60"))
BREAKDOWN_ALERT_MINUTES = int(os.environ.get("MCC_BREAKDOWN_ALERT_MIN", "10"))
CALL_ALERT_MINUTES = int(os.environ.get("MCC_CALL_ALERT_MIN", "30"))
ERROR_ALERT_MINUTES = int(os.environ.get("MCC_ERROR_ALERT_MIN", "30"))
BREAKDOWN_CALL_TEXT = os.environ.get(
    "MCC_CALL_TEXT", "MCC line 1 is in breakdown for more than 30 minutes, please check."
)
PIPER_BIN = os.environ.get("MCC_PIPER_BIN", os.path.join(BASE_DIR, ".venv_tts", "bin", "piper"))
PIPER_MODEL = os.environ.get(
    "MCC_PIPER_MODEL", os.path.join(BASE_DIR, "voices", "en_US-ryan-medium.onnx")
)
FFMPEG_BIN = os.environ.get("MCC_FFMPEG_BIN", "ffmpeg")
TTS_FALLBACK_GOOGLE = os.environ.get("MCC_TTS_FALLBACK_GOOGLE", "1") == "1"
BREAKDOWN_CALL_TO = os.environ.get("BREAKDOWN_CALL_TO")
TELEGRAM_CHAT_ID = os.environ.get("MCC_CHAT_ID")
DRY_RUN = os.environ.get("MCC_DRY_RUN", "") == "1"

# Call provider chain: "auto" -> A7670 module first, then Telegram voice note
MCC_CALL_PROVIDER = os.environ.get("MCC_CALL_PROVIDER", "auto").strip().lower()
A7670_AT_PORT = os.environ.get("A7670_AT_PORT", "/dev/ttyUSB1")
A7670_BAUD = int(os.environ.get("A7670_BAUD", "115200"))
A7670_AUDIO_DEVICE = os.environ.get("A7670_AUDIO_DEVICE", "default")
A7670_AUDIO_MAX_SECONDS = int(os.environ.get("A7670_AUDIO_MAX_SECONDS", "30"))
A7670_RING_TIMEOUT_SECONDS = int(os.environ.get("A7670_RING_TIMEOUT_SECONDS", "60"))
SHIFT_REPORT_ENABLED = os.environ.get("MCC_SHIFT_REPORT", "1") == "1"
SHIFT_REPORT_MIN_BEFORE = int(os.environ.get("MCC_SHIFT_REPORT_MIN_BEFORE", "2"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("mcc_monitor")

if not BOT_TOKEN:
    log.critical("MCC_BOT_TOKEN is not set in mcc_monitor.env - Telegram alerts will not work")

TELEGRAM_API = "https://api.telegram.org/bot"


def now_ist():
    return datetime.now(IST)


def get_shift(dt):
    h = dt.hour
    if 6 <= h < 14:
        return 1
    if 14 <= h < 22:
        return 2
    return 3


def shift_end_time(dt):
    h = dt.hour
    if 6 <= h < 14:
        return dt.replace(hour=14, minute=0, second=0)
    if 14 <= h < 22:
        return dt.replace(hour=22, minute=0, second=0)
    return dt.replace(hour=6, minute=0, second=0)


def parse_stop_minutes(stop_str):
    m = re.search(r"(\d+(?:\.\d+)?)\s*min", str(stop_str), re.IGNORECASE)
    return float(m.group(1)) if m else None


def parse_hms(hms_str):
    m = re.fullmatch(r"(\d{1,2}):(\d{2}):(\d{2})", str(hms_str).strip())
    if not m:
        return None
    return datetime.now(IST).replace(
        hour=int(m.group(1)), minute=int(m.group(2)), second=int(m.group(3)), microsecond=0
    )


def build_api_url(dt, date_str=None):
    date_str = date_str or dt.strftime("%Y-%m-%d")
    return (
        f"{API_BASE}?shift={get_shift(dt)}"
        f"&date={date_str}&machine_id={MACHINE_ID}"
    )


def fetch_report(dt, date_str=None):
    url = build_api_url(dt, date_str)
    log.info("Fetching %s", url)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("Unexpected API response")
    return data


def load_last_date():
    try:
        with open(LAST_DATE_FILE) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def save_last_date(date_str):
    with open(LAST_DATE_FILE, "w") as f:
        f.write(date_str)


def fetch_with_fallback(dt):
    expected = dt.strftime("%Y-%m-%d")
    try:
        data = fetch_report(dt, expected)
        if load_last_date() != expected:
            save_last_date(expected)
        return data
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            candidates = []
            saved = load_last_date()
            if saved and saved != expected:
                candidates.append(saved)
            yesterday = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
            if yesterday != expected and yesterday not in candidates:
                candidates.append(yesterday)
            for cand in candidates:
                log.warning(
                    "No data for %s yet (404); trying date %s",
                    expected,
                    cand,
                )
                try:
                    data = fetch_report(dt, cand)
                    if load_last_date() != cand:
                        save_last_date(cand)
                    return data
                except Exception as fb:
                    log.warning("Fallback fetch for %s failed: %s", cand, fb)
        raise


def fmt_ist(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S IST")


def format_report(data, dt, header):
    shift = get_shift(dt)
    status = "RUNNING" if data.get("machine_status") else "STOPPED"
    lines = [
        header,
        "",
        f"Machine: {data.get('machine')} | {data.get('machine_name')}",
        f"Shift: {shift} | Date: {dt.strftime('%Y-%m-%d')}",
        f"Part: {data.get('part')}",
        f"Status: {status}",
        "",
        f"Time: {data.get('time')}",
        f"Good Products: {data.get('good_products')}",
        f"Bad Products: {data.get('bad_products')}",
        f"Total Count: {data.get('total_count')}",
        f"Running Hours: {data.get('machine_running_hours')}",
        f"OEE: {data.get('oee')}",
        f"TEEP: {data.get('teep')}",
        f"Last Updated: {data.get('last_updated')}",
    ]
    if not data.get("machine_status"):
        lines.append(f"Machine Stop Time: {data.get('machine_stop_time')}")
    return "\n".join(lines)


def format_status(data, dt):
    status = "RUNNING" if data.get("machine_status") else "STOPPED"
    shift = get_shift(dt)
    lines = [
        "MACHINE STATUS",
        f"Machine {data.get('machine')} | {data.get('machine_name')}",
        f"Status: {status}",
    ]
    if not data.get("machine_status"):
        stop_min = parse_stop_minutes(data.get("machine_stop_time"))
        if stop_min:
            start = parse_hms(data.get("last_updated") or "")
            if start:
                since = (start - timedelta(minutes=stop_min)).strftime("%H:%M:%S")
                lines.append(f"Stopped Since: {since} IST (Stop {data.get('machine_stop_time')})")
            else:
                lines.append(f"Stop Time: {data.get('machine_stop_time')}")
    lines += [
        f"Shift: {shift} | Date: {dt.strftime('%Y-%m-%d')}",
        f"Part: {data.get('part')}",
        f"Good: {data.get('good_products')} | Bad: {data.get('bad_products')} | Total: {data.get('total_count')}",
        f"OEE: {data.get('oee')} | TEEP: {data.get('teep')}",
        f"Running Hours: {data.get('machine_running_hours')}",
        f"Last Updated: {data.get('last_updated')} IST",
    ]
    return "\n".join(lines)


def format_breakdown(data, dt, breakdown_start):
    return "\n".join(
        [
            "MACHINE BREAKDOWN",
            "",
            f"Machine: {data.get('machine')} | {data.get('machine_name')}",
            f"Part: {data.get('part')}",
            f"Breakdown Started: {fmt_ist(breakdown_start)}",
            "",
            f"Machine Stop Time: {data.get('machine_stop_time')}",
            f"Good: {data.get('good_products')} | Bad: {data.get('bad_products')} | Total: {data.get('total_count')}",
            f"Reported At: {fmt_ist(dt)}",
        ]
    )


def save_chat_ids(chat_ids):
    unique = []
    for cid in chat_ids:
        cid = str(cid).strip()
        if cid and cid not in unique:
            unique.append(cid)
    with open(CHAT_ID_FILE, "w") as f:
        f.write("\n".join(unique) + ("\n" if unique else ""))
    os.chmod(CHAT_ID_FILE, 0o600)


def load_chat_ids():
    ids = []
    if TELEGRAM_CHAT_ID:
        for cid in str(TELEGRAM_CHAT_ID).split(","):
            cid = cid.strip()
            if cid and cid not in ids:
                ids.append(cid)
    try:
        with open(CHAT_ID_FILE) as f:
            for line in f:
                cid = line.strip()
                if cid and cid not in ids:
                    ids.append(cid)
    except FileNotFoundError:
        pass
    return ids


def discover_chat_ids():
    ids = load_chat_ids()
    if DRY_RUN:
        return ids
    url = f"{TELEGRAM_API}{BOT_TOKEN}/getUpdates"
    try:
        resp = requests.get(url, params={"timeout": 8, "limit": 100}, timeout=25)
        data = resp.json()
        found = set()
        for upd in data.get("result", []):
            msg = upd.get("message") or upd.get("channel_post") or {}
            chat = msg.get("chat") or {}
            cid = chat.get("id")
            if cid:
                found.add(str(cid))
        new_ids = [cid for cid in found if cid not in ids]
        if new_ids:
            ids.extend(new_ids)
            save_chat_ids(ids)
            log.info("Discovered new recipients: %s", ", ".join(new_ids))
    except Exception as e:
        log.error("getUpdates failed: %s", e)
    return ids


def send_telegram(text, chat_ids=None, bot_token=BOT_TOKEN):
    if chat_ids is None:
        chat_ids = discover_chat_ids()
    targets = [str(cid) for cid in chat_ids]
    if not targets:
        log.warning("No Telegram recipients available. Message your bot and press Start.")
        return False
    if DRY_RUN:
        log.info("[DRY RUN] Would send to %s:\n%s", ", ".join(targets), text)
        return True
    url = f"{TELEGRAM_API}{bot_token}/sendMessage"
    sent = 0
    for cid in targets:
        try:
            resp = requests.post(url, json={"chat_id": cid, "text": text}, timeout=30)
            ok = resp.status_code == 200 and resp.json().get("ok")
            if ok:
                sent += 1
                log.info("Message sent to chat %s", cid)
            else:
                log.error("Send to %s failed (%s): %s", cid, resp.status_code, resp.text[:300])
        except Exception as e:
            log.error("Send to %s error: %s", cid, e)
    return sent > 0


def load_update_offset():
    try:
        with open(UPDATE_OFFSET_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return 0


def save_update_offset(offset):
    with open(UPDATE_OFFSET_FILE, "w") as f:
        f.write(str(offset))


def process_incoming(data, dt):
    offset = load_update_offset()
    try:
        resp = requests.get(
            f"{TELEGRAM_API}{BOT_TOKEN}/getUpdates",
            params={"timeout": 2, "offset": offset, "limit": 20},
            timeout=25,
        )
        updates = resp.json().get("result", [])
        if updates:
            new_offset = offset
            for upd in updates:
                upd_id = upd["update_id"]
                msg = upd.get("message") or upd.get("channel_post") or {}
                chat = msg.get("chat") or {}
                cid = chat.get("id")
                if cid:
                    cid = str(cid)
                    ids = load_chat_ids()
                    if cid not in ids:
                        ids.append(cid)
                        save_chat_ids(ids)
                        log.info("Added new recipient %s", cid)
                    text = (msg.get("text") or "").strip().lower()
                    if text in ("/status", "status", "status?"):
                        send_telegram(format_status(data, dt), chat_ids=[cid])
                new_offset = max(new_offset, upd_id + 1)
            save_update_offset(new_offset)
    except Exception as e:
        log.error("Incoming message poll failed: %s", e)


def tts_piper_wav(text):
    import subprocess
    import tempfile
    wav_fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="mcc_")
    os.close(wav_fd)
    try:
        proc = subprocess.run(
            [PIPER_BIN, "-m", PIPER_MODEL, "-f", wav_path],
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError("piper failed: " + proc.stderr.decode("utf-8", "replace")[:300])
        return wav_path
    except Exception as e:
        log.error("Piper TTS failed: %s", e)
        try:
            os.unlink(wav_path)
        except OSError:
            pass
        raise


def tts_piper(text):
    import subprocess
    import tempfile
    wav_path = tts_piper_wav(text)
    try:
        ogg_fd, ogg_path = tempfile.mkstemp(suffix=".ogg", prefix="mcc_")
        os.close(ogg_fd)
        subprocess.run(
            [FFMPEG_BIN, "-y", "-loglevel", "error", "-i", wav_path, "-c:a", "libopus", "-b:a", "48k", ogg_path],
            capture_output=True,
            timeout=120,
        )
        with open(ogg_path, "rb") as f:
            return f.read(), ogg_path
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass


def tts_voice_google(text):
    url = "https://translate.google.com/translate_tts?ie=UTF-8&client=tw-ob&tl=en&q=" + quote(text)
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("Google TTS fetch failed: %s", e)
        return None
    return resp.content, "mp3"


def send_tts_voice(text):
    if DRY_RUN:
        log.info("[DRY RUN] Would send TTS voice message:\n%s", text)
        return True
    try:
        audio, name = tts_piper(text)
        method = "sendVoice"
        mime = "audio/ogg"
        field = "voice"
        caption = f"MCC Line 1 breakdown > {CALL_ALERT_MINUTES} mins - audible alert"
    except Exception:
        if not TTS_FALLBACK_GOOGLE:
            return False
        log.info("Falling back to Google TTS")
        result = tts_voice_google(text)
        if not result:
            return False
        audio, name = result
        method = "sendAudio"
        mime = "audio/mpeg"
        field = "audio"
        caption = f"MCC Line 1 breakdown > {CALL_ALERT_MINUTES} mins - audible alert"
    ok = True
    for cid in load_chat_ids():
        try:
            up = requests.post(
                f"{TELEGRAM_API}{BOT_TOKEN}/{method}",
                data={"chat_id": cid, "caption": caption},
                files={field: (name + ("." + mime.split("/")[1]), audio, mime)},
                timeout=60,
            )
            if up.status_code == 200 and up.json().get("ok"):
                log.info("Voice note sent to chat %s", cid)
            else:
                log.error("Voice send to %s failed (%s): %s", cid, up.status_code, up.text[:200])
                ok = False
        except Exception as e:
            log.error("Voice send to %s error: %s", cid, e)
            ok = False
        finally:
            if name and name != "mp3":
                try:
                    os.unlink(name)
                except OSError:
                    pass
    return ok


def _a7670_open(port, baud):
    import termios
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY)
    attrs = termios.tcgetattr(fd)
    attrs[0] = termios.IGNPAR
    attrs[1] = 0
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[3] = 0
    attrs[4] = getattr(termios, "B%d" % baud)
    attrs[5] = attrs[4]
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 1
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    return fd


def _a7670_expect(fd, patterns, timeout):
    buf = b""
    end = time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                chunk = os.read(fd, 1024)
            except OSError:
                break
            if chunk:
                buf += chunk
        if b"\r\nOK\r\n" in buf or b"\r\nERROR\r\n" in buf:
            break
        if patterns and any(p in buf for p in patterns):
            break
    return buf


def _a7670_cmd(fd, raw, expect, timeout):
    os.write(fd, raw)
    return _a7670_expect(fd, expect, timeout)


def _a7670_drain(fd):
    end = time.time() + 0.4
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try:
                os.read(fd, 1024)
            except OSError:
                return


def _a7670_clcc_stat(out):
    m = re.findall(rb'\+CLCC:\s*(\d+),(\d+),(\d+),(\d+),(\d+),"?([^"\\]*)"?,(\d+)', out)
    if m:
        inactive = [x for x in m if x[2] != b"6"]
        if inactive:
            return inactive[0][2].decode()
    if b"NO CARRIER" in out:
        return "6"
    return None


def _a7670_reg_report(out):
    """Return a human-readable registration summary -> str, or '' if unavailable."""
    m = re.search(rb"\+CEREG:\s*\d+,\s*(\d+)", out)
    if not m:
        m = re.search(rb"\+CREG:\s*\d+,\s*(\d+)", out)
    if not m:
        return ""
    stat = m.group(1).decode()
    label = {
        "0": "not registered",
        "1": "home (voice ok)",
        "2": "searching",
        "3": "denied",
        "5": "roaming (voice ok)",
        "10": "emergency only",
        "11": "SMS only (no voice)",
    }.get(stat, "stat " + stat)
    return "%s=%s (%s)" % ("CEREG" if b"+CEREG" in out else "CREG", stat, label)


def _a7670_usb_path():
    """Return sysfs USB device id for the A7670/A76XX LTE module, or None."""
    for base in ("/sys/bus/usb/devices/1-1",):
        vidp = os.path.join(base, "idVendor")
        if not os.path.exists(vidp):
            continue
        try:
            with open(vidp) as f:
                vid = f.read().strip()
            with open(os.path.join(base, "idProduct")) as f:
                pid = f.read().strip()
        except OSError:
            continue
        if vid == "1e0e" and pid == "9011":
            return base
    return None


def _a7670_reset_usb():
    """Power-cycle the A7670 over USB (unbind/bind). Returns True if reset was issued."""
    usb_path = _a7670_usb_path()
    if not usb_path:
        log.error("Cannot reset A7670: USB device not found")
        return False
    dev = os.path.basename(usb_path)
    try:
        with open("/sys/bus/usb/drivers/usb/unbind", "w") as f:
            f.write(dev)
        time.sleep(3)
        with open("/sys/bus/usb/drivers/usb/bind", "w") as f:
            f.write(dev)
        log.info("USB reset issued for A7670 (%s)", dev)
        return True
    except Exception as e:
        log.error("USB reset failed for %s: %s", dev, e)
        return False


def _a7670_wait_ready(timeout):
    """Wait until the A7670 AT port exists and replies SIM ready. Returns True if ready."""
    end = time.time() + timeout
    while time.time() < end:
        if os.path.exists(A7670_AT_PORT):
            fd = None
            try:
                fd = _a7670_open(A7670_AT_PORT, A7670_BAUD)
                _a7670_drain(fd)
                _a7670_cmd(fd, b"\r", [b"OK"], 1)
                out = _a7670_cmd(fd, b"AT+CPIN?\r", [b"READY", b"ERR"], 2)
                if b"READY" in out:
                    return True
            except Exception:
                pass
            finally:
                try:
                    os.close(fd)
                except Exception:
                    pass
        time.sleep(1)
    return False


def _a7670_recover():
    """Reset the module if hung and wait for it to come back. Returns True on success."""
    log.warning("A7670 module unresponsive/hung, attempting USB power-cycle")
    if not _a7670_reset_usb():
        return False
    ok = _a7670_wait_ready(40)
    if ok:
        log.info("A7670 recovered after USB power-cycle")
    else:
        log.error("A7670 did not recover within timeout after USB power-cycle")
    return ok


def _a7670_ready():
    """Quick check: is the module present and SIM ready right now?"""
    if not os.path.exists(A7670_AT_PORT):
        return False
    try:
        fd = _a7670_open(A7670_AT_PORT, A7670_BAUD)
    except Exception:
        return False
    try:
        _a7670_drain(fd)
        _a7670_cmd(fd, b"\r", [b"OK"], 1)
        out = _a7670_cmd(fd, b"AT+CPIN?\r", [b"READY", b"ERR"], 2)
        return b"READY" in out
    except Exception:
        return False
    finally:
        try:
            os.close(fd)
        except Exception:
            pass


def wav_duration(path):
    try:
        with open(path, "rb") as f:
            data = f.read(4096)
        if data[:4] != b"RIFF":
            return None
        i = 12
        byte_rate = 0
        while i + 8 <= len(data):
            cid, size = data[i:i + 4], struct.unpack("<I", data[i + 4:i + 8])[0]
            if cid == b"fmt " and i + 20 <= len(data):
                byte_rate = struct.unpack("<I", data[i + 12:i + 16])[0]
            elif cid == b"data" and byte_rate:
                return struct.unpack("<I", data[i + 4:i + 8])[0] / float(byte_rate)
            i += 8 + size + (size % 2)
    except Exception:
        return None
    return None


_CALL_WAV_PATH = None


def get_call_wav_path():
    """Return a cached piper WAV of the call text, generating it once on first use."""
    global _CALL_WAV_PATH
    if _CALL_WAV_PATH and os.path.exists(_CALL_WAV_PATH):
        return _CALL_WAV_PATH
    _CALL_WAV_PATH = tts_piper_wav(BREAKDOWN_CALL_TEXT)
    return _CALL_WAV_PATH


def play_audio_to_call(wav_path):
    import subprocess
    dur = wav_duration(wav_path)
    secs = int(A7670_AUDIO_MAX_SECONDS)
    if dur:
        secs = min(int(dur) + (1 if dur != int(dur) else 0), A7670_AUDIO_MAX_SECONDS)
    secs = max(1, secs)
    if shutil.which("aplay"):
        subprocess.run(
            ["aplay", "-q", "-D", A7670_AUDIO_DEVICE, "-d", str(secs + 1), wav_path],
            timeout=secs + 15,
        )
    else:
        subprocess.run(
            ["timeout", str(secs + 2), "ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", wav_path],
            timeout=secs + 15,
        )


def module_make_call(number, _retried=False):
    """Place a real voice call through the A7670E module (SIM voice)."""
    port = A7670_AT_PORT
    report = {"answered": False, "detail": ""}

    if not port or not os.path.exists(port):
        log.warning("A7670 module port missing at %s; attempting USB power-cycle", port)
        if not _retried and _a7670_recover():
            return module_make_call(number, _retried=True)
        if not _a7670_ready():
            log.error("A7670 module not present/ready at %s", port)
            return None

    if not _a7670_ready():
        log.warning("A7670 not ready before call; attempting USB power-cycle")
        if not _retried and _a7670_recover():
            return module_make_call(number, _retried=True)
        report["detail"] = "no SIM ready"
        return report

    try:
        fd = _a7670_open(port, A7670_BAUD)
    except Exception as e:
        log.error("Cannot open module port %s: %s", port, e)
        if not _retried and _a7670_recover():
            return module_make_call(number, _retried=True)
        return None
    try:
        _a7670_drain(fd)
        _a7670_cmd(fd, b"ATE0\r", [b"OK"], 2)
        if b"READY" not in _a7670_cmd(fd, b"AT+CPIN?\r", [b"READY", b"ERR"], 2):
            report["detail"] = "no SIM ready"
            return report
        for probe in (b"AT+CEREG?\r", b"AT+CREG?\r"):
            out = _a7670_cmd(fd, probe, [], 2)
            reg = _a7670_reg_report(out)
            if reg:
                report["detail"] += reg + "; "
        out = _a7670_cmd(fd, b"ATD" + number.encode("ascii") + b";\r",
                         [b"OK", b"CONNECT", b"No carrier", b"NO CARRIER", b"ERROR"], 3)
        low = out.lower()
        if b"no carrier" in low or b"error" in low:
            m = re.search(rb"\+CME ERROR:?\s*([^\r\n]*)", out, re.IGNORECASE)
            detail = ("+CME ERROR: " + m.group(1).decode("utf-8", "replace")) if m else out.decode("utf-8", "replace").strip()[:80]
            report["detail"] += "dial rejected: " + detail
            return report
        start = time.time()
        answered = False
        drop = None
        while time.time() - start < A7670_RING_TIMEOUT_SECONDS:
            out = _a7670_cmd(fd, b"AT+CLCC\r", [b"OK"], 2)
            low = out.lower()
            if b"no carrier" in low or b"error" in low:
                m = re.search(rb"\+CME ERROR:?\s*([^\r\n]*)", out, re.IGNORECASE)
                drop = ("+CME ERROR: " + m.group(1).decode("utf-8", "replace")) if m else out.decode("utf-8", "replace").strip()[:60]
                break
            stat = _a7670_clcc_stat(out)
            if stat == "0":
                answered = True
                break
            if stat == "6":
                drop = "disconnected (no carrier)"
                break
            time.sleep(1)
        if answered:
            report["answered"] = True
            try:
                log.info("Call active, playing message to %s", number)
                play_audio_to_call(get_call_wav_path())
            except Exception as e:
                log.error("Failed to play message: %s", e)
                report["detail"] = "answered, but audio playback failed: %s" % e
            _a7670_cmd(fd, b"ATH\r", [b"OK"], 2)
            if "audio playback failed" not in report["detail"]:
                report["detail"] = "answered, message played, hung up"
        else:
            _a7670_cmd(fd, b"ATH\r", [b"OK"], 2)
            if drop:
                report["detail"] += "call dropped: " + drop
            else:
                report["detail"] += "no answer within %ss" % A7670_RING_TIMEOUT_SECONDS
        return report
    except Exception as e:
        log.error("A7670 call error: %s", e)
        report["detail"] = "module error: %s" % e
        return report
    finally:
        try:
            _a7670_cmd(fd, b"ATH\r", [b"OK"], 1)
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass


def send_breakdown_call(data, dt):
    provider = MCC_CALL_PROVIDER if MCC_CALL_PROVIDER in ("auto", "module", "telegram") else "auto"
    order = {
        "auto": ["module"],
        "module": ["module"],
        "telegram": [],
    }[provider]
    for p in order:
        if p == "module":
            if not BREAKDOWN_CALL_TO:
                continue
            try:
                res = module_make_call(BREAKDOWN_CALL_TO)
            except Exception as e:
                log.error("A7670 call attempt failed: %s", e)
                res = None
            if res and res.get("answered"):
                log.info("SIM call completed: %s", res["detail"])
                return
            log.warning("A7670 call not completed (%s), using Telegram voice message", (res or {}).get("detail"))
    send_tts_voice(BREAKDOWN_CALL_TEXT)


def estimate_breakdown_start(data, detected_dt):
    start = detected_dt
    stop_min = parse_stop_minutes(data.get("machine_stop_time"))
    last_upd = parse_hms(data.get("last_updated") or "")
    if stop_min and last_upd:
        est = last_upd - timedelta(minutes=stop_min)
        if est <= detected_dt + timedelta(minutes=1):
            start = est
    return start


def run_monitor():
    log.info("MCC Monitor starting (machine %s)", MACHINE_ID)
    log.info("IST now: %s | shift: %s | polls every %ss", now_ist().strftime("%H:%M:%S"), get_shift(now_ist()), POLL_INTERVAL_SECONDS)

    try:
        get_call_wav_path()
        log.info("Call TTS message pre-generated for instant playback")
    except Exception as e:
        log.warning("Call TTS pre-generation failed (will retry at call time): %s", e)

    was_running = None
    breakdown_started = None
    breakdown_alerted = False
    call_alerted = False
    last_error_alert = 0.0
    shift_report_sent = None

    while True:
        try:
            dt = now_ist()
            data = fetch_with_fallback(dt)
            is_running = bool(data.get("machine_status"))

            if is_running:
                if was_running is False:
                    downtime = dt - breakdown_started if breakdown_started else timedelta(0)
                    if downtime.total_seconds() >= BREAKDOWN_ALERT_MINUTES * 60:
                        send_telegram(format_report(data, dt, "MACHINE RUNNING (AFTER BREAKDOWN)"))
                        log.info("Recovery message sent after breakdown of %s", downtime)
                breakdown_started = None
                breakdown_alerted = False
                call_alerted = False
            else:
                if was_running is not False and breakdown_started is None:
                    breakdown_started = estimate_breakdown_start(data, dt)
                    breakdown_alerted = False
                    call_alerted = False
                    log.info("Machine stopped at %s", fmt_ist(breakdown_started))

                if breakdown_started:
                    down_seconds = (dt - breakdown_started).total_seconds()
                    if down_seconds >= BREAKDOWN_ALERT_MINUTES * 60 and not breakdown_alerted:
                        send_telegram(format_breakdown(data, dt, breakdown_started))
                        breakdown_alerted = True
                        log.info("Breakdown alert sent")
                    if down_seconds >= CALL_ALERT_MINUTES * 60 and not call_alerted:
                        log.info("Breakdown > %s mins, triggering call", CALL_ALERT_MINUTES)
                        send_breakdown_call(data, dt)
                        call_alerted = True

            was_running = is_running

            if SHIFT_REPORT_ENABLED:
                shift = get_shift(dt)
                end = shift_end_time(dt)
                secs_to_end = (end - dt).total_seconds()
                if 0 <= secs_to_end <= SHIFT_REPORT_MIN_BEFORE * 60 and shift_report_sent != shift:
                    shift_report_sent = shift
                    send_telegram(
                        format_report(
                            data,
                            dt,
                            f"SHIFT {shift} PRODUCTION REPORT (ends {end.strftime('%H:%M')} IST)",
                        )
                    )
                    log.info("Shift %s end report sent (%s before end)", shift, fmt_ist(dt))

            process_incoming(data, dt)
        except Exception as e:
            log.error("Poll failed: %s", e)
            if time.time() - last_error_alert >= ERROR_ALERT_MINUTES * 60:
                expected = now_ist().strftime("%Y-%m-%d")
                send_telegram(
                    "MONITOR ALERT\n"
                    f"API data unavailable for {expected} (machine {MACHINE_ID}).\n"
                    f"Error: {e}\n"
                    "Still running, will keep trying and notify when data returns."
                )
                last_error_alert = time.time()

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    if "--testcall" in sys.argv:
        log.info("TEST CALL mode: placing a breakdown call now")
        try:
            dt = now_ist()
            data = fetch_with_fallback(dt)
            send_breakdown_call(data, dt)
        except Exception as e:
            log.error("Test call failed: %s", e)
    elif "--testmodule" in sys.argv:
        if not BREAKDOWN_CALL_TO:
            log.error("TEST MODULE mode requires BREAKDOWN_CALL_TO to be set (none configured, not dialing)")
        else:
            log.info("TEST MODULE mode: dialing %s via A7670", BREAKDOWN_CALL_TO)
            res = module_make_call(BREAKDOWN_CALL_TO)
            log.info("Test module result: %s", res)
    else:
        try:
            run_monitor()
        except KeyboardInterrupt:
            log.info("Stopped by user")