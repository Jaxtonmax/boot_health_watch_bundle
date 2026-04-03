#!/bin/sh

set -eu

BASE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SVC_NAME=root_boot_health_watch.service
ENV_DST=/etc/default/root_boot_health_watch
ENV_EXAMPLE_DST=/etc/default/root_boot_health_watch.example
HOOK_DST=/usr/local/sbin/root_guest_stage_hook.sh

log() {
    printf '[install] %s\n' "$*"
}

install -m 0755 "$BASE_DIR/ivc_boot_watch" /usr/local/sbin/ivc_boot_watch
install -m 0755 "$BASE_DIR/root_boot_health_watch.sh" /usr/local/sbin/root_boot_health_watch.sh
install -m 0755 "$BASE_DIR/root_guest_health_watch.py" /usr/local/sbin/root_guest_health_watch.py
if [ ! -f "$HOOK_DST" ]; then
    install -m 0755 "$BASE_DIR/root_guest_stage_hook.sh" "$HOOK_DST"
    log "created $HOOK_DST"
else
    log "kept existing $HOOK_DST"
fi
install -m 0644 "$BASE_DIR/root_boot_health_watch.service" "/etc/systemd/system/$SVC_NAME"
install -m 0644 "$BASE_DIR/root_boot_health_watch.env.example" "$ENV_EXAMPLE_DST"

if [ ! -f "$ENV_DST" ]; then
    install -m 0644 "$BASE_DIR/root_boot_health_watch.env.example" "$ENV_DST"
    log "created $ENV_DST from example"
else
    log "kept existing $ENV_DST"
fi

if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload
    if [ "${NO_START:-0}" = "1" ]; then
        log "NO_START=1, skipped enable/start"
    else
        systemctl enable --now "$SVC_NAME"
        log "enabled and started $SVC_NAME"
    fi
else
    log "systemctl not found, skipped service reload/start"
fi

log "done"
log "edit config if needed: $ENV_DST"
log "view logs: journalctl -u $SVC_NAME -f"
