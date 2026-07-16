# Deployment: reproducibility, verification, and field reliability

## TL;DR

```bash
git clone https://github.com/eva00212/iot-sensor-project.git
cd iot-sensor-project/raspberry_pi
./install.sh          # reboots itself partway through if needed -- just re-run after
./install.sh          # (only needed if the first run rebooted)
nano config/site_config.yaml   # the one remaining manual step: site_id + server settings
sudo systemctl restart sensor-collector
./verify_install.sh   # full PASS/FAIL report of every deployment prerequisite
```

## Why two identical SD cards behaved differently

One SD card worked immediately; the other required manual `raspi-config`
changes before the sensors could communicate, even though it was running
the same project code. The application itself was never the problem —
this was **OS-level configuration drift between two Raspberry Pi OS
images**, and it's exactly the class of problem `install.sh` now closes.

The concrete mechanisms, in order of likelihood:

1. **UART not enabled / login console still attached to the serial port.**
   A fresh Raspberry Pi OS image binds the primary UART to the login
   console by default. Until `enable_uart=1` is set in `config.txt` *and*
   `console=serial0,...` is removed from `cmdline.txt` (both requiring a
   reboot to take effect), `/dev/serial0` either doesn't exist or is
   fought over by a `serial-getty` login prompt, corrupting any Modbus
   traffic on it. This is the single most common cause, and is exactly
   what `raspi-config nonint do_serial_hw 0` / `do_serial_cons 1`
   automate. **Before this project automated it, this was a manual,
   easy-to-forget step** — the most likely explanation for the
   discrepancy you saw.

2. **`raspi-config` not reliably patching Pi 5's boot files on every OS
   image.** Pi 5 moved GPIO/UART handling to the RP1 southbridge chip,
   and different Raspberry Pi OS point releases have had inconsistencies
   in exactly how `raspi-config`'s serial toggle patches
   `/boot/firmware/config.txt` and `cmdline.txt` on Pi 5 specifically —
   this is precisely the kind of subtle image-to-image difference that
   makes "identical" SD cards behave differently. `install.sh` now
   verifies the actual file contents *after* calling `raspi-config` and
   patches them directly if the expected lines aren't there (see
   `install.sh`'s "belt-and-suspenders" section) — it doesn't just trust
   that `raspi-config` succeeded.

3. **Bootloader/EEPROM version mismatch.** The Pi 5's bootloader firmware
   (EEPROM) is independently updatable and not always in sync across two
   boards flashed at different times, even from the same OS image. An
   outdated EEPROM is a documented source of peripheral
   initialization-order quirks. `install.sh` now checks
   (`rpi-eeprom-update`) and surfaces this — but does **not** apply the
   update automatically (see "What's still manual" below).

4. **Different base OS image or Raspberry Pi Imager customization.** If
   the two SD cards were flashed at different times, they may have
   shipped with different `config.txt` defaults, kernel versions, or
   Raspberry Pi Imager "OS customization" presets. `verify_install.sh`
   reports the OS codename and kernel version specifically so this is
   visible at a glance when comparing two boards.

`verify_install.sh` exists specifically so you never have to
reverse-engineer "what's different about this board" by hand again — it
checks all of the above (and more) and reports PASS/FAIL/WARN for each.

## What `install.sh` automates

| Step | Mechanism |
|------|-----------|
| System packages (`python3-venv`, `python3-pip`, `raspi-config`, `raspi-gpio`) | `apt-get install` |
| `dialout` group membership | `usermod -aG dialout` |
| UART hardware enabled | `raspi-config nonint do_serial_hw 0`, verified directly against `config.txt` afterward |
| Login console detached from the serial port | `raspi-config nonint do_serial_cons 1`, verified directly against `cmdline.txt` afterward |
| Reboot handling | Detects whether the above actually changed anything (or `/dev/serial0` is still missing); if so, reboots itself (with a 15s-timeout confirmation prompt) and tells you to just re-run `./install.sh` |
| Bootloader/EEPROM awareness | Checked and surfaced as a note; not auto-applied (see below) |
| Persistent systemd journal | `mkdir -p /var/log/journal` + `systemd-tmpfiles --create` |
| Python virtualenv + dependencies | `python3 -m venv` + `pip install -r requirements.txt` |
| Stale AI model cleanup | Removes `models/*.pkl` so they retrain fresh |
| `site_config.yaml` scaffolding | Copied from `site_config.example.yaml` if missing |
| systemd service | Installed, enabled at boot, started |

Every one of these is **idempotent** — re-running `./install.sh` on an
already-configured Pi is safe and a no-op for anything already correct.
This matters for SD card replacement and multi-device rollout: the same
script is the right tool whether it's the very first run on a blank image
or a re-run after a `git pull` on an already-deployed device.

## What's still manual, and why

These are **not** automated, deliberately:

- **Physical wiring and board seating.** RS485 transceiver board, sensor
  cabling, power. Inherently physical — no script can verify a crimped
  connector is actually seated.
- **LTE modem/router configuration.** ModemManager/PPP/routing setup is
  hardware-specific to whatever LTE modem is attached; the service only
  requires that *some* interface eventually holds a default route and
  doesn't care which one, so there's nothing generic to automate here
  without knowing the exact modem hardware.
- **`site_config.yaml` (site_id, server host).** This is deliberately the
  one per-device setting kept outside of what `install.sh` can decide for
  you — see "Keep configuration separated from code" in `CLAUDE.md`.
- **Bootloader/EEPROM updates.** `install.sh` detects and surfaces when
  one is available, but does not apply it automatically. Firmware updates
  carry real risk (an interrupted flash is much harder to recover from
  than a bad config.txt edit) and should be a deliberate, reviewed action
  — not something a script silently does as a side effect of an
  unrelated deployment. Apply with `sudo rpi-eeprom-update -a && sudo reboot`
  when you're ready to.
- **Which Raspberry Pi OS image / Raspberry Pi Imager settings to use in
  the first place.** Out of this repo's control by definition — but
  `verify_install.sh` reports OS codename, kernel version, and model so
  you can compare two boards' starting images directly.

## Using `verify_install.sh`

Run it any time — after `install.sh`, after a reboot, after swapping an
SD card, or when troubleshooting a specific device that's behaving
differently from another:

```bash
./verify_install.sh
```

It's **read-only** — it never changes system state, only reports on it —
so it's always safe to run, including on a device that's already in the
field. It checks: Raspberry Pi model, OS version/codename, kernel
version, bootloader/EEPROM status, `enable_uart=1` in `config.txt`, no
login console in `cmdline.txt`, no active `serial-getty` on the serial
port, `/dev/serial0` existence and target, GPIO14/15 pinmux (via
`pinctrl`, falling back to `raspi-gpio`), required system packages,
`dialout` group membership (both `/etc/group` and the *current session* —
these can differ right after `usermod`, and that distinction matters),
the Python virtualenv and every required module, required directories and
free disk space, `site_config.yaml` presence and required keys, the
systemd service (installed/enabled/active), NTP time sync status, and a
live MQTT broker connectivity check using your actual `site_config.yaml`
settings. Each line is `[PASS]`, `[FAIL]`, or `[WARN]`; the script exits
non-zero if anything failed.

## Fleet deployment via SD card cloning

For deploying several Pis at once (e.g. 8 test beds), preparing one Pi
fully via `install.sh` and then cloning its SD card to the others is
faster than running `install.sh` on all 8 individually — but a raw disk
clone also duplicates state that must be *unique* per device: an
undelivered MQTT buffer tagged with the wrong `site_id`, SSH host keys,
and `/etc/machine-id` all get copied byte-for-byte along with everything
else. `prepare_master_image.sh` and `finalize_clone.sh` handle the two
ends of that safely.

**What's safe to clone as-is, and why:** `client_id` and every MQTT topic
are derived entirely from `site_config.yaml`'s `site_id` at runtime
(`server_uploader.py`: `f"{client_id}-{site_id}"`;
`onem2m_converter.py`: `f"/multisensing/{site_id}/{device_id}"`) — nothing
is hardcoded, so as long as each clone ends up with a unique `site_id`,
there's no MQTT collision risk. UART/`config.txt`/`cmdline.txt` settings
are also safe and *desirable* to clone unchanged, since they're
hardware-level config that's identical across every Pi 5 in the fleet,
not per-site.

**What must not be cloned as-is:** `logs/buffer.jsonl` is a fixed path,
not `site_id`-qualified — if the master ever had an undelivered MQTT
message queued before imaging, that file contains a fully-built payload
with the master's `site_id` already baked into the JSON body. Cloned to
another device, the first successful reconnect would publish the
master's stale data under the clone's identity. `models/*.pkl` are
`site_id`-qualified by filename (`anomaly_ai.py`), so a stale clone is
harmless but wasteful — dead files that never match the new `site_id`.
SSH host keys and `/etc/machine-id` aren't tracked in this repo but are
real files that get cloned with the disk image regardless; identical
values across 8 devices is a real (if minor) security weakness for SSH
host keys specifically, since any of them could then be impersonated
against the others.

### 1. Prepare the master (once, before imaging)

```bash
cd ~/iot-sensor-project/raspberry_pi
./prepare_master_image.sh
```

Stops `sensor-collector`, removes `logs/buffer.jsonl`,
`logs/collector.log*`, and `models/*.pkl`, and prints exactly what it
removed. Leaves `config/site_config.yaml`, project code, `.venv/`, UART
config, and the systemd service untouched — the master keeps its own
valid config, since (per your plan) it's one of the deployed units, not
a throwaway golden image. Safe to run more than once.

Then power off (`sudo systemctl poweroff`) and image the SD card with
your cloning tool of choice.

### 2. Clone the image to the other cards

Outside this repo's scope (tooling-dependent — Raspberry Pi Imager
writing a captured `.img`, `dd`, `rpi-clone`, etc.). One general note: if
you clone via raw `dd` without a tool that randomizes partition UUIDs,
every card gets an identical `PARTUUID`. For this deployment pattern
(each Pi always running its own single SD card, never multiple cards
visible to the same system at once) that causes no practical conflict —
worth knowing, not worth worrying about here.

### 3. Finalize each clone (once per clone, after first boot)

```bash
cd ~/iot-sensor-project/raspberry_pi
./finalize_clone.sh testBed03      # use that device's actual site_id
```

Do **not** run this on the master — it already has its own valid
identity from step 1. Run it once on each of the *other* clones. It:

1. Validates `site_id` (must be `testBed01`..`testBed08`)
2. If this device was already finalized before, requires you to
   **type the site_id again to confirm** before proceeding — this is
   the safeguard against accidentally re-finalizing (or reusing) a
   clone under the wrong identity
3. Updates only the `site_id:` line in `config/site_config.yaml` (via
   `tools/update_site_id.py`), leaving every other setting — server
   host, MQTT tuning, everything cloned correctly from the master —
   byte-for-byte untouched
4. Sets the hostname to match (lowercased, e.g. `testbed03`)
5. Regenerates `/etc/machine-id`
6. Regenerates SSH host keys
7. Refreshes `/var/lib/systemd/random-seed`
8. Prints the resulting MQTT `client_id` and an example topic, so you
   can see the new identity took effect before trusting it
9. Enables and restarts `sensor-collector`
10. Runs `./verify_install.sh`

Every step's result is tracked and reported in a final PASS/FAIL summary
regardless of whether an earlier step failed, so one failure doesn't
hide whether the rest of the process succeeded — read the whole summary
before deciding the device is ready.

### Testing

`tools/update_site_id.py`'s surgical edit (the part of this workflow with
real correctness risk — accidentally clobbering the rest of
`site_config.yaml`) has a full test suite in `tests/test_update_site_id.py`
(part of the normal `python -m unittest discover raspberry_pi/tests` run).
`prepare_master_image.sh`'s file-handling logic has its own bash-level
test, `tests/test_prepare_master_image.sh` — run separately with
`bash raspberry_pi/tests/test_prepare_master_image.sh`, since it isn't
Python and won't be picked up by `unittest discover`.
`finalize_clone.sh` doesn't have an equivalent end-to-end test: unlike
`prepare_master_image.sh`, its operations (hostname, `/etc/machine-id`,
SSH host keys) are genuinely system-level and can't be exercised safely
without modifying a real system. Its one piece of real logic risk — the
`site_config.yaml` edit — is covered indirectly, since it calls the same
tested `update_site_id.py` rather than reimplementing that logic itself.

## Long-term field deployment: additional findings

Beyond the UART/reproducibility work above, reviewing the project for
long-term unattended field operation surfaced a few things worth knowing
about, not all of which were in scope to fix here:

- **SD card corruption from abrupt power loss.** These are LTE-only field
  devices with no guaranteed graceful shutdown path. Raspberry Pi OS's
  default writable-root filesystem is vulnerable to corruption on sudden
  power loss — the single most common real-world Raspberry Pi field
  failure mode. `logs/collector.log` (rotated) and `logs/buffer.jsonl`
  (bounded) already minimize *how much* gets written, but the
  architecturally complete fix is a read-only root filesystem with a
  writable overlay (`raspi-config` → Performance Options → Overlay File
  System, or `overlayroot`). This is **not** applied here because it's a
  bigger structural change than a deployment script should make silently
  — a read-only root needs `config/site_config.yaml` to live on the
  writable overlay/partition specifically, which changes how updates and
  config edits are done. Worth a deliberate follow-up if SD card
  corruption becomes a recurring field issue.
- **No hardware watchdog.** `Restart=always` in the systemd unit recovers
  from the Python process crashing, but not from the whole OS/kernel
  hanging. The Pi's hardware watchdog (`dtparam=watchdog=on` +
  `systemd`'s `RuntimeWatchdogSec`, or a `sd_notify`-based watchdog ping
  from `collector.py` itself) would close that gap. Not implemented here
  — it's a real feature addition (touches the systemd unit and
  `collector.py`'s main loop), not a deployment-automation change; happy
  to add it as a follow-up if wanted.
- **No hardware RTC.** Most Pi boards have no battery-backed real-time
  clock, so the system clock can be meaningfully wrong at boot until NTP
  syncs — which itself depends on the LTE link being up.
  `verify_install.sh` reports current NTP sync status; `data_validator.py`
  already tolerates some clock skew (rejects timestamps more than 60s in
  the future), but a boot-time clock that's wrong by hours (e.g. after
  weeks powered off) could reject valid readings until NTP catches up. A
  battery-backed RTC HAT would remove this dependency entirely if it
  becomes a problem in practice.
- **Multi-device rollout.** Because every step in `install.sh` is
  idempotent and every per-site value lives only in
  `config/site_config.yaml`, `git clone && ./install.sh` is the correct
  procedure for the first Pi, the tenth Pi, or re-imaging an existing one
  after an SD card failure — there's no separate "fleet setup" process to
  maintain. For deploying several Pis at once specifically, see "Fleet
  deployment via SD card cloning" above — `prepare_master_image.sh` /
  `finalize_clone.sh` handle the state that a raw disk clone duplicates
  but shouldn't (undelivered MQTT buffer, SSH host keys, machine-id).
