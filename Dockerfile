# syntax=docker/dockerfile:1
FROM python:3.12-slim

# ble_flood.py drives the adapter raw over HCI_CHANNEL_USER — pure stdlib, no BlueZ, no pip
# deps. Kept deliberately tiny; this is not the firmware-tester toolchain image.
COPY ble_flood.py /usr/local/bin/ble_flood.py
RUN chmod +x /usr/local/bin/ble_flood.py

# Convenience for `docker run`; CI runners (Woodpecker) invoke the script explicitly instead.
ENTRYPOINT ["python3", "/usr/local/bin/ble_flood.py"]
CMD ["--index", "0", "--rate", "40"]
