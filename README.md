# ble-loadgen

BLE advertisement load generator for the ESPresense HIL bench. It advertises from a
**static random** address ~40 times a second, rotating through a pool of 4096 of them, so
every rotation costs a listening node a new fingerprint slot — a room full of phones,
compressed. It exists to make the slow heap decline in
[ESPresense#2309](https://github.com/ESPresense/ESPresense/issues/2309) show up in a HIL
window instead of over days on a shelf.

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

On `EBUSY` the flood retries for 30s before giving up — busy is usually transient. If it
still fails, something is holding the adapter: `bluetoothd`
(`systemctl mask --now bluetooth`) or a leftover detached ble-flood from a previous HIL run
(`pkill -f ble_flood.py`). Missing `CAP_NET_ADMIN` or a missing adapter fails immediately
instead, and the message says which.

## Usage

The script is pure stdlib — run it directly:

```bash
python3 ble_flood.py --selftest              # framing + address rules, no hardware
sudo python3 ble_flood.py --index 0 --rate 40        # flood until killed
sudo python3 ble_flood.py --index 0 --seconds 30     # one 30s burst
sudo python3 ble_flood.py --index 0 --pool 512       # smaller address pool
```

### Address pool

Addresses come from a pool (default 4096, or `$BLE_FLOOD_POOL`) and repeat once it wraps.
The churn a node sees is unchanged — every rotation is still a different MAC until the pool
wraps — but anything downstream that keys off the address stops growing at `--pool` rows.
That matters because the flood's addresses escape the bench: ESPresense Companion turns each
one into an MQTT discovery config and Home Assistant into a `device_tracker`, and BlueZ
caches each under `/var/lib/bluetooth/*/cache`. A 58-hour unbounded soak minted 8.3M
addresses, left 53k orphaned HA entities, and exhausted the inodes on the HA host.

`--pool 0` restores the old unbounded behaviour. Only use it against a bench whose
subscribers you are willing to rebuild.

### iBeacons

Some rotations advertise iBeacon frames instead, from a fixed set of MACs (`--ibeacons`, default
2; `$BLE_FLOOD_IBEACONS`; 0 turns them off). Each beacon keeps its MAC and changes its proximity
UUID every `--ibeacon-period` seconds (default 30), the way a BC04P does while in motion
(ESPresense#2492). The UUIDs are derived from the beacon index and the period, and every change
is logged as `[flood] ibeacon <n> uuid <uuid> (phase <p>)`, so a run can assert on what it
should have seen.

**Ignoring them downstream:** every loadgen iBeacon UUID starts with `f1ad0000`, so
Companion and other consumers can drop the flood's beacons by prefix (`f1ad0000-` in the UUID
string). `--ibeacons` is capped at 65535, because beacon *i* advertises major *i + 1* and the
major is 16 bits; larger values are rejected at startup.

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

## Releasing

Every push to `main` publishes `latest` and `sha-<short>`. The semver tags the HIL pipeline
pins (`:1`, `:1.2`, `:1.2.0`) move only when a `v*` git tag is pushed, so a merged change
reaches the bench only once it has been released.

1. **Merge to `main` through a PR**, and wait for CI (`--selftest`) to pass on the merge commit.
2. **Pick the version.** Bump the patch for fixes and the minor for new flags or behaviour,
   including a changed default (v1.1.0 bounded the address pool). Bump the major only when
   an existing invocation would break, such as a removed or renamed flag or a changed
   entrypoint, since that is what moves people off `:1`.
3. **Tag the merge commit and push the tag.** Tags are lightweight and are always cut from
   `main`, never from a feature branch:

   ```bash
   git fetch origin
   git tag v1.2.0 origin/main
   git push origin v1.2.0
   ```

4. **Check the image.** The tag starts *Build and Push Docker Image*, which publishes
   `1.2.0`, `1.2` and `1`:

   ```bash
   gh run list --workflow docker.yml -L 1
   ```

5. **Publish a GitHub Release** for the tag. `--generate-notes` lists the merged PRs; for
   anything beyond that, write the notes by hand. Call out any change to a default, because
   everyone pinned to `:1` gets it without asking:

   ```bash
   gh release create v1.2.0 --generate-notes
   ```

## Troubleshooting

**`HCI_Reset` unanswered, or `no completion event`** — the controller is claimed (the bind
succeeded, so nothing else can be driving it) but it is not answering. The reset is retried
3 × 15s for exactly this reason: a command sent into the window right after a user-channel bind
can be lost, or answered only once a USB part finishes its firmware setup. The bench hit this
while crow did not. If it still gives up, the message prints the rfkill state and the causes a
successful bind cannot explain — a soft rfkill block, an autosuspended USB port, a dongle that
needs a re-plug, or a flood leaked from an earlier run (`pkill -f ble_flood.py`).

**The bench is running old code.** `:1` and `:1.0` are semver tags: they only move when a `v*`
git tag is pushed, while `latest`/`sha-*` track `main`. So a fix merged to `main` does *not*
reach a pipeline that pins `:1` until a release is cut — which is how the bench spent six weeks
on an image that predated the SIGTERM cleanup, leaving `hci0` wedged between runs.

## Why static random addresses with the MAC in the name

ESPresense keys `ID_TYPE_RAND_STATIC_MAC` off the top two bits of the address MSB, so each
rotation is a distinct identity. Each advert also carries a name containing its own MAC —
without that, a live node collapsed two addresses to one id (`ID_TYPE_NAME` outranks
`ID_TYPE_RAND_STATIC_MAC`), and the id space this exists to exercise never churned. That
detail came from the bench, not theory.
