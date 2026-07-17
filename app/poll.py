"""Polling daemon — connects to the inverter and publishes to MQTT on a schedule.

Configuration via environment variables (all required unless marked optional):

    HIFLOW_ADDRESS    BLE MAC address or RMI-* advertisement name
    HIFLOW_ENC_RAND   32 hex chars (extract once with --extract, then reuse)
    HIFLOW_SN         12-char serial tail, e.g. 4161A02AE0B9
    HIFLOW_BLE_ID     persistent bleId  (optional; generated on first run)
    HIFLOW_PIN        BLE PIN           (optional; only needed on first pairing)
    HIFLOW_INTERVAL   poll interval in seconds (optional, default 60)
    HIFLOW_STATE_DIR  writable dir for the BLE-watchdog flag file (optional;
                      no-op if unset — see scripts/ble-watchdog.sh)

    MQTT_HOST         MQTT broker hostname / IP
    MQTT_PORT         MQTT broker port   (optional, default 1883)
    MQTT_USER         MQTT username      (optional)
    MQTT_PASS         MQTT password      (optional)
    MQTT_TOPIC        base topic prefix  (optional, default "hiflow")

Usage::

    python -m hiflow_ble.poll
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

from bleak import BleakScanner

from hiflow_ble.errors import EncRandStale
from hiflow_ble.hiflow import HiFlow
from .mqtt import HiFlowMQTT

logger = logging.getLogger(__name__)


def _require(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"ERROR: environment variable {name} is required", file=sys.stderr)
        sys.exit(1)
    return v


def _opt(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


_STATE_DIR = _opt("HIFLOW_STATE_DIR", "/app/state")
_INPROGRESS_FLAG = os.path.join(_STATE_DIR, "restart_bluetooth.state") if _STATE_DIR else None


def _signal_inprogress() -> None:
    """Tell the host watchdog that in-process BlueZ recovery has been exhausted.

    Written only after HiFlow.connect()'s own RemoveDevice retry already
    failed — not on every transient InProgress — so the host only reloads
    kernel modules / restarts the adapter when it's actually needed.
    """
    if not _INPROGRESS_FLAG:
        return
    try:
        with open(_INPROGRESS_FLAG, "w") as f:
            f.write(f"{time.time()}\n")
    except OSError as e:
        logger.warning("Could not write InProgress flag: %s", e)


def _clear_inprogress_flag() -> None:
    if not _INPROGRESS_FLAG:
        return
    try:
        os.remove(_INPROGRESS_FLAG)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.debug("Could not remove InProgress flag: %s", e)


async def _resolve_address(target: str) -> tuple[str, str | None]:
    if ":" in target:
        return target, None
    sn = target.split("-", 1)[1][-12:].upper() if "-" in target else None
    for attempt in range(10):
        try:
            logger.info("Scanning for %s … (attempt %d)", target, attempt + 1)
            dev = await BleakScanner.find_device_by_name(target, timeout=20)
            if not dev:
                logger.warning("Device %r not found in scan window — retrying", target)
                await asyncio.sleep(5)
                continue
            logger.info("Found %s at %s", target, dev.address)
            return dev.address, sn
        except Exception as e:
            if "InProgress" in str(e):
                # BlueZ still has a stale scan from a previous crash; wait for it to clear
                wait = min(30, 5 * (attempt + 1))
                logger.warning("BlueZ scanner busy (InProgress) — retrying in %ds …", wait)
                await asyncio.sleep(wait)
            else:
                raise
    logger.error("Could not resolve %r after retries — check Bluetooth adapter", target)
    sys.exit(1)


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    target   = _require("HIFLOW_ADDRESS")
    enc_rand = bytes.fromhex(_opt("HIFLOW_ENC_RAND"))
    sn       = _opt("HIFLOW_SN").upper()
    ble_id   = _opt("HIFLOW_BLE_ID")
    pin      = _opt("HIFLOW_PIN")
    interval = int(_opt("HIFLOW_INTERVAL", "60"))
    loglevel = _opt("HIFLOW_LOGLEVEL", "INFO").upper()
    logging.getLogger().setLevel(loglevel)

    mqtt_host = _require("MQTT_HOST")
    mqtt_port = int(_opt("MQTT_PORT", "1883"))
    mqtt_user = _opt("MQTT_USER")
    mqtt_pass = _opt("MQTT_PASS")
    mqtt_topic = _opt("MQTT_TOPIC", "hiflow")

    address, sn_from_name = await _resolve_address(target)
    sn = sn or sn_from_name or sn

    pub = HiFlowMQTT(
        mqtt_host, mqtt_port, mqtt_user, mqtt_pass,
        sn=sn, base_topic=mqtt_topic,
    )
    pub.connect()
    pub.publish_discovery()
    logger.info("MQTT discovery published → %s", pub.state_topic)

    def _on_disconnect() -> None:
        logger.warning("HiFlow: link dropped by inverter")
        _signal_inprogress()


    hf = HiFlow(
        address,
        enc_rand=enc_rand,
        sn=sn,
        ble_id=ble_id,
        pin=pin,
        timeout=15,
    )

    logger.info("Starting poll loop (interval=%ds)", interval)
    consecutive_errors = 0

    try:
        while True:
            try:
                if not hf.is_connected:
                    logger.info("Connecting …")
                    await hf.connect()
                
                if not enc_rand:
                    er = await hf.async_extract_enc_rand()
                    logger.info(
                        "enc_rand extracted: %s  ← update HIFLOW_ENC_RAND if you restart the container",
                        er.hex(),
                    )
                    enc_rand = er

                if not hf._handshake_done:
                    ok = await hf.async_do_comm_cmd_handshake()
                    if not ok:
                        logger.warning("Handshake failed — will retry")
                        await hf._safe_disconnect()
                        pub.publish_status(False)
                        await asyncio.sleep(min(30, interval))
                        continue
                    if hf.ble_id and hf.ble_id != ble_id:
                        ble_id = hf.ble_id
                        logger.info("bleId whitelisted: %s  (set HIFLOW_BLE_ID=%s)", ble_id, ble_id)

                data = await hf.async_get_real_data_new()
                if data is None:
                    raise RuntimeError("no data returned")

                pub.publish_state(data)
                pub.publish_status(True)
                logger.info(
                    "Published: ac=%.1fW  pv1=%.1fW  pv2=%.1fW  temp=%.1f°C",
                    data.sgs_data[0].active_power / 10 if data.sgs_data else 0,
                    next((p.power / 10 for p in data.pv_data if p.port_number == 1), 0),
                    next((p.power / 10 for p in data.pv_data if p.port_number == 2), 0),
                    data.sgs_data[0].temperature / 10 if data.sgs_data else 0,
                )
                consecutive_errors = 0
                _clear_inprogress_flag()

            except asyncio.CancelledError:
                raise
            except EncRandStale:
                logger.warning("enc_rand stale (inverter rebooted?) — re-running V0 pairing …")
                await hf._safe_disconnect()
                pub.publish_status(False)
                try:
                    new_er = await hf.async_extract_enc_rand()
                    logger.info(
                        "New enc_rand: %s  ← update HIFLOW_ENC_RAND if you restart the container",
                        new_er.hex(),
                    )
                    consecutive_errors = 0
                except Exception as pair_err:
                    consecutive_errors += 1
                    logger.warning("Re-pairing failed: %s — retrying in %ds", pair_err,
                                   min(300, interval * consecutive_errors))
                    await asyncio.sleep(min(300, interval * consecutive_errors))
                continue
            except Exception as e:
                consecutive_errors += 1
                logger.warning("Poll error #%d: %s", consecutive_errors, e)
                await hf._safe_disconnect()
                pub.publish_status(False)
                if "InProgress" in str(e):
                    # HiFlow.connect() already tried RemoveDevice + one retry —
                    # this means BlueZ itself is stuck. Signal the host watchdog.
                    logger.warning("InProgress survived in-process recovery — signalling host watchdog")
                    _signal_inprogress()
                backoff = min(300, interval * consecutive_errors)
                logger.info("Retrying in %ds …", backoff)
                await asyncio.sleep(backoff)
                continue

            await asyncio.sleep(interval)

    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutting down …")
    finally:
        await hf._safe_disconnect()
        pub.disconnect()
        logger.info("Disconnected.")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
