# ble-loadgen

BLE advertisement load generator for the ESPresense HIL bench. It advertises from a fresh
**static random** address ~40 times a second, so every rotation costs a listening node a
new fingerprint slot — a room full of phones, compressed. It exists to make the slow heap
decline in [ESPresense#2309](https://github.com/ESPresense/ESPresense/issues/2309) show up
in a HIL window instead of over days on a shelf.

Split out of `firmware-tester` because it shares nothing with it: no PlatformIO, no serial,
no toolchain — just Python stdlib and a raw HCI socket. Its own image
(`ghcr.io/espresense/ble-loadgen`) stays tiny and releases on its own cadence.

## How it runs

As a **detached step in the ESPresense HIL pipeline** (`.woodpecker/hil.yml`): it floods for
the run and the runner kills it at the end — no host service, no gating.

```yaml
- name: ble-flood
  image: ghcr.io/espresense/ble-loadgen:1
  detach: true
  privileged: true
  network_mode: host          # raw HCI only works in the host netns (see below)
  # no commands: the image ENTRYPOINT is `python3 ble_flood.py`, CMD supplies --index/--rate
```

## Requirements

- **`network_mode: host`.** `HCI_CHANNEL_USER` only works in the host network namespace — a
  bridge-net container can't even open an `AF_BLUETOOTH` socket (`EAFNOSUPPORT`). `privileged`
  supplies `CAP_NET_ADMIN`.
- **A USB Bluetooth adapter** on the host. `ls /sys/class/bluetooth` should show `hci0`.
- **BlueZ absent.** `HCI_CHANNEL_USER` takes exclusive control of a *down* adapter;
  `bluetoothd` would fight for it. Don't install it, or mask it.

## Usage

The script is pure stdlib — run it directly:

```bash
python3 ble_flood.py --selftest              # framing + address rules, no hardware
sudo python3 ble_flood.py --index 0 --rate 40        # flood until killed
sudo python3 ble_flood.py --index 0 --seconds 30     # one 30s burst
```

### Docker

The image entrypoint is `python3 ble_flood.py`, so arguments go straight after the image.
Flooding needs the host network namespace (raw HCI) and `CAP_NET_ADMIN`:

```bash
# flood until stopped — default args are --index 0 --rate 40
docker run --rm --network host --privileged ghcr.io/espresense/ble-loadgen:1

# one 30s burst on hci0
docker run --rm --network host --privileged ghcr.io/espresense/ble-loadgen:1 --seconds 30

# selftest needs neither host net nor privileged (it touches no socket)
docker run --rm ghcr.io/espresense/ble-loadgen:1 --selftest
```

`--cap-add NET_ADMIN` in place of `--privileged` also works; `--privileged` is what the HIL
pipeline already grants, so the docs use it for parity.

## Why static random addresses with the MAC in the name

ESPresense keys `ID_TYPE_RAND_STATIC_MAC` off the top two bits of the address MSB, so each
rotation is a distinct identity. Each advert also carries a name containing its own MAC —
without that, a live node collapsed two addresses to one id (`ID_TYPE_NAME` outranks
`ID_TYPE_RAND_STATIC_MAC`), and the id space this exists to exercise never churned. That
detail came from the bench, not theory.
