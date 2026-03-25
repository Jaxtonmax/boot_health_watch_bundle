#!/usr/bin/env python3
"""Guest boot health watcher based on ivc_demo receive output."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Dict, Optional, TextIO


def ts() -> str:
    t = time.time()
    sec = time.strftime("%F %T", time.localtime(t))
    ms = int((t % 1.0) * 1000)
    return f"{sec}.{ms:03d}"


def log(level: str, msg: str) -> None:
    print(f"{ts()} [root-guest-health] [{level}] {msg}", flush=True)


def parse_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw, 0)
    except ValueError as exc:
        raise ValueError(f"invalid {name}: {raw}") from exc
    if value < 0:
        raise ValueError(f"invalid {name}: {raw}")
    return value


@dataclass
class GuestState:
    name: str
    ivc_id: int
    device_path: str
    proc: Optional[subprocess.Popen[str]] = None
    stdout: Optional[TextIO] = None
    restart_at: float = 0.0
    last_seq: Optional[int] = None
    seq_updates: int = 0
    first_seen_mono: Optional[float] = None
    last_seen_mono: Optional[float] = None
    startup_ok: bool = False
    device_missing_logged: bool = False
    startup_timeout_logged: bool = False
    heartbeat_timeout_logged: bool = False


class GuestHealthWatch:
    def __init__(self) -> None:
        self.ivc_demo_bin = os.environ.get("IVC_DEMO_BIN", "/usr/bin/ivc_demo").strip() or "/usr/bin/ivc_demo"
        self.json_prefix = os.environ.get("IVC_JSON_PREFIX", "IVC_JSON:").strip() or "IVC_JSON:"
        self.startup_timeout_s = parse_positive_int("STARTUP_TIMEOUT_S", 30)
        self.heartbeat_timeout_s = parse_positive_int("HEARTBEAT_TIMEOUT_S", 15)
        self.min_seq_updates = parse_positive_int("MIN_SEQ_UPDATES", 2)
        self.restart_backoff_s = parse_positive_int("RESTART_BACKOFF_S", 1)
        self.device_retry_s = parse_positive_int("DEVICE_RETRY_S", 5)
        self.select_timeout_s = 0.5
        self.stop = False
        self.start_mono = time.monotonic()
        self.startup_confirmed_logged = False
        self.sel = selectors.DefaultSelector()
        self.fd_map: Dict[TextIO, GuestState] = {}

        linux_id = parse_positive_int("GUEST_LINUX_IVC_ID", 0)
        rtt_id = parse_positive_int("GUEST_RTTHREAD_IVC_ID", 2)
        linux_name = os.environ.get("GUEST_LINUX_NAME", "Guest Linux").strip() or "Guest Linux"
        rtt_name = os.environ.get("GUEST_RTTHREAD_NAME", "RT-Thread").strip() or "RT-Thread"

        self.guests = [
            GuestState(name=linux_name, ivc_id=linux_id, device_path=f"/dev/hivc{linux_id}"),
            GuestState(name=rtt_name, ivc_id=rtt_id, device_path=f"/dev/hivc{rtt_id}"),
        ]

        if self.startup_timeout_s == 0:
            raise ValueError("STARTUP_TIMEOUT_S must be > 0")
        if self.heartbeat_timeout_s == 0:
            raise ValueError("HEARTBEAT_TIMEOUT_S must be > 0")
        if self.min_seq_updates == 0:
            raise ValueError("MIN_SEQ_UPDATES must be > 0")
        if not os.path.exists(self.ivc_demo_bin):
            raise FileNotFoundError(f"ivc_demo not found: {self.ivc_demo_bin}")
        if not os.access(self.ivc_demo_bin, os.X_OK):
            raise PermissionError(f"ivc_demo not executable: {self.ivc_demo_bin}")

    def _start_guest_proc(self, guest: GuestState) -> None:
        cmd = [self.ivc_demo_bin, str(guest.ivc_id), "receive"]
        guest.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        if guest.proc.stdout is None:
            raise RuntimeError(f"failed to capture stdout for {guest.name}")
        guest.stdout = guest.proc.stdout
        self.sel.register(guest.stdout, selectors.EVENT_READ)
        self.fd_map[guest.stdout] = guest
        log("INFO", f"started ivc receiver for {guest.name}: {' '.join(cmd)}")

    def _stop_guest_proc(self, guest: GuestState) -> None:
        if guest.stdout is not None:
            try:
                self.sel.unregister(guest.stdout)
            except Exception:
                pass
            self.fd_map.pop(guest.stdout, None)
        if guest.proc is not None:
            try:
                guest.proc.terminate()
                guest.proc.wait(timeout=1.5)
            except Exception:
                try:
                    guest.proc.kill()
                except Exception:
                    pass
        guest.proc = None
        guest.stdout = None

    def _restart_if_needed(self, guest: GuestState, now_mono: float) -> None:
        if self.stop:
            return
        if guest.proc is None:
            if now_mono >= guest.restart_at:
                if not os.path.exists(guest.device_path):
                    if not guest.device_missing_logged:
                        log("WARN", f"{guest.name} device not ready: {guest.device_path}")
                        guest.device_missing_logged = True
                    guest.restart_at = now_mono + float(self.device_retry_s)
                    return
                if guest.device_missing_logged:
                    log("INFO", f"{guest.name} device ready: {guest.device_path}")
                    guest.device_missing_logged = False
                try:
                    self._start_guest_proc(guest)
                except Exception as exc:
                    log("WARN", f"failed to start receiver for {guest.name}: {exc}")
                    guest.restart_at = now_mono + float(self.restart_backoff_s)
            return
        rc = guest.proc.poll()
        if rc is None:
            return
        log("WARN", f"{guest.name} ivc receiver exited (rc={rc}), scheduling restart")
        self._stop_guest_proc(guest)
        guest.restart_at = now_mono + float(self.restart_backoff_s)

    def _handle_json(self, guest: GuestState, line: str, now_mono: float) -> None:
        if self.json_prefix not in line:
            return
        payload = line.split(self.json_prefix, 1)[1].strip()
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return

        seq_raw = data.get("seq")
        if seq_raw is None:
            return
        try:
            seq = int(seq_raw)
        except (TypeError, ValueError):
            return
        if seq < 0:
            return

        guest.last_seen_mono = now_mono
        if guest.first_seen_mono is None:
            guest.first_seen_mono = now_mono
            log("INFO", f"{guest.name} first frame received (seq={seq})")
            guest.startup_timeout_logged = False

        if guest.last_seq is None:
            guest.last_seq = seq
            return

        if seq > guest.last_seq:
            guest.seq_updates += 1
            guest.last_seq = seq
            guest.heartbeat_timeout_logged = False
            if not guest.startup_ok and guest.seq_updates >= self.min_seq_updates:
                guest.startup_ok = True
                guest.startup_timeout_logged = False
                log(
                    "INFO",
                    f"{guest.name} startup healthy: seq updated {guest.seq_updates} times (seq={seq})",
                )
            return

        if seq < guest.last_seq:
            log("WARN", f"{guest.name} seq regressed: {guest.last_seq} -> {seq}")
            guest.last_seq = seq

    def _check_timeouts(self, now_mono: float) -> None:
        elapsed = now_mono - self.start_mono

        for guest in self.guests:
            if not guest.startup_ok and elapsed >= float(self.startup_timeout_s) and not guest.startup_timeout_logged:
                if guest.first_seen_mono is None:
                    reason = f"no heartbeat yet on {guest.device_path}"
                else:
                    reason = f"need {self.min_seq_updates} seq updates in {self.startup_timeout_s}s"
                log(
                    "ERROR",
                    f"startup timeout for {guest.name}: {reason}",
                )
                guest.startup_timeout_logged = True

        if all(g.startup_ok for g in self.guests) and not self.startup_confirmed_logged:
            self.startup_confirmed_logged = True
            log("INFO", "healthy boot confirmed for all guests")

        for guest in self.guests:
            if not guest.startup_ok:
                continue
            if guest.last_seen_mono is None:
                continue
            silence = now_mono - guest.last_seen_mono
            if silence >= float(self.heartbeat_timeout_s) and not guest.heartbeat_timeout_logged:
                log(
                    "ERROR",
                    f"heartbeat timeout for {guest.name}: no frame for {silence:.1f}s",
                )
                guest.heartbeat_timeout_logged = True

    def cleanup(self) -> None:
        for guest in self.guests:
            self._stop_guest_proc(guest)
        self.sel.close()

    def run(self) -> int:
        log(
            "INFO",
            (
                f"watch config: startup_timeout={self.startup_timeout_s}s "
                f"heartbeat_timeout={self.heartbeat_timeout_s}s min_seq_updates={self.min_seq_updates}"
            ),
        )
        try:
            while not self.stop:
                now_mono = time.monotonic()
                for guest in self.guests:
                    self._restart_if_needed(guest, now_mono)

                events = self.sel.select(timeout=self.select_timeout_s)
                now_mono = time.monotonic()
                for key, _ in events:
                    fd = key.fileobj
                    guest = self.fd_map.get(fd)
                    if guest is None:
                        continue
                    assert guest.stdout is not None
                    line = guest.stdout.readline()
                    if not line:
                        continue
                    self._handle_json(guest, line, now_mono)

                self._check_timeouts(now_mono)
        finally:
            self.cleanup()
        return 0


def main() -> int:
    try:
        watcher = GuestHealthWatch()
    except Exception as exc:
        log("ERROR", str(exc))
        return 1

    def _on_signal(signum: int, _frame: object) -> None:
        watcher.stop = True
        log("INFO", f"received signal {signum}, stopping")

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    return watcher.run()


if __name__ == "__main__":
    sys.exit(main())
