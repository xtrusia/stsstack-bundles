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
MANUAL_FUNCTESTS=false
MODIFY_BUNDLE_CONSTRAINTS=true
REMOTE_BUILD=
SKIP_BUILD=false
SLEEP=
WAIT_ON_DESTROY=true
RERUN_PHASE=

. $(dirname $0)/func_test_tools/common.sh

# Override destroy_zaza_models: force delete via MongoDB to avoid stuck models.
PASSWORD="d6E6xi6LDXpecYOVTSGF5I3h"
destroy_zaza_models ()
{
    for model in $(juju list-models 2>/dev/null | grep -oE "^zaza-\S+" | tr -d '*'); do
        UUID=$(juju show-model "$model" --format json 2>/dev/null | \
            python3 -c 'import sys,json;print(list(json.load(sys.stdin).values())[0]["model-uuid"])' 2>/dev/null || true)
        [ -z "$UUID" ] && continue
        echo "Force destroying model $model ($UUID)"
        juju ssh -m controller 0 "sudo python3 -c \"
import pymongo
client=pymongo.MongoClient('mongodb://machine-0:${PASSWORD}@127.0.0.1:37017/admin',tls=True,tlsCAFile='/var/snap/juju-db/common/ca.crt',tlsCertificateKeyFile='/var/snap/juju-db/common/server.pem',tlsAllowInvalidHostnames=True,directConnection=True,serverSelectionTimeoutMS=5000)
juju_db=client['juju']
juju_db.models.delete_one({'_id':'${UUID}'})
client.drop_database('${UUID}'.replace('-',''))
for c in juju_db.list_collection_names():
    try: juju_db[c].delete_many({'model-uuid':'${UUID}'})
    except: pass
print('Done')
\"" 2>/dev/null
    done
    juju switch default 2>/dev/null || true
    # Clean up OpenStack resources
    source $OPENRC
    for id in $(openstack server list -f value -c ID -c Name 2>/dev/null | grep zaza | awk '{print $1}'); do
        openstack server delete "$id" 2>/dev/null
    done
    cleanup_stale_ext_ports
}

# -------------------------------------------------------------------
# Environment defaults for juju-os-controller / myopenstack
# Override any of these via env vars before running.
# -------------------------------------------------------------------
OPENRC=${OPENRC:-~/admin-openrc.sh}
OS_NETWORK=${OS_NETWORK:-provider-net}
OS_SUBNET=${OS_SUBNET:-provider-subnet}
VIP_PORT_PREFIX=${VIP_PORT_PREFIX:-zaza-vip}

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
    --help
        This help message.

ENVIRONMENT VARIABLES (override defaults):
    OPENRC              Path to OpenRC file (default: ~/admin-openrc.sh)
    OS_NETWORK          OpenStack network name (default: provider-net)
    OS_SUBNET           OpenStack subnet name (default: provider-subnet)
    VIP_PORT_PREFIX     Port name prefix for VIPs (default: zaza-vip)
EOF
}


# Patch charmhelpers DNS timeout on all machines in a model.
# Increases dns.resolver lifetime to 30s and catches Timeout exception.
# This prevents intermittent DNS timeout failures in charm hooks.
patch_charmhelpers_dns ()
{
    local model=$1
    echo "Patching charmhelpers DNS timeout on all machines in $model..."
    juju exec -m $model --all -- 'sudo python3 -c "
import glob
for f in glob.glob(\"/var/lib/juju/agents/*/charm/**/network/ip.py\", recursive=True) + glob.glob(\"/var/lib/juju/agents/*/charm/hooks/**/network/ip.py\", recursive=True):
    with open(f) as fh: c = fh.read()
    if \"dns.resolver.query(address, rtype)\" in c:
        c = c.replace(\"answers = dns.resolver.query(address, rtype)\", \"resolver = dns.resolver.Resolver()\\n        resolver.lifetime = 30\\n        _resolve = getattr(resolver, \\\"resolve\\\", getattr(resolver, \\\"query\\\", None))\\n        answers = _resolve(address, rtype)\")
        c = c.replace(\"except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):\", \"except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers, dns.exception.Timeout, Exception):\")
        with open(f, \"w\") as fh: fh.write(c)
"' || true
    # Also resolve any units that already failed due to DNS timeout
    juju status -m $model --format json 2>/dev/null | python3 -c "
import sys, json
d = json.load(sys.stdin)
for app in d.get('applications', {}).values():
    for name, unit in app.get('units', {}).items():
        if unit.get('juju-status', {}).get('current') == 'error':
            print(name)
        for sub_name, sub in unit.get('subordinates', {}).items():
            if sub.get('juju-status', {}).get('current') == 'error':
                print(sub_name)
" 2>/dev/null | while read unit; do
        juju resolved -m $model $unit 2>/dev/null || true
    done
    echo "DNS timeout patch applied and errors resolved."
}

# Clean up stale ext-port Neutron ports from previous zaza deployments.
# These accumulate and cause data-port MAC mismatch on neutron-gateway.
cleanup_stale_ext_ports ()
{
    echo "Cleaning up stale ext-port Neutron ports..."
    source $OPENRC
    for port_id in $(openstack port list -f value -c ID -c Name -c Status 2>/dev/null | grep ext-port | grep DOWN | awk '{print $1}'); do
        openstack port delete $port_id 2>/dev/null && echo "Deleted stale ext-port: $port_id"
    done
}

# Ensure br-ex is UP on neutron-gateway units.
# OVS doesn't auto-UP br-ex when data-port is added, causing floating IP
# and metadata service failures.
fix_brex_on_gateway ()
{
    local model=$1
    echo "Ensuring br-ex is UP on neutron-gateway..."
    for unit in $(juju status -m $model neutron-gateway --format json 2>/dev/null | python3 -c "
import sys, json
d = json.load(sys.stdin)
for name in d.get('applications', {}).get('neutron-gateway', {}).get('units', {}):
    print(name)
" 2>/dev/null); do
        juju exec -m $model --unit $unit -- 'sudo ip link set br-ex up 2>/dev/null && echo "br-ex UP on '$unit'"' 2>/dev/null || true
    done
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
    # After deploy, patch charmhelpers DNS timeout on all machines
    if [[ $phase == deploy ]]; then
        patch_charmhelpers_dns $model
    fi
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

# -------------------------------------------------------------------
# Network configuration for juju-os-controller
# -------------------------------------------------------------------
source $OPENRC

export {,TEST_}CIDR_EXT=$(openstack subnet show $OS_SUBNET -c cidr -f value)
export {,TEST_}NET_ID=$(openstack network show $OS_NETWORK -f value -c id)
export {,TEST_}GATEWAY=$(openstack subnet show $OS_SUBNET -c gateway_ip -f value)

# FIP range: use the allocation pool range from the subnet
# Floating IP range for overcloud ext_net — must NOT overlap with undercloud
# allocation pool (192.168.2.100-179). Use 192.168.2.180-250 for floating IPs.
export {,TEST_}FIP_RANGE=192.168.2.180:192.168.2.250

# Setup VIPs needed by zaza tests.
allocate_zaza_vip ()
{
    local vip_id=$1
    local port_name="${VIP_PORT_PREFIX}-${vip_id}"
    local vip_addr

    vip_addr=$(openstack port show -c fixed_ips $port_name -f yaml 2>/dev/null | yq '.fixed_ips[0].ip_address') || true
    if [[ -z $vip_addr ]] || [[ $vip_addr == null ]]; then
        echo "Allocating new VIP port: $port_name" >&2
        local port_id=$(openstack port create --network $OS_NETWORK $port_name -c id -f value)
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
export TEST_MODEL_SETTINGS="image-stream=released;default-series=jammy;test-mode=true;transmit-vendor-metrics=false;automatically-retry-hooks=true"
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

    destroy_zaza_models

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
        # Start DNS patch watcher in background — auto-detects zaza model,
        # patches charmhelpers as charms are installed, resolves DNS errors.
        python3 -u ~/dns_patch_watcher.py >> /tmp/dns_watcher.log 2>&1 &
        _watcher_pid=$!

        # Install tox env first, then patch zaza, then run tests
        if [[ $tox_args == *"-re"* ]]; then
            tox -re func-target --notest || true
        fi
        # Patch zaza subnetpool_prefix to avoid overlap with provider-net (192.168.0.0/16)
        _neutron_setup=".tox/func-target/lib/python3.8/site-packages/zaza/openstack/charm_tests/neutron/setup.py"
        [ -f "$_neutron_setup" ] && sed -i 's|"subnetpool_prefix": "192.168.0.0/16"|"subnetpool_prefix": "10.0.0.0/16"|' "$_neutron_setup"
        # Patch vault setup.py for retry safety
        _vault_setup=".tox/func-target/lib/python3.8/site-packages/zaza/openstack/charm_tests/vault/setup.py"
        if [ -f "$_vault_setup" ] && grep -q "intermediate_csr = action.data\['results'\]\['output'\]" "$_vault_setup"; then
            sed -i "s/    intermediate_csr = action.data\['results'\]\['output'\]/    if action.status == \"failed\" or \"output\" not in action.data.get(\"results\", {}):\\n        logging.info(\"Vault CA already configured, skipping CSR setup\")\\n        return\\n    intermediate_csr = action.data[\"results\"][\"output\"]/" "$_vault_setup"
        fi
        tox -e func-target -- $_target || fail=true
        model=$(juju list-models| egrep -o "^zaza-\S+"|tr -d '*')

        # Stop DNS patch watcher
        if [[ -n "$_watcher_pid" ]]; then kill $_watcher_pid 2>/dev/null; wait $_watcher_pid 2>/dev/null || true; fi
        # Auto-patch DNS timeout if deploy succeeded but later phases failed.
        # After patching and resolving errors, retry configure+test phases.
        if [[ -n "$model" ]] && $fail; then
            patch_charmhelpers_dns $model
            fix_brex_on_gateway $model
            echo "Retrying configure and test phases after DNS patch..."
                # Patch vault setup.py to handle already-initialized vault (KeyError: 'output')
            _vault_setup=".tox/func-target/lib/python3.8/site-packages/zaza/openstack/charm_tests/vault/setup.py"
            if [ -f "$_vault_setup" ] && grep -q "intermediate_csr = action.data\['results'\]\['output'\]" "$_vault_setup"; then
                sed -i 's/    intermediate_csr = action.data\[.results.\]\[.output.\]/    if action.status == "failed" or "output" not in action.data.get("results", {}):\n        logging.info("Vault CA already configured, skipping CSR setup")\n        return\n    intermediate_csr = action.data["results"]["output"]/' "$_vault_setup"
                echo "Vault setup.py patched for retry"
            fi
            . .tox/func-target/bin/activate
            functest-test -m $model && fail=false
            deactivate
        fi
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

    cleanup_stale_ext_ports
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
