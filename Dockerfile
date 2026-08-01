# syntax=docker/dockerfile:1
FROM python:3.12-slim

# ble_flood.py drives the adapter raw over HCI_CHANNEL_USER — pure stdlib, no BlueZ, no pip
# deps. Kept deliberately tiny; this is not the firmware-tester toolchain image.
WORKDIR /app
COPY ble_flood.py .

ENTRYPOINT ["python3", "ble_flood.py"]
CMD ["--index", "0", "--rate", "40"]
