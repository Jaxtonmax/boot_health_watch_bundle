#!/bin/sh

set -eu

ts() {
    date '+%F %T.%3N'
}

log() {
    printf '%s [root-boot-health] %s\n' "$(ts)" "$*"
}

console_log() {
    if [ "${CONSOLE_BOOT_LOG_ENABLED:-0}" = "1" ] && [ -w /dev/console ]; then
        printf '%s [root-boot-health] %s\n' "$(ts)" "$*" > /dev/console 2>/dev/null || true
    fi
}

boot_log() {
    log "$*"
    console_log "$*"
}

wait_for_file() {
    path="$1"
    timeout_s="$2"
    waited=0

    while [ ! -e "$path" ]; do
        if [ "$timeout_s" -gt 0 ] && [ "$waited" -ge "$timeout_s" ]; then
            log "timeout waiting for $path"
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done

    return 0
}

IVC_BOOT_WATCH_BIN=${IVC_BOOT_WATCH_BIN:-/usr/local/sbin/ivc_boot_watch}
ROOT_GUEST_HEALTH_BIN=${ROOT_GUEST_HEALTH_BIN:-/usr/local/sbin/root_guest_health_watch.py}
IVC_DEMO_BIN=${IVC_DEMO_BIN:-/usr/bin/ivc_demo}
IVC_JSON_PREFIX=${IVC_JSON_PREFIX:-IVC_JSON:}
GUEST_LINUX_IRQ=${GUEST_LINUX_IRQ:-107}
GUEST_RTTHREAD_IRQ=${GUEST_RTTHREAD_IRQ:-109}
GUEST_LINUX_IVC_ID=${GUEST_LINUX_IVC_ID:-0}
GUEST_RTTHREAD_IVC_ID=${GUEST_RTTHREAD_IVC_ID:-2}
POLL_MS=${POLL_MS:-200}
STARTUP_TIMEOUT_S=${STARTUP_TIMEOUT_S:-30}
REARM_S=${REARM_S:-0}
MONITOR_TIMEOUT_S=${MONITOR_TIMEOUT_S:-0}
HEARTBEAT_TIMEOUT_S=${HEARTBEAT_TIMEOUT_S:-15}
MIN_SEQ_UPDATES=${MIN_SEQ_UPDATES:-2}
RESTART_BACKOFF_S=${RESTART_BACKOFF_S:-1}
IRQ_FILE_TIMEOUT_S=${IRQ_FILE_TIMEOUT_S:-15}
ROOT_READY_FILE=${ROOT_READY_FILE:-}
ENABLE_IRQ_WATCH=${ENABLE_IRQ_WATCH:-1}
CONSOLE_BOOT_ONCE_FILE=${CONSOLE_BOOT_ONCE_FILE:-/run/root_boot_health_watch.console_once}

export IVC_DEMO_BIN IVC_JSON_PREFIX
export STARTUP_TIMEOUT_S HEARTBEAT_TIMEOUT_S MIN_SEQ_UPDATES RESTART_BACKOFF_S
export GUEST_LINUX_IVC_ID GUEST_RTTHREAD_IVC_ID

IRQ_WATCH_PID=""

if [ -e "$CONSOLE_BOOT_ONCE_FILE" ]; then
    CONSOLE_BOOT_LOG_ENABLED=0
else
    CONSOLE_BOOT_LOG_ENABLED=1
    : > "$CONSOLE_BOOT_ONCE_FILE" 2>/dev/null || CONSOLE_BOOT_LOG_ENABLED=0
fi

cleanup() {
    if [ -n "$IRQ_WATCH_PID" ]; then
        kill "$IRQ_WATCH_PID" >/dev/null 2>&1 || true
        wait "$IRQ_WATCH_PID" >/dev/null 2>&1 || true
        IRQ_WATCH_PID=""
    fi
}

trap cleanup EXIT INT TERM

if [ -n "$ROOT_READY_FILE" ]; then
    log "waiting for root ready marker: $ROOT_READY_FILE"
    wait_for_file "$ROOT_READY_FILE" "$IRQ_FILE_TIMEOUT_S"
fi

if [ ! -x "$ROOT_GUEST_HEALTH_BIN" ]; then
    log "root guest health watcher not found or not executable: $ROOT_GUEST_HEALTH_BIN"
    exit 1
fi

boot_log "Root Linux healthy boot stage reached"
boot_log "heartbeat config: linux_ivc_id=$GUEST_LINUX_IVC_ID rtt_ivc_id=$GUEST_RTTHREAD_IVC_ID startup_timeout=${STARTUP_TIMEOUT_S}s heartbeat_timeout=${HEARTBEAT_TIMEOUT_S}s"

if [ "$ENABLE_IRQ_WATCH" = "1" ]; then
    if [ ! -x "$IVC_BOOT_WATCH_BIN" ]; then
        log "ivc_boot_watch not found or not executable: $IVC_BOOT_WATCH_BIN"
    else
        if wait_for_file "/proc/irq/$GUEST_LINUX_IRQ/spurious" "$IRQ_FILE_TIMEOUT_S" && \
           wait_for_file "/proc/irq/$GUEST_RTTHREAD_IRQ/spurious" "$IRQ_FILE_TIMEOUT_S"; then
            boot_log "starting IRQ watcher: linux_irq=$GUEST_LINUX_IRQ rtt_irq=$GUEST_RTTHREAD_IRQ"
            "$IVC_BOOT_WATCH_BIN" \
                -g "$GUEST_LINUX_IRQ" \
                -r "$GUEST_RTTHREAD_IRQ" \
                -p "$POLL_MS" \
                -s "$STARTUP_TIMEOUT_S" \
                -R "$REARM_S" \
                -t "$MONITOR_TIMEOUT_S" &
            IRQ_WATCH_PID=$!
        else
            log "IRQ watcher skipped: spurious IRQ path not ready"
        fi
    fi
fi

boot_log "starting guest heartbeat watcher: $ROOT_GUEST_HEALTH_BIN"
"$ROOT_GUEST_HEALTH_BIN"
