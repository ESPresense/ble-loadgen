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
import glob
import os
import secrets
import select
import signal
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

# A constant rather than a CLI flag: a busy adapter is transient for one of two reasons
# (bluetoothd re-powering it, or the previous run's flood still exiting) and 30s covers both.
# Make it a flag if the bench ever shows a legitimate wait longer than this.
BIND_TIMEOUT = 30.0

# The first command after the bind is the fragile one, and the bench proved it: HCI_Reset timed
# out at 2s there on a controller that crow's never did. A bind hands us a controller the kernel
# has just opened, so a command sent into that window can be lost, or answered only once a USB
# part has finished its firmware setup. Idempotent and cheap to repeat, so it gets a longer wait
# and retries; the per-rotation commands stay strict, because a wedged controller must still be
# caught there rather than quietly radiating nothing.
RESET_TIMEOUT = 15.0
RESET_ATTEMPTS = 3
RESET_RETRY_DELAY = 1.0

# The HIL runs the flood as a *detached* step, and a detached step cannot fail the pipeline: it
# can die in its first second and the soak still reports green, having loaded the node with
# nothing. So the flood publishes liveness to a file that a non-detached guard step reads, and the
# soak asserts it was still fresh when the run ended. PID 1 in a container, so a file is the only
# channel that crosses the step boundary.
HEARTBEAT_FILENAME = "ble-flood.alive"
HEARTBEAT_INTERVAL = 30.0


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
    # Both HCIDEVDOWN and a HCI_CHANNEL_USER bind gate on CAP_NET_ADMIN in the kernel, so
    # EPERM from either is the same missing capability.
    if errno_mod.EPERM in (down_err, bind_err):
        return (f"{what}. Denied (EPERM): this needs CAP_NET_ADMIN — run as root, or the "
                f"container with --privileged / --cap-add NET_ADMIN.")
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
            # Carry the errno that explains the failure, not the one that merely reports it:
            # anything keying off exc.errno should see EPERM, not the EBUSY it caused.
            cause = down_err if down_err in (errno_mod.EPERM, errno_mod.ENODEV) else bind_err
            raise OSError(cause, _bind_hint(index, down_err, bind_err))
        if not announced:
            print(f"[flood] hci{index} busy, retrying for {BIND_TIMEOUT:.0f}s...", flush=True)
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
    drained = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # "Nothing arrived" and "events arrived, just not this reply" are different faults,
            # and the count is the only thing in a CI log that separates them.
            saw = f"drained {drained} unrelated events" if drained else "nothing arrived at all"
            raise HciError(f"opcode 0x{opcode:04x}: no completion event within {timeout}s ({saw})")
        if not select.select([sock], [], [], remaining)[0]:
            continue
        try:
            pkt = sock.recv(258)
        except BlockingIOError:
            continue
        status = command_status(pkt, opcode)
        if status is None:
            drained += 1
            continue  # an unrelated event (advertising reports etc.) — keep draining
        if status != 0x00:
            raise HciError(f"opcode 0x{opcode:04x} rejected with status 0x{status:02x}")
        return


def _silent_hint():
    """What to check when a claimed controller stops answering — the cases a bind cannot explain.

    A successful bind already rules out the usual causes (adapter busy, no CAP_NET_ADMIN, wrong
    netns), so what is left is controller-level: an rfkill block, an autosuspended USB port, or a
    dongle left wedged by a flood leaked from a previous run. The rfkill state is read live
    because it is the one of those that is visible from inside the container.
    """
    states = []
    for path in sorted(glob.glob("/sys/class/bluetooth/hci*/rfkill*/state")):
        try:
            with open(path) as fh:
                states.append(f"{path}={'blocked' if fh.read().strip() == '1' else 'unblocked'}")
        except OSError:
            pass
    return ("The bind succeeded, so hciN is ours and nothing else can be driving it — the "
            "controller is simply silent. Check `rfkill list` (a soft block leaves it silent), "
            "that the USB port is not autosuspended, and whether the dongle needs a re-plug; a "
            "flood leaked by an earlier run wedges it the same way (`pkill -f ble_flood.py`). "
            f"rfkill: {', '.join(states) if states else 'unreadable'}")


def reset_controller(sock, timeout=RESET_TIMEOUT, attempts=RESET_ATTEMPTS, delay=RESET_RETRY_DELAY):
    """HCI_Reset until the controller answers it.

    Safe to repeat: reset drops whatever state the controller was left in, which is why it is the
    first command anyway. Retrying is what turns a controller that is still coming up into a
    working flood instead of a failed HIL step.
    """
    last = None
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            send(sock, OCF_RESET, timeout=timeout)
        except HciError as exc:
            last = exc
            print(f"[flood] HCI_Reset unanswered after {timeout:g}s "
                  f"(attempt {attempt}/{attempts})", flush=True)
            if attempt < attempts:
                time.sleep(delay)
            continue
        suffix = "" if attempt == 1 else f" (attempt {attempt})"
        print(f"[flood] controller answered HCI_Reset in {time.monotonic() - started:.2f}s{suffix}",
              flush=True)
        return
    raise HciError(f"HCI_Reset never completed in {attempts} attempts: {last}. {_silent_hint()}")


def write_heartbeat(path, rotations, rate, address):
    """Publish flood liveness atomically, so a reader never catches a half-written file.

    Failing to write is fatal on purpose: a heartbeat the pipeline cannot read is worse than no
    heartbeat, because the guard would then fail every run and a mount problem would look like a
    flood problem.
    """
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w") as fh:
            fh.write(f"pid={os.getpid()}\nrotations={rotations}\nrate={rate}\n"
                     f"last_address={address.hex() if address else ''}\n"
                     f"updated={time.time():.3f}\n")
        os.replace(tmp, path)
    except OSError as exc:
        raise RuntimeError(f"cannot write flood heartbeat {path}: {exc}") from exc


def _read_heartbeat(path):
    """The parsed heartbeat, or None while it is not there yet."""
    try:
        with open(path) as fh:
            text = fh.read()
    except OSError:
        return None
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def check_heartbeat(path, wait, stale, watch=0.0, poll=1.0):
    """Fail unless a live flood is refreshing ``path`` — the pipeline's non-detached guard.

    The flood itself cannot fail the build (detached), so the check has to live in a step that
    can. Waiting for the file catches a flood that never advertised at all; the staleness limit
    catches one that started and then wedged. Either way the soak has no BLE load, which is worse
    than a red build: it looks like evidence.
    """
    deadline = time.monotonic() + wait
    while _read_heartbeat(path) is None:
        if time.monotonic() >= deadline:
            raise HciError(f"no flood heartbeat at {path} within {wait:g}s: the detached flood "
                           f"never advertised anything, so this run has no BLE load")
        time.sleep(poll)

    stop = time.monotonic() + watch
    while True:
        fields = _read_heartbeat(path) or {}
        try:
            age = time.time() - float(fields["updated"])
        except (KeyError, ValueError):
            raise HciError(f"unreadable flood heartbeat at {path}: {fields or 'empty'}")
        if age > stale:
            raise HciError(f"flood heartbeat {path} is {age:.0f}s old (limit {stale:g}s): the "
                           f"flood stopped after {fields.get('rotations')} rotations")
        if time.monotonic() >= stop:
            print(f"[flood] heartbeat OK: {age:.1f}s old, {fields.get('rotations')} rotations",
                  flush=True)
            return
        time.sleep(poll)


def flood(sock, rate, stop_after, heartbeat=None):
    """Rotate the advertised address forever (or until time runs out).

    Lifetime is the process's: the HIL pipeline runs this as a detached step, so the flood
    starts with the run and the runner kills it when the run ends. No external gating.

    Order matters: the controller rejects LE Set Random Address while advertising is
    enabled, so each rotation is disable -> re-address -> enable.
    """
    reset_controller(sock)
    time.sleep(0.1)
    send(sock, OCF_LE_SET_ADV_PARAMETERS, adv_parameters())
    # Advertise-ready is the moment the guard cares about: from here the node is being loaded.
    # Every later refresh only says the flood is still going.
    if heartbeat:
        write_heartbeat(heartbeat, 0, rate, b"")

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
        if now - reported >= HEARTBEAT_INTERVAL:
            print(f"[flood] {rotations} unique addresses in {now - started:.0f}s "
                  f"({rotations / (now - started):.1f}/s)", flush=True)
            if heartbeat:
                write_heartbeat(heartbeat, rotations, rate, address)
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
    # bind() gates on CAP_NET_ADMIN too, so EPERM from it alone must give the same advice.
    assert "CAP_NET_ADMIN" in _bind_hint(0, None, errno_mod.EPERM)
    nodev = _bind_hint(0, errno_mod.ENODEV, errno_mod.EBUSY)
    assert "network host" in nodev and "sys/class/bluetooth" in nodev, nodev
    assert "network host" in _bind_hint(0, None, errno_mod.ENODEV)
    busy = _bind_hint(0, None, errno_mod.EBUSY)
    assert "bluetoothd" in busy and "ble_flood.py" in busy, busy
    assert "CAP_NET_ADMIN" not in busy, busy

    # The reset is the one command that must not give up early — the bench timed out on exactly
    # it while crow never did. Both paths run against a socket-shaped fake, so CI fails if the
    # retry is ever dropped: retry-then-succeed, and give-up-with-a-reason.
    import threading

    def fake_socket():
        """A socket-shaped stand-in. select() needs a real fd, so wrap a socketpair."""

        rd, wr = socket.socketpair()

        class Fake:
            def __init__(self):
                self.sent = []

            def fileno(self):
                return rd.fileno()

            def sendall(self, data):
                self.sent.append(data)

            def recv(self, n):
                return rd.recv(n)

            def answer(self, opcode):
                wr.send(bytes([HCI_EVENT_PKT, EVT_CMD_COMPLETE, 4, 1])
                        + struct.pack("<H", opcode) + b"\x00")

            def close(self):
                rd.close()
                wr.close()

        return Fake()

    # Answer only the second attempt: the first must time out and be retried, not fatal.
    retrying = fake_socket()

    def answer_on_second():
        while len(retrying.sent) < 2:
            time.sleep(0.005)
        retrying.answer(OCF_RESET)

    threading.Thread(target=answer_on_second, daemon=True).start()
    reset_controller(retrying, timeout=0.2, attempts=3, delay=0.0)
    assert len(retrying.sent) == 2, retrying.sent
    assert all(pkt == hci_command(OCF_RESET) for pkt in retrying.sent), retrying.sent
    retrying.close()

    # Silence every attempt: it must fail with the attempt count and the cause, not hang.
    dead = fake_socket()
    try:
        reset_controller(dead, timeout=0.05, attempts=3, delay=0.0)
        raise AssertionError("a silent controller must not look like success")
    except HciError as exc:
        assert "3 attempts" in str(exc) and "rfkill" in str(exc), exc
    assert len(dead.sent) == 3, dead.sent
    dead.close()

    # A timeout must say what it saw: silence and "events I could not use" differ.
    quiet = fake_socket()
    try:
        send(quiet, OCF_LE_SET_ADV_ENABLE, b"\x01", timeout=0.05)
        raise AssertionError("an unanswered command must raise")
    except HciError as exc:
        assert "nothing arrived at all" in str(exc), exc
    quiet.close()

    # The guard decides whether a dead flood is allowed to look like a passing run, so all three
    # outcomes are pinned here: fresh passes, stale fails, absent fails. No hardware involved.
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        hb = os.path.join(tmpdir, HEARTBEAT_FILENAME)
        write_heartbeat(hb, 1234, 40.0, bytes.fromhex("aabbccddeeff"))
        fields = _read_heartbeat(hb)
        assert fields["rotations"] == "1234" and fields["rate"] == "40.0", fields
        assert fields["last_address"] == "aabbccddeeff", fields
        assert abs(time.time() - float(fields["updated"])) < 5, fields
        # The writer replaces atomically, so a reader must never see the temp file left behind.
        assert not os.path.exists(f"{hb}.tmp"), "temp file left behind"
        check_heartbeat(hb, wait=0.05, stale=60, watch=0, poll=0.02)

        # Stale: the flood started and then stopped refreshing — the soak has no load.
        with open(hb, "w") as fh:
            fh.write(f"rotations=99\nupdated={time.time() - 600:.3f}\n")
        try:
            check_heartbeat(hb, wait=0.05, stale=90, watch=0, poll=0.02)
            raise AssertionError("a stale heartbeat must fail the guard")
        except HciError as exc:
            assert "600s old" in str(exc) and "99 rotations" in str(exc), exc

        # Absent: the flood never advertised at all — the bench failure that went unnoticed.
        try:
            check_heartbeat(os.path.join(tmpdir, "never-created"), wait=0.05, stale=90,
                            watch=0, poll=0.02)
            raise AssertionError("a missing heartbeat must fail the guard")
        except HciError as exc:
            assert "no flood heartbeat" in str(exc), exc

        # An unwritable heartbeat path has to be fatal at startup, not a silent no-op.
        try:
            write_heartbeat(os.path.join(tmpdir, "missing-dir", HEARTBEAT_FILENAME), 0, 40.0, b"")
            raise AssertionError("an unwritable heartbeat path must fail")
        except RuntimeError as exc:
            assert "cannot write" in str(exc), exc

    print("selftest OK")


def main():
    p = argparse.ArgumentParser(description="BLE advertisement flood with unique addresses")
    p.add_argument("--index", type=int, default=0, help="hciN adapter index")
    p.add_argument("--rate", type=float, default=40.0, help="address rotations per second")
    p.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = forever)")
    p.add_argument("--selftest", action="store_true", help="verify framing, no hardware")
    p.add_argument("--heartbeat", metavar="PATH",
                   help="publish liveness here (default: $BLE_FLOOD_HEARTBEAT), so a guard step "
                        "can see a detached flood that never started")
    p.add_argument("--check-heartbeat", metavar="PATH",
                   help="guard mode: fail unless a live flood is refreshing PATH")
    p.add_argument("--wait", type=float, default=120.0,
                   help="guard mode: seconds to wait for the heartbeat to appear")
    p.add_argument("--stale", type=float, default=90.0,
                   help="guard mode: fail once the heartbeat is older than this")
    p.add_argument("--watch", type=float, default=0.0,
                   help="guard mode: keep checking for this long before succeeding")
    args = p.parse_args()

    if args.selftest:
        selftest()
        return
    if args.check_heartbeat:
        check_heartbeat(args.check_heartbeat, args.wait, args.stale, args.watch)
        return
    if args.rate <= 0:
        p.error("--rate must be positive")

    heartbeat = args.heartbeat or os.environ.get("BLE_FLOOD_HEARTBEAT", "")
    if heartbeat:
        print(f"[flood] heartbeat -> {heartbeat}", flush=True)

    # The HIL runs this as a detached step, so it's PID 1 in its container and the runner stops
    # it with SIGTERM (docker stop). The kernel ignores un-handled signals for PID 1, so without
    # this the flood outlives the run, keeps hci0, and the next run's bind() fails EBUSY (and its
    # node sees a flood it never asked for). Route SIGTERM through the same KeyboardInterrupt path
    # that disables advertising and releases the adapter cleanly.
    signal.signal(signal.SIGTERM, signal.default_int_handler)

    sock = open_adapter(args.index)
    print(f"[flood] hci{args.index} claimed, rotating at {args.rate}/s", flush=True)
    try:
        flood(sock, args.rate, args.seconds, heartbeat or None)
    except KeyboardInterrupt:
        send(sock, OCF_LE_SET_ADV_ENABLE, b"\x00")
        print("[flood] stopped on signal", flush=True)
    finally:
        sock.close()


if __name__ == "__main__":
    main()
