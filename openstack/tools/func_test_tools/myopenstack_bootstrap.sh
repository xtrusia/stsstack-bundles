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
# juju 3.6.25 has a "cannot apply changes: permission denied" regression on
# relation hooks (breaks reactive charms' ha-relation-joined etc.). Bootstrap
# lane controllers at the version the known-good os-ctl controller runs.
# Override with AGENT_VERSION=x.y.z.
if [ -z "${AGENT_VERSION:-}" ]; then
    AGENT_VERSION=$(JUJU_DATA="$HOME/.local/share/juju" juju show-controller os-ctl --format json 2>/dev/null | python3 -c 'import sys,json;print(list(json.load(sys.stdin).values())[0]["details"]["agent-version"])' 2>/dev/null || true)
fi
AGENT_VERSION=${AGENT_VERSION:-3.6.23}  # fallback when os-ctl is gone; bump when juju past 3.6.25 fixes the regression

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
    --bootstrap-constraints "$BOOTSTRAP_CONSTRAINTS" ${AGENT_VERSION:+--agent-version "$AGENT_VERSION"}

# destroy_zaza_models force-deletes stuck models via juju-db mongo, which needs
# pymongo on the controller machine.
JUJU_DATA="$JD" juju ssh -m controller 0 \
    "sudo apt-get update -qq && sudo apt-get install -y python3-pymongo" || true
# Serve local image metadata over http and point new models at it so the
# openstack provisioner resolves cloud images. bootstrap --metadata-source and
# "juju metadata add-image" do NOT feed the provisioner; image-metadata-url does.
pgrep -f "http.server 8099" >/dev/null || (cd "$HOME/simplestreams/images" && setsid python3 -m http.server 8099 --bind 0.0.0.0 >/tmp/meta-http.log 2>&1 &)
JUJU_DATA="$JD" juju model-defaults -c "$CONTROLLER" image-metadata-url="http://192.168.0.9:8099/" 2>/dev/null
# Set network as a model-default too: bootstrap --config network only sets the
# CONTROLLER model, but zaza's per-test models must also carry it. juju only
# auto-skips security groups on a port_security-disabled network when that network
# is in the model's networks list (openstack provider, BUG 1680787). myopenstack
# disables port_security for the hacluster VIP, so without this the zaza model has
# no network -> juju attaches an SG -> nova SecurityGroupCannotBeApplied, nothing boots.
JUJU_DATA="$JD" juju model-defaults -c "$CONTROLLER" network="$NET" 2>/dev/null
echo "Controller $CONTROLLER ready."
