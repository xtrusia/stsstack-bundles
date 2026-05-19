#!/bin/bash -eu
#
# Run Charmed Openstack CI tests on juju-os-controller (myopenstack cloud).
#
# Based on charmed_openstack_functest_runner.sh, adapted for the self-hosted
# Gazpacho OpenStack environment (provider-net, 192.168.2.100-250).
#
# Usage: clone/fetch charm to test and run from within charm root dir.
#
FUNC_TEST_PR=
FUNC_TEST_TARGET=()
BAKE_BASELINE=
BAKE_SKIP_CLEANUP=false
DEPLOY_FROM_BASELINE=
DEPLOY_FROM_BASELINE_RUN_TEST=false
CHECKPOINT_ALLOW_VOLUME_BACKED=false
CHECKPOINT_CREATE=
CHECKPOINT_DIR=${JUJU_MODEL_CHECKPOINT_DIR:-$HOME/juju-model-checkpoints}
CHECKPOINT_JUJU_CMD=${CHECKPOINT_JUJU_CMD:-$(command -v juju || true)}
CHECKPOINT_OPENSTACK_CMD=${CHECKPOINT_OPENSTACK_CMD:-$(command -v openstack || true)}
CHECKPOINT_RESET_AFTER_TEST=false
CHECKPOINT_RESTORE=
CHECKPOINT_RUN_TEST=false
CHECKPOINT_VERIFY_IMAGES=false
MANUAL_FUNCTESTS=false
MODIFY_BUNDLE_CONSTRAINTS=true
REMOTE_BUILD=
SKIP_BUILD=false
SLEEP=
WAIT_ON_DESTROY=true
RERUN_PHASE=
ZAZA_TEMPLATE=

. $(dirname $0)/func_test_tools/common.sh

# -------------------------------------------------------------------
# Environment defaults for juju-os-controller / myopenstack
# Override any of these via env vars before running.
# -------------------------------------------------------------------
OPENRC=${OPENRC:-~/admin-openrc.sh}
OS_NETWORK=${OS_NETWORK:-provider-net}
OS_SUBNET=${OS_SUBNET:-provider-subnet}
VIP_PORT_PREFIX=${VIP_PORT_PREFIX:-zaza-vip}
JUJU_CONTROLLER=${JUJU_CONTROLLER:-juju-os-controller}
JUJU_MODEL_OWNER=${JUJU_MODEL_OWNER:-admin}

usage () {
    cat << EOF
USAGE: $(basename $0) OPTIONS

Run OpenStack charms functional tests on juju-os-controller (myopenstack).
This is a variant of charmed_openstack_functest_runner.sh adapted for the
self-hosted Gazpacho OpenStack environment.

Run from within a charm root directory.

OPTIONS:
    --func-test-target TARGET_NAME
        Provide the name of a specific test target to run. If none provided
        all tests are run based on what is defined in osci.yaml. This option
        can be provided more than once.
    --func-test-pr PR_ID
        Provides similar functionality to Func-Test-Pr in commit message. Set
        to zaza-openstack-tests Pull Request ID.
    --no-wait
        By default we wait before destroying the model after a test run. This
        flag can used to override that behaviour.
    --manual-functests
        Runs functest commands separately (deploy,configure,test) instead of
        the entire suite.
    --remote-build USER@HOST,GIT_PATH
        Builds the charm in a remote location and transfers the charm file over.
        Implies --skip-build. Example:
          --remote-build ubuntu@10.171.168.1,~/git/charm-nova-compute
    --rerun deploy|configure|test
        Re-run a specific phase.
    --skip-build
        Skip building charm if already done to save time.
    --skip-modify-bundle-constraints
        Skip modifying test bundle constraints.
    --sleep TIME_SECS
        Specify amount of seconds to sleep between functest steps.
    --zaza-template DIR
        Inject tests/, tox.ini, and test-requirements.* from DIR into the
        charm's test runner location (src/ for source charms, repo root
        for ops/classic). Files are copied in for the duration of the
        run and removed at exit. The target location must not already
        contain these names. Use this when the charm repo no longer
        ships its zaza tests inline.
    --checkpoint-create NAME
        Run zaza deploy and configure phases for one target, then create an
        OpenStack-backed checkpoint. The model is kept for later restore.
    --checkpoint-dir DIR
        Directory where checkpoint metadata is written
        (default: \$HOME/juju-model-checkpoints).
    --checkpoint-run-test
        With --checkpoint-create, run the zaza test phase after checkpointing.
    --checkpoint-restore DIR_OR_MANIFEST
        Restore an existing checkpoint, then run the zaza test phase against
        the restored model.
    --checkpoint-reset-after-test
        Restore the checkpoint again after the zaza test phase so the model
        returns to the checkpoint state.
    --checkpoint-allow-volume-backed
        Pass --allow-volume-backed to the checkpoint creation tool.
    --checkpoint-verify-images
        Download each snapshot image during checkpoint creation and verify
        Glance hash metadata before accepting the checkpoint.
    --bake-baseline NAME
        Run zaza deploy and configure phases for one target, then bake
        baseline Glance images for fast redeploy. The source model becomes
        unusable after baking (juju agent is wiped, cloud-init is reset)
        and should be destroyed before deploying from the baseline.
    --bake-skip-cleanup
        Skip in-VM cleanup (cloud-init clean, juju agent removal) before
        snapshotting. Debugging only — new deploys will likely fail.
    --deploy-from-baseline DIR_OR_MANIFEST
        Run zaza deploy and configure phases using a previously baked
        baseline. The bundle is rewritten to pin each machine to its
        baseline Glance image via the image-id constraint.
    --deploy-from-baseline-run-test
        With --deploy-from-baseline, run the zaza test phase after deploy
        and configure complete.
    --help
        This help message.

ENVIRONMENT VARIABLES (override defaults):
    OPENRC              Path to OpenRC file (default: ~/admin-openrc.sh)
    OS_NETWORK          OpenStack network name (default: provider-net)
    OS_SUBNET           OpenStack subnet name (default: provider-subnet)
    VIP_PORT_PREFIX     Port name prefix for VIPs (default: zaza-vip)
    JUJU_CONTROLLER     Juju controller name (default: juju-os-controller)
    JUJU_MODEL_OWNER    Juju model owner (default: admin)
    CHECKPOINT_JUJU_CMD
                        Juju CLI path used by checkpoint tool.
    CHECKPOINT_OPENSTACK_CMD
                        OpenStack CLI path used by checkpoint tool.
EOF
}


qualify_model ()
{
    local model=$1

    if [[ $model == *:* ]]; then
        echo "$model"
    else
        echo "${JUJU_CONTROLLER}:${JUJU_MODEL_OWNER}/${model}"
    fi
}


ensure_func_noop_env ()
{
    local recreate=$1
    local tox_args="-e func-noop"

    if $recreate; then
        tox_args="-re func-noop"
    fi
    tox $tox_args
}


configure_checkpoint_model ()
{
    local model=$1
    local constraints=()
    local settings=()

    IFS=';' read -ra settings <<< "$TEST_MODEL_SETTINGS"
    IFS=';' read -ra constraints <<< "$TEST_MODEL_CONSTRAINTS"
    juju model-config -m "$model" "${settings[@]}" || return $?
    juju set-model-constraints -m "$model" "${constraints[@]}"
}


checkpoint_tool ()
{
    realpath "$(dirname "$0")/juju_model_checkpoint_openstack.py"
}


checkpoint_create ()
{
    local model=$1
    local target=$2
    local name=$CHECKPOINT_CREATE
    local args=()

    if [[ -z $name ]]; then
        name="${CHARM_NAME}-${target}-${COMMIT_ID}"
    fi
    if $CHECKPOINT_ALLOW_VOLUME_BACKED; then
        args+=( --allow-volume-backed )
    fi
    if $CHECKPOINT_VERIFY_IMAGES; then
        args+=( --verify-images )
    fi
    JUJU_CMD="$CHECKPOINT_JUJU_CMD" \
        OPENSTACK_CMD="$CHECKPOINT_OPENSTACK_CMD" \
        "$(checkpoint_tool)" create \
        -m "$model" \
        --name "$name" \
        --output-dir "$CHECKPOINT_DIR" \
        "${args[@]}"
}


checkpoint_restore_model ()
{
    local checkpoint=$1

    python3 - "$checkpoint" << 'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1]).expanduser()
manifest = path / "manifest.json" if path.is_dir() else path
with manifest.open(encoding="utf-8") as fh:
    print(json.load(fh)["model"])
PY
}


checkpoint_restore ()
{
    local checkpoint=$1
    local model=$2

    JUJU_CMD="$CHECKPOINT_JUJU_CMD" \
        OPENSTACK_CMD="$CHECKPOINT_OPENSTACK_CMD" \
        "$(checkpoint_tool)" restore -m "$model" "$checkpoint" --yes
}


run_checkpoint_restore_test ()
{
    local checkpoint=$1
    local model
    local ret=0

    model=$(checkpoint_restore_model "$checkpoint")
    [[ -d src ]] && pushd src &>/dev/null || true
    ensure_func_noop_env true || return $?
    checkpoint_restore "$checkpoint" "$model" || return $?
    . .tox/func-noop/bin/activate
    functest-test -m "$model" || ret=$?
    deactivate
    if $CHECKPOINT_RESET_AFTER_TEST; then
        checkpoint_restore "$checkpoint" "$model" || return $?
    fi
    popd &>/dev/null || true
    return $ret
}


run_checkpoint_create_flow ()
{
    local target=$1
    local recreate_noop=$2
    local bundle
    local checkpoint_output
    local checkpoint_path
    local model
    local ret=0

    bundle="$(python3 "$TOOLS_PATH/extract_job_target.py" "$target")"
    model="$(qualify_model "test-$target")"

    ensure_func_noop_env "$recreate_noop" || return $?
    juju add-model "test-$target" --no-switch || return $?
    configure_checkpoint_model "$model" || return $?

    . .tox/func-noop/bin/activate
    functest-deploy -b "tests/bundles/$bundle.yaml" -m "$model" || ret=$?
    if ((! ret)); then
        juju status -m "$model"
        functest-configure -m "$model" || ret=$?
    fi
    if ((! ret)); then
        juju status -m "$model"
        checkpoint_output=$(checkpoint_create "$model" "$target") || ret=$?
        echo "$checkpoint_output"
        if ((! ret)); then
            checkpoint_path=$(awk '/checkpoint written to/ {print $4}' \
                <<< "$checkpoint_output")
        fi
    fi
    if ((! ret)) && $CHECKPOINT_RUN_TEST; then
        functest-test -m "$model" || ret=$?
        if $CHECKPOINT_RESET_AFTER_TEST; then
            if [[ -n $checkpoint_path ]]; then
                checkpoint_restore "$checkpoint_path" "$model"
            fi
        fi
    fi
    deactivate
    return $ret
}

bake_baseline ()
{
    local model=$1
    local target=$2
    local name=$BAKE_BASELINE
    local args=( --allow-volume-backed )

    if [[ -z $name ]]; then
        name="${CHARM_NAME}-${target}-${COMMIT_ID}"
    fi
    if $BAKE_SKIP_CLEANUP; then
        args+=( --skip-cleanup )
    fi
    JUJU_CMD="$CHECKPOINT_JUJU_CMD" \
        OPENSTACK_CMD="$CHECKPOINT_OPENSTACK_CMD" \
        "$(checkpoint_tool)" bake \
        -m "$model" \
        --name "$name" \
        --output-dir "$CHECKPOINT_DIR" \
        "${args[@]}"
}


run_bake_baseline_flow ()
{
    local target=$1
    local recreate_noop=$2
    local bundle
    local model
    local ret=0

    bundle="$(python3 "$TOOLS_PATH/extract_job_target.py" "$target")"
    model="$(qualify_model "test-$target")"

    ensure_func_noop_env "$recreate_noop" || return $?
    juju add-model "test-$target" --no-switch || return $?
    configure_checkpoint_model "$model" || return $?

    . .tox/func-noop/bin/activate
    functest-deploy -b "tests/bundles/$bundle.yaml" -m "$model" || ret=$?
    if ((! ret)); then
        juju status -m "$model"
        functest-configure -m "$model" || ret=$?
    fi
    if ((! ret)); then
        juju status -m "$model"
        bake_baseline "$model" "$target" || ret=$?
    fi
    deactivate
    return $ret
}


apply_baseline_to_bundle ()
{
    local baseline=$1
    local in_bundle=$2
    local out_bundle=$3

    JUJU_CMD="$CHECKPOINT_JUJU_CMD" \
        OPENSTACK_CMD="$CHECKPOINT_OPENSTACK_CMD" \
        "$(checkpoint_tool)" apply-baseline \
        --baseline "$baseline" \
        --bundle "$in_bundle" \
        --output "$out_bundle"
}


run_deploy_from_baseline_flow ()
{
    local target=$1
    local recreate_noop=$2
    local baseline=$DEPLOY_FROM_BASELINE
    local bundle
    local in_bundle
    local out_bundle
    local model
    local ret=0

    bundle="$(python3 "$TOOLS_PATH/extract_job_target.py" "$target")"
    in_bundle="tests/bundles/$bundle.yaml"
    out_bundle="tests/bundles/${bundle}-baseline.yaml"
    model="$(qualify_model "test-$target")"

    apply_baseline_to_bundle "$baseline" "$in_bundle" "$out_bundle" \
        || return $?
    ensure_func_noop_env "$recreate_noop" || return $?
    juju add-model "test-$target" --no-switch || return $?
    configure_checkpoint_model "$model" || return $?

    . .tox/func-noop/bin/activate
    functest-deploy -b "$out_bundle" -m "$model" || ret=$?
    if ((! ret)); then
        juju status -m "$model"
        functest-configure -m "$model" || ret=$?
    fi
    if ((! ret)) && $DEPLOY_FROM_BASELINE_RUN_TEST; then
        functest-test -m "$model" || ret=$?
    fi
    deactivate
    return $ret
}


run_test_phase ()
{
    local phase=$1
    local model=$2
    local bundle=${3:-""}
    local args=
    local ret=

    unit_errors=$(juju status --format json| jq '.applications[]| select(.units!=null)| .units[]."workload-status"| select(.current=="error")')
    if [[ -n $unit_errors ]]; then
        echo -e "\nNOTE: before you run a phase make sure that any hook errors have been resolved.\n"
        echo "$unit_errors"
        read -p "press [ENTER] to continue"
    fi

    . .tox/func-target/bin/activate
    echo "Running '$phase' phase..."
    if [[ $phase == deploy ]]; then
        if [[ -z $bundle ]]; then
            read -p "Enter name of bundle we are running (from tests/bundles/): " bundle
        fi
        args="-b tests/bundles/$bundle.yaml"
    fi
    functest-$phase -m $model ${args}
    ret=$?
    deactivate
    return $ret
}


retry_on_fail ()
{
    local model=$1
    local bundle=$2
    local ret=
    juju switch $model
cat << EOF
The tests have failed. You now have the choice to either exit or re-run a test phase.

To re-run the tests you need to choose which of the following phases you want to run:
  * deploy
  * configure
  * test

EOF
    read -p "Enter phase to run (exit|deploy|configure|test): " phase
    case "$phase" in
        deploy|configure|test)
            while true; do
                run_test_phase $phase $model $bundle
                ret=$?
                if (($ret)); then
                    read -p "Failed. Try $phase phase again? [Y/n]" answer
                    [[ -z $answer ]] || [[ ${answer,,} == y ]] || break
                else
                    [[ $phase == test ]] && break
                    [[ $phase == deploy ]] && phase=configure || phase=test
                fi
            done
        ;;
        exit|quit|q)
            echo "Exiting."
            return 1
        ;;
        *)
            echo "ERROR: unrecognised phase name '$phase'"
            return 1
        ;;
    esac
    return $ret
}


while (($# > 0)); do
    case "$1" in
        --debug)
            set -x
            ;;
        --func-test-target)
            FUNC_TEST_TARGET+=( $2 )
            shift
            ;;
        --func-test-pr)
            FUNC_TEST_PR=$2
            shift
            ;;
        --manual-functests)
            MANUAL_FUNCTESTS=true
            ;;
        --no-wait)
            WAIT_ON_DESTROY=false
            ;;
        --remote-build)
            REMOTE_BUILD=$2
            SKIP_BUILD=true
            shift
            ;;
        --rerun)
            RERUN_PHASE=$2
            [[ $2 = deploy ]] || [[ $2 = configure ]] || [[ $2 = test ]] || opt_error $1 $2
            shift
            ;;
        --skip-modify-bundle-constraints)
            MODIFY_BUNDLE_CONSTRAINTS=false
            ;;
        --skip-build)
            SKIP_BUILD=true
            ;;
        --sleep)
            SLEEP=$2
            shift
            ;;
        --zaza-template)
            ZAZA_TEMPLATE=$2
            shift
            ;;
        --checkpoint-create)
            CHECKPOINT_CREATE=$2
            shift
            ;;
        --checkpoint-dir)
            CHECKPOINT_DIR=$2
            shift
            ;;
        --checkpoint-run-test)
            CHECKPOINT_RUN_TEST=true
            ;;
        --checkpoint-restore)
            CHECKPOINT_RESTORE=$2
            shift
            ;;
        --checkpoint-reset-after-test)
            CHECKPOINT_RESET_AFTER_TEST=true
            ;;
        --checkpoint-allow-volume-backed)
            CHECKPOINT_ALLOW_VOLUME_BACKED=true
            ;;
        --checkpoint-verify-images)
            CHECKPOINT_VERIFY_IMAGES=true
            ;;
        --bake-baseline)
            BAKE_BASELINE=$2
            shift
            ;;
        --bake-skip-cleanup)
            BAKE_SKIP_CLEANUP=true
            ;;
        --deploy-from-baseline)
            DEPLOY_FROM_BASELINE=$2
            shift
            ;;
        --deploy-from-baseline-run-test)
            DEPLOY_FROM_BASELINE_RUN_TEST=true
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: invalid input '$1'"
            usage
            exit 1
            ;;
    esac
    shift
done

if [[ -n $CHECKPOINT_CREATE && -n $CHECKPOINT_RESTORE ]]; then
    echo "ERROR: --checkpoint-create and --checkpoint-restore are mutually exclusive" >&2
    exit 1
fi

if [[ -n $BAKE_BASELINE ]] && \
   [[ -n $CHECKPOINT_CREATE || -n $CHECKPOINT_RESTORE ]]; then
    echo "ERROR: --bake-baseline is mutually exclusive with --checkpoint-create/--checkpoint-restore" >&2
    exit 1
fi

if [[ -n $DEPLOY_FROM_BASELINE ]] && \
   [[ -n $CHECKPOINT_CREATE || -n $CHECKPOINT_RESTORE || -n $BAKE_BASELINE ]]; then
    echo "ERROR: --deploy-from-baseline is mutually exclusive with --checkpoint-*/--bake-baseline" >&2
    exit 1
fi

if $DEPLOY_FROM_BASELINE_RUN_TEST && [[ -z $DEPLOY_FROM_BASELINE ]]; then
    echo "ERROR: --deploy-from-baseline-run-test requires --deploy-from-baseline" >&2
    exit 1
fi

if $CHECKPOINT_RUN_TEST && [[ -z $CHECKPOINT_CREATE ]]; then
    echo "ERROR: --checkpoint-run-test requires --checkpoint-create" >&2
    exit 1
fi

# Install dependencies
which yq &>/dev/null || sudo snap install yq

# Ensure zosci-config checked out and up-to-date
get_and_update_repo https://github.com/openstack-charmers/zosci-config

TOOLS_PATH=$(realpath $(dirname $0))/func_test_tools
export CHARM_ROOT_PATH=$PWD

# Get commit we are running tests against.
COMMIT_ID=$(git -C $CHARM_ROOT_PATH rev-parse --short HEAD)
CHARM_NAME=$(awk '/^name: .+/{print $2}' metadata.yaml)

echo "Running functional tests for charm $CHARM_NAME commit $COMMIT_ID"
echo "Environment: juju-os-controller / myopenstack (Gazpacho)"

# If --zaza-template was provided, copy tests/, tox.ini and
# test-requirements.* from the template into the charm's test runner
# location (src/ for source charms, repo root for ops/classic). Files
# are removed at exit via trap. We copy (not symlink) because the
# MODIFY_BUNDLE_CONSTRAINTS block below mutates tests/bundles/*.yaml
# via `yq -i`, which would write through a symlink into the template.
if [[ -n $ZAZA_TEMPLATE ]]; then
    ZAZA_TEMPLATE=$(realpath "$ZAZA_TEMPLATE")
    [[ -d $ZAZA_TEMPLATE ]] || { echo "ERROR: --zaza-template '$ZAZA_TEMPLATE' is not a directory" >&2; exit 1; }

    if [[ -d $CHARM_ROOT_PATH/src ]]; then
        ZAZA_INJECT_DIR=$CHARM_ROOT_PATH/src
    else
        ZAZA_INJECT_DIR=$CHARM_ROOT_PATH
    fi

    declare -a ZAZA_INJECTED=()
    cleanup_zaza_template () {
        local p
        for p in "${ZAZA_INJECTED[@]:-}"; do
            rm -rf "$p"
        done
    }
    trap cleanup_zaza_template EXIT

    entries=( tests tox.ini )
    while IFS= read -r f; do
        entries+=( "$f" )
    done < <(cd "$ZAZA_TEMPLATE" && ls test-requirements* 2>/dev/null || true)
    for entry in "${entries[@]}"; do
        src_path=$ZAZA_TEMPLATE/$entry
        dst_path=$ZAZA_INJECT_DIR/$entry
        [[ -e $src_path ]] || continue
        if [[ -e $dst_path || -L $dst_path ]]; then
            echo "ERROR: $dst_path already exists; refusing to overwrite. Move or remove it first." >&2
            exit 1
        fi
        cp -a "$src_path" "$dst_path"
        ZAZA_INJECTED+=( "$dst_path" )
        echo "Injected $dst_path (from $src_path)"
    done
fi

# -------------------------------------------------------------------
# Network configuration for juju-os-controller
# -------------------------------------------------------------------
source $OPENRC

export {,TEST_}CIDR_EXT=$(openstack subnet show $OS_SUBNET -c cidr -f value)
export {,TEST_}NET_ID=$(openstack network show $OS_NETWORK -f value -c id)
export {,TEST_}GATEWAY=$(openstack subnet show $OS_SUBNET -c gateway_ip -f value)

# FIP range: use the allocation pool range from the subnet
# provider-subnet allocation pool: 192.168.2.100 - 192.168.2.250
FIP_RANGE_RAW=$(openstack subnet show $OS_SUBNET -f json -c allocation_pools | \
    python3 -c "import sys,json; pools=json.load(sys.stdin)['allocation_pools']; p=pools[0] if isinstance(pools,list) else pools; print(p['start']+':'+p['end'])" 2>/dev/null || true)
if [[ -z $FIP_RANGE_RAW ]]; then
    # Fallback: parse from yaml
    FIP_RANGE_RAW=$(openstack subnet show $OS_SUBNET -f yaml -c allocation_pools | \
        python3 -c "import sys,yaml; pools=yaml.safe_load(sys.stdin)['allocation_pools']; print(pools[0]['start']+':'+pools[0]['end'])")
fi
export {,TEST_}FIP_RANGE=$FIP_RANGE_RAW

# Setup VIPs needed by zaza tests.
allocate_zaza_vip ()
{
    local vip_id=$1
    local port_name="${VIP_PORT_PREFIX}-${vip_id}"
    local vip_addr

    vip_addr=$(openstack port show -c fixed_ips $port_name -f yaml 2>/dev/null | yq '.fixed_ips[0].ip_address') || true
    if [[ -z $vip_addr ]] || [[ $vip_addr == null ]]; then
        echo "Allocating new VIP port: $port_name" >&2
        local port_id
        port_id=$(openstack port create --network $OS_NETWORK $port_name -c id -f value)
        vip_addr=$(openstack port show -c fixed_ips $port_id -f yaml | yq '.fixed_ips[0].ip_address')
    fi
    echo $vip_addr
}

for ((i=2;i;i-=1)); do
    export {OS,TEST}_VIP0$((i-1))=$(allocate_zaza_vip 0$((i-1)))
done
echo "VIPs allocated: TEST_VIP00=$TEST_VIP00, TEST_VIP01=$TEST_VIP01"

export {,TEST_}NAME_SERVER=${TEST_NAME_SERVER:-8.8.8.8}
export {,TEST_}CIDR_PRIV=${TEST_CIDR_PRIV:-192.168.21.0/24}

# Model settings: config-drive is required for Gazpacho environment
# Set model-level default constraints so VMs get adequate resources.
# m1.tiny (1 vCPU/1GB) is too small for most charms — use at least 2 cores/4GB.
export TEST_MODEL_SETTINGS="image-stream=released;default-series=jammy;test-mode=true;transmit-vendor-metrics=false"
export TEST_MODEL_CONSTRAINTS="mem=4G;cores=2;root-disk=20G"
export TEST_JUJU3=1
export TEST_ZAZA_BUG_LP1987332=1

# Juju 3.x constraints file
juju_version=$(juju --version)
[[ $juju_version =~ 2.9.* ]] || export TEST_CONSTRAINTS_FILE=https://raw.githubusercontent.com/openstack-charmers/zaza/master/constraints-juju36.txt

LOGFILE=$(mktemp --suffix=-charm-func-test-results)
(
# 2. Build
if ! $SKIP_BUILD; then
    CHARMCRAFT_CHANNEL=$(grep charmcraft_channel osci.yaml | sed -r 's/.+:\s+(\S+)/\1/')
    sudo snap refresh charmcraft --channel ${CHARMCRAFT_CHANNEL:-"1.5/stable"}
    lxd init --auto || true
    tox -re build
elif [[ -n $REMOTE_BUILD ]]; then
    IFS=',' read -ra remote_build_params <<< "$REMOTE_BUILD"
    REMOTE_BUILD_DESTINATION=${remote_build_params[0]}
    REMOTE_BUILD_PATH=${remote_build_params[1]}
    ssh $REMOTE_BUILD_DESTINATION "cd $REMOTE_BUILD_PATH;git log -1;rm -rf *.charm;tox -re build"
    rm -rf *.charm
    rsync -vza $REMOTE_BUILD_DESTINATION:$REMOTE_BUILD_PATH/*.charm .
fi

# 3. Run functional tests.

if [[ -n $FUNC_TEST_PR ]]; then
    apply_func_test_pr $FUNC_TEST_PR
fi

declare -A func_target_state=()
declare -a func_target_order
if ((${#FUNC_TEST_TARGET[@]})); then
    for t in ${FUNC_TEST_TARGET[@]}; do
        func_target_state[$t]=null
        func_target_order+=( $t )
    done
else
    voting_targets=()
    non_voting_targets=()
    for target in $(python3 $TOOLS_PATH/identify_charm_func_test_jobs.py); do
        if $(python3 $TOOLS_PATH/test_is_voting.py $target); then
            voting_targets+=( $target )
        else
            non_voting_targets+=( $target )
        fi
    done
    for target in ${voting_targets[@]} ${non_voting_targets[@]}; do
        func_target_order+=( $target )
        func_target_state[$target]=null
    done
fi

if [[ -n $CHECKPOINT_CREATE && ${#func_target_order[@]} -ne 1 ]]; then
    echo "ERROR: --checkpoint-create requires exactly one --func-test-target" >&2
    exit 1
fi

if [[ -n $CHECKPOINT_RESTORE && ${#func_target_order[@]} -ne 1 ]]; then
    echo "ERROR: --checkpoint-restore requires exactly one --func-test-target" >&2
    exit 1
fi

if [[ -n $CHECKPOINT_RESTORE && -n $RERUN_PHASE ]]; then
    echo "ERROR: --checkpoint-restore cannot be combined with --rerun" >&2
    exit 1
fi

# Ensure nova-compute has enough resources to create vms in tests.
if $MODIFY_BUNDLE_CONSTRAINTS; then
    (
    [[ -d src ]] && cd src
    for f in tests/bundles/*.yaml; do
        if $(grep -q "nova-compute:" $f); then
            if [[ $(yq '.applications' $f) = null ]]; then
                yq -i '.services.nova-compute.constraints="root-disk=80G mem=8G"' $f
            else
                yq -i '.applications.nova-compute.constraints="root-disk=80G mem=8G"' $f
            fi
        fi
    done
    )
fi

if [[ -n $CHECKPOINT_RESTORE ]]; then
    fail=false
    run_checkpoint_restore_test "$CHECKPOINT_RESTORE" || fail=true
    for target in ${func_target_order[@]}; do
        if $fail; then
            func_target_state[$target]='fail'
        else
            func_target_state[$target]='success'
        fi
    done
fi

if [[ -n $RERUN_PHASE ]]; then
    fail=false
    [[ -d src ]] && pushd src &>/dev/null || true
    model=$(juju list-models| egrep -o "^zaza-\S+"|tr -d '*')
    echo "Re-running functest-$RERUN_PHASE (model=$model)"
    juju switch $model
    if ((${#FUNC_TEST_TARGET[@]} == 1)); then
        bundle=${FUNC_TEST_TARGET[0]}
    else
        bundle=
    fi
    run_test_phase $RERUN_PHASE $model $bundle
    popd
fi

first=true
init_noop_target=true
for target in ${func_target_order[@]}; do
    [[ -z $RERUN_PHASE && -z $CHECKPOINT_RESTORE ]] || continue

    destroy_zaza_models

    if [[ -n $CHECKPOINT_CREATE ]]; then
        [[ -d src ]] && pushd src &>/dev/null || true
        fail=false
        run_checkpoint_create_flow "$target" "$init_noop_target" || fail=true
        popd &>/dev/null || true
        init_noop_target=false
        if $fail; then
            func_target_state[$target]='fail'
        else
            func_target_state[$target]='success'
        fi
        continue
    fi

    if [[ -n $BAKE_BASELINE ]]; then
        [[ -d src ]] && pushd src &>/dev/null || true
        fail=false
        run_bake_baseline_flow "$target" "$init_noop_target" || fail=true
        popd &>/dev/null || true
        init_noop_target=false
        if $fail; then
            func_target_state[$target]='fail'
        else
            func_target_state[$target]='success'
        fi
        continue
    fi

    if [[ -n $DEPLOY_FROM_BASELINE ]]; then
        [[ -d src ]] && pushd src &>/dev/null || true
        fail=false
        run_deploy_from_baseline_flow "$target" "$init_noop_target" || fail=true
        popd &>/dev/null || true
        init_noop_target=false
        if $fail; then
            func_target_state[$target]='fail'
        else
            func_target_state[$target]='success'
        fi
        continue
    fi

    if $first; then
        first=false
        tox_args="-re func-target"
    else
        tox_args="-e func-target"
    fi
    [[ -d src ]] && pushd src &>/dev/null || true
    fail=false
    _target="$(python3 $TOOLS_PATH/extract_job_target.py $target)"
    if ! $MANUAL_FUNCTESTS; then
        tox ${tox_args} -- $_target || fail=true
        model=$(juju list-models| egrep -o "^zaza-\S+"|tr -d '*')
    else
        $TOOLS_PATH/manual_functests_runner.sh "$_target" $SLEEP $init_noop_target || fail=true
        model=test-$target
        init_noop_target=false
    fi

    $fail && retry_on_fail "$model" "$target" && fail=false
    if $fail; then
        func_target_state[$target]='fail'
    else
        func_target_state[$target]='success'
    fi

    if $WAIT_ON_DESTROY; then
        read -p "Destroy model '$model' and run next test? [ENTER]"
    fi

    destroy_zaza_models
done
popd &>/dev/null || true

# Report results
echo -e "\nTest results for charm $CHARM_NAME functional tests @ commit $COMMIT_ID:"
for target in ${func_target_order[@]}; do
    if $(python3 $TOOLS_PATH/test_is_voting.py $target); then
        voting_info=""
    else
        voting_info=" (non-voting)"
    fi

    if [[ ${func_target_state[$target]} = null ]]; then
        echo "  * $target: SKIPPED$voting_info"
    elif [[ ${func_target_state[$target]} = success ]]; then
        echo "  * $target: SUCCESS$voting_info"
    else
        echo "  * $target: FAILURE$voting_info"
    fi
done
) 2>&1 | tee $LOGFILE
echo -e "\nResults also saved to $LOGFILE"
