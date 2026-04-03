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

current_boot_id() {
    if [ -r /proc/sys/kernel/random/boot_id ]; then
        tr -d '\n' < /proc/sys/kernel/random/boot_id
        return 0
    fi
    return 1
}

IVC_BOOT_WATCH_BIN=${IVC_BOOT_WATCH_BIN:-/usr/local/sbin/ivc_boot_watch}
ROOT_GUEST_HEALTH_BIN=${ROOT_GUEST_HEALTH_BIN:-/usr/local/sbin/root_guest_health_watch.py}
IVC_DEMO_BIN=${IVC_DEMO_BIN:-/usr/bin/ivc_demo}
IVC_JSON_PREFIX=${IVC_JSON_PREFIX:-IVC_JSON:}
GUEST_LINUX_IRQ=${GUEST_LINUX_IRQ:-107}
GUEST_RTTHREAD_IRQ=${GUEST_RTTHREAD_IRQ:-109}
GUEST_LINUX_ZONE_CFG=${GUEST_LINUX_ZONE_CFG:-/root/SeawayHyper/zone1/zone1-linux.json}
GUEST_RTTHREAD_ZONE_CFG=${GUEST_RTTHREAD_ZONE_CFG:-/root/SeawayHyper/zone2/zone2-rtt.json}
GUEST_LINUX_DEVICE_PATH=${GUEST_LINUX_DEVICE_PATH:-}
GUEST_RTTHREAD_DEVICE_PATH=${GUEST_RTTHREAD_DEVICE_PATH:-}
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
FRESH_UPTIME_SLACK_S=${FRESH_UPTIME_SLACK_S:-20}
CONSOLE_BOOT_LOG_MODE=${CONSOLE_BOOT_LOG_MODE:-off}
CONSOLE_BOOT_ONCE_FILE=${CONSOLE_BOOT_ONCE_FILE:-/run/root_boot_health_watch.console_once}
ROOT_BOOT_STAGE_BOOT_ID_FILE=${ROOT_BOOT_STAGE_BOOT_ID_FILE:-/run/root_boot_health_watch.boot_id}

export IVC_DEMO_BIN IVC_JSON_PREFIX
export STARTUP_TIMEOUT_S HEARTBEAT_TIMEOUT_S MIN_SEQ_UPDATES RESTART_BACKOFF_S
export GUEST_LINUX_ZONE_CFG GUEST_RTTHREAD_ZONE_CFG
export GUEST_LINUX_DEVICE_PATH GUEST_RTTHREAD_DEVICE_PATH
export GUEST_LINUX_IVC_ID GUEST_RTTHREAD_IVC_ID
export FRESH_UPTIME_SLACK_S

IRQ_WATCH_PID=""
ROOT_BOOT_STAGE_LOG_ENABLED=1

case "$CONSOLE_BOOT_LOG_MODE" in
    always|1|true|on)
        CONSOLE_BOOT_LOG_ENABLED=1
        ;;
    once)
        if [ -e "$CONSOLE_BOOT_ONCE_FILE" ]; then
            CONSOLE_BOOT_LOG_ENABLED=0
        else
            CONSOLE_BOOT_LOG_ENABLED=1
            : > "$CONSOLE_BOOT_ONCE_FILE" 2>/dev/null || CONSOLE_BOOT_LOG_ENABLED=0
        fi
        ;;
    *)
        CONSOLE_BOOT_LOG_ENABLED=0
        ;;
esac

boot_id=""
if boot_id=$(current_boot_id); then
    if [ -r "$ROOT_BOOT_STAGE_BOOT_ID_FILE" ] && [ "$(cat "$ROOT_BOOT_STAGE_BOOT_ID_FILE" 2>/dev/null || true)" = "$boot_id" ]; then
        ROOT_BOOT_STAGE_LOG_ENABLED=0
    else
        mkdir -p "$(dirname "$ROOT_BOOT_STAGE_BOOT_ID_FILE")" 2>/dev/null || true
        printf '%s\n' "$boot_id" > "$ROOT_BOOT_STAGE_BOOT_ID_FILE" 2>/dev/null || true
        ROOT_BOOT_STAGE_LOG_ENABLED=1
    fi
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

linux_device=${GUEST_LINUX_DEVICE_PATH:-/dev/hivc$GUEST_LINUX_IVC_ID}
rtt_device=${GUEST_RTTHREAD_DEVICE_PATH:-/dev/hivc$GUEST_RTTHREAD_IVC_ID}
if [ "$ROOT_BOOT_STAGE_LOG_ENABLED" = "1" ]; then
    boot_log "Root Linux healthy boot stage reached"
    boot_log "Root Linux 启动成功"
    boot_log "heartbeat config: linux_device=$linux_device rtt_device=$rtt_device startup_timeout=${STARTUP_TIMEOUT_S}s heartbeat_timeout=${HEARTBEAT_TIMEOUT_S}s fresh_uptime_slack=${FRESH_UPTIME_SLACK_S}s"
fi
log "console boot log enabled=$CONSOLE_BOOT_LOG_ENABLED mode=$CONSOLE_BOOT_LOG_MODE once_file=$CONSOLE_BOOT_ONCE_FILE boot_stage_enabled=$ROOT_BOOT_STAGE_LOG_ENABLED boot_id_file=$ROOT_BOOT_STAGE_BOOT_ID_FILE"
log "watcher bins: ivc_boot_watch=$IVC_BOOT_WATCH_BIN root_guest_health=$ROOT_GUEST_HEALTH_BIN ivc_demo_bin=$IVC_DEMO_BIN"

if [ "$ENABLE_IRQ_WATCH" = "1" ]; then
    if [ ! -x "$IVC_BOOT_WATCH_BIN" ]; then
        log "ivc_boot_watch not found or not executable: $IVC_BOOT_WATCH_BIN"
    else
        if wait_for_file "/proc/irq/$GUEST_LINUX_IRQ/spurious" "$IRQ_FILE_TIMEOUT_S" && \
           wait_for_file "/proc/irq/$GUEST_RTTHREAD_IRQ/spurious" "$IRQ_FILE_TIMEOUT_S"; then
            if [ "$ROOT_BOOT_STAGE_LOG_ENABLED" = "1" ]; then
                boot_log "starting IRQ watcher: linux_irq=$GUEST_LINUX_IRQ rtt_irq=$GUEST_RTTHREAD_IRQ"
            else
                log "starting IRQ watcher: linux_irq=$GUEST_LINUX_IRQ rtt_irq=$GUEST_RTTHREAD_IRQ"
            fi
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

log "starting guest heartbeat watcher: $ROOT_GUEST_HEALTH_BIN"
"$ROOT_GUEST_HEALTH_BIN"
