"""Home Assistant MQTT Discovery publisher for HiFlow BLE data.

Usage::

    from hiflow_ble.mqtt import HiFlowMQTT
    pub = HiFlowMQTT("192.168.1.10", sn="4161A02AE0B9")
    pub.connect()
    pub.publish_discovery()
    pub.publish_state(real_data_new_response)
    pub.disconnect()
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

# Sensor definitions: (json_key, friendly_name, unit, device_class, state_class, icon)
# device_class=None → no HA device class; state_class=None → no state class
_SENSORS: list[tuple[str, str, str | None, str | None, str | None, str | None]] = [
    ("ac_power",       "AC Power",        "W",   "power",        "measurement",      None),
    ("ac_voltage",     "AC Voltage",      "V",   "voltage",      "measurement",      None),
    ("ac_current",     "AC Current",      "A",   "current",      "measurement",      None),
    ("ac_frequency",   "AC Frequency",    "Hz",  "frequency",    "measurement",      None),
    ("ac_power_factor","AC Power Factor", None,  "power_factor", "measurement",      None),
    ("temperature",    "Temperature",     "°C",  "temperature",  "measurement",      None),
    ("power_limit",    "Power Limit",     "%",   None,           "measurement",      "mdi:tune"),
    ("pv1_voltage",    "PV1 Voltage",     "V",   "voltage",      "measurement",      None),
    ("pv1_current",    "PV1 Current",     "A",   "current",      "measurement",      None),
    ("pv1_power",      "PV1 Power",       "W",   "power",        "measurement",      None),
    ("pv1_today",      "PV1 Energy Today","Wh",  "energy",       "total_increasing", None),
    ("pv1_total",      "PV1 Energy Total","Wh",  "energy",       "total_increasing", None),
    ("pv2_voltage",    "PV2 Voltage",     "V",   "voltage",      "measurement",      None),
    ("pv2_current",    "PV2 Current",     "A",   "current",      "measurement",      None),
    ("pv2_power",      "PV2 Power",       "W",   "power",        "measurement",      None),
    ("pv2_today",      "PV2 Energy Today","Wh",  "energy",       "total_increasing", None),
    ("pv2_total",      "PV2 Energy Total","Wh",  "energy",       "total_increasing", None),
]


def scale_real_data(msg: Any, sn: str) -> dict[str, Any]:
    """Convert a RealDataNewReqDTO protobuf message to a scaled dict.

    All raw integer fields are divided by the Hoymiles-standard scaling factor
    so values arrive at Home Assistant in proper SI units.
    """
    data: dict[str, Any] = {"sn": sn}

    if msg.sgs_data:
        ac = msg.sgs_data[0]
        data["ac_power"]        = round(ac.active_power / 10, 1)
        data["ac_voltage"]      = round(ac.voltage       / 10, 1)
        data["ac_current"]      = round(ac.current       / 100, 2)
        data["ac_frequency"]    = round(ac.frequency     / 100, 2)
        data["ac_power_factor"] = round(ac.power_factor  / 1000, 3)
        data["temperature"]     = round(ac.temperature   / 10, 1)
        data["power_limit"]     = round(ac.power_limit   / 100, 1)

    for pv in msg.pv_data:
        p = pv.port_number          # 1 or 2
        prefix = f"pv{p}"
        data[f"{prefix}_voltage"] = round(pv.voltage      / 10, 1)
        data[f"{prefix}_current"] = round(pv.current      / 100, 2)
        data[f"{prefix}_power"]   = round(pv.power        / 10, 1)
        data[f"{prefix}_today"]   = round(pv.energy_daily, 1)
        data[f"{prefix}_total"]   = round(pv.energy_total, 1)

    return data


class HiFlowMQTT:
    """Thin wrapper around paho-mqtt for HA MQTT Discovery + state publishing."""

    def __init__(
        self,
        host: str,
        port: int = 1883,
        username: str = "",
        password: str = "",
        sn: str = "",
        base_topic: str = "hiflow",
        discovery_prefix: str = "homeassistant",
    ):
        try:
            import paho.mqtt.client as mqtt  # type: ignore[import]
        except ImportError as e:
            raise ImportError(
                "paho-mqtt is required: pip install hiflow-ble[mqtt]"
            ) from e

        self._mqtt = mqtt
        self.sn = sn.upper()
        self.state_topic = f"{base_topic}/{self.sn}/state"
        self.status_topic = f"{base_topic}/{self.sn}/status"
        self._discovery_prefix = discovery_prefix
        self._client = mqtt.Client(client_id=f"hiflow-ble-{self.sn}")
        if username:
            self._client.username_pw_set(username, password)
        self._host = host
        self._port = port
        # Last-Will: if this process dies without a clean disconnect, the
        # broker flips status to offline for us — the web UI relies on this.
        self._client.will_set(
            self.status_topic,
            json.dumps({"ble_connected": False, "ts": None}),
            retain=True,
        )

    def connect(self) -> None:
        self._client.connect(self._host, self._port, keepalive=60)
        self._client.loop_start()

    def disconnect(self) -> None:
        self.publish_status(False)
        self._client.loop_stop()
        self._client.disconnect()

    def publish_discovery(self) -> None:
        """Publish HA MQTT Discovery config topics for all sensors."""
        device = {
            "identifiers": [f"hiflow_{self.sn}"],
            "name": f"HiFlow {self.sn}",
            "manufacturer": "Hoymiles",
            "model": "HiFlow Pro (HMS-*-2WB)",
        }
        for key, name, unit, dev_class, state_class, icon in _SENSORS:
            cfg: dict[str, Any] = {
                "name": name,
                "unique_id": f"hiflow_{self.sn}_{key}",
                "state_topic": self.state_topic,
                "value_template": f"{{{{ value_json.{key} }}}}",
                "device": device,
            }
            if unit:
                cfg["unit_of_measurement"] = unit
            if dev_class:
                cfg["device_class"] = dev_class
            if state_class:
                cfg["state_class"] = state_class
            if icon:
                cfg["icon"] = icon

            topic = (
                f"{self._discovery_prefix}/sensor/"
                f"hiflow_{self.sn}_{key}/config"
            )
            self._client.publish(topic, json.dumps(cfg), retain=True)

    def publish_state(self, msg: Any) -> None:
        """Scale and publish a RealDataNewReqDTO as a single JSON state message."""
        payload = scale_real_data(msg, self.sn)
        payload["ts"] = round(time.time())
        self._client.publish(self.state_topic, json.dumps(payload), retain=True)

    def publish_status(self, ble_connected: bool) -> None:
        """Publish link status — consumed by the web UI to show BLE health."""
        payload = {"ble_connected": ble_connected, "ts": round(time.time())}
        self._client.publish(self.status_topic, json.dumps(payload), retain=True)
