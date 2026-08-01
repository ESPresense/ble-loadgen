# syntax=docker/dockerfile:1
FROM python:3.12-slim

# ble_flood.py drives the adapter raw over HCI_CHANNEL_USER — pure stdlib, no BlueZ, no pip
# deps. Kept deliberately tiny; this is not the firmware-tester toolchain image.
COPY ble_flood.py /app/ble_flood.py

# Absolute path on purpose: CI runners (Woodpecker) set the container's working directory to
# the repo checkout, so a relative entrypoint would look for ble_flood.py there and miss.
ENTRYPOINT ["python3", "/app/ble_flood.py"]
CMD ["--index", "0", "--rate", "40"]
