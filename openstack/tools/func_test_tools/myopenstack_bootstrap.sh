#!/bin/bash
# Bootstrap a dedicated Juju controller into the myopenstack (Gazpacho) cloud,
# configured for a parallel func-test lane. Each lane gets its own controller so
# the runner's --parallel cleanup stays scoped to that controller's models.
#
# Usage: myopenstack_bootstrap.sh <controller-name> <juju-data-dir>
# Env overrides: OPENRC PROVIDER_NET METADATA_SOURCE BOOTSTRAP_BASE
#                BOOTSTRAP_CONSTRAINTS CLOUD
set -eu
CONTROLLER=${1:?usage: myopenstack_bootstrap.sh <controller-name> <juju-data-dir>}
JD=${2:?usage: myopenstack_bootstrap.sh <controller-name> <juju-data-dir>}
OPENRC=${OPENRC:-$HOME/admin-openrc.sh}
PROVIDER_NET=${PROVIDER_NET:-provider-net}
METADATA_SOURCE=${METADATA_SOURCE:-$HOME/simplestreams/images}
BOOTSTRAP_BASE=${BOOTSTRAP_BASE:-ubuntu@22.04}
BOOTSTRAP_CONSTRAINTS=${BOOTSTRAP_CONSTRAINTS:-mem=4G cores=2}
CLOUD=${CLOUD:-myopenstack}

# Seed the isolated JUJU_DATA with the cloud + credential definitions so the
# bootstrap does not touch the shared default juju client state.
mkdir -p "$JD"
for f in clouds.yaml credentials.yaml public-clouds.yaml; do
    [ -f "$HOME/.local/share/juju/$f" ] && cp "$HOME/.local/share/juju/$f" "$JD/"
done
chmod 600 "$JD"/*.yaml 2>/dev/null || true

# shellcheck disable=SC1090
source "$OPENRC"
NET=$(openstack network show "$PROVIDER_NET" -f value -c id)

echo "Bootstrapping $CONTROLLER into $CLOUD (net=$NET base=$BOOTSTRAP_BASE)..."
JUJU_DATA="$JD" juju bootstrap "$CLOUD" "$CONTROLLER" \
    --metadata-source "$METADATA_SOURCE" \
    --bootstrap-base "$BOOTSTRAP_BASE" \
    --config network="$NET" \
    --config use-default-secgroup=false \
    --bootstrap-constraints "$BOOTSTRAP_CONSTRAINTS"

# destroy_zaza_models force-deletes stuck models via juju-db mongo, which needs
# pymongo on the controller machine.
JUJU_DATA="$JD" juju ssh -m controller 0 \
    "sudo apt-get update -qq && sudo apt-get install -y python3-pymongo" || true
echo "Controller $CONTROLLER ready."
