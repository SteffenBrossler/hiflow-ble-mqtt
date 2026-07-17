FROM python:3.12-slim AS builder

# git is only needed to fetch hiflow-ble; it never ships in the final image
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --user paho-mqtt git+https://github.com/TheTiEr/hiflow-ble

FROM python:3.12-slim

# BlueZ + D-Bus required for Bleak on Linux
RUN apt-get update && apt-get install -y --no-install-recommends \
        bluez \
        dbus \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

WORKDIR /app
COPY app/ ./app

CMD ["python", "-m", "app.poll"]
