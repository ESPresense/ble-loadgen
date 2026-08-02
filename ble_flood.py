#!/usr/bin/env python3
"""Flood the bench with BLE advertisements from a fresh random address every time.

Why: ESPresense fingerprints a *static random* address by MAC (BleFingerprint.cpp,
ID_TYPE_RAND_STATIC_MAC when the top two bits of the MSB are set), so every rotation
here costs the node a new fingerprint slot against a pool of 100-200. That is the
churn a real room full of phones produces, compressed — the load under which
ESPresense#2309's slow heap decline shows up in hours instead of days.

The adapter is driven raw over HCI_CHANNEL_USER, which means BlueZ is not involved and
does not need to be installed: the kernel hands us the controller exclusively and
nothing else can fight us for it. Requires root (CAP_NET_ADMIN) and an adapter that no
other process has powered up.

Runs as a detached step in the HIL pipeline (.woodpecker/hil.yml), so it floods for the
run and the runner stops it at the end — no external gating.

  ble_flood.py --index 0 --rate 40   # flood until killed
  ble_flood.py --index 0 --seconds 30  # one 30s burst
  ble_flood.py --selftest            # framing/address rules, no hardware
"""

import argparse
import ctypes
import errno as errno_mod
import fcntl
import os
import secrets
import select
import socket
import struct
import time

AF_BLUETOOTH = 31
BTPROTO_HCI = 1
HCI_CHANNEL_USER = 1
HCIDEVDOWN = 0x400448CA

HCI_COMMAND_PKT = 0x01
HCI_EVENT_PKT = 0x04
EVT_CMD_COMPLETE = 0x0E
EVT_CMD_STATUS = 0x0F

OCF_RESET = 0x0C03
OCF_LE_SET_RANDOM_ADDRESS = 0x2005
OCF_LE_SET_ADV_PARAMETERS = 0x2006
OCF_LE_SET_ADV_DATA = 0x2008
OCF_LE_SET_ADV_ENABLE = 0x200A

ADV_NONCONN_IND = 0x03
OWN_ADDR_TYPE_RANDOM = 0x01
ADV_INTERVAL = 0x0020  # 20 ms — the BLE minimum, so a packet lands on all 3 channels

# ponytail: one constant, no --wait flag. A busy adapter is usually transient (bluetoothd
# re-powering it, or the previous run's flood still exiting); 30s covers both. Make it a CLI
# flag only if the bench shows a legitimate wait longer than this.
BIND_TIMEOUT = 30.0


def hci_command(opcode, params=b""):
    """Frame one HCI command packet: type, opcode (LE), parameter length, parameters."""
    if len(params) > 255:
        raise ValueError(f"HCI parameters too long: {len(params)}")
    return struct.pack("<BHB", HCI_COMMAND_PKT, opcode, len(params)) + params


def random_static_address():
    """A valid BLE static random address, in the little-endian order HCI wants.

    Spec: the two most significant bits must both be 1, and the remaining 46 bits must
    not be all-zeros or all-ones. The MSB is the *last* byte on the wire. Those top bits
    are also exactly what makes ESPresense classify it as ID_TYPE_RAND_STATIC_MAC rather
    than discarding it as a resolvable private address it cannot resolve.
    """
    while True:
        addr = bytearray(secrets.token_bytes(6))
        addr[5] |= 0xC0
        rest = int.from_bytes(addr, "little") & ((1 << 46) - 1)
        if rest not in (0, (1 << 46) - 1):
            return bytes(addr)


def advertising_payload(address):
    """Flags + a complete local name that is unique to this address.

    The name must vary per rotation. ESPresense ranks a name (ID_TYPE_NAME, 35) above a
    static random address (ID_TYPE_RAND_STATIC_MAC, 5), so a constant name would collapse
    every advert in the flood onto one logical id — the slot pool would still churn, but
    the id space this is meant to exercise would not. Confirmed on a live node, which
    reported two different MACs both as id "name:hil-flood".

    The full MAC goes in the name rather than a short suffix: at 40/s a 3-byte suffix
    collides tens of thousands of times over an 8h soak, quietly merging ids again.
    """
    name = b"HIL-" + address[::-1].hex().encode()  # MSB-first, matching how nodes show it
    fields = bytes([2, 0x01, 0x06]) + bytes([len(name) + 1, 0x09]) + name
    if len(fields) > 31:
        raise ValueError(f"advertising payload too long: {len(fields)}")
    return bytes([len(fields)]) + fields.ljust(31, b"\x00")


def adv_parameters():
    return struct.pack(
        "<HHBBB6sBB",
        ADV_INTERVAL, ADV_INTERVAL,
        ADV_NONCONN_IND,
        OWN_ADDR_TYPE_RANDOM,
        0x00,            # peer address type (unused for undirected)
        b"\x00" * 6,     # peer address (unused)
        0x07,            # all three advertising channels
        0x00,            # no filtering — anyone may scan
    )


def _bind_hint(index, down_err, bind_err):
    """What actually went wrong, given how HCIDEVDOWN and bind() each failed.

    The ioctl's errno is the only thing that separates the causes: EBUSY on bind alone says
    "the adapter is up", not *why*. Guessing at all three (the old message did) sends whoever
    is on the bench chasing the wrong one.
    """
    what = f"bind(hci{index}, HCI_CHANNEL_USER) failed: {os.strerror(bind_err)}"
    if down_err == errno_mod.EPERM:
        return (f"{what}. HCIDEVDOWN was denied (EPERM): this needs CAP_NET_ADMIN — run as "
                f"root, or the container with --privileged / --cap-add NET_ADMIN.")
    if down_err == errno_mod.ENODEV or bind_err == errno_mod.ENODEV:
        return (f"{what}. No hci{index}: check `ls /sys/class/bluetooth` on the host, and that "
                f"the container has --network host (raw HCI only works in the host netns).")
    if bind_err == errno_mod.EBUSY:
        return (f"{what} after {BIND_TIMEOUT:.0f}s of retries. Something keeps hci{index} up: "
                f"bluetoothd (systemctl mask --now bluetooth), or a leftover ble_flood.py from "
                f"a previous run holding the user channel (pkill -f ble_flood.py).")
    return what


def _try_bind(index):
    """One down + bind attempt. Returns the socket, or (down_errno, bind_errno) on failure."""
    down_err = None
    ctl = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW, BTPROTO_HCI)
    try:
        fcntl.ioctl(ctl.fileno(), HCIDEVDOWN, index)
    except OSError as exc:
        # Already down is the normal case (no bluetoothd) and reports as EALREADY/success.
        # Keep the errno regardless: EPERM and ENODEV are the ones worth naming later.
        down_err = exc.errno
    finally:
        ctl.close()

    sock = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW, BTPROTO_HCI)
    # CPython's bind() for BTPROTO_HCI cannot set hci_channel, so build sockaddr_hci
    # ourselves: { sa_family, hci_dev, hci_channel }, all u16.
    addr = struct.pack("<HHH", AF_BLUETOOTH, index, HCI_CHANNEL_USER)
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    if libc.bind(sock.fileno(), ctypes.c_char_p(addr), len(addr)) != 0:
        bind_err = ctypes.get_errno()
        sock.close()
        return down_err, bind_err
    sock.setblocking(False)
    return sock


def open_adapter(index):
    """Take exclusive raw control of hciN, waiting out a transient EBUSY.

    The kernel only allows HCI_CHANNEL_USER on a *down* adapter, which is also what
    guarantees exclusivity: if this succeeds, nothing else is driving the controller. It also
    means EBUSY is a race as often as a real conflict — bluetoothd re-powers the adapter in
    the gap between the ioctl and the bind — so busy is retried and everything else is not.
    """
    deadline = time.monotonic() + BIND_TIMEOUT
    announced = False
    while True:
        result = _try_bind(index)
        if not isinstance(result, tuple):
            return result
        down_err, bind_err = result

        retryable = (bind_err == errno_mod.EBUSY
                     and down_err not in (errno_mod.EPERM, errno_mod.ENODEV))
        if not retryable or time.monotonic() >= deadline:
            raise OSError(bind_err, _bind_hint(index, down_err, bind_err))
        if not announced:
            print(f"[flood] hci{index} busy, retrying for {BIND_TIMEOUT:.0f}s…", flush=True)
            announced = True
        time.sleep(0.5)


class HciError(Exception):
    """The controller rejected a command, or never answered one."""


def command_status(pkt, opcode):
    """Status byte from a Command Complete/Status event for opcode, else None."""
    if len(pkt) < 6 or pkt[0] != HCI_EVENT_PKT:
        return None
    if pkt[1] == EVT_CMD_COMPLETE:  # type, 0x0e, plen, ncmd, opcode(2), status
        if struct.unpack_from("<H", pkt, 4)[0] != opcode:
            return None
        return pkt[6] if len(pkt) > 6 else 0x00
    if pkt[1] == EVT_CMD_STATUS:    # type, 0x0f, plen, status, ncmd, opcode(2)
        if len(pkt) < 7 or struct.unpack_from("<H", pkt, 5)[0] != opcode:
            return None
        return pkt[3]
    return None


def send(sock, opcode, params=b"", timeout=2.0):
    """Send one command and confirm the controller accepted it.

    Waiting for the completion event is what makes a run mean something: without it a
    wedged or unplugged adapter silently swallows every command and the flood reports
    thousands of addresses while radiating nothing at all.
    """
    sock.sendall(hci_command(opcode, params))
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HciError(f"opcode 0x{opcode:04x}: no completion event within {timeout}s")
        if not select.select([sock], [], [], remaining)[0]:
            continue
        try:
            pkt = sock.recv(258)
        except BlockingIOError:
            continue
        status = command_status(pkt, opcode)
        if status is None:
            continue  # an unrelated event (advertising reports etc.) — keep draining
        if status != 0x00:
            raise HciError(f"opcode 0x{opcode:04x} rejected with status 0x{status:02x}")
        return


def flood(sock, rate, stop_after):
    """Rotate the advertised address forever (or until time runs out).

    Lifetime is the process's: the HIL pipeline runs this as a detached step, so the flood
    starts with the run and the runner kills it when the run ends. No external gating.

    Order matters: the controller rejects LE Set Random Address while advertising is
    enabled, so each rotation is disable -> re-address -> enable.
    """
    send(sock, OCF_RESET)
    time.sleep(0.1)
    send(sock, OCF_LE_SET_ADV_PARAMETERS, adv_parameters())

    interval = 1.0 / rate
    started = time.monotonic()
    rotations = 0
    reported = started

    while True:
        if stop_after and time.monotonic() - started >= stop_after:
            break

        cycle = time.monotonic()
        address = random_static_address()
        send(sock, OCF_LE_SET_ADV_ENABLE, b"\x00")
        send(sock, OCF_LE_SET_RANDOM_ADDRESS, address)
        send(sock, OCF_LE_SET_ADV_DATA, advertising_payload(address))
        send(sock, OCF_LE_SET_ADV_ENABLE, b"\x01")
        rotations += 1

        now = time.monotonic()
        if now - reported >= 30:
            print(f"[flood] {rotations} unique addresses in {now - started:.0f}s "
                  f"({rotations / (now - started):.1f}/s)", flush=True)
            reported = now
        time.sleep(max(0.0, interval - (now - cycle)))

    send(sock, OCF_LE_SET_ADV_ENABLE, b"\x00")
    print(f"[flood] stopped after {rotations} unique addresses", flush=True)


def selftest():
    """Check the packet framing and address rules without touching an adapter."""
    pkt = hci_command(OCF_LE_SET_ADV_ENABLE, b"\x01")
    assert pkt == b"\x01\x0a\x20\x01\x01", pkt.hex()

    assert hci_command(OCF_RESET) == b"\x01\x03\x0c\x00"

    for _ in range(2000):
        addr = random_static_address()
        assert len(addr) == 6
        # The MSB is the last byte on the wire; ESPresense keys ID_TYPE_RAND_STATIC_MAC
        # off exactly this test, so if it ever fails the flood stops being fingerprinted.
        assert addr[5] & 0xC0 == 0xC0, addr.hex()
        rest = int.from_bytes(addr, "little") & ((1 << 46) - 1)
        assert rest not in (0, (1 << 46) - 1)

    assert len({random_static_address() for _ in range(5000)}) == 5000, "addresses repeat"

    # Every advert must carry an identity unique to its address, or ESPresense merges them.
    addr_a, addr_b = random_static_address(), random_static_address()
    payload = advertising_payload(addr_a)
    assert len(payload) == 32, len(payload)
    assert payload[0] == 21 and payload[1:4] == b"\x02\x01\x06", payload.hex()
    assert payload[4:6] == b"\x11\x09", payload.hex()  # 16-byte name, "complete local name"
    assert payload[6:22] == b"HIL-" + addr_a[::-1].hex().encode(), payload.hex()
    assert advertising_payload(addr_b) != payload, "payload must vary with the address"
    assert len({advertising_payload(random_static_address()) for _ in range(2000)}) == 2000
    assert len(adv_parameters()) == 15, len(adv_parameters())

    try:
        hci_command(OCF_LE_SET_ADV_DATA, b"\x00" * 256)
        raise AssertionError("oversized parameters must be rejected")
    except ValueError:
        pass

    # Completion parsing decides whether a rejected command is noticed at all. If this
    # goes wrong the flood happily reports thousands of addresses while radiating none.
    op = OCF_LE_SET_ADV_ENABLE
    complete_ok = bytes([HCI_EVENT_PKT, EVT_CMD_COMPLETE, 4, 1]) + struct.pack("<H", op) + b"\x00"
    complete_bad = bytes([HCI_EVENT_PKT, EVT_CMD_COMPLETE, 4, 1]) + struct.pack("<H", op) + b"\x12"
    status_ok = bytes([HCI_EVENT_PKT, EVT_CMD_STATUS, 4, 0x00, 1]) + struct.pack("<H", op)
    status_bad = bytes([HCI_EVENT_PKT, EVT_CMD_STATUS, 4, 0x0C, 1]) + struct.pack("<H", op)
    other_op = bytes([HCI_EVENT_PKT, EVT_CMD_COMPLETE, 4, 1]) + struct.pack("<H", OCF_RESET) + b"\x00"
    adv_report = bytes([HCI_EVENT_PKT, 0x3E, 12]) + b"\x02" * 12

    assert command_status(complete_ok, op) == 0x00
    assert command_status(complete_bad, op) == 0x12
    assert command_status(status_ok, op) == 0x00
    assert command_status(status_bad, op) == 0x0C
    assert command_status(other_op, op) is None, "another command's reply must not be claimed"
    assert command_status(adv_report, op) is None, "an advertising report is not a completion"
    assert command_status(b"", op) is None and command_status(b"\x04\x0e", op) is None

    # The failure message is the whole diagnosis: EBUSY alone never says which cause it is.
    perm = _bind_hint(0, errno_mod.EPERM, errno_mod.EBUSY)
    assert "CAP_NET_ADMIN" in perm and "privileged" in perm, perm
    nodev = _bind_hint(0, errno_mod.ENODEV, errno_mod.EBUSY)
    assert "network host" in nodev and "sys/class/bluetooth" in nodev, nodev
    assert "network host" in _bind_hint(0, None, errno_mod.ENODEV)
    busy = _bind_hint(0, None, errno_mod.EBUSY)
    assert "bluetoothd" in busy and "ble_flood.py" in busy, busy
    assert "CAP_NET_ADMIN" not in busy, busy

    print("selftest OK")


def main():
    p = argparse.ArgumentParser(description="BLE advertisement flood with unique addresses")
    p.add_argument("--index", type=int, default=0, help="hciN adapter index")
    p.add_argument("--rate", type=float, default=40.0, help="address rotations per second")
    p.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = forever)")
    p.add_argument("--selftest", action="store_true", help="verify framing, no hardware")
    args = p.parse_args()

    if args.selftest:
        selftest()
        return
    if args.rate <= 0:
        p.error("--rate must be positive")

    sock = open_adapter(args.index)
    print(f"[flood] hci{args.index} claimed, rotating at {args.rate}/s", flush=True)
    try:
        flood(sock, args.rate, args.seconds)
    except KeyboardInterrupt:
        send(sock, OCF_LE_SET_ADV_ENABLE, b"\x00")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
