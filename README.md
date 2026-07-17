# hiflow-ble Docker Setup

This project reads live data from a Hoymiles HiFlow inverter over Bluetooth
Low Energy (BLE) and publishes it via MQTT (including Home Assistant MQTT
Discovery). Since the BLE stack on Raspberry Pi adapters occasionally hangs,
a host-side watchdog setup (`scripts/`) is included that automatically
resets the adapter.

It uses the already existing hiflow-ble library from https://github.com/TheTiEr/hiflow-ble

## Overview

| Component | Runs where | Purpose |
|---|---|---|
| `hiflow-ble` (Docker container, `app/poll.py`) | in the container | connects to the inverter over BLE and publishes the values via MQTT |
| `ble-watchdog.sh` + systemd units (`scripts/`) | on the host | detects a hung `hci0` adapter and reloads the kernel modules |

The watchdog must run on the **host**, not in the container, since it
needs `rmmod`/`modprobe` and therefore root access to the kernel.

## Prerequisites

- Linux host with Docker Engine and the `docker compose` plugin
- Onboard or USB Bluetooth adapter (`hci0`)
- systemd on the host (for the watchdog units)
- An MQTT broker (e.g. Mosquitto), ideally with Home Assistant integration

## 1. Setting up the Docker container

1. Clone the repository onto the host, e.g. to `/opt/hoymiles-ble-mqtt`
   (the path is freely choosable — see section 2):

   ```bash
   sudo git clone <repo-url> /opt/hoymiles-ble-mqtt
   cd /opt/hoymiles-ble-mqtt/docker
   ```

2. Create `.env` from the template and fill in your own values.
   `docker-compose.yml` loads this file directly as the container
   environment via `env_file:` (no `${VARIABLE}` interpolation in the
   compose file); it is listed in `.gitignore`/`.dockerignore` and is
   therefore never committed or copied into the image:

   ```bash
   cp .env.example .env
   $EDITOR .env
   ```

   | Variable | Description |
   |---|---|
   | `HIFLOW_ADDRESS` | BLE advertisement name (`RMI-...`) or MAC address of the inverter |
   | `HIFLOW_ENC_RAND` | 32 hex chars, extracted once (see step 3) |
   | `HIFLOW_SN` | serial number (last 12 characters) |
   | `HIFLOW_BLE_ID` | assigned automatically on first pairing |
   | `HIFLOW_PIN` | only needed for the very first pairing, leave empty afterward |
   | `HIFLOW_INTERVAL` | poll interval in seconds (default: `60`) |
   | `MQTT_HOST` | address of the MQTT broker |
   | `MQTT_PORT` | broker port (default: `1883`) |
   | `MQTT_USER` / `MQTT_PASS` | MQTT credentials (optional) |
   | `MQTT_TOPIC` | base topic prefix (default: `hiflow`) |
   | `HIFLOW_LOGLEVEL`| log level for the container (default: `info`) |

   `HIFLOW_STATE_DIR` is intentionally not configurable via `.env` — it is
   the fixed path inside the container that `./state` is mounted to.

   The values for `HIFLOW_ADDRESS`, `HIFLOW_ENC_RAND` and `HIFLOW_SN` can be
   found e.g. on the nameplate, or picked up from the logs during the first
   pairing run (see below).

3. Build and start the container:

   ```bash
   docker compose up -d --build
   ```

   On the very first start, without `HIFLOW_ENC_RAND`/`HIFLOW_BLE_ID` set,
   the pairing process runs automatically; the resulting values appear in
   the log:

   ```bash
   docker compose logs -f hiflow-ble
   ```

   Afterward, enter the printed `enc_rand` and `bleId` values into `.env`
   (`HIFLOW_ENC_RAND`, `HIFLOW_BLE_ID`) and restart the container
   (`docker compose up -d`) so it doesn't need to re-pair on every restart.

4. The container creates the file `restart_bluetooth.state` under `./state`
   once its own in-process BLE error handling is exhausted — this
   directory is watched by the host watchdog (section 2).

## 2. Setting up the BLE watchdog scripts

1. Install on the host (adjust the path to your actual checkout):

   ```bash
   cd /opt/hoymiles-ble-mqtt/docker
   sudo ./scripts/install-ble-watchdog.sh /opt/hoymiles-ble-mqtt
   ```

   The install script (`scripts/install-ble-watchdog.sh`)

   - copies `ble-watchdog.sh` to `/usr/local/bin/`,
   - writes the chosen path to `/etc/default/ble-watchdog`
     (`COMPOSE_DIR=...`),
   - installs `ble-watchdog.service` and `ble-watchdog.timer` to
     `/etc/systemd/system/`,
   - renders `ble-watchdog.path` with the actual flag-file path
     (systemd `.path` units cannot read environment variables, hence the
     `@FLAG_FILE@` placeholder in the template),
   - enables `ble-watchdog.timer` and `ble-watchdog.path` via
     `systemctl enable --now`.

   If no path is given, `/opt/hoymiles-ble-mqtt` is used as the default.

2. Check status:

   ```bash
   systemctl status ble-watchdog.timer ble-watchdog.path
   journalctl -t ble-watchdog -f
   ```

3. On a reinstall or a changed path, `install-ble-watchdog.sh` can simply
   be run again with the new path — existing units/config files are
   overwritten.

### Manual configuration (without the install script)

Alternatively, `/etc/default/ble-watchdog` can be created by hand
(template: `scripts/ble-watchdog.env.example`):

```bash
sudo cp scripts/ble-watchdog.env.example /etc/default/ble-watchdog
sudo $EDITOR /etc/default/ble-watchdog   # adjust COMPOSE_DIR
```

In this case, `ble-watchdog.sh`, `ble-watchdog.service` and
`ble-watchdog.timer` still need to be installed manually; in
`ble-watchdog.path`, `@FLAG_FILE@` must be replaced by hand with
`<COMPOSE_DIR>/state/restart_bluetooth.state` before the file is copied to
`/etc/systemd/system/`.

## Directory structure

```
docker/
├── app/                      # Python application (poll loop, MQTT, web UI)
├── scripts/
│   ├── ble-watchdog.sh              # recovery logic, runs on the host
│   ├── ble-watchdog.env.example     # template for /etc/default/ble-watchdog
│   ├── ble-watchdog.service         # systemd oneshot service
│   ├── ble-watchdog.timer           # 5-minute fallback timer
│   ├── ble-watchdog.path            # trigger on restart_bluetooth.state (template)
│   └── install-ble-watchdog.sh      # installs the units above with a freely chosen path
├── state/                    # runtime directory, mounted as a volume (not versioned)
├── .env                      # local configuration/credentials (not versioned)
├── .env.example              # template for .env
├── Dockerfile
├── docker-compose.yml
└── README.md
```

## Testing the full chain end-to-end

You can verify the whole path — `.path` unit → `ble-watchdog.sh` recovery →
container restart — without waiting for a real BLE failure, by manually
creating the flag file the container would write.

1. Confirm the units are active and note the configured directory:

   ```bash
   systemctl status ble-watchdog.path ble-watchdog.timer
   cat /etc/default/ble-watchdog   # COMPOSE_DIR=...
   ```

2. In one terminal, follow the log live:

   ```bash
   journalctl -t ble-watchdog -f
   ```

3. In a second terminal, create the flag file inside `$COMPOSE_DIR/state`
   (same directory `docker-compose.yml` mounts to `/app/state`):

   ```bash
   sudo touch "$COMPOSE_DIR/state/restart_bluetooth.state"
   ```

   `ble-watchdog.path` should fire within a second or two and trigger
   `ble-watchdog.service`.

4. Expect to see in the journal (first terminal): `InProgress flag present …`,
   `reloading hci_uart/btbcm modules`, `recovery done — hci0: …`, and finally
   `hiflow-ble container restarted`. Afterward the flag file is gone
   (`ls "$COMPOSE_DIR/state"`) and `docker compose ps` shows a freshly
   restarted `hiflow-ble` container.

**Caveats:**

- This is a real recovery run, not a dry run — it actually unloads/reloads
  the `hci_uart`/`btbcm` kernel modules, restarts the `bluetooth` service,
  and restarts the `hiflow-ble` container. Don't run it against a live
  inverter connection you don't want interrupted.
- `MIN_INTERVAL` (60s) in `ble-watchdog.sh` throttles repeated runs. If a
  test right after a previous trigger just logs `cooldown active … —
  skipping`, either wait it out or remove `/run/ble-watchdog.lastrun` to
  force an immediate re-run.
- The file must be created as root (or by a user allowed to write into
  `./state`); only the file's *existence* matters to `PathExists=`, not its
  owner or content.

## Troubleshooting

- **Container can't find the inverter**: check `docker compose logs -f
  hiflow-ble`; `HIFLOW_ADDRESS` must be either the `RMI-...` name or the
  MAC address.
- **`hci0` hangs persistently**: `journalctl -t ble-watchdog -f` shows
  whether the watchdog is reloading the modules; `hciconfig hci0` gives
  the current adapter status.
- **Watchdog doesn't kick in**: check whether `COMPOSE_DIR` in
  `/etc/default/ble-watchdog` points to the right directory and whether a
  `state/` folder actually exists there (created via volume by
  `docker-compose.yml`).
