"""Dashboard web server — shows BLE/MQTT link status and live inverter data.

Reads the same MQTT topics ``poll.py`` publishes to (state + status), so it
runs as an independent process/container and never touches the BLE adapter
itself.

Configuration via environment variables:

    MQTT_HOST         MQTT broker hostname / IP (required)
    MQTT_PORT         MQTT broker port   (optional, default 1883)
    MQTT_USER         MQTT username      (optional)
    MQTT_PASS         MQTT password      (optional)
    MQTT_TOPIC        base topic prefix  (optional, default "hiflow")
    HIFLOW_INTERVAL   expected poll interval in seconds — used to flag data
                       as stale in the UI (optional, default 60)
    WEBUI_HOST        bind address       (optional, default 0.0.0.0)
    WEBUI_PORT         bind port          (optional, default 8080)

Usage::

    python -m hiflow_ble.webui
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from typing import Any

from . import logger


def _require(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"ERROR: environment variable {name} is required", file=sys.stderr)
        sys.exit(1)
    return v


def _opt(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class DeviceStore:
    """Thread-safe in-memory cache of the latest MQTT state/status per inverter."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._devices: dict[str, dict[str, Any]] = {}
        self.mqtt_connected = False

    def update_state(self, sn: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._devices.setdefault(sn, {})["state"] = payload

    def update_status(self, sn: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._devices.setdefault(sn, {})["status"] = payload

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            devices = {sn: dict(d) for sn, d in self._devices.items()}
        return {
            "mqtt_connected": self.mqtt_connected,
            "server_ts": round(time.time()),
            "poll_interval": int(_opt("HIFLOW_INTERVAL", "60")),
            "devices": devices,
        }


store = DeviceStore()


def _build_mqtt_client(topic_base: str):
    import paho.mqtt.client as mqtt  # type: ignore[import]

    def on_connect(client, userdata, flags, rc) -> None:
        store.mqtt_connected = rc == 0
        if rc == 0:
            client.subscribe(f"{topic_base}/+/state")
            client.subscribe(f"{topic_base}/+/status")
            logger.info("webui: MQTT connected — subscribed to %s/+/state, %s/+/status", topic_base, topic_base)
        else:
            logger.warning("webui: MQTT connect failed, rc=%s", rc)

    def on_disconnect(client, userdata, rc) -> None:
        store.mqtt_connected = False

    def on_message(client, userdata, msg) -> None:
        parts = msg.topic.split("/")
        if len(parts) != 3:
            return
        _base, sn, kind = parts
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if kind == "state":
            store.update_state(sn, payload)
        elif kind == "status":
            store.update_status(sn, payload)

    client = mqtt.Client(client_id="hiflow-webui")
    user = _opt("MQTT_USER")
    if user:
        client.username_pw_set(user, _opt("MQTT_PASS"))
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    return client


def create_app():
    from flask import Flask, jsonify, render_template

    app = Flask(__name__)

    topic_base = _opt("MQTT_TOPIC", "hiflow")
    mqtt_client = _build_mqtt_client(topic_base)
    mqtt_client.connect(
        _require("MQTT_HOST"),
        int(_opt("MQTT_PORT", "1883")),
        keepalive=30,
    )
    mqtt_client.loop_start()

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/state")
    def api_state():
        return jsonify(store.snapshot())

    return app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("HIFLOW_LOGLEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = create_app()
    app.run(
        host=_opt("WEBUI_HOST", "0.0.0.0"),
        port=int(_opt("WEBUI_PORT", "8080")),
    )


if __name__ == "__main__":
    main()
