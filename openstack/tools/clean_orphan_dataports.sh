#!/bin/bash
# Pre-flight cleanup of leaked gateway ext-ports on the undercloud.
#
# zaza attaches an extra undercloud port "<server>_ext-port" to each gateway
# instance via interface_attach. nova does not own these ports, so they leak
# when the model is torn down. A leaked port keeps a fixed IP in the shared
# external network's floating-IP range; a later test's floating IP that reuses
# that address then collides with the dead port at L2/ARP on the shared segment
# and is unreachable.
#
# We delete ports named "*_ext-port" whose attached instance no longer exists.
# interface_attach records the instance UUID in the port's device_id, so we key
# on that (immune to rename / duplicate names) rather than the port name. Live
# models keep a running instance and are left alone.
#
# Usage: clean_orphan_dataports.sh [OPENRC]   (default: $UNDERCLOUD_OPENRC, then
#   ~/novarc, then ~/admin-openrc.sh)
# Env: MIN_AGE_MINUTES (default 30) | EXT_PORT_NETWORK (restrict to one network,
#   useful under admin creds) | DRY_RUN=true (report only)
set -u

OPENRC="${1:-${UNDERCLOUD_OPENRC:-}}"
if [[ -z $OPENRC ]]; then
    for c in "$HOME/novarc" "$HOME/admin-openrc.sh"; do
        [[ -r $c ]] && { OPENRC=$c; break; }
    done
fi
if [[ -z $OPENRC || ! -r $OPENRC ]]; then
    echo "clean_orphan_dataports: no undercloud openrc found - skipping"
    exit 0
fi
# shellcheck disable=SC1090
source "$OPENRC"
if ! command -v openstack &>/dev/null; then
    echo "clean_orphan_dataports: openstack CLI not found - skipping"
    exit 0
fi

MIN_AGE_MINUTES="${MIN_AGE_MINUTES:-30}"
DRY_RUN="${DRY_RUN:-false}"

list_args=(port list -f value -c ID -c Name)
[[ -n ${EXT_PORT_NETWORK:-} ]] && list_args+=(--network "$EXT_PORT_NETWORK")

total=0 deleted=0
while read -r pid name; do
    [[ $name == *_ext-port ]] || continue
    total=$((total + 1))
    # device_owner/device_id are set by interface_attach and survive instance
    # deletion; -f value prints the three columns in -c order, one per line.
    mapfile -t f < <(openstack port show "$pid" \
        -c device_id -c device_owner -c created_at -f value 2>/dev/null)
    device_id=${f[0]:-} device_owner=${f[1]:-} created=${f[2]:-}
    # Only attached gateway ports leak this way; skip unattached/live ones.
    [[ $device_owner == compute:* && -n $device_id ]] || continue
    openstack server show "$device_id" -c id -f value &>/dev/null && continue
    # Age guard: leave recently created ports alone (parallel deploy safety).
    if ((MIN_AGE_MINUTES > 0)) && [[ -n $created ]]; then
        age=$(( ($(date -u +%s) - $(date -u -d "$created" +%s 2>/dev/null || echo 0)) / 60 ))
        ((age < MIN_AGE_MINUTES)) && \
            { echo "clean_orphan_dataports: skip $pid ($name) - only ${age}m old"; continue; }
    fi
    if [[ $DRY_RUN == true ]]; then
        echo "clean_orphan_dataports: WOULD delete $pid ($name, instance $device_id gone)"
    else
        echo "clean_orphan_dataports: deleting $pid ($name, instance $device_id gone)"
        openstack port delete "$pid" && deleted=$((deleted + 1))
    fi
done < <(openstack "${list_args[@]}" 2>/dev/null)

echo "clean_orphan_dataports: ext-ports=$total deleted=$deleted"
