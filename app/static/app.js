const REFRESH_MS = 5000;
const STALE_FACTOR = 3;       // data older than poll_interval * this = "stale"
const PANEL_REF_WATT = 450;   // used only to size the little power bar

const deviceTpl = document.getElementById("device-template");
const panelTpl = document.getElementById("panel-template");
const devicesEl = document.getElementById("devices");
const emptyEl = document.getElementById("empty-state");
const pillMqtt = document.getElementById("pill-mqtt");
const pillBle = document.getElementById("pill-ble");
const lastRefreshEl = document.getElementById("last-refresh");

function setPill(el, level, text) {
  el.className = "pill pill-" + level;
  el.innerHTML = '<span class="dot"></span> ' + text;
}

function formatEnergy(wh) {
  if (wh === undefined || wh === null) return "–";
  if (Math.abs(wh) >= 1000) return { num: (wh / 1000).toFixed(2), unit: "kWh" };
  return { num: Math.round(wh).toString(), unit: "Wh" };
}

function setTile(root, field, num, unit) {
  const tile = root.querySelector('[data-field="' + field + '"]');
  if (!tile) return;
  tile.querySelector(".num").textContent = num;
  if (unit !== undefined) tile.querySelector(".unit").textContent = unit;
}

function deviceStatusLevel(dev, serverTs, pollInterval) {
  const status = dev.status;
  if (!status) return "muted";
  if (!status.ble_connected) return "critical";
  if (status.ts && serverTs - status.ts > pollInterval * STALE_FACTOR) return "warning";
  return "good";
}

function renderDevice(sn, dev, serverTs, pollInterval) {
  const node = deviceTpl.content.firstElementChild.cloneNode(true);
  const state = dev.state || {};
  const level = deviceStatusLevel(dev, serverTs, pollInterval);

  node.querySelector(".device-title").textContent = "Wechselrichter " + sn;
  const meta = node.querySelector(".device-meta");
  if (level === "critical") {
    meta.textContent = "BLE getrennt";
    node.classList.add("is-stale", "is-critical");
  } else if (level === "warning") {
    meta.textContent = "Daten veraltet";
    node.classList.add("is-stale", "is-warning");
  } else if (level === "muted") {
    meta.textContent = "warte auf Statusmeldung …";
    node.classList.add("is-stale", "is-muted");
  } else {
    const ageSec = state.ts ? Math.max(0, serverTs - state.ts) : null;
    meta.textContent = ageSec === null ? "" : "aktualisiert vor " + ageSec + "s";
  }

  // panel count / keys
  const panelNums = Object.keys(state)
    .map((k) => (k.match(/^pv(\d+)_power$/) || [])[1])
    .filter(Boolean)
    .map(Number)
    .sort((a, b) => a - b);

  setTile(node, "ac_power", state.ac_power !== undefined ? Math.round(state.ac_power) : "–");
  setTile(node, "panel_count", panelNums.length || "–", "");
  setTile(node, "temperature", state.temperature !== undefined ? state.temperature.toFixed(1) : "–");
  setTile(node, "ac_voltage", state.ac_voltage !== undefined ? state.ac_voltage.toFixed(1) : "–");
  setTile(node, "ac_frequency", state.ac_frequency !== undefined ? state.ac_frequency.toFixed(2) : "–");

  const todayTotal = panelNums.reduce((sum, p) => sum + (state["pv" + p + "_today"] || 0), 0);
  const energyTotal = panelNums.reduce((sum, p) => sum + (state["pv" + p + "_total"] || 0), 0);
  const todayFmt = formatEnergy(panelNums.length ? todayTotal : undefined);
  const totalFmt = formatEnergy(panelNums.length ? energyTotal : undefined);
  setTile(node, "today_total", todayFmt === "–" ? "–" : todayFmt.num, todayFmt === "–" ? undefined : todayFmt.unit);
  setTile(node, "energy_total", totalFmt === "–" ? "–" : totalFmt.num, totalFmt === "–" ? undefined : totalFmt.unit);

  const panelRow = node.querySelector(".panel-row");
  panelNums.forEach((p) => {
    const pNode = panelTpl.content.firstElementChild.cloneNode(true);
    const power = state["pv" + p + "_power"];
    pNode.querySelector(".panel-name").textContent = "Panel " + p;
    pNode.querySelector(".panel-power").textContent =
      power !== undefined ? Math.round(power) + " W" : "–";
    pNode.querySelector(".panel-bar-fill").style.width =
      Math.max(0, Math.min(100, ((power || 0) / PANEL_REF_WATT) * 100)) + "%";
    pNode.querySelector(".p-voltage").textContent =
      state["pv" + p + "_voltage"] !== undefined ? state["pv" + p + "_voltage"].toFixed(1) + " V" : "–";
    pNode.querySelector(".p-current").textContent =
      state["pv" + p + "_current"] !== undefined ? state["pv" + p + "_current"].toFixed(2) + " A" : "–";
    const pToday = formatEnergy(state["pv" + p + "_today"]);
    const pTotal = formatEnergy(state["pv" + p + "_total"]);
    pNode.querySelector(".p-today").textContent = pToday === "–" ? "–" : pToday.num + " " + pToday.unit;
    pNode.querySelector(".p-total").textContent = pTotal === "–" ? "–" : pTotal.num + " " + pTotal.unit;
    panelRow.appendChild(pNode);
  });

  return node;
}

async function refresh() {
  let data;
  try {
    const res = await fetch("/api/state", { cache: "no-store" });
    data = await res.json();
  } catch (e) {
    setPill(pillMqtt, "critical", "Server nicht erreichbar");
    setPill(pillBle, "muted", "unbekannt");
    return;
  }

  setPill(
    pillMqtt,
    data.mqtt_connected ? "good" : "critical",
    data.mqtt_connected ? "MQTT verbunden" : "MQTT getrennt"
  );

  const snList = Object.keys(data.devices || {});
  if (snList.length === 0) {
    emptyEl.style.display = "";
    devicesEl.innerHTML = "";
    setPill(pillBle, "muted", "warte auf Gerät …");
  } else {
    emptyEl.style.display = "none";
    devicesEl.innerHTML = "";
    const levels = [];
    snList
      .sort()
      .forEach((sn) => {
        const dev = data.devices[sn];
        levels.push(deviceStatusLevel(dev, data.server_ts, data.poll_interval));
        devicesEl.appendChild(renderDevice(sn, dev, data.server_ts, data.poll_interval));
      });

    if (levels.includes("critical")) setPill(pillBle, "critical", "BLE getrennt");
    else if (levels.includes("warning")) setPill(pillBle, "warning", "Daten veraltet");
    else if (levels.every((l) => l === "good")) setPill(pillBle, "good", "BLE verbunden");
    else setPill(pillBle, "muted", "unbekannt");
  }

  lastRefreshEl.textContent = "zuletzt aktualisiert: " + new Date().toLocaleTimeString("de-DE");
}

refresh();
setInterval(refresh, REFRESH_MS);
