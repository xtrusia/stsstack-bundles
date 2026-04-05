#!/bin/bash -eu
#
# Run Charmed Openstack CI tests manually in a similar way to how they are run
# by OpenStack CI (OSCI) -- adapted for MAAS-based Juju cloud environments.
#
# This is a variant of charmed_openstack_functest_runner.sh for environments
# where MAAS is registered as a Juju cloud (instead of OpenStack).
#
# Since MAAS is already a Juju cloud (bootstrapped, models available), we
# only need the network parameters that zaza tests require. No OpenStack CLI
# or MAAS CLI is needed.
#
# Usage: clone/fetch charm to test and run from within charm root dir.
#
#   Example:
#     ./charmed_openstack_functest_runner_maas.sh \
#         --cidr 10.0.0.0/24 \
#         --gateway 10.0.0.1 \
#         --vip00 10.0.0.200 \
#         --vip01 10.0.0.201 \
#         --func-test-target jammy-antelope
#
FUNC_TEST_PR=
FUNC_TEST_TARGET=()
MANUAL_FUNCTESTS=false
MODIFY_BUNDLE_CONSTRAINTS=true
REMOTE_BUILD=
SKIP_BUILD=false
SLEEP=
WAIT_ON_DESTROY=true
RERUN_PHASE=

# Network settings (required)
OPT_CIDR_EXT=
OPT_GATEWAY=
OPT_VIP00=
OPT_VIP01=
OPT_FIP_RANGE=
OPT_NET_ID=

# Bundle patching
declare -a BUNDLE_PATCHES=()
REMOVE_STORAGE=false

# Model config
declare -a EXTRA_MODEL_CONFIGS=()
FIX_APT_SOURCES=false

. $(dirname $0)/func_test_tools/common.sh

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
        exit)
            return 1
        ;;
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
        *)
            echo "ERROR: unrecognised phase name '$phase'"
            exit 1
        ;;
    esac
    return $ret
}

usage () {
    cat << EOF
USAGE: $(basename $0) OPTIONS

Run OpenStack charms functional tests on a MAAS-based Juju cloud environment.
This is a variant of charmed_openstack_functest_runner.sh for environments
where MAAS is registered as a Juju cloud.

REQUIRED NETWORK OPTIONS:
    --cidr CIDR
        External subnet CIDR (e.g. 10.0.0.0/24).
    --gateway IP
        Gateway IP address (e.g. 10.0.0.1).
    --vip00 IP
        First VIP address for zaza tests.
    --vip01 IP
        Second VIP address for zaza tests.

OPTIONAL NETWORK OPTIONS:
    --fip-range MIN:MAX
        Floating IP range (e.g. 10.0.0.210:10.0.0.250).
        If not specified, auto-computed from --cidr.
    --net-id ID
        Network/space ID (optional, depends on charm tests).

BUNDLE PATCHING OPTIONS:
    --patch-bundle 'YQ_EXPRESSION'
        Apply a yq expression to all test bundles before deployment.
        Can be specified multiple times. Patches are applied in order.
        Examples:
          Remove storage from ceph-osd:
            --patch-bundle 'del(.applications.ceph-osd.storage)'
          Add MAAS tag constraint to ceph-osd:
            --patch-bundle '.applications.ceph-osd.constraints="tags=compute"'
          Change num_units:
            --patch-bundle '.applications.ceph-osd.num_units=1'
    --remove-storage
        Remove all storage directives from all applications in test bundles.
        This is a shortcut for the common MAAS "does not support dynamic
        storage" error.

MODEL CONFIG OPTIONS:
    --model-config KEY=VALUE
        Add extra Juju model config. Can be specified multiple times.
        These are appended to TEST_MODEL_SETTINGS before zaza runs.
        Example: --model-config logging-config="<root>=DEBUG"
    --fix-apt-sources
        Fix duplicate apt sources on Noble (24.04) MAAS machines. Injects
        cloud-init userdata that truncates /etc/apt/sources.list when
        /etc/apt/sources.list.d/ubuntu.sources exists (DEB822 format).

OTHER OPTIONS:
    --func-test-target TARGET_NAME
        Provide the name of a specific test target to run. If none provided
        all tests are run based on what is defined in osci.yaml i.e. will do
        what osci would do by default. This option can be provided more than
        once.
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
        The destination needs to be prepared for the build and authorized for
        ssh. Implies --skip-build. Specify parameter as <destination>,<path>.
        Example: --remote-build ubuntu@10.171.168.1,~/git/charm-nova-compute
    --rerun deploy|configure|test
        Re-run a specific phase. This is useful if the deployment is part of a
        test run that failed, perhaps because of an infra issue, but can safely
        continue where it left off.
    --skip-build
        Skip building charm if already done to save time.
    --skip-modify-bundle-constraints
        By default we modify test bundle constraints to ensure that applications
        have the resources they need. For example nova-compute needs to have
        enough capacity to boot the vms required by the tests.
    --sleep TIME_SECS
        Specify amount of seconds to sleep between functest steps.
    --help
        This help message.
EOF
}

while (($# > 0)); do
    case "$1" in
        --debug)
            set -x
            ;;
        --cidr)
            OPT_CIDR_EXT=$2
            shift
            ;;
        --gateway)
            OPT_GATEWAY=$2
            shift
            ;;
        --vip00)
            OPT_VIP00=$2
            shift
            ;;
        --vip01)
            OPT_VIP01=$2
            shift
            ;;
        --fip-range)
            OPT_FIP_RANGE=$2
            shift
            ;;
        --net-id)
            OPT_NET_ID=$2
            shift
            ;;
        --patch-bundle)
            BUNDLE_PATCHES+=( "$2" )
            shift
            ;;
        --remove-storage)
            REMOVE_STORAGE=true
            ;;
        --model-config)
            EXTRA_MODEL_CONFIGS+=( "$2" )
            shift
            ;;
        --fix-apt-sources)
            FIX_APT_SOURCES=true
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

# --- Validate required network options ---
missing=()
[[ -n "$OPT_CIDR_EXT" ]] || missing+=("--cidr")
[[ -n "$OPT_GATEWAY" ]]  || missing+=("--gateway")
[[ -n "$OPT_VIP00" ]]    || missing+=("--vip00")
[[ -n "$OPT_VIP01" ]]    || missing+=("--vip01")

if ((${#missing[@]} > 0)); then
    echo "ERROR: missing required network options: ${missing[*]}"
    echo ""
    usage
    exit 1
fi

# Install dependencies
which yq &>/dev/null || sudo snap install yq
which ipcalc &>/dev/null || sudo apt-get install -y ipcalc

# Ensure zosci-config checked out and up-to-date
get_and_update_repo https://github.com/openstack-charmers/zosci-config

TOOLS_PATH=$(realpath $(dirname $0))/func_test_tools
# This is used generally to identify the charm root.
export CHARM_ROOT_PATH=$PWD

# Get commit we are running tests against.
COMMIT_ID=$(git -C $CHARM_ROOT_PATH rev-parse --short HEAD)
CHARM_NAME=$(awk '/^name: .+/{print $2}' metadata.yaml)

echo "Running functional tests for charm $CHARM_NAME commit $COMMIT_ID (MAAS cloud mode)"

# --- Network setup ---
export {,TEST_}CIDR_EXT="$OPT_CIDR_EXT"
export {,TEST_}GATEWAY="$OPT_GATEWAY"
export {OS,TEST}_VIP00="$OPT_VIP00"
export {OS,TEST}_VIP01="$OPT_VIP01"

# Auto-compute FIP range from CIDR if not provided
if [[ -n "$OPT_FIP_RANGE" ]]; then
    export {,TEST_}FIP_RANGE="$OPT_FIP_RANGE"
else
    FIP_MAX=$(ipcalc "$OPT_CIDR_EXT"| awk '$1=="HostMax:" {print $2}')
    FIP_MIN=$(ipcalc "$OPT_CIDR_EXT"| awk '$1=="HostMin:" {print $2}')
    FIP_MIN_ABC=${FIP_MIN%.*}
    FIP_MIN_D=${FIP_MIN##*.}
    FIP_MIN=${FIP_MIN_ABC}.$(($FIP_MIN_D + 64))
    export {,TEST_}FIP_RANGE=$FIP_MIN:$FIP_MAX
fi

if [[ -n "$OPT_NET_ID" ]]; then
    export {,TEST_}NET_ID="$OPT_NET_ID"
fi

echo "Network configuration:"
echo "  CIDR_EXT:   $CIDR_EXT"
echo "  GATEWAY:    $GATEWAY"
echo "  VIP00:      $OS_VIP00"
echo "  VIP01:      $OS_VIP01"
echo "  FIP_RANGE:  $FIP_RANGE"
echo "  NET_ID:     ${NET_ID:-(not set)}"

export {,TEST_}NAME_SERVER=${TEST_NAME_SERVER:-91.189.91.131}
export {,TEST_}CIDR_PRIV=${TEST_CIDR_PRIV:-192.168.21.0/24}
TEST_MODEL_SETTINGS="image-stream=released;default-series=jammy;test-mode=true;transmit-vendor-metrics=false"

# Fix apt sources on Noble/MAAS machines.
# On MAAS-provisioned Noble machines, /etc/apt/sources.list may have stale
# entries (archive.ubuntu.com) while ubuntu.sources has the correct mirror.
# We write a .zaza.yaml with cloudinit-userdata to fix this before charm hooks.
if $FIX_APT_SOURCES; then
    echo "Configuring .zaza.yaml with apt fix cloudinit-userdata"
    ZAZA_YAML="$HOME/.zaza.yaml"
    # Back up existing .zaza.yaml if present
    if [[ -f "$ZAZA_YAML" ]]; then
        cp "$ZAZA_YAML" "${ZAZA_YAML}.bak.$$"
        echo "  Backed up existing $ZAZA_YAML to ${ZAZA_YAML}.bak.$$"
    fi
    cat > "$ZAZA_YAML" << 'ZAZAEOF'
model_settings:
  cloudinit-userdata: |
    #cloud-config
    preruncmd:
      - truncate -s 0 /etc/apt/sources.list
      - rm -rf /var/lib/apt/lists/*
      - apt-get update -y
ZAZAEOF
    echo "  Written $ZAZA_YAML"
fi

# Append any extra model configs
for mc in "${EXTRA_MODEL_CONFIGS[@]}"; do
    TEST_MODEL_SETTINGS="${TEST_MODEL_SETTINGS};${mc}"
done

export TEST_MODEL_SETTINGS
echo "Model settings: $TEST_MODEL_SETTINGS"

# We need to set TEST_JUJU3 as well as the constraints file
# Ref: https://github.com/openstack-charmers/zaza/blob/e96ab098f00951079fccb34bc38d4ae6ebb38606/setup.py#L47
export TEST_JUJU3=1

# NOTE: this should not be necessary for > juju 2.x but since we still have a need for it we add it in
export TEST_ZAZA_BUG_LP1987332=1

# Some charms point to an upstream constraints file that installs python-libjuju 2.x so we need to do this to ensure we get 3.x
# NOTE: we only do this if we are using Juju >= 3.x
juju_version=$(juju --version)
[[ $juju_version =~ 2.9.* ]] || export TEST_CONSTRAINTS_FILE=https://raw.githubusercontent.com/openstack-charmers/zaza/master/constraints-juju36.txt

LOGFILE=$(mktemp --suffix=-charm-func-test-results)
(
# 2. Build
if ! $SKIP_BUILD; then
    # default value is 1.5/stable, assumed that later charm likely have charmcraft_channel value
    CHARMCRAFT_CHANNEL=$(grep charmcraft_channel osci.yaml | sed -r 's/.+:\s+(\S+)/\1/')
    sudo snap refresh charmcraft --channel ${CHARMCRAFT_CHANNEL:-"1.5/stable"}

    # ensure lxc initialised
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

# If a func test pr is provided switch to that pr.
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
    # Ensure voting targets processed first.
    for target in ${voting_targets[@]} ${non_voting_targets[@]}; do
        func_target_order+=( $target )
        func_target_state[$target]=null
    done
fi

# Ensure nova-compute has enough resources to create vms in tests. Not all
# charms have bundles with constraints set so we need to cover both cases here.
if $MODIFY_BUNDLE_CONSTRAINTS; then
    (
    [[ -d src ]] && cd src
    for f in tests/bundles/*.yaml; do
        # Dont do this if the test does not have nova-compute
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

# --- MAAS bundle patching ---
# Remove all storage directives if requested (MAAS does not support dynamic storage).
if $REMOVE_STORAGE; then
    (
    [[ -d src ]] && cd src
    for f in tests/bundles/*.yaml; do
        # Remove .applications.*.storage and .services.*.storage
        if [[ $(yq '.applications' $f) != null ]]; then
            for app in $(yq '.applications | keys | .[]' $f); do
                if [[ $(yq ".applications.$app.storage" $f) != null ]]; then
                    echo "  [patch] Removing storage from applications.$app in $f"
                    yq -i "del(.applications.$app.storage)" $f
                fi
            done
        fi
        if [[ $(yq '.services' $f) != null ]]; then
            for app in $(yq '.services | keys | .[]' $f); do
                if [[ $(yq ".services.$app.storage" $f) != null ]]; then
                    echo "  [patch] Removing storage from services.$app in $f"
                    yq -i "del(.services.$app.storage)" $f
                fi
            done
        fi
    done
    )
fi

# Apply user-provided yq patches to all test bundles.
if ((${#BUNDLE_PATCHES[@]} > 0)); then
    (
    [[ -d src ]] && cd src
    for f in tests/bundles/*.yaml; do
        for patch in "${BUNDLE_PATCHES[@]}"; do
            echo "  [patch] Applying '$patch' to $f"
            yq -i "$patch" $f
        done
    done
    )
fi

if [[ -n $RERUN_PHASE ]]; then
    fail=false
    [[ -d src ]] && pushd src &>/dev/null || true
    model=$(juju list-models| egrep -o "^zaza-\S+"|tr -d '*')
    echo "Re-running functest-$RERUN_PHASE (model=$model)"
    juju switch $model
    ((${#FUNC_TEST_TARGET[@]}==1)) && bundle=${FUNC_TEST_TARGET[0]} || bundle=
    run_test_phase $RERUN_PHASE $model $bundle
    popd
fi

first=true
init_noop_target=true
for target in ${func_target_order[@]}; do
    [[ -z $RERUN_PHASE ]] || continue

    # Destroy any existing zaza models to ensure we have all the resources we
    # need.
    destroy_zaza_models

    # Only rebuild on first run.
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

    # Cleanup before next run
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
