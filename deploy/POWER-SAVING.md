# Power-saving setup (Orange Pi Zero / H3) — reproducible

Goal: lower SBC power draw WITHOUT losing functionality (OLED, serial scale,
WiFi, web UI, systemd service all stay on).

## Apply on a fresh SBC

### 1. CPU governor -> powersave (persists via systemd oneshot)
```sh
sudo cp /opt/scale/deploy/scale-powersave.service /etc/systemd/system/
sudo chmod 644 /etc/systemd/system/scale-powersave.service
sudo systemctl daemon-reload
sudo systemctl enable scale-powersave
# apply immediately (no reboot needed):
echo powersave | sudo tee /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
```
- Drops CPU from 1.296 GHz to 480 MHz. The app is light (single python3,
  ~13 MB RSS) so no performance impact on OLED/serial/web.
- Verified available governors: conservative ondemand userspace powersave
  performance schedutil

### 2. Disable unused HDMI / GPU display engine
Edit `/boot/armbianEnv.txt`:
```
disp_mode=off
extraargs=video=HDMI-A-1:d fbcon=map:0 modprobe.blacklist=lima
```
- `modprobe.blacklist=lima` removes the GPU driver (the real display power
  consumer) — verified NOT loaded after reboot.
- `video=HDMI-A-1:d` disables the HDMI connector; `fbcon=map:0` unbinds the
  text console.
- NOTE: on this mainline Armbian the `sun4i-drm` display driver is BUILT INTO
  the kernel, so `/dev/fb0` (an unused device node) still appears. It draws
  negligible power once lima/HDMI are off. Fully removing it needs a custom
  kernel rebuild — NOT worth the risk (could break I2C/OLED). Leave it.
- REQUIRES REBOOT to take effect.

### 3. Disable unused serial getty (ttyS0 UART console)
```sh
sudo systemctl disable serial-getty@ttyS0
sudo systemctl stop serial-getty@ttyS0
```

## Deliberately NOT changed (keep functionality)
- WiFi / ethernet: left on so the web UI + SSH stay reachable.
- Arduino USB port (/dev/scale): left at power/control=on — it MUST stay
  awake for the 115200 serial link. Do NOT autosuspend it.
- scale-status.service: untouched (auto-starts, Restart=always).

## Verification after reboot
```sh
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor   # powersave
grep -E "disp_mode|extraargs" /boot/armbianEnv.txt
lsmod | grep lima                                            # should be EMPTY
systemctl is-active scale-status                            # active
systemctl is-active scale-powersave scale-leds              # active
curl -s http://127.0.0.1:8080/api                            # weight + conn
cat /sys/class/thermal/thermal_zone0/temp                    # expect ~10-15C lower
```

Observed effect on this unit: idle temp dropped from ~73C to ~60C. Governor
pins CPU at 480 MHz; lima GPU unloaded. (Don't expect fb0 to vanish — it's a
built-in driver node, harmless.)

## Also applied (LEDs)
- `scale-leds.service` sets all /sys/class/leds/*/brightness to 0 at boot
  (cosmetic indicator LEDs off). No functionality loss.

## Boot-time optimization (scale + OLED up first)
Goal: OLED + serial scale ready ASAP, independent of slow network/login stack.

- `scale-status.service` changed to `WantedBy=sysinit.target` with
  `DefaultDependencies=no`, `After=systemd-udevd.service local-fs.target`,
  and `Before=network.target dbus.service systemd-logind.service graphical.target`.
  -> starts at ~8s into boot (was ~25s).
- `plymouth` (boot splash) masked — pure delay, GPU already off. Saves seconds.
- KEEP dbus/logind/NetworkManager: needed for SSH + WiFi. They no longer block
  the scale service.

Measured: total boot ~61s (userspace ~52s), but scale-status active @~8s and
weight/conn available immediately. Overall userspace is dominated by
dbus/logind/NetworkManager which are irrelevant to scale operation.

To reproduce on a new SBC: copy the updated `scale-status.service` from
`deploy/` to /etc/systemd/system/, `systemctl daemon-reload`,
`systemctl enable scale-status`, `systemctl mask plymouth-start plymouth-read-write`.
