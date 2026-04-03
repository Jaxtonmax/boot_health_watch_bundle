#!/usr/bin/env python3
"""Guest boot health watcher based on ivc_demo receive output."""

from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, TextIO


DEFAULT_LINUX_ZONE_CFG = "/root/SeawayHyper/zone1/zone1-linux.json"
DEFAULT_RTTHREAD_ZONE_CFG = "/root/SeawayHyper/zone2/zone2-rtt.json"


def ts() -> str:
    t = time.time()
    sec = time.strftime("%F %T", time.localtime(t))
    ms = int((t % 1.0) * 1000)
    return f"{sec}.{ms:03d}"


def log(level: str, msg: str) -> None:
    print(f"{ts()} [root-guest-health] [{level}] {msg}", flush=True)


def console_log(msg: str) -> None:
    try:
        with open("/dev/console", "w", encoding="utf-8", buffering=1) as fp:
            fp.write(f"{ts()} [root-guest-health] [INFO] {msg}\n")
    except Exception:
        pass


def debug_enabled() -> bool:
    raw = os.environ.get("GUEST_HEALTH_DEBUG", "").strip().lower()
    return raw in {"1", "true", "yes", "on", "debug"}


def debug(msg: str) -> None:
    if debug_enabled():
        log("DEBUG", msg)


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


def parse_optional_non_negative_int(name: str) -> Optional[int]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw, 0)
    except ValueError as exc:
        raise ValueError(f"invalid {name}: {raw}") from exc
    if value < 0:
        raise ValueError(f"invalid {name}: {raw}")
    return value


def parse_bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid {name}: {raw}")


def parse_optional_path(name: str, default: str = "") -> str:
    raw = os.environ.get(name, "").strip()
    if raw:
        return raw
    return default.strip()


def parse_device_map(raw: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for item in raw.split(","):
        entry = item.strip()
        if not entry or ":" not in entry:
            continue
        key, value = entry.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            result[key] = value
    return result


def parse_device_map_by_id(raw: str) -> Dict[int, str]:
    result: Dict[int, str] = {}
    for item in raw.split(","):
        entry = item.strip()
        if not entry or ":" not in entry:
            continue
        raw_id, value = entry.split(":", 1)
        raw_id = raw_id.strip()
        value = value.strip()
        if not raw_id or not value:
            continue
        try:
            zone_id = int(raw_id, 0)
        except ValueError:
            continue
        result[zone_id] = value
    return result


def device_path_to_ivc_id(device_path: str) -> Optional[int]:
    match = re.match(r"^/dev/hivc(\d+)$", (device_path or "").strip())
    if not match:
        return None
    try:
        return int(match.group(1), 10)
    except ValueError:
        return None


def infer_guest_os(*values: str) -> str:
    for value in values:
        lower = (value or "").strip().lower()
        if not lower:
            continue
        if "rtthread" in lower or re.search(r"(^|[^a-z])rtt($|[^a-z])", lower):
            return "rtt"
        if "linux" in lower:
            return "linux"
    return ""


def load_zone_cfg_meta(path: str) -> Dict[str, object]:
    meta: Dict[str, object] = {}
    if not path or not os.path.isfile(path):
        return meta
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        debug(f"failed to load zone cfg {path}: {exc}")
        return meta

    if not isinstance(data, dict):
        return meta

    zone_id = data.get("zone_id")
    if isinstance(zone_id, int):
        meta["zone_id"] = zone_id
    elif isinstance(zone_id, str):
        try:
            meta["zone_id"] = int(zone_id, 0)
        except ValueError:
            pass

    zone_name = data.get("name")
    if isinstance(zone_name, str) and zone_name.strip():
        meta["zone_name"] = zone_name.strip()

    guest_os = data.get("guest_os")
    if isinstance(guest_os, str) and guest_os.strip():
        meta["guest_os"] = guest_os.strip().lower()

    if "guest_os" not in meta:
        cfg_base = os.path.basename(path)
        parent = os.path.basename(os.path.dirname(path))
        inferred = infer_guest_os(str(meta.get("zone_name", "")), cfg_base, parent)
        if inferred:
            meta["guest_os"] = inferred

    if "zone_aliases" not in meta:
        aliases = []
        for value in (meta.get("zone_name"), os.path.basename(os.path.dirname(path)), os.path.splitext(os.path.basename(path))[0]):
            if not isinstance(value, str):
                continue
            cleaned = value.strip()
            if cleaned and cleaned not in aliases:
                aliases.append(cleaned)
        meta["zone_aliases"] = aliases

    return meta


@dataclass
class GuestState:
    name: str
    short_name: str
    ivc_id: int
    device_path: str
    device_source: str
    zone_id: Optional[int] = None
    zone_cfg: str = ""
    zone_name: str = ""
    guest_os: str = ""
    zone_aliases: list[str] = field(default_factory=list)
    monitor_active: bool = False
    progress_armed: bool = False
    zone_was_running: bool = False
    startup_suppressed: bool = False
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
    cycle_start_mono: float = 0.0
    last_uptime: Optional[int] = None
    announce_armed: bool = True
    stale_frame_logged: bool = False
    zone_not_running_logged: bool = False
    receiver_started_mono: Optional[float] = None
    current_stage: str = ""
    current_stage_detail: str = ""
    current_stage_progress: int = 0
    current_stage_hook_detail: str = ""
    stage_enter_mono: float = 0.0
    last_zone_check_mono: float = 0.0
    cached_zone_running: Optional[bool] = None
    last_logged_stage: str = ""
    last_logged_progress: int = -1
    last_progress_emit_mono: float = 0.0


class GuestHealthWatch:
    def __init__(self) -> None:
        self.ivc_demo_bin = os.environ.get("IVC_DEMO_BIN", "/usr/bin/ivc_demo").strip() or "/usr/bin/ivc_demo"
        self.hvisor_bin = os.environ.get("HVISOR_BIN", "/root/SeawayHyper/bin/hvisor").strip() or "/root/SeawayHyper/bin/hvisor"
        self.json_prefix = os.environ.get("IVC_JSON_PREFIX", "IVC_JSON:").strip() or "IVC_JSON:"
        self.startup_timeout_s = parse_positive_int("STARTUP_TIMEOUT_S", 30)
        self.heartbeat_timeout_s = parse_positive_int("HEARTBEAT_TIMEOUT_S", 15)
        self.min_seq_updates = parse_positive_int("MIN_SEQ_UPDATES", 2)
        self.restart_backoff_s = parse_positive_int("RESTART_BACKOFF_S", 1)
        self.device_retry_s = parse_positive_int("DEVICE_RETRY_S", 5)
        self.fresh_uptime_slack_s = parse_positive_int("FRESH_UPTIME_SLACK_S", 20)
        self.reboot_uptime_rollback_s = parse_positive_int("REBOOT_UPTIME_ROLLBACK_S", 60)
        self.progress_enabled = parse_bool_env("GUEST_PROGRESS_ENABLE", True)
        self.progress_interval_s = parse_positive_int("GUEST_PROGRESS_INTERVAL_S", 2)
        self.progress_to_console = parse_bool_env("GUEST_PROGRESS_TO_CONSOLE", True)
        self.progress_to_journal = parse_bool_env("GUEST_PROGRESS_TO_JOURNAL", True)
        self.stage_hook = os.environ.get("GUEST_STAGE_HOOK", "/usr/local/sbin/root_guest_stage_hook.sh").strip()
        self.stage_hook_timeout_s = parse_positive_int("GUEST_STAGE_HOOK_TIMEOUT_S", 2)
        self.select_timeout_s = 0.5
        self.stop = False
        self.start_mono = time.monotonic()
        self.startup_confirmed_logged = False
        self.sel = selectors.DefaultSelector()
        self.fd_map: Dict[TextIO, GuestState] = {}
        self.stage_hook_missing_logged = False
        self.stage_hook_perm_logged = False
        self.device_map = parse_device_map(os.environ.get("WEB_HVISOR_IVC_DEVICE_MAP", ""))
        self.device_map_by_id = parse_device_map_by_id(os.environ.get("WEB_HVISOR_IVC_DEVICE_MAP_BY_ID", ""))
        self.device_map_by_os = parse_device_map(os.environ.get("WEB_HVISOR_IVC_DEVICE_MAP_BY_OS", ""))

        self.guests = [
            self._build_guest(
                env_prefix="GUEST_LINUX",
                default_name="Guest Linux",
                default_zone_cfg=DEFAULT_LINUX_ZONE_CFG,
                default_ivc_id=0,
            ),
            self._build_guest(
                env_prefix="GUEST_RTTHREAD",
                default_name="RT-Thread",
                default_zone_cfg=DEFAULT_RTTHREAD_ZONE_CFG,
                default_ivc_id=2,
            ),
        ]
        for guest in self.guests:
            guest.cycle_start_mono = self.start_mono
            guest.stage_enter_mono = self.start_mono

        if self.startup_timeout_s == 0:
            raise ValueError("STARTUP_TIMEOUT_S must be > 0")
        if self.heartbeat_timeout_s == 0:
            raise ValueError("HEARTBEAT_TIMEOUT_S must be > 0")
        if self.min_seq_updates == 0:
            raise ValueError("MIN_SEQ_UPDATES must be > 0")
        if self.fresh_uptime_slack_s == 0:
            raise ValueError("FRESH_UPTIME_SLACK_S must be > 0")
        if self.reboot_uptime_rollback_s == 0:
            raise ValueError("REBOOT_UPTIME_ROLLBACK_S must be > 0")
        if self.progress_enabled and self.progress_interval_s == 0:
            raise ValueError("GUEST_PROGRESS_INTERVAL_S must be > 0 when progress is enabled")
        if self.stage_hook and self.stage_hook_timeout_s == 0:
            raise ValueError("GUEST_STAGE_HOOK_TIMEOUT_S must be > 0")
        if not os.path.exists(self.ivc_demo_bin):
            raise FileNotFoundError(f"ivc_demo not found: {self.ivc_demo_bin}")
        if not os.access(self.ivc_demo_bin, os.X_OK):
            raise PermissionError(f"ivc_demo not executable: {self.ivc_demo_bin}")
        if not os.path.exists(self.hvisor_bin):
            raise FileNotFoundError(f"hvisor not found: {self.hvisor_bin}")
        if not os.access(self.hvisor_bin, os.X_OK):
            raise PermissionError(f"hvisor not executable: {self.hvisor_bin}")

    def _guest_short_name(self, name: str, fallback_ivc_id: int) -> str:
        lower = name.lower()
        if "linux" in lower:
            return "linux"
        if "rt" in lower:
            return "rtt"
        return f"ivc{fallback_ivc_id}"

    def _resolve_device_path(
        self,
        explicit_path: str,
        zone_aliases: list[str],
        zone_id: Optional[int],
        guest_os: str,
        fallback_ivc_id: int,
    ) -> tuple[str, str]:
        if explicit_path:
            return explicit_path, "explicit_device_path"

        for alias in zone_aliases:
            path = self.device_map.get(alias)
            if path:
                return path, f"zone_alias:{alias}"

        if zone_id is not None:
            path = self.device_map_by_id.get(zone_id)
            if path:
                return path, f"zone_id:{zone_id}"

        if guest_os:
            path = self.device_map_by_os.get(guest_os.lower())
            if path:
                return path, f"guest_os:{guest_os.lower()}"

        return f"/dev/hivc{fallback_ivc_id}", f"legacy_ivc_id:{fallback_ivc_id}"

    def _build_guest(self, env_prefix: str, default_name: str, default_zone_cfg: str, default_ivc_id: int) -> GuestState:
        name = os.environ.get(f"{env_prefix}_NAME", default_name).strip() or default_name
        legacy_ivc_id = parse_positive_int(f"{env_prefix}_IVC_ID", default_ivc_id)
        explicit_zone_id = parse_optional_non_negative_int(f"{env_prefix}_ZONE_ID")
        zone_cfg = parse_optional_path(f"{env_prefix}_ZONE_CFG", default_zone_cfg)
        explicit_device_path = parse_optional_path(f"{env_prefix}_DEVICE_PATH")
        meta = load_zone_cfg_meta(zone_cfg)
        zone_id = explicit_zone_id if explicit_zone_id is not None else meta.get("zone_id")
        if not isinstance(zone_id, int):
            zone_id = None
        zone_name = str(meta.get("zone_name", "")).strip()
        guest_os = str(meta.get("guest_os", "")).strip().lower()
        zone_aliases = [alias for alias in meta.get("zone_aliases", []) if isinstance(alias, str)]
        if not zone_aliases:
            cfg_base = os.path.splitext(os.path.basename(zone_cfg))[0]
            zone_dir = os.path.basename(os.path.dirname(zone_cfg))
            zone_aliases = [item for item in (zone_name, zone_dir, cfg_base) if item]
        device_path, device_source = self._resolve_device_path(
            explicit_path=explicit_device_path,
            zone_aliases=zone_aliases,
            zone_id=zone_id,
            guest_os=guest_os,
            fallback_ivc_id=legacy_ivc_id,
        )
        ivc_id = device_path_to_ivc_id(device_path)
        if ivc_id is None:
            ivc_id = legacy_ivc_id
        short_name = self._guest_short_name(name, ivc_id)
        return GuestState(
            name=name,
            short_name=short_name,
            ivc_id=ivc_id,
            device_path=device_path,
            device_source=device_source,
            zone_id=zone_id,
            zone_cfg=zone_cfg,
            zone_name=zone_name,
            guest_os=guest_os,
            zone_aliases=zone_aliases,
        )

    def _is_fresh_uptime(self, guest: GuestState, uptime: Optional[int], now_mono: float) -> bool:
        if uptime is None:
            return True
        cycle_age = max(0.0, now_mono - guest.cycle_start_mono)
        return uptime <= int(cycle_age + float(self.fresh_uptime_slack_s))

    def _zone_running(self, guest: GuestState) -> bool:
        if guest.zone_id is None:
            return True
        try:
            cp = subprocess.run(
                [self.hvisor_bin, "zone", "list"],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except Exception:
            return False

        if cp.returncode not in (0, 1):
            return False

        for line in cp.stdout.splitlines():
            if "|" not in line:
                continue
            parts = [part.strip() for part in line.split("|")]
            if len(parts) < 5:
                continue
            try:
                zone_id = int(parts[1], 0)
            except ValueError:
                continue
            status = parts[4].lower()
            if zone_id == guest.zone_id:
                return "running" in status
        return False

    def _zone_running_cached(self, guest: GuestState, now_mono: float) -> bool:
        if guest.zone_id is None:
            guest.cached_zone_running = True
            guest.last_zone_check_mono = now_mono
            return True
        if guest.cached_zone_running is not None and (now_mono - guest.last_zone_check_mono) < 1.0:
            return guest.cached_zone_running
        guest.cached_zone_running = self._zone_running(guest)
        guest.last_zone_check_mono = now_mono
        return guest.cached_zone_running

    def _progress_log(self, msg: str) -> None:
        if self.progress_to_journal:
            log("INFO", msg)
        if self.progress_to_console:
            console_log(msg)

    def _progress_interp(self, start: int, end: int, elapsed: float, window: float) -> int:
        if end <= start:
            return start
        if window <= 0.0:
            return end
        ratio = max(0.0, min(1.0, elapsed / window))
        return start + int((end - start) * ratio)

    def _stage_elapsed(self, guest: GuestState, now_mono: float, target_stage: str) -> float:
        if guest.current_stage != target_stage or guest.stage_enter_mono <= 0.0:
            return 0.0
        return max(0.0, now_mono - guest.stage_enter_mono)

    def _reset_progress_log_state(self, guest: GuestState) -> None:
        guest.current_stage = ""
        guest.current_stage_detail = ""
        guest.current_stage_progress = 0
        guest.current_stage_hook_detail = ""
        guest.stage_enter_mono = 0.0
        guest.last_logged_stage = ""
        guest.last_logged_progress = -1
        guest.last_progress_emit_mono = 0.0

    def _reset_guest_startup_state(
        self,
        guest: GuestState,
        now_mono: float,
        *,
        announce_armed: bool,
        startup_suppressed: bool,
    ) -> None:
        guest.restart_at = 0.0
        guest.last_seq = None
        guest.seq_updates = 0
        guest.first_seen_mono = None
        guest.last_seen_mono = None
        guest.startup_ok = False
        guest.device_missing_logged = False
        guest.startup_timeout_logged = False
        guest.heartbeat_timeout_logged = False
        guest.cycle_start_mono = now_mono
        guest.last_uptime = None
        guest.announce_armed = announce_armed
        guest.stale_frame_logged = False
        guest.zone_not_running_logged = False
        guest.receiver_started_mono = None
        guest.startup_suppressed = startup_suppressed
        self._reset_progress_log_state(guest)

    def _arm_startup_progress(self, guest: GuestState, now_mono: float) -> None:
        guest.monitor_active = True
        guest.progress_armed = True
        self._reset_guest_startup_state(
            guest,
            now_mono,
            announce_armed=True,
            startup_suppressed=False,
        )
        log("INFO", f"{guest.name} startup observation armed by zone start")

    def _initialize_guest_monitor_mode(self, guest: GuestState, now_mono: float) -> None:
        zone_running = self._zone_running_cached(guest, now_mono)
        if guest.zone_id is None:
            guest.monitor_active = True
            guest.progress_armed = False
            guest.zone_was_running = True
            self._reset_guest_startup_state(
                guest,
                now_mono,
                announce_armed=True,
                startup_suppressed=False,
            )
            return

        guest.zone_was_running = zone_running
        guest.monitor_active = zone_running
        guest.progress_armed = False
        self._reset_guest_startup_state(
            guest,
            now_mono,
            announce_armed=not zone_running,
            startup_suppressed=zone_running,
        )
        if zone_running:
            debug(f"{guest.name} already running when watcher started, entering silent monitor mode")

    def _refresh_guest_activation(self, guest: GuestState, now_mono: float) -> None:
        if guest.zone_id is None:
            guest.monitor_active = True
            return

        zone_running = self._zone_running_cached(guest, now_mono)
        if zone_running and not guest.zone_was_running:
            guest.zone_was_running = True
            self._arm_startup_progress(guest, now_mono)
            return

        if not zone_running and guest.zone_was_running:
            guest.zone_was_running = False
            guest.monitor_active = False
            guest.progress_armed = False
            self.startup_confirmed_logged = False
            if guest.proc is not None:
                self._stop_guest_proc(guest)
            self._reset_guest_startup_state(
                guest,
                now_mono,
                announce_armed=True,
                startup_suppressed=False,
            )
            log("INFO", f"{guest.name} zone stopped, watcher returned to idle")

    def _stage_snapshot(self, guest: GuestState, now_mono: float) -> tuple[str, int, str, str]:
        startup_wait = int(max(0.0, now_mono - guest.cycle_start_mono))
        startup_budget = max(1, self.startup_timeout_s)
        zone_running = self._zone_running_cached(guest, now_mono)
        device_ready = os.path.exists(guest.device_path)
        receiver_alive = guest.proc is not None and guest.proc.poll() is None
        receiver_detail = "receiver_ready"

        if guest.startup_ok:
            return ("startup_ok", 100, "ok", "startup_ok")

        if guest.zone_id is not None and not zone_running:
            elapsed = self._stage_elapsed(guest, now_mono, "waiting_zone")
            progress = self._progress_interp(0, 15, elapsed, max(5.0, startup_budget * 0.25))
            return ("waiting_zone", progress, f"{startup_wait}s/{startup_budget}s", "zone_wait")

        if not device_ready:
            elapsed = self._stage_elapsed(guest, now_mono, "waiting_device")
            progress = self._progress_interp(15, 30, elapsed, max(5.0, startup_budget * 0.25))
            return ("waiting_device", progress, f"{startup_wait}s/{startup_budget}s", "device_wait")

        if not receiver_alive:
            if guest.restart_at > now_mono:
                receiver_detail = f"backoff={max(0, int(guest.restart_at - now_mono))}s"
            elapsed = self._stage_elapsed(guest, now_mono, "waiting_device")
            progress = self._progress_interp(20, 30, elapsed, max(2.0, float(self.restart_backoff_s)))
            return ("waiting_device", progress, receiver_detail, "receiver_wait")

        if guest.first_seen_mono is None:
            receiver_started = guest.receiver_started_mono or now_mono
            since_receiver = max(0.0, now_mono - receiver_started)
            if since_receiver < 1.0:
                progress = self._progress_interp(30, 40, since_receiver, 1.0)
                return ("receiver_started", progress, "receiver_ready", "receiver_started")
            elapsed = self._stage_elapsed(guest, now_mono, "waiting_first_frame")
            progress = self._progress_interp(40, 70, elapsed, max(5.0, startup_budget * 0.5))
            return ("waiting_first_frame", progress, f"{startup_wait}s/{startup_budget}s", "first_frame_wait")

        updates = min(self.seq_updates_cap(guest), self.min_seq_updates)
        ratio = float(updates) / float(self.min_seq_updates)
        elapsed = self._stage_elapsed(guest, now_mono, "waiting_seq_updates")
        base = 70 + int(20.0 * ratio)
        progress = min(95, base + self._progress_interp(0, 5, elapsed, max(2.0, self.heartbeat_timeout_s * 0.5)))
        detail = f"{updates}/{self.min_seq_updates}"
        return ("waiting_seq_updates", progress, detail, f"seq_updates={detail}")

    def seq_updates_cap(self, guest: GuestState) -> int:
        if guest.seq_updates < 0:
            return 0
        return guest.seq_updates

    def _invoke_stage_hook(self, guest: GuestState, stage_name: str, progress_percent: int, detail: str, now_mono: float) -> None:
        if not self.stage_hook:
            return
        if not os.path.exists(self.stage_hook):
            if debug_enabled() and not self.stage_hook_missing_logged:
                debug(f"stage hook not found, skipping: {self.stage_hook}")
                self.stage_hook_missing_logged = True
            return
        self.stage_hook_missing_logged = False
        if not os.access(self.stage_hook, os.X_OK):
            if not self.stage_hook_perm_logged:
                log("WARN", f"stage hook not executable: {self.stage_hook}")
                self.stage_hook_perm_logged = True
            return
        self.stage_hook_perm_logged = False

        hook_env = os.environ.copy()
        hook_env["GUEST_IVC_ID"] = str(guest.ivc_id)
        hook_env["GUEST_ZONE_ID"] = "" if guest.zone_id is None else str(guest.zone_id)
        hook_env["GUEST_LAST_SEQ"] = "" if guest.last_seq is None else str(guest.last_seq)
        hook_env["GUEST_LAST_UPTIME"] = "" if guest.last_uptime is None else str(guest.last_uptime)
        hook_env["GUEST_WAITED_S"] = str(int(max(0.0, now_mono - guest.cycle_start_mono)))

        try:
            cp = subprocess.run(
                [self.stage_hook, guest.name, stage_name, str(progress_percent), detail],
                check=False,
                timeout=float(self.stage_hook_timeout_s),
                env=hook_env,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired:
            log("WARN", f"stage hook timeout for {guest.name} stage={stage_name}")
            return
        except Exception as exc:
            log("WARN", f"failed to run stage hook for {guest.name} stage={stage_name}: {exc}")
            return

        if cp.returncode != 0:
            stderr = cp.stderr.strip()
            suffix = f": {stderr}" if stderr else ""
            log("WARN", f"stage hook failed for {guest.name} stage={stage_name} rc={cp.returncode}{suffix}")

    def _update_guest_stage(self, guest: GuestState, now_mono: float) -> None:
        if not guest.progress_armed:
            self._reset_progress_log_state(guest)
            return

        stage_name, progress, detail, hook_detail = self._stage_snapshot(guest, now_mono)
        if guest.current_stage != stage_name:
            debug(
                f"{guest.name} stage transition {guest.current_stage} -> {stage_name} "
                f"progress={progress} detail={detail}"
            )
            guest.current_stage = stage_name
            guest.stage_enter_mono = now_mono
            guest.current_stage_progress = progress
            guest.current_stage_detail = detail
            guest.current_stage_hook_detail = hook_detail
            self._invoke_stage_hook(guest, stage_name, progress, hook_detail, now_mono)
            return

        guest.current_stage_progress = progress
        guest.current_stage_detail = detail
        guest.current_stage_hook_detail = hook_detail

    def _maybe_log_progress(self, _now_mono: float) -> None:
        if not self.progress_enabled:
            return
        for guest in self.guests:
            if not guest.progress_armed:
                continue
            if not guest.current_stage:
                continue
            stage_changed = guest.current_stage != guest.last_logged_stage
            progress_changed = guest.current_stage_progress != guest.last_logged_progress
            if not (stage_changed or progress_changed):
                continue
            self._progress_log(
                f"guest-progress[{guest.short_name}]: {guest.current_stage_progress}% "
                f"{guest.current_stage} {guest.current_stage_detail}"
            )
            guest.last_logged_stage = guest.current_stage
            guest.last_logged_progress = guest.current_stage_progress
            guest.last_progress_emit_mono = time.monotonic()

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
        guest.receiver_started_mono = time.monotonic()
        self.sel.register(guest.stdout, selectors.EVENT_READ)
        self.fd_map[guest.stdout] = guest
        log("INFO", f"started ivc receiver for {guest.name}: {' '.join(cmd)}")
        debug(f"{guest.name} receiver attached to {guest.device_path} source={guest.device_source}")

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
        debug(f"{guest.name} receiver detached from {guest.device_path}")
        guest.proc = None
        guest.stdout = None
        guest.receiver_started_mono = None

    def _restart_if_needed(self, guest: GuestState, now_mono: float) -> None:
        if self.stop or not guest.monitor_active:
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
            debug(f"{guest.name} ignored non-json line: {line.rstrip()}")
            return
        payload = line.split(self.json_prefix, 1)[1].strip()
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            debug(f"{guest.name} invalid json payload: {payload}")
            return

        seq_raw = data.get("seq")
        if seq_raw is None:
            debug(f"{guest.name} json without seq: {payload}")
            return
        uptime_raw = data.get("uptime")
        try:
            seq = int(seq_raw)
        except (TypeError, ValueError):
            debug(f"{guest.name} bad seq value: {seq_raw!r}")
            return
        if seq < 0:
            debug(f"{guest.name} negative seq ignored: {seq}")
            return
        uptime: Optional[int]
        try:
            uptime = int(uptime_raw) if uptime_raw is not None else None
        except (TypeError, ValueError):
            uptime = None

        debug(
            f"{guest.name} frame seq={seq} uptime={uptime} startup_ok={guest.startup_ok} "
            f"announce_armed={guest.announce_armed} last_seq={guest.last_seq} last_uptime={guest.last_uptime} "
            f"seq_updates={guest.seq_updates} progress_armed={guest.progress_armed}"
        )

        if not self._zone_running_cached(guest, now_mono):
            if not guest.zone_not_running_logged:
                zone_desc = f"zone_id={guest.zone_id}" if guest.zone_id is not None else "zone=unknown"
                log("WARN", f"{guest.name} heartbeat ignored because {zone_desc} is not running")
                guest.zone_not_running_logged = True
            return

        guest.zone_not_running_logged = False

        if guest.first_seen_mono is None and not self._is_fresh_uptime(guest, uptime, now_mono):
            if not guest.stale_frame_logged:
                cycle_age = int(max(0.0, now_mono - guest.cycle_start_mono))
                log(
                    "WARN",
                    (
                        f"{guest.name} stale heartbeat ignored: seq={seq}, uptime={uptime}, "
                        f"cycle_age={cycle_age}s, slack={self.fresh_uptime_slack_s}s"
                    ),
                )
                guest.stale_frame_logged = True
            return

        guest.stale_frame_logged = False
        guest.last_seen_mono = now_mono
        if guest.first_seen_mono is None:
            guest.first_seen_mono = now_mono
            if guest.progress_armed:
                guest.cycle_start_mono = now_mono
            log("INFO", f"{guest.name} first frame received (seq={seq})")
            guest.startup_timeout_logged = False

        if guest.last_seq is None:
            guest.last_seq = seq
            if uptime is not None:
                guest.last_uptime = uptime
            return

        if seq > guest.last_seq:
            guest.seq_updates += 1
            guest.last_seq = seq
            if uptime is not None:
                guest.last_uptime = uptime
            guest.heartbeat_timeout_logged = False
            if not guest.startup_ok and guest.seq_updates >= self.min_seq_updates:
                guest.startup_ok = True
                guest.startup_timeout_logged = False
                if guest.startup_suppressed:
                    debug(f"{guest.name} startup observed in silent monitor mode, suppressing startup announce")
                else:
                    log(
                        "INFO",
                        f"{guest.name} startup healthy: seq updated {guest.seq_updates} times (seq={seq})",
                    )
                    if guest.announce_armed:
                        log("INFO", f"{guest.name} 启动成功")
                        console_log(f"{guest.name} 启动成功")
                        guest.announce_armed = False
                    else:
                        log("INFO", f"{guest.name} startup recovered without reboot, suppress success re-announce")
            return

        if seq < guest.last_seq:
            log("WARN", f"{guest.name} seq regressed: {guest.last_seq} -> {seq}")
            rollback: Optional[int] = None
            if uptime is not None and guest.last_uptime is not None and uptime < guest.last_uptime:
                rollback = guest.last_uptime - uptime
            fresh_reboot_detected = rollback is not None and uptime is not None and uptime <= self.fresh_uptime_slack_s
            reboot_detected = rollback is not None and rollback >= self.reboot_uptime_rollback_s
            debug(
                f"{guest.name} seq regression analysis: rollback={rollback} "
                f"threshold={self.reboot_uptime_rollback_s} fresh_reboot={fresh_reboot_detected}"
            )
            if reboot_detected or fresh_reboot_detected:
                if guest.startup_ok:
                    if reboot_detected:
                        log(
                            "INFO",
                            f"{guest.name} reboot detected by uptime rollback ({guest.last_uptime} -> {uptime}), waiting for fresh startup heartbeat",
                        )
                    else:
                        log(
                            "INFO",
                            (
                                f"{guest.name} reboot detected by fresh uptime after seq reset "
                                f"({guest.last_uptime} -> {uptime}), waiting for fresh startup heartbeat"
                            ),
                        )
                self.startup_confirmed_logged = False
                guest.seq_updates = 0
                guest.startup_ok = False
                guest.first_seen_mono = now_mono
                guest.last_seen_mono = now_mono
                guest.startup_timeout_logged = False
                guest.heartbeat_timeout_logged = False
                guest.cycle_start_mono = now_mono
                guest.last_seq = seq
                guest.last_uptime = uptime
                guest.announce_armed = not guest.startup_suppressed
                guest.stale_frame_logged = False
                if not guest.startup_suppressed:
                    guest.progress_armed = True
                    self._reset_progress_log_state(guest)
            else:
                if uptime is not None and guest.last_uptime is not None:
                    log(
                        "INFO",
                        f"{guest.name} seq reset without uptime rollback ({guest.last_uptime} -> {uptime}), treat as sender/receiver reset",
                    )
                guest.last_seq = seq
                if uptime is not None:
                    guest.last_uptime = uptime
                guest.last_seen_mono = now_mono
                guest.heartbeat_timeout_logged = False

    def _check_timeouts(self, now_mono: float) -> None:
        for guest in self.guests:
            if not guest.monitor_active:
                continue
            cycle_elapsed = now_mono - guest.cycle_start_mono
            if (
                guest.progress_armed
                and not guest.startup_ok
                and cycle_elapsed >= float(self.startup_timeout_s)
                and not guest.startup_timeout_logged
            ):
                if guest.first_seen_mono is None:
                    reason = f"no heartbeat yet on {guest.device_path}"
                else:
                    reason = f"need {self.min_seq_updates} seq updates in {self.startup_timeout_s}s"
                log("ERROR", f"startup timeout for {guest.name}: {reason}")
                guest.startup_timeout_logged = True

        if all(g.startup_ok for g in self.guests) and not self.startup_confirmed_logged:
            self.startup_confirmed_logged = True
            log("INFO", "healthy boot confirmed for all guests")
            log("INFO", "所有 guest 启动成功")

        for guest in self.guests:
            if not guest.startup_ok:
                continue
            if guest.last_seen_mono is None:
                continue
            silence = now_mono - guest.last_seen_mono
            if silence >= float(self.heartbeat_timeout_s) and not guest.heartbeat_timeout_logged:
                log("ERROR", f"heartbeat timeout for {guest.name}: no frame for {silence:.1f}s")
                guest.heartbeat_timeout_logged = True
                self._reset_guest_startup_state(
                    guest,
                    now_mono,
                    announce_armed=False,
                    startup_suppressed=True,
                )
                guest.progress_armed = False
                debug(f"{guest.name} startup state reset after heartbeat timeout")

    def cleanup(self) -> None:
        for guest in self.guests:
            self._stop_guest_proc(guest)
        self.sel.close()

    def run(self) -> int:
        for guest in self.guests:
            self._initialize_guest_monitor_mode(guest, self.start_mono)

        log(
            "INFO",
            (
                f"watch config: startup_timeout={self.startup_timeout_s}s "
                f"heartbeat_timeout={self.heartbeat_timeout_s}s min_seq_updates={self.min_seq_updates} "
                f"fresh_uptime_slack={self.fresh_uptime_slack_s}s"
            ),
        )
        debug(
            "watch binaries: "
            f"ivc_demo_bin={self.ivc_demo_bin} hvisor_bin={self.hvisor_bin} json_prefix={self.json_prefix} "
            f"guests={[f'{g.name}:{g.device_path}:source={g.device_source}:zone={g.zone_id}:cfg={g.zone_cfg}' for g in self.guests]}"
        )
        try:
            while not self.stop:
                now_mono = time.monotonic()
                for guest in self.guests:
                    self._refresh_guest_activation(guest, now_mono)
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
                for guest in self.guests:
                    self._update_guest_stage(guest, now_mono)
                self._maybe_log_progress(now_mono)
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
