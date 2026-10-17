# GaugeDeck

https://github.com/Cyoulater/gaugedeck

![GaugeDeck](screenshot.png)

Car-style dashboard gauges for your Linux PC: CPU, graphics and compute
cards, fans, drives, RAM and board, found and named automatically.

Built as a native GTK 4 / Wayland app, so it works on current GNOME desktops
(Ubuntu 25.10 / 26.04 and later) where desktop widgets like Conky freeze or
fail to draw.

## Install
    sh install.sh                # or: sh install.sh "MY RIG NAME"   (sets the title shown under the gauges)
Then open **GaugeDeck** from your app menu. Right-click it in the dock
and choose "Pin to Dash" to keep it there. Close it with the X.

## Settings
Edit `~/.config/gaugedeck/config`:

    title=MY RIG
    scale=0.6      # 0.3 = tiny, 1.0 = large

## Alerts
GaugeDeck warns you when something is wrong: a desktop notification, an alarm
sound, and the gauge flashes red. It alerts once when a reading stays in the
red for 5 seconds (quick spikes are ignored), and again when it's back to normal.

It also warns if a fan that was spinning stops (0 RPM).

Test it:  `gaugedeck --test-alert` (pop-up, alarm, and the CPU TEMP gauge flashes for 10 seconds)

Alerts only run while GaugeDeck is open (minimize it instead of closing it).
Change the limits in `~/.config/gaugedeck/config`:

    alerts=on
    alert_sound=on
    alert_seconds=5
    cpu_temp_alert=80
    gpu_temp_alert=85
    board_temp_alert=60
    ram_alert=95
    fan_stop_alert=on
    nv_temp_alert=85
    drive_temp_alert=70

## Sensors
GaugeDeck finds the machine's sensors by itself at startup, on any PC, and
names each gauge after the real part:

- CPU: temperature (Intel coretemp, AMD k10temp / zenpower, ARM thermal zones),
  load from /proc, and package power where RAPL is readable
- Graphics cards: AMD, Intel and nouveau through /sys (temperature, hot spot,
  VRAM, fan, power, load); NVIDIA through `nvidia-smi`, including compute cards
  like the Tesla P100 (temperature, power, load). A card's gauges carry its
  model, e.g. `RX 6600 TEMP`, `P100 POWER`.
- Drives: NVMe and SATA drive temperatures, named by model (`980 PRO TEMP`)
- Board: temperature and every fan header that is spinning (Nuvoton, ITE,
  Fintek, Winbond, ASUS, Gigabyte, Dell, ThinkPad, HP...)
- Others: memory module (DIMM), chipset, Wi-Fi and ACPI temperatures
- RAM use from /proc

See everything it found, with a live reading of each:

    gaugedeck --check

The deck shows the main sensors (marked `*`). Add or drop gauges by key in
`~/.config/gaugedeck/config`:

    show=cpu_tccd1,gpu_junction
    hide=sda_temp

Any sensor can also be opened in its own window: `gaugedeck --gauge KEY`.

A gauge shows `--` if a reading goes away. For board sensors and case fans,
run `sudo sensors-detect` once (package `lm-sensors`). Temperatures alert at
`cpu_temp_alert` (CPU), `gpu_temp_alert` (display card), `nv_temp_alert`
(NVIDIA cards next to the display card), `board_temp_alert` and
`drive_temp_alert` (default 70).

Read-only: the app only reads from /sys, /proc and nvidia-smi. It changes nothing.

## Tests
    python3 -m unittest discover -s tests -v

The tests run on fake machines (a desktop with a Tesla P100, a ThinkPad, a
two-card server, a bare VM) from `tests/fake_machine.py`. To see what
GaugeDeck would find on one:

    python3 tests/fake_machine.py desktop /tmp/fake
    GAUGEDECK_ROOT=/tmp/fake PATH=/tmp/fake/bin:$PATH ./gaugedeck.py --check

## Uninstall
    sh uninstall.sh

## License
GPL-3.0-or-later. Free to use, share, and modify. If you share a changed
version, you must share its source code under the same license. See LICENSE.
