# Smart Scale

A WiFi-enabled smart scale built on an **Orange Pi Zero** (armv7l / 32-bit
Armbian) talking to an **LBV1** controller board — an Arduino Nano driving
**4× HX711** load-cell amplifiers (one per corner). The Pi reads the four
channels over USB serial, renders a live weight on a 128×64 **SSD1306 OLED**,
serves a real-time **web dashboard**, and logs settled weigh-ins to a CSV file.

## Features

- **Live OLED display** — big weight readout. Auto power-off after 40 s of a
  steady value; instant wake on any weight change; clean power-off on SBC
  shutdown (no frozen last frame).
- **Web dashboard** (`http://<pi-ip>:8080/`) — total + per-channel weight,
  WiFi/CPU/temp/RAM/uptime, overload status, all updating live (no refresh).
- **Step-on auto-tare** — step on the scale, wait through a 5 s window + 5 s
  grace, and the scale arms a logging lockout (OLED shows `STEP ON` →
  `TIMEOUT`).
- **Weight logging** — after the timeout lockout, a settled weight **> 20 kg**
  is written to `/opt/scale/weights.csv` as `timestamp,total_kg`. Exactly
  **one row per step-on session**.
- **Weight Log page** (`/log`) — view entries, download CSV (`/log.csv`),
  clear the log. Dashboard shows a `log: ARMED/idle` indicator so you can
  confirm the timeout fired before a session will log.
- **OLED LOGGED screen** — on a successful write the display flashes a full
  `LOGGED` confirmation (logged weight + label) for 3 s.
- **Controls** — tare, restart program, restart/reboot/shutdown the SBC, and
  calibrate channels (known-mass + direct factor) from the web UI.
- **Power saving** — CPU governor `powersave`, HDMI framebuffer off, serial
  kept awake. See `deploy/POWER-SAVING.md`.

## Hardware

| Part | Role |
|------|------|
| Orange Pi Zero | host (Armbian, 32-bit armv7l) |
| LBV1 (Arduino Nano) | firmware; 4× HX711 channels |
| 4× HX711 | load cells (one per corner) |
| SSD1306 128×64 | OLED via I²C (`/dev/i2c-0`, addr `0x3C`) |
| CH340 USB-serial | Nano link, symlinked to `/dev/scale` |

Serial framing (115200 baud, frames end with `$`):

- `<v1>|<v2>|<v3>|<v4>$` — weight per channel ×100 (centiunits). Sum = total kg.
- `CAL:<f1>|<f2>|<f3>|<f4>$` — calibration factors ×10.
- `OVL:<0|1>|<0|1>|<0|1>|<0|1>$` — overload per channel.

Host→firmware: `t` (tare all), `k<ch>:<mass>` (calibrate), `c` (print cal),
`at0`/`at1` (autotare on boot), `o<ch>:<thr>` (overload threshold).
Calibration persists in Arduino EEPROM.

## Deploy to the Pi

Source of truth lives on the device at `/opt/scale/`. The repo's
`scale_status.py` and `ssd1306.py` are the deployed files.

```bash
# from this repo
scp scale_status.py ssd1306.py ibster@192.168.178.91:/tmp/
ssh ibster@192.168.178.91
echo poplol | sudo -S cp /tmp/scale_status.py /opt/scale/scale_status.py
echo poplol | sudo -S cp /tmp/ssd1306.py /opt/scale/ssd1306.py
# /opt/scale must be writable by ibster for the CSV log:
echo poplol | sudo -S chmod 777 /opt/scale
echo poplol | sudo -S systemctl restart scale-status
```

Check: `systemctl is-active scale-status` and `curl -s http://127.0.0.1:8080/api`.

> **Note:** deploy to `/opt/scale`, not `/tmp` — `/tmp` is a tmpfs and is wiped
> on reboot. Python deps use `pip --break-system-packages` (PEP 668, no venv).

## Device layout

```
/opt/scale/scale_status.py        main program (OLED + web + serial)
/opt/scale/ssd1306.py             SSD1306 I2C driver
/opt/scale/weights.csv            weight log (timestamp,total_kg)
/etc/systemd/system/scale-status.service
/etc/udev/rules.d/99-scale.rules  CH340 -> /dev/scale
/etc/sudoers.d/scale-status       passwordless sudo for web buttons
```

## Web UI

- `/` — live dashboard
- `/log` — log table (auto-refresh), download, clear
- `/log.csv` — CSV download
- `/api` — JSON snapshot (`SNAP`)
- `/api/log` — `{rows, count}`
- `/api/action?cmd=<tare|cal|readcal|setcal|restart_prog|restart|shutdown|clear_log>`

## Project layout

```
scale_status.py     main host program
ssd1306.py          minimal SSD1306 driver (pure stdlib, fcntl I2C)
LBV1.ino            firmware entry
firmware/           Arduino Nano firmware (src/, arduino-cli.yaml)
deploy/             systemd services, sudoers, power-saving docs
bin/arduino-cli     arduino-cli binary (gitignored)
```

## Tunables (top of scale_status.py)

| Constant | Default | Meaning |
|----------|---------|---------|
| `TARE_WINDOW` | 5.0 s | window after step-on before timeout |
| `TARE_GRACE` | 5.0 s | grace after window before lockout |
| `LOG_MIN_KG` | 20.0 | minimum weight to log |
| `LOG_SETTLE_S` | 2.0 s | stability duration before logging |
| `LOG_SETTLE_TOL` | 0.3 kg | stability tolerance |
| `OLED_OFF_S` | 40.0 s | idle display-off delay |

## License

MIT (see LICENSE if present).
