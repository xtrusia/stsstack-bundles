#!/bin/bash
# Run several charm func-test lanes in parallel on the myopenstack (Gazpacho)
# cloud. Each lane gets its own Juju controller, so the runner's --parallel
# cleanup only ever touches that lane's own instances. The runner serializes the
# charmcraft build with a flock (CPU-heavy builds otherwise starve sibling lanes'
# libjuju event loops and drop their controller websockets); deploy and test
# phases overlap freely.
#
# Usage: parallel_functest_driver.sh <lanes-file>
# Each non-empty, non-'#' line of <lanes-file>:
#     <name> <charmdir> <controller> <juju-data-dir> <func-test-pr> [target]
#   name         label; per-lane log at ~/parallel-<name>.log
#   charmdir     charm checkout to run from (use a separate worktree per lane)
#   controller   Juju controller name (bootstrapped via myopenstack_bootstrap.sh
#                if not already registered in juju-data-dir)
#   juju-data-dir  isolated JUJU_DATA for this lane
#   func-test-pr   zaza PR number, or '-' for none
#   target       optional bundle; omit or '-' to run the full default set
set -u
LANES_FILE=${1:?usage: parallel_functest_driver.sh <lanes-file>}
HERE=$(realpath "$(dirname "$0")")
RUNNER="$HERE/charmed_openstack_functest_runner_juju_os.sh"
BOOTSTRAP="$HERE/func_test_tools/myopenstack_bootstrap.sh"
STAGGER=${STAGGER:-30}

run_lane() {
    local name=$1 charmdir=$2 controller=$3 jd=$4 pr=$5 target=${6:-}
    local log="$HOME/parallel-$name.log"
    : > "$log"
    if ! JUJU_DATA="$jd" juju show-controller "$controller" >/dev/null 2>&1; then
        echo "### [$name] bootstrapping $controller" >> "$log"
        if ! "$BOOTSTRAP" "$controller" "$jd" >> "$log" 2>&1; then
            echo "### [$name] BOOTSTRAP FAILED" >> "$log"; return 1
        fi
    fi
    cd "$charmdir" || { echo "### [$name] no charmdir $charmdir" >> "$log"; return 1; }
    local args=(--parallel --no-wait)
    [ "$pr" != "-" ] && args+=(--func-test-pr "$pr")
    [ -n "$target" ] && [ "$target" != "-" ] && args+=(--func-test-target "$target")
    JUJU_DATA="$jd" juju switch "$controller" >/dev/null 2>&1
    { echo "### LANE $name START $(date '+%F %T') controller=$controller pr=$pr target=${target:-<all>}"
      echo "### HEAD: $(git -C "$charmdir" log --oneline -1 2>/dev/null)"; } >> "$log"
    JUJU_DATA="$jd" "$RUNNER" "${args[@]}" < /dev/null >> "$log" 2>&1
    local rc=$?
    local rf; rf=$(grep -oaE '/tmp/tmp\.[A-Za-z0-9]+-charm-func-test-results' "$log" | tail -1)
    [ -n "$rf" ] && [ -f "$rf" ] && cp "$rf" "$HOME/parallel-$name-result.txt"
    echo "### LANE $name END $(date '+%F %T') rc=$rc" >> "$log"
}

pids=()
while read -r name charmdir controller jd pr target _; do
    [ -z "${name:-}" ] && continue
    case "$name" in \#*) continue ;; esac
    run_lane "$name" "$charmdir" "$controller" "$jd" "$pr" "$target" &
    pids+=("$!")
    sleep "$STAGGER"
done < "$LANES_FILE"

echo "Launched ${#pids[@]} lane(s); waiting for completion..."
fail=0
for p in "${pids[@]}"; do wait "$p" || fail=$((fail+1)); done
echo "All lanes finished ($fail non-zero). Per-lane logs: ~/parallel-<name>.log"
for f in "$HOME"/parallel-*-result.txt; do
    [ -f "$f" ] && echo "  $(basename "$f"): $(grep -aoE '\* [a-z0-9-]+: (SUCCESS|FAILURE)' "$f" | paste -sd, )"
done
