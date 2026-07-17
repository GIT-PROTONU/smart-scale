# AGENTS.md — Smart Scale (Orange Pi Zero + LBV1)

This project is **deployed on a remote Orange Pi Zero** (armv7l / 32-bit Armbian), not built locally. The device is reachable over SSH. All source of truth lives on the device.

## Device access
- SSH: `ssh ibster@192.168.178.91` (password `poplol`). IP is DHCP — may change.
- The board runs **32-bit ARM (armv7l)**. **There is no armv7l pixi binary** and no aarch64 capability — pixi cannot run natively here. Install Python deps with `pip --break-system-packages` (PEP 668 enforced, no venv).
- Temp runs ~60–73°C under load; keep software lean. Avoid heavy deps (no numpy/Pillow/luma.oled). The OLED driver is a hand-rolled pure-stdlib driver using `fcntl` ioctl I2C.

## Deployed layout (on the Pi)
- `/opt/scale/scale_status.py` — main program (OLED + web + serial reader). **This path persists reboots; `/tmp` does NOT** (tmpfs is wiped on reboot — do not deploy there).
- `/opt/scale/ssd1306.py` — minimal SSD1306 OLED driver (I2C via `/dev/i2c-0`, addr `0x3C`, TWI0). Uses segment remap `0xA0` (mirrored fix) — keep it.
- `/etc/systemd/system/scale-status.service` — `Type=simple`, user `ibster`, `Restart=always`, serves on port 8080.
- `/etc/udev/rules.d/99-scale.rules` — CH340 (idVendor `1a86`, idProduct `7523`) → stable symlink **`/dev/scale`** + `dialout` group.
- `/etc/sudoers.d/scale-status` — passwordless `sudo systemctl` for the web UI buttons (restart/shutdown/reboot program).

## How to change code
1. Edit locally, `scp` to `/tmp/`, then `sudo cp /tmp/scale_status.py /opt/scale/scale_status.py` (do not rely on `/tmp` surviving).
2. Apply: `sudo systemctl restart scale-status`.
3. Check: `systemctl is-active scale-status` and `curl -s http://127.0.0.1:8080/api`.

**Common failure:** after a reboot the service crash-loops with `can't open file '/tmp/scale_status.py'` — that means the script was deployed to `/tmp` instead of `/opt/scale/`. Fix the path and restart.

## Scale / LBV1 firmware (Arduino Nano + 4× HX711)
- Arduino appears as **`/dev/scale`** (symlink to `ttyUSB0`/`ttyACM0`). Reader auto-detects it; if symlink missing it falls back to `/dev/ttyUSB*`.
- Serial **115200 baud**. Output frames end with `$`:
  - `<v1>|<v2>|<v3>|<v4>$` — weight per channel ×100 (centiunits, signed). Sum = total kg.
  - `CAL:<f1>|<f2>|<f3>|<f4>$` — calibration factors ×10 (floats).
  - `OVL:<0|1>|<0|1>|<0|1>|<0|1>$` — overload per channel.
- **Calibration persists in Arduino EEPROM** (`saveCal()` on `k<ch>:<mass>` / `v<ch>:<value>`), reloaded on boot. The host stores none of it — nothing to persist on the Pi beyond the code.
- Host→firmware commands: `t` (tare all), `k<ch>:<mass>` (known-mass cal), `c` (print cal — response is buried among streaming weight frames, so poll `SNAP["cal"]` a few seconds after sending), `at0`/`at1` (autotare on boot), `o<ch>:<thr>` (overload threshold).

## OLED quirk
Text was initially mirrored; fixed by segment remap `0xA0` in `ssd1306.py` init. If upside-down appears, flip `0xC8`→`0xC0`. Don't replace the font with a random one — the current 5x7 table is verified-correct.

## Web UI
- `http://<pi-ip>:8080/` — live dashboard, no page refresh (JS `fetch` polling `/api` every 1s).
- `/api` returns JSON `SNAP`. `/api/action?cmd=<tare|cal|readcal|restart_prog|restart|shutdown>` (cal takes `&ch=&mass=`).
- Buttons need passwordless sudo (`/etc/sudoers.d/scale-status`); test with `curl`.

## Gotchas already hit (avoid repeating)
- `sudo` in non-interactive SSH needs `echo poplol | sudo -S ...`; `sudo -S bash -c "..."` for redirects.
- pip got corrupted by a partial `--break-system-packages` install; repaired via re-running `get-pip.py --force-reinstall`.
- Arduino USB can fail to enumerate (`error -62`) if cable is bad or board draws too much current — reseat cable / use powered hub.

## Power saving (applied, reproduce on new SBC)
- CPU governor set to `powersave` (480 MHz) via `scale-powersave.service` (oneshot, enabled). See `deploy/POWER-SAVING.md`.
- `/boot/armbianEnv.txt` has `disp_mode=off` — disables the unused HDMI/GPU framebuffer. **Requires reboot.**
- `serial-getty@ttyS0` disabled.
- Do NOT autosuspend the Arduino USB port (`/dev/scale`) — serial must stay awake. WiFi/eth left on so web UI + SSH work.
- Full procedure + files in `deploy/` (`POWER-SAVING.md`, `scale-powersave.service`, `sudoers-scale-status`).

## Feature history (scale_status.py)
- **Boot auto-tare:** after the serial link first comes up, waits ~2 s then sends `t` (tare) once per boot (`auto_tare_done` flag in `scale_reader`).
- **Step-on / stand-on auto-tare (OLED feedback):** once booted & tared, a "window" state machine auto-tares by stepping on the scale:
  - Weight >10 kg → opens a 5 s window, OLED shows `STEP ON` + live countdown (1–5) at bottom-right.
  - Weight <5 kg within the window → sends `t`, OLED shows `TARED` (~3 s).
  - Window expires while still on → 5 s `TIMEOUT` grace countdown; step off during grace → tares, stay on → locks out (`step_tare_locked`).
  - Lock clears only when weight <5 kg (scale empty), re-arming for next use.
  - State vars: `step_tare_state` (`idle`/`window`/`grace`), `step_tare_wait`, `step_tare_tared_at`, `step_tare_locked`. Tunables: `TARE_WINDOW=5.0`, `TARE_GRACE=5.0`.
  - SNAP fields added: `taresync` (status text), `taresync_t` (countdown seconds, for OLED).

## Weight logging (scale_status.py)
- **CSV log** at `/opt/scale/weights.csv` — columns `timestamp,total_kg` (c1–c4 were removed). Written once per step-on session.
- **Trigger:** only after the auto-tare TIMEOUT lockout (`step_tare_locked` True), weight **> `LOG_MIN_KG` (20 kg)**, and **settled** (±`LOG_SETTLE_TOL` 0.3 kg for `LOG_SETTLE_S` 2.0 s). Exactly **one** row per step-on (gated by `LOG_STATE["done"]`, reset when scale empties / lock clears).
- **`/opt/scale` is owned by root** — the service runs as `ibster` and CANNOT write there by default. Fixed with `sudo chmod 777 /opt/scale` (done on device; if CSV fails silently, re-apply). `log_weight()` now also `makedirs` the dir and logs write errors to `/tmp/scale_debug.log` instead of swallowing them.
- **Web UI:** `/log` (view table, auto-refresh), `/log.csv` (download), `/api/log` (JSON `{rows,count}`). Dashboard has a "Weight Log" card (`log_count`, `last_log`) and an `log: ARMED/idle` indicator (`tare_locked` SNAP field) so you can confirm the timeout fired before a session will log. Clear Log button → `clear_log` action.
- **OLED:** on a successful write, shows a full `LOGGED` confirmation screen (big logged weight + centered `LOGGED`) for 3 s via `log_flash`/`last_log_w` SNAP fields, then returns to live weight.
- Debug transitions (`->window`, `->grace`, `->LOCKED`, `LOG`, `serial reopen`, `LOG ERR`) are appended to `/tmp/scale_debug.log`.
- **Gotcha:** weight must stay >5 kg AND stable through the full ~10 s window+grace to reach `LOCKED`; if it dips below 5 kg the cycle restarts (no log). The WebUI `/log` JS originally required `c.length<6` rows and silently showed nothing after c1–c4 were removed — keep row parsing tolerant of 2 columns.

## OLED power management (scale_status.py + ssd1306.py)
- Driver gained a `power(on)` method (`0xAF`=on / `0xAE`=off, SSD1306 sleep).
- **Idle-off:** `draw()` turns the OLED off after it shows a steady value (±0.05 kg) for `OLED_OFF_S` (40 s) while `scale_conn` is true. Updates `oled_on`/`last_val`/`last_change` state. The idle-off is suppressed while `shutting_down` or `shutdown_hold > 0` is set (so the shutdown screens stay lit).
- **Instant wake:** any weight change (or empty→present) powers the display back on immediately.
- **Clean shutdown:** `oled_shutdown()` registered via `atexit` + `SIGTERM`/`SIGINT` handlers and a `finally` in the server loop, so the display powers off on SBC shutdown instead of freezing on the last frame. `oled_shutdown()` now also `clear()`s the buffer before powering off.
- `ssd1306.py` lives in `/opt/scale/` alongside `scale_status.py` (imported as `from ssd1306 import SSD1306`) — deploy both files.
- **Display is rotated 180°:** `ssd1306.py` init uses segment remap `0xA1` + COM scan `0xC0` (was `0xA0`/`0xC8`). Keep both flipped together for a true 180° rotation.

## Shutdown screens (scale_status.py + ssd1306.py)
- **WebUI shutdown button** (`/api/action?cmd=shutdown`): sets `SNAP["shutting_down"]=True`, then `systemctl poweroff`. OLED shows a `SHUTTING DOWN` screen and stays on (idle-off suppressed) until the SBC powers off; `oled_shutdown()` clears + powers off the display at process exit.
- **Hold-to-poweroff (hardware trigger):** holding **5–10 kg** on the scale for `SHUTDOWN_HOLD_S` (5 s) triggers a clean `systemctl poweroff`. Implemented in `scale_reader` — tracks `shutdown_hold_start`; `SNAP["shutdown_hold"]` holds the remaining seconds. OLED shows `HOLD TO` / `SHUTDOWN` + live countdown while held; removing the weight cancels (resets). Tunables: `SHUTDOWN_MIN_KG=5.0`, `SHUTDOWN_MAX_KG=10.0`, `SHUTDOWN_HOLD_S=5.0`. The 5–10 kg band does NOT collide with the step-on auto-tare window (opens at >10 kg).

## Reader bug history (avoid repeating)
- The `scale_reader` loop assigns `w` only inside the `if len(v)==4:` parse branch. Any code that references `w` at the outer loop scope (e.g. the hold-to-poweroff check) MUST read `SNAP["weight_kg"]` directly, never the local `w` — referencing `w` there throws `UnboundLocalError` on non-4-int frames and crash-loops the reader into `serial reopen`. The hold block now uses `hw = SNAP["weight_kg"]`.
- The reader's broad `except Exception` swallows the real error and just logs `serial reopen`. To debug, it now also logs `READ ERR <exception>` to `/tmp/scale_debug.log`. Check that before assuming a USB/cable fault — a software throw looks identical to a disconnect.


