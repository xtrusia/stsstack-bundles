#!/usr/bin/env python3
"""OpenStack-backed Juju model checkpoint proof of concept.

This tool is intentionally not a Juju feature.  It is a narrow PoC for
OpenStack-based lab environments where we can validate checkpoint semantics
before deciding whether a real Juju provider-level implementation is worth
building.

The first implementation only supports in-place rollback:

* checkpoint a non-controller Juju model when units are idle
* snapshot each model machine's Nova server into a Glance image
* rebuild the same Nova servers from those images during restore

It does not clone a model and it does not restore Juju controller database
state.  Restore is refused unless the current model UUID and machine
instance IDs still match the checkpoint manifest.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from typing import Any

import yaml


MANIFEST = "manifest.json"
DEFAULT_CHECKPOINT_DIR = "~/.local/share/juju-model-checkpoints"
JUJU_CMD = os.environ.get("JUJU_CMD", "juju")
OPENSTACK_CMD = os.environ.get("OPENSTACK_CMD", "openstack")

# Cleanup commands run via ssh inside each machine before baking a baseline
# image. The goal is to remove the prior juju agent and reset cloud-init so
# the snapshot can be reused as a fresh boot image by a new juju model.
SSH_CLEANUP_SCRIPT = """\
set -u
sudo pkill -9 -f jujud 2>/dev/null || true
sudo rm -rf /var/lib/juju /var/log/juju
sudo rm -f /etc/systemd/system/jujud-machine-*.service
sudo rm -f /etc/systemd/system/multi-user.target.wants/jujud-machine-*.service
sudo systemctl daemon-reload || true
sudo cloud-init clean --logs --seed
sudo sync
"""


class CheckpointError(RuntimeError):
    """Raised when a checkpoint operation cannot continue."""


def run(cmd: list[str], *, json_output: bool = False) -> Any:
    """Run a command and optionally parse JSON output."""
    proc = subprocess.run(
        cmd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode:
        joined = " ".join(cmd)
        raise CheckpointError(
            f"command failed ({proc.returncode}): {joined}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )
    if json_output:
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise CheckpointError(
                f"failed to parse JSON from {' '.join(cmd)}: {exc}"
            ) from exc
    return proc.stdout


def utc_timestamp() -> str:
    """Return a sortable UTC timestamp."""
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )


def slug(value: str) -> str:
    """Return a conservative identifier for filenames and image names."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return cleaned.strip("-") or "model"


def checkpoint_root(path: str | None) -> pathlib.Path:
    """Return the directory used to store checkpoint metadata."""
    raw_path = path or os.environ.get(
        "JUJU_MODEL_CHECKPOINT_DIR", DEFAULT_CHECKPOINT_DIR
    )
    return pathlib.Path(raw_path).expanduser()


def ensure_commands() -> None:
    """Ensure required command line tools are available."""
    missing = [
        name for name in (JUJU_CMD, OPENSTACK_CMD) if not shutil.which(name)
    ]
    if missing:
        raise CheckpointError(
            f"missing required command(s): {', '.join(missing)}"
        )


def juju_status(model: str) -> dict[str, Any]:
    """Return Juju status for a model."""
    return run(
        [JUJU_CMD, "status", "-m", model, "--format=json"],
        json_output=True,
    )


def juju_model_info(model: str) -> dict[str, Any]:
    """Return Juju show-model information for a model."""
    raw = run([JUJU_CMD, "show-model", "--format=json", model],
              json_output=True)
    if not raw:
        raise CheckpointError(f"juju show-model returned no data for {model}")
    return next(iter(raw.values()))


# Charms whose state survives a destroy/restore cycle but whose distributed
# service does NOT auto-recover after every node is power-cycled at the same
# time. These need an explicit "I am rebooting from a complete outage" admin
# action before the cluster re-forms.
#
# Value is (action_name, target_strategy):
#   "leader"   — try the leader unit only
#   "any-unit" — try each deployed unit in turn, stop on the first success.
#                Needed for mysql-innodb-cluster because the action must be
#                run on the unit that holds the most up-to-date GTID set,
#                which is not always the juju leader.
KNOWN_POST_RESTORE_ACTIONS: dict[str, tuple[str, str]] = {
    "mysql-innodb-cluster": (
        "reboot-cluster-from-complete-outage", "any-unit",
    ),
}


def juju_run_action(
    model: str,
    target: str,
    action_name: str,
    *,
    wait: str = "10m",
) -> dict[str, Any]:
    """Run a Juju action and return the parsed result map."""
    return run(
        [
            JUJU_CMD, "run", "-m", model,
            target, action_name,
            "--wait", wait,
            "--format", "json",
        ],
        json_output=True,
    )


def detect_post_restore_actions(
    model: str,
) -> list[tuple[str, list[str], str]]:
    """Build a plan of post-restore actions by inspecting juju status.

    Returns a list of (app_name, [units to try in order], action_name).
    For "leader" strategies the unit list contains a single "<app>/leader"
    pseudo-target; for "any-unit" it contains every concrete unit name,
    which the caller is expected to try in order until one succeeds.
    """
    status = juju_status(model)
    plans: list[tuple[str, list[str], str]] = []
    for app, info in (status.get("applications") or {}).items():
        charm_name = (
            info.get("charm-name") or info.get("charm") or ""
        )
        candidates = {charm_name}
        if "/" in charm_name:
            candidates.add(charm_name.rsplit("/", 1)[-1])
        match = next(
            (KNOWN_POST_RESTORE_ACTIONS[c] for c in candidates
             if c in KNOWN_POST_RESTORE_ACTIONS),
            None,
        )
        if not match:
            continue
        action_name, strategy = match
        if strategy == "any-unit":
            units = sorted((info.get("units") or {}).keys())
            if not units:
                continue
            plans.append((app, units, action_name))
        else:
            plans.append((app, [f"{app}/leader"], action_name))
    return plans


def model_uuid(model_info: dict[str, Any]) -> str:
    """Return the Juju model UUID from show-model output."""
    uuid = model_info.get("model-uuid")
    if not uuid:
        raise CheckpointError(
            "juju show-model output does not include model UUID"
        )
    return uuid


def model_short_name(model_info: dict[str, Any], requested_model: str) -> str:
    """Return a concise model name for metadata."""
    return model_info.get("short-name") or requested_model.split("/")[-1]


def assert_openstack_model(model_info: dict[str, Any]) -> None:
    """Refuse non-OpenStack and controller models."""
    if model_info.get("is-controller"):
        raise CheckpointError(
            "refusing to checkpoint the Juju controller model"
        )
    if model_info.get("type") != "openstack":
        raise CheckpointError(
            "only OpenStack-backed Juju models are supported; got "
            f"{model_info.get('type')!r}"
        )


def unit_is_idle(unit: dict[str, Any]) -> bool:
    """Return whether a Juju unit is idle."""
    return unit.get("juju-status", {}).get("current") == "idle"


def model_idle(status: dict[str, Any]) -> tuple[bool, list[str]]:
    """Return idle state and human-readable blockers."""
    blockers: list[str] = []
    for machine_id, machine in sorted(status.get("machines", {}).items()):
        modification = machine.get("modification-status", {}).get("current")
        if modification and modification != "idle":
            blockers.append(
                f"machine {machine_id} modification-status is {modification}"
            )

    for app_name, app in sorted(status.get("applications", {}).items()):
        for unit_name, unit in sorted(app.get("units", {}).items()):
            if not unit_is_idle(unit):
                current = unit.get("juju-status", {}).get("current")
                blockers.append(f"unit {unit_name} juju-status is {current}")
        app_status = app.get("application-status", {}).get("current")
        if app_status in {"error", "terminated"}:
            blockers.append(f"application {app_name} status is {app_status}")
    return not blockers, blockers


def machine_instance_map(status: dict[str, Any]) -> dict[str, str]:
    """Return Juju machine id to OpenStack instance id mapping."""
    machines: dict[str, str] = {}
    for machine_id, machine in sorted(status.get("machines", {}).items()):
        instance_id = machine.get("instance-id")
        if not instance_id or instance_id == "pending":
            raise CheckpointError(
                f"machine {machine_id} does not have a usable instance-id"
            )
        machines[machine_id] = instance_id
    if not machines:
        raise CheckpointError("model has no machines to checkpoint")
    return machines


def _base_from_status(machine: dict[str, Any]) -> str:
    """Return a `name@channel` base string for a juju status machine entry."""
    base = machine.get("base") or {}
    name = base.get("name")
    channel = base.get("channel", "")
    if name and channel:
        return f"{name}@{channel.split('/')[0]}"
    series = machine.get("series")
    if series:
        return f"ubuntu@{series}"
    raise CheckpointError(
        "machine entry has neither base nor series; cannot derive base"
    )


def collect_machine_metadata(
    status: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Return machine_id -> {base, series, address} for baseline baking."""
    meta: dict[str, dict[str, Any]] = {}
    for machine_id, machine in sorted(status.get("machines", {}).items()):
        address = machine.get("dns-name") or next(
            iter(machine.get("ip-addresses") or []), None
        )
        if not address:
            raise CheckpointError(
                f"machine {machine_id} has no usable address for ssh"
            )
        meta[machine_id] = {
            "base": _base_from_status(machine),
            "series": machine.get("series"),
            "address": address,
        }
    return meta


def openstack_server_nova_attrs(server_id: str) -> dict[str, Any]:
    """Extract Nova attributes needed to re-create a sibling VM.

    Returns flavor_id, networks (name->[ips]), security_groups, key_name,
    availability_zone. These are needed by restore-v2 to boot a new VM
    that mirrors the original machine's placement.
    """
    raw = run([
        OPENSTACK_CMD, "server", "show", server_id,
        "-f", "yaml",
        "-c", "flavor",
        "-c", "addresses",
        "-c", "security_groups",
        "-c", "key_name",
        "-c", "OS-EXT-AZ:availability_zone",
    ])
    info = yaml.safe_load(raw) or {}
    flavor_field = info.get("flavor")
    flavor_id: str | None = None
    if isinstance(flavor_field, dict):
        flavor_id = flavor_field.get("id")
    elif isinstance(flavor_field, str):
        match = re.search(r"\(([0-9a-fA-F-]{36})\)", flavor_field)
        flavor_id = match.group(1) if match else flavor_field
    sg_field = info.get("security_groups") or []
    sg_names = [
        sg.get("name") if isinstance(sg, dict) else sg
        for sg in sg_field
    ]
    return {
        "flavor_id": flavor_id,
        "networks": info.get("addresses") or {},
        "security_groups": [s for s in sg_names if s],
        "key_name": info.get("key_name"),
        "availability_zone": info.get("OS-EXT-AZ:availability_zone"),
    }


def juju_create_backup(
    controller: str,
    output_path: pathlib.Path,
) -> None:
    """Run `juju create-backup` against the controller model.

    Captures controller MongoDB state (model UUIDs, machines, units,
    relations, secrets) into a tar.gz. Pair with VM snapshots for a
    controller-aware restore.
    """
    run([
        JUJU_CMD, "create-backup",
        "-B",
        "-m", f"{controller}:admin/controller",
        "--filename", str(output_path),
    ])


def ssh_cleanup_machine(
    model: str,
    machine_id: str,
    ssh_user: str,
) -> None:
    """Run cleanup commands inside a juju machine via `juju ssh`."""
    target = f"{ssh_user}@{machine_id}" if ssh_user else machine_id
    cmd = [
        JUJU_CMD, "ssh",
        "-m", model,
        target,
        "bash -s",
    ]
    proc = subprocess.run(
        cmd,
        input=SSH_CLEANUP_SCRIPT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode:
        raise CheckpointError(
            f"juju ssh cleanup of machine {machine_id} failed "
            f"({proc.returncode}):\nstdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )


def openstack_server_show(server_id: str) -> dict[str, Any]:
    """Return OpenStack server metadata."""
    return run([OPENSTACK_CMD, "server", "show", server_id, "-f", "json"],
               json_output=True)


_NET_ID_CACHE: dict[str, str] = {}


def openstack_network_id(name_or_id: str) -> str:
    """Return a Neutron network UUID given either a name or an existing UUID.

    Cached because openstack server create's --nic option only accepts
    net-id=<uuid>, never net-name=, so we resolve once and reuse.
    """
    if name_or_id in _NET_ID_CACHE:
        return _NET_ID_CACHE[name_or_id]
    out = run([
        OPENSTACK_CMD, "network", "show", name_or_id,
        "-c", "id", "-f", "value",
    ])
    net_id = (out or "").strip()
    if not net_id:
        raise CheckpointError(
            f"could not resolve network id for {name_or_id!r}"
        )
    _NET_ID_CACHE[name_or_id] = net_id
    return net_id


def has_attached_volumes(server: dict[str, Any]) -> bool:
    """Return whether OpenStack reports attached volumes for a server."""
    value = (
        server.get("volumes_attached")
        or server.get("os-extended-volumes:volumes_attached")
    )
    if value in (None, "", [], {}, "[]"):
        return False
    if isinstance(value, str) and value.strip() in {"", "[]", "None"}:
        return False
    return True


def openstack_server_snapshot(
    server_id: str,
    image_name: str,
    properties: dict[str, str],
) -> str:
    """Create a Nova server image and return the image id."""
    cmd = [OPENSTACK_CMD, "server", "image", "create", "--wait"]
    for key, value in sorted(properties.items()):
        cmd.extend(["--property", f"{key}={value}"])
    cmd.extend(["--name", image_name, server_id, "-f", "json"])
    result = run(cmd, json_output=True)
    image_id = (
        result.get("id")
        or result.get("ID")
        or result.get("image")
        or result.get("Image")
    )
    if not image_id:
        raise CheckpointError(
            f"could not determine snapshot image id for server {server_id}: "
            f"{result}"
        )
    return image_id


def wait_for_server_status(
    server_id: str,
    target: str,
    *,
    timeout: int = 180,
    interval: float = 2.0,
) -> None:
    """Block until an OpenStack server reaches the target status."""
    deadline = time.monotonic() + timeout
    last_status = "(unknown)"
    while time.monotonic() < deadline:
        server = openstack_server_show(server_id)
        last_status = server.get("status") or server.get("Status") or "(unset)"
        if last_status == target:
            return
        if last_status == "ERROR":
            raise CheckpointError(
                f"server {server_id} entered ERROR state while waiting "
                f"for {target}"
            )
        time.sleep(interval)
    raise CheckpointError(
        f"timed out waiting for server {server_id} to reach {target} "
        f"(last status: {last_status})"
    )


def openstack_server_stop(server_id: str) -> None:
    """Stop a Nova server gracefully and wait until it is SHUTOFF.

    A graceful shutdown lets the guest OS unmount its filesystems and
    flush ext4 journal + page cache to disk. Snapshotting an offline VM
    produces a clean image, where live snapshots (and nova `suspend`,
    which is implemented as a libvirt pause on stsstack and does NOT
    flush the guest's in-memory state) leave the resulting image with
    corrupted inodes for any file modified close to snapshot time:
    truncated mmap'd binaries, 0-byte unit files, "Structure needs
    cleaning" on the ext4 metadata.
    """
    run([OPENSTACK_CMD, "server", "stop", server_id])
    wait_for_server_status(server_id, "SHUTOFF", timeout=300)


def openstack_server_start(server_id: str) -> None:
    """Start a previously stopped Nova server and wait until ACTIVE."""
    run([OPENSTACK_CMD, "server", "start", server_id])
    wait_for_server_status(server_id, "ACTIVE", timeout=300)


def openstack_rebuild_server(
    server_id: str,
    image_id: str,
    *,
    reimage_boot_volume: bool,
) -> None:
    """Rebuild a Nova server from an image."""
    cmd = [OPENSTACK_CMD, "server", "rebuild", "--wait", "--image", image_id]
    if reimage_boot_volume:
        cmd.append("--reimage-boot-volume")
    cmd.append(server_id)
    run(cmd)
    server = openstack_server_show(server_id)
    status = server.get("status") or server.get("Status")
    if status != "ACTIVE":
        raise CheckpointError(
            f"server {server_id} rebuild ended with status {status}: "
            f"{server.get('fault') or server.get('Fault')}"
        )


def openstack_delete_image(image_id: str) -> None:
    """Delete a Glance image by id."""
    run([OPENSTACK_CMD, "image", "delete", image_id])


def openstack_image_show(image_id: str) -> dict[str, Any]:
    """Return OpenStack image metadata."""
    return run([OPENSTACK_CMD, "image", "show", image_id, "-f", "json"],
               json_output=True)


def snapshot_with_verify_retry(
    *,
    instance_id: str,
    base_image_name: str,
    properties: dict[str, str],
    verify: bool,
    max_attempts: int,
    stop_source: bool = True,
) -> tuple[str, str]:
    """Snapshot a server, optionally verify the image download, retry on fail.

    When ``stop_source`` is true (the default) the source server is
    stopped (graceful shutdown) for the entire snapshot+verify window so
    the resulting image is fully consistent. Without that, nova's live
    snapshot leaves ext4 in a state where mmap'd binaries and files
    modified near snapshot time end up truncated or unreadable on the
    new instance — the root cause of the M5 "agent lost" / SIGSEGV jujud
    behaviour. ``nova suspend`` was tried first but on stsstack it
    resolves to a libvirt pause that does not flush guest memory, so it
    is insufficient.

    Glance/Nova snapshots in this lab also occasionally land in an
    unbootable state at the image level (download returns InvalidResponse,
    `nova boot` reports Corrupt image download). The retry loop covers
    both classes of failure. Returns (image_id, image_name) on success.
    """
    last_error: Exception | None = None
    stopped = False
    try:
        if stop_source:
            print(f"  stop source {instance_id} for clean snapshot")
            openstack_server_stop(instance_id)
            stopped = True
        for attempt in range(1, max_attempts + 1):
            attempt_name = (
                base_image_name if attempt == 1
                else f"{base_image_name}-retry{attempt}"
            )
            try:
                image_id = openstack_server_snapshot(
                    instance_id, attempt_name, properties
                )
            except CheckpointError as exc:
                last_error = exc
                print(
                    f"snapshot attempt {attempt}/{max_attempts} failed: {exc}"
                )
                continue

            if not verify:
                return image_id, attempt_name

            try:
                openstack_verify_image_download(image_id)
                return image_id, attempt_name
            except CheckpointError as exc:
                last_error = exc
                print(
                    f"verify attempt {attempt}/{max_attempts} failed for "
                    f"{image_id}: {exc}"
                )
                try:
                    openstack_delete_image(image_id)
                except CheckpointError as cleanup_exc:
                    print(f"  failed to delete corrupt image: {cleanup_exc}")

        raise CheckpointError(
            f"snapshot+verify failed after {max_attempts} attempts for "
            f"server {instance_id}: {last_error}"
        )
    finally:
        if stopped:
            print(f"  start source {instance_id}")
            try:
                openstack_server_start(instance_id)
            except CheckpointError as exc:
                print(
                    f"  WARNING: start failed for {instance_id}: {exc}"
                )


def openstack_verify_image_download(image_id: str) -> None:
    """Download an image stream and verify its Glance hash metadata."""
    image = openstack_image_show(image_id)
    hash_algo = image.get("os_hash_algo")
    hash_value = image.get("os_hash_value")
    digest = hashlib.new(hash_algo) if hash_algo else None
    with subprocess.Popen(
        [OPENSTACK_CMD, "image", "save", image_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as proc:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(1024 * 1024)
            if not chunk:
                break
            if digest:
                digest.update(chunk)
        _stdout, stderr = proc.communicate()
        if proc.returncode:
            raise CheckpointError(
                f"failed to download image {image_id}: {stderr.decode()}"
            )
    if digest and digest.hexdigest() != hash_value:
        raise CheckpointError(
            f"image {image_id} hash mismatch: "
            f"{digest.hexdigest()} != {hash_value}"
        )


def checkpoint_path(
    root: pathlib.Path,
    name: str,
    timestamp: str,
) -> pathlib.Path:
    """Return a new checkpoint metadata directory path."""
    return root / f"{slug(name)}-{timestamp}"


def write_json(path: pathlib.Path, data: dict[str, Any]) -> None:
    """Write a JSON document with stable formatting."""
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def read_manifest(path: pathlib.Path) -> dict[str, Any]:
    """Read a checkpoint manifest from a directory or manifest path."""
    manifest_path = path
    if path.is_dir():
        manifest_path = path / MANIFEST
    if not manifest_path.exists():
        raise CheckpointError(f"manifest not found: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def load_yaml(path: pathlib.Path) -> dict[str, Any]:
    """Read a YAML document into a Python mapping."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def dump_yaml(data: dict[str, Any], path: pathlib.Path) -> None:
    """Write a YAML document, preserving key order."""
    path.write_text(
        yaml.safe_dump(data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def inject_image_id_into_bundle(
    bundle: dict[str, Any],
    baseline: dict[str, Any],
) -> None:
    """Add `image-id` constraints to bundle machines from a baseline."""
    machines = bundle.get("machines") or {}
    if not machines:
        raise CheckpointError(
            "bundle has no explicit 'machines:' section; this PoC requires "
            "machine-pinned bundles"
        )
    baseline_machines = baseline.get("machines") or {}
    missing = sorted(set(machines) - set(baseline_machines))
    if missing:
        raise CheckpointError(
            f"baseline lacks images for bundle machines: {missing}"
        )
    extra = sorted(set(baseline_machines) - set(machines))
    if extra:
        print(
            f"warning: baseline has machines not used by bundle: {extra}",
            file=sys.stderr,
        )
    for machine_id, machine in machines.items():
        image_id = baseline_machines[str(machine_id)]["snapshot_image_id"]
        existing = (machine or {}).get("constraints") or ""
        parts = [
            piece for piece in existing.split()
            if piece and not piece.startswith("image-id=")
        ]
        parts.insert(0, f"image-id={image_id}")
        if machine is None:
            machines[machine_id] = {"constraints": " ".join(parts)}
        else:
            machine["constraints"] = " ".join(parts)


def save_supporting_state(snapshot_dir: pathlib.Path, model: str) -> None:
    """Save Juju state useful for later inspection."""
    (snapshot_dir / "juju-status.json").write_text(
        run([JUJU_CMD, "status", "-m", model, "--format=json"]),
        encoding="utf-8",
    )
    (snapshot_dir / "juju-show-model.json").write_text(
        run([JUJU_CMD, "show-model", "--format=json", model]),
        encoding="utf-8",
    )
    run([
        JUJU_CMD,
        "export-bundle",
        "-m",
        model,
        "--filename",
        str(snapshot_dir / "exported-bundle.yaml"),
    ])


def validate_model(model: str, *, allow_busy: bool) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, str],
]:
    """Validate model support and return model details."""
    ensure_commands()
    info = juju_model_info(model)
    assert_openstack_model(info)
    status = juju_status(model)
    _is_idle, blockers = model_idle(status)
    if blockers and not allow_busy:
        raise CheckpointError(
            "model is not idle; use --force to override:\n  "
            + "\n  ".join(blockers)
        )
    return info, status, machine_instance_map(status)


def command_check(args: argparse.Namespace) -> int:
    """Check whether a model can be checkpointed."""
    info, _status, machines = validate_model(args.model, allow_busy=args.force)
    print(
        f"model: {model_short_name(info, args.model)} "
        f"uuid={model_uuid(info)} cloud={info.get('cloud')}/"
        f"{info.get('region')}"
    )
    for machine_id, instance_id in machines.items():
        server = openstack_server_show(instance_id)
        marker = " volume-backed" if has_attached_volumes(server) else ""
        print(
            f"machine {machine_id}: server={instance_id} "
            f"name={server.get('name') or server.get('Name')} "
            f"status={server.get('status') or server.get('Status')}"
            f"{marker}"
        )
    return 0


def command_create(args: argparse.Namespace) -> int:
    """Create an OpenStack-backed checkpoint for a Juju model."""
    info, _status, machines = validate_model(args.model, allow_busy=args.force)
    timestamp = utc_timestamp()
    name = args.name or model_short_name(info, args.model)
    root = checkpoint_root(args.output_dir)
    snapshot_dir = checkpoint_path(root, name, timestamp)
    snapshot_dir.mkdir(parents=True, exist_ok=False)

    manifest: dict[str, Any] = {
        "schema": 1,
        "backend": "openstack",
        "created_at": timestamp,
        "name": name,
        "model": args.model,
        "model_uuid": model_uuid(info),
        "model_short_name": model_short_name(info, args.model),
        "controller": info.get("controller-name"),
        "cloud": info.get("cloud"),
        "region": info.get("region"),
        "juju_agent_version": info.get("agent-version"),
        "limitations": [
            "Restores in place only; it does not clone models.",
            "Does not restore Juju controller database state.",
            "Refuses restore if model UUID or machine instance IDs changed.",
        ],
        "machines": {},
    }
    write_json(snapshot_dir / MANIFEST, manifest)
    save_supporting_state(snapshot_dir, args.model)

    for machine_id, instance_id in machines.items():
        server = openstack_server_show(instance_id)
        if has_attached_volumes(server) and not args.allow_volume_backed:
            raise CheckpointError(
                f"server for machine {machine_id} has attached volumes; "
                "this PoC only allows that with --allow-volume-backed"
            )
        image_name = (
            f"{slug(name)}-{slug(model_short_name(info, args.model))}-"
            f"m{slug(machine_id)}-{timestamp}"
        )
        properties = {
            "juju_model_checkpoint": slug(name),
            "juju_model_uuid": model_uuid(info),
            "juju_machine_id": machine_id,
            "juju_instance_id": instance_id,
        }
        print(f"snapshot machine {machine_id}: {instance_id} -> {image_name}")
        image_id = openstack_server_snapshot(
            instance_id,
            image_name,
            properties,
        )
        if args.verify_images:
            print(f"verify snapshot image {image_id}")
            openstack_verify_image_download(image_id)
        manifest["machines"][machine_id] = {
            "instance_id": instance_id,
            "server": server,
            "snapshot_image_id": image_id,
            "snapshot_image_name": image_name,
            "volume_backed": has_attached_volumes(server),
        }
        write_json(snapshot_dir / MANIFEST, manifest)

    print(f"checkpoint written to {snapshot_dir}")
    return 0


def command_bake(args: argparse.Namespace) -> int:
    """Bake baseline images for a juju model so it can be redeployed fast.

    Unlike ``create`` this is destructive to the source model: the in-VM
    cleanup wipes the juju agent and resets cloud-init, so the source model
    is expected to be destroyed afterwards. The resulting Glance images are
    meant to be referenced by ``image-id`` constraints when redeploying the
    same bundle into a fresh model.
    """
    info, status, machines = validate_model(args.model, allow_busy=args.force)
    timestamp = utc_timestamp()
    name = args.name or model_short_name(info, args.model)
    root = checkpoint_root(args.output_dir)
    baseline_dir = root / f"baseline-{slug(name)}-{timestamp}"
    baseline_dir.mkdir(parents=True, exist_ok=False)

    machine_meta = collect_machine_metadata(status)
    bundle_sha: str | None = None
    if args.bundle_path:
        bundle_path = pathlib.Path(args.bundle_path).expanduser()
        if bundle_path.is_file():
            bundle_sha = hashlib.sha256(
                bundle_path.read_bytes()
            ).hexdigest()
    manifest: dict[str, Any] = {
        "schema": 2,
        "type": "baseline",
        "backend": "openstack",
        "created_at": timestamp,
        "name": name,
        "workload": args.workload,
        "openstack_release": args.release,
        "ubuntu_series": args.ubuntu_series,
        "bundle_path": (
            str(pathlib.Path(args.bundle_path).expanduser())
            if args.bundle_path else None
        ),
        "bundle_sha256": bundle_sha,
        "source_model": args.model,
        "source_model_uuid": model_uuid(info),
        "source_model_short_name": model_short_name(info, args.model),
        "controller": info.get("controller-name"),
        "cloud": info.get("cloud"),
        "region": info.get("region"),
        "juju_agent_version": info.get("agent-version"),
        "machines": {},
    }
    write_json(baseline_dir / MANIFEST, manifest)
    save_supporting_state(baseline_dir, args.model)

    for machine_id, instance_id in machines.items():
        meta = machine_meta[machine_id]
        server = openstack_server_show(instance_id)
        if has_attached_volumes(server) and not args.allow_volume_backed:
            raise CheckpointError(
                f"server for machine {machine_id} has attached volumes; "
                "this PoC only allows that with --allow-volume-backed"
            )

        if not args.skip_cleanup:
            print(
                f"cleanup machine {machine_id} via juju ssh "
                f"(address={meta['address']})"
            )
            ssh_cleanup_machine(args.model, machine_id, args.ssh_user)

        image_name_base = (
            f"baseline-{slug(name)}-m{slug(machine_id)}-{timestamp}"
        )
        properties = {
            "juju_baseline": slug(name),
            "juju_source_model_uuid": model_uuid(info),
            "juju_machine_id": machine_id,
            "juju_source_instance_id": instance_id,
            "juju_base": meta["base"],
        }
        print(
            f"snapshot machine {machine_id}: {instance_id} -> "
            f"{image_name_base} (verify={args.verify_images}, "
            f"retries={args.snapshot_retries})"
        )
        image_id, image_name = snapshot_with_verify_retry(
            instance_id=instance_id,
            base_image_name=image_name_base,
            properties=properties,
            verify=args.verify_images,
            max_attempts=args.snapshot_retries,
            stop_source=not args.no_stop_source,
        )

        manifest["machines"][machine_id] = {
            "source_instance_id": instance_id,
            "base": meta["base"],
            "series": meta.get("series"),
            "snapshot_image_id": image_id,
            "snapshot_image_name": image_name,
            "nova_attrs": openstack_server_nova_attrs(instance_id),
        }
        write_json(baseline_dir / MANIFEST, manifest)

    if args.with_controller_backup:
        controller = info.get("controller-name") or ""
        if not controller:
            raise CheckpointError(
                "cannot find controller name in juju show-model output"
            )
        backup_path = baseline_dir / "controller-backup.tar.gz"
        print(f"create controller backup -> {backup_path}")
        juju_create_backup(controller, backup_path)
        manifest["controller_backup"] = backup_path.name
        manifest["controller_name"] = controller
        write_json(baseline_dir / MANIFEST, manifest)

    print(f"baseline written to {baseline_dir}")
    print(
        "note: source model machines have been cleaned in place; "
        "destroy the source model before deploying from this baseline."
    )
    return 0


def juju_add_machine(
    model: str,
    *,
    base: str,
    image_id: str,
    extra_constraints: str = "",
) -> None:
    """Pre-create a juju machine pinned to a baseline image."""
    constraints = f"image-id={image_id}"
    if extra_constraints:
        constraints += " " + extra_constraints
    run([
        JUJU_CMD, "add-machine",
        "-m", model,
        "--base", base,
        "--constraints", constraints,
    ])


def wait_for_machines_started(
    model: str,
    expected: int,
    timeout: int,
    interval: int = 15,
) -> None:
    """Wait until at least `expected` machines reach the 'started' state."""
    deadline = time.monotonic() + timeout
    last_started = -1
    started = 0
    while time.monotonic() < deadline:
        status = juju_status(model)
        machines = status.get("machines", {})
        started = sum(
            1 for m in machines.values()
            if m.get("juju-status", {}).get("current") == "started"
        )
        if started != last_started:
            print(f"machines started: {started}/{expected}")
            last_started = started
        if started >= expected:
            return
        time.sleep(interval)
    raise CheckpointError(
        f"only {started}/{expected} machines started within {timeout}s"
    )


def strip_image_id_constraints(bundle: dict[str, Any]) -> None:
    """Remove `image-id=` tokens from machine constraints (CLI-only token)."""
    machines = bundle.get("machines") or {}
    for machine_id, machine in list(machines.items()):
        if not machine:
            continue
        existing = machine.get("constraints") or ""
        parts = [
            p for p in existing.split()
            if p and not p.startswith("image-id=")
        ]
        if parts:
            machine["constraints"] = " ".join(parts)
        else:
            machine.pop("constraints", None)


def wait_for_model_idle(
    model: str,
    timeout: int,
    interval: int = 30,
) -> None:
    """Wait until a juju model reports idle (no blockers)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status = juju_status(model)
            is_idle, blockers = model_idle(status)
            if is_idle:
                print("juju model is idle")
                return
            head = "; ".join(blockers[:3])
            more = f" (+{len(blockers) - 3} more)" if len(blockers) > 3 else ""
            print(f"waiting for Juju idle: {head}{more}")
        except CheckpointError as exc:
            print(f"waiting for Juju status: {exc}")
        time.sleep(interval)
    raise CheckpointError(
        f"model did not become idle within {timeout} seconds"
    )


def command_redeploy(args: argparse.Namespace) -> int:
    """Redeploy a bundle into a model using a baseline.

    Pre-creates juju machines pinned to the baseline glance images, then
    runs `juju deploy <bundle> --map-machines=existing` so the bundle
    re-uses those machines instead of provisioning fresh VMs.
    """
    baseline_path = pathlib.Path(args.baseline).expanduser()
    baseline = read_manifest(baseline_path)
    if baseline.get("type") != "baseline":
        raise CheckpointError(
            f"manifest is not a baseline: type={baseline.get('type')!r}"
        )

    bundle_path = pathlib.Path(args.bundle).expanduser()
    bundle = load_yaml(bundle_path)
    strip_image_id_constraints(bundle)
    cleaned_path = bundle_path.with_name(
        bundle_path.stem + "-redeploy" + bundle_path.suffix
    )
    dump_yaml(bundle, cleaned_path)
    print(f"cleaned bundle written to {cleaned_path}")

    model = args.model
    machines = baseline.get("machines") or {}
    expected = len(machines)
    if not expected:
        raise CheckpointError("baseline has no machines to redeploy")

    print(f"add-machine x {expected} with image-id constraints")
    for machine_id, mv in sorted(machines.items()):
        print(
            f"  add-machine {machine_id}: image={mv['snapshot_image_id']} "
            f"base={mv['base']}"
        )
        juju_add_machine(
            model,
            base=mv["base"],
            image_id=mv["snapshot_image_id"],
        )

    print(f"wait until {expected} machines reach 'started'")
    wait_for_machines_started(
        model, expected, timeout=args.machine_timeout
    )

    bundle_arg = str(cleaned_path.resolve())
    print(f"deploy bundle {bundle_arg} with --map-machines=existing")
    run([
        JUJU_CMD, "deploy",
        "-m", model,
        bundle_arg,
        "--map-machines=existing",
    ])

    if args.wait_for_idle:
        print("wait for model idle")
        wait_for_model_idle(
            model,
            timeout=args.idle_timeout,
            interval=args.wait_interval,
        )
    return 0


def nova_boot_from_image(
    *,
    image_id: str,
    name: str,
    flavor_id: str,
    networks: list[str],
    fixed_ips: dict[str, list[str]] | None = None,
    security_groups: list[str],
    key_name: str | None,
    availability_zone: str | None,
    max_attempts: int = 3,
    retry_delay: int = 30,
    build_timeout: int = 300,
) -> str:
    """Boot a Nova server from a baseline image and return its new UUID.

    Retries on transient "Corrupt image download" / scheduling failures
    we observe when nova-compute fetches a freshly created snapshot
    image from glance/ceph. Status is polled here rather than via
    ``--wait`` so we can inspect the ERROR fault detail and clean up
    before the next attempt.
    """
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        attempt_name = name if attempt == 1 else f"{name}-retry{attempt}"
        cmd = [
            OPENSTACK_CMD, "server", "create",
            "--image", image_id,
            "--flavor", flavor_id,
            "-f", "json",
        ]
        for net in networks:
            v4 = None
            if fixed_ips:
                for ip in fixed_ips.get(net, []):
                    if ip and ":" not in ip:
                        v4 = ip
                        break
            if v4:
                cmd.extend([
                    "--nic",
                    f"net-id={openstack_network_id(net)},v4-fixed-ip={v4}",
                ])
            else:
                cmd.extend(["--network", net])
        for sg in security_groups:
            cmd.extend(["--security-group", sg])
        if key_name:
            cmd.extend(["--key-name", key_name])
        if availability_zone:
            cmd.extend(["--availability-zone", availability_zone])
        cmd.append(attempt_name)
        try:
            result = run(cmd, json_output=True)
        except CheckpointError as exc:
            last_error = exc
            print(
                f"  boot attempt {attempt}/{max_attempts} (create) "
                f"failed: {exc}"
            )
            if attempt < max_attempts:
                time.sleep(retry_delay)
            continue
        new_id = result.get("id") or result.get("ID")
        if not new_id:
            last_error = CheckpointError(
                f"no id from server create: {result}"
            )
            if attempt < max_attempts:
                time.sleep(retry_delay)
            continue

        deadline = time.monotonic() + build_timeout
        status = "BUILD"
        fault: Any = None
        while time.monotonic() < deadline:
            srv = openstack_server_show(new_id)
            status = (
                srv.get("status") or srv.get("Status") or "(unknown)"
            )
            if status == "ACTIVE":
                return new_id
            if status == "ERROR":
                fault = srv.get("fault") or srv.get("Fault")
                break
            time.sleep(3)
        last_error = CheckpointError(
            f"server {new_id} ended with status={status} fault={fault}"
        )
        print(
            f"  boot attempt {attempt}/{max_attempts} ERROR "
            f"(status={status}); cleaning up {new_id}"
        )
        try:
            run([OPENSTACK_CMD, "server", "delete", "--wait", new_id])
        except CheckpointError as cleanup_exc:
            print(f"    cleanup of {new_id} failed: {cleanup_exc}")
        if attempt < max_attempts:
            time.sleep(retry_delay)
    raise CheckpointError(
        f"nova boot failed after {max_attempts} attempts: {last_error}"
    )


def juju_restore_on_controller(
    controller: str,
    backup_local_path: pathlib.Path,
    juju_restore_local_path: pathlib.Path,
    *,
    dry_run: bool = False,
) -> None:
    """Copy the backup tarball + juju-restore tool to controller machine 0
    and run `juju-restore`. Same-controller restore (no --copy-controller)."""
    ctrl_model = f"{controller}:admin/controller"
    remote_backup = "/home/ubuntu/controller-backup.tar.gz"
    remote_tool = "/home/ubuntu/juju-restore"
    print(f"scp backup -> {ctrl_model} machine 0")
    run([JUJU_CMD, "scp", "-m", ctrl_model,
         str(backup_local_path), f"0:{remote_backup}"])
    print(f"scp juju-restore tool")
    run([JUJU_CMD, "scp", "-m", ctrl_model,
         str(juju_restore_local_path), f"0:{remote_tool}"])
    flags = "--yes" if not dry_run else "--dry-run"
    print(f"running juju-restore on controller {flags}")
    run([
        JUJU_CMD, "ssh", "-m", ctrl_model, "0",
        f"chmod +x {remote_tool} && "
        f"sudo {remote_tool} {flags} {remote_backup}",
    ])


MONGO_UPDATE_SCRIPT_TEMPLATE = """\
set -u
PASS=$(grep '^statepassword' /var/lib/juju/agents/machine-0/agent.conf | awk '{print $2}')
# Mongo's TLS config requires the snap-visible CA copy. Idempotent.
if [ ! -f /var/snap/juju-db/common/ca.crt ]; then
    cp /var/lib/juju/ca.crt /var/snap/juju-db/common/ca.crt
    chmod 644 /var/snap/juju-db/common/ca.crt
fi
/snap/bin/juju-db.mongo --quiet \\
    --port 37017 \\
    --tls --tlsAllowInvalidCertificates \\
    --tlsCAFile /var/snap/juju-db/common/ca.crt \\
    --tlsCertificateKeyFile /var/snap/juju-db/common/server.pem \\
    --authenticationDatabase admin \\
    -u machine-0 -p "$PASS" \\
    juju --eval '
__JS__
'
"""

MONGO_UPDATE_JS_TEMPLATE = """\
var modelUuid = "__MODEL_UUID__";
var updates = __UPDATES_JSON__;
updates.forEach(function(u) {
    var id = modelUuid + ":" + u.machine_id;
    var res = db.instanceData.updateOne(
        { _id: id, "model-uuid": modelUuid, machineid: u.machine_id },
        { $set: { instanceid: u.new_instance_id } }
    );
    print(JSON.stringify({
        machine_id: u.machine_id,
        new_instance_id: u.new_instance_id,
        matched: res.matchedCount,
        modified: res.modifiedCount
    }));
});
"""


def mongo_update_instance_ids(
    controller: str,
    model_uuid: str,
    updates: list[dict[str, str]],
) -> None:
    """Update each machine.instance-id in the controller MongoDB.

    `updates` is a list of {machine_id, new_instance_id}. Runs the mongo
    shell inside controller machine 0 via `juju ssh ... -- sudo bash -s`.
    """
    if not updates:
        return
    js = (
        MONGO_UPDATE_JS_TEMPLATE
        .replace("__MODEL_UUID__", model_uuid)
        .replace("__UPDATES_JSON__", json.dumps(updates))
    )
    script = MONGO_UPDATE_SCRIPT_TEMPLATE.replace("__JS__", js)
    ctrl_model = f"{controller}:admin/controller"
    cmd = [JUJU_CMD, "ssh", "-m", ctrl_model, "0", "sudo bash -s"]
    proc = subprocess.run(
        cmd,
        input=script,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode:
        raise CheckpointError(
            f"mongo update failed ({proc.returncode}):\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    print(proc.stdout.rstrip())


def command_mongo_update_machines(args: argparse.Namespace) -> int:
    """Apply instance-id updates from a new-instance map file."""
    map_path = pathlib.Path(args.map_file).expanduser()
    if not map_path.exists():
        raise CheckpointError(f"map file not found: {map_path}")
    data = json.loads(map_path.read_text(encoding="utf-8"))
    controller = args.controller or data.get("controller_name")
    model_uuid = args.model_uuid or data.get("source_model_uuid")
    new_instances = data.get("new_instances") or {}
    if not controller or not model_uuid or not new_instances:
        raise CheckpointError(
            "map file missing controller_name / source_model_uuid / "
            "new_instances"
        )
    updates = [
        {"machine_id": mid, "new_instance_id": uid}
        for mid, uid in sorted(new_instances.items())
    ]
    print(
        f"updating instanceData on {controller} for model {model_uuid}: "
        f"{len(updates)} machine(s)"
    )
    mongo_update_instance_ids(controller, model_uuid, updates)
    return 0


def command_run_post_restore_actions(args: argparse.Namespace) -> int:
    """Run charm actions needed to bring stateful workloads back online.

    Some charms (mysql-innodb-cluster, etc.) do not auto-recover their
    distributed service after every member is power-cycled at the same
    time, which is exactly what a checkpoint restore does. After mongo
    is updated and jujud has reconnected the new VMs, the leader unit
    of those charms needs an explicit "rebooting from outage" action.
    """
    model = args.model
    # Explicit --action UNIT=ACTION items each become their own single-target
    # plan; the app name is derived from the unit prefix.
    explicit_plans: list[tuple[str, list[str], str]] = []
    for spec in args.action or []:
        unit, sep, name = spec.partition("=")
        if not sep or not unit or not name:
            raise CheckpointError(
                f"--action must be UNIT=ACTION (e.g. "
                f"'mysql-innodb-cluster/leader=reboot-cluster-from-complete-outage'),"
                f" got {spec!r}"
            )
        app = unit.split("/", 1)[0] if "/" in unit else unit
        explicit_plans.append((app, [unit], name))

    detected_plans: list[tuple[str, list[str], str]] = []
    if not args.no_auto_detect:
        detected_plans = detect_post_restore_actions(model)

    seen_keys: set[tuple[str, str]] = set()
    plans: list[tuple[str, list[str], str]] = []
    for plan in explicit_plans + detected_plans:
        key = (plan[0], plan[2])  # (app, action) — dedupe explicit vs detected
        if key in seen_keys:
            continue
        seen_keys.add(key)
        plans.append(plan)

    if not plans:
        print("no post-restore actions to run "
              "(no --action given and no known charms detected)")
        return 0

    print(f"planned {len(plans)} post-restore action(s):")
    for app, units, name in plans:
        print(f"  - {app} via {units} -> {name}")

    failures: list[str] = []
    for app, units, name in plans:
        succeeded = False
        last_error = "(no unit attempted)"
        for unit in units:
            print(f"--- run {name} on {unit} ---")
            try:
                result = juju_run_action(model, unit, name, wait=args.wait)
            except CheckpointError as exc:
                last_error = str(exc)
                print(f"  call failed: {exc}")
                continue
            task = (
                next(iter(result.values()), {})
                if isinstance(result, dict) else {}
            ) or {}
            status = (task.get("status") or "(unknown)").lower()
            message = task.get("message") or ""
            print(f"  outcome: {status} {('('+message+')') if message else ''}")
            if status in ("completed", "success"):
                succeeded = True
                break
            last_error = f"{status}: {message}"
        if not succeeded:
            failures.append(f"{app}/{name}: {last_error}")

    if failures:
        raise CheckpointError(
            "post-restore actions failed:\n  " + "\n  ".join(failures)
        )
    return 0


def command_restore_v2(args: argparse.Namespace) -> int:
    """Restore a baseline + controller backup into the same controller.

    Steps:
      1. boot a new Nova VM from each baseline image
      2. transfer controller backup + juju-restore tool to controller machine 0
      3. run juju-restore (MongoDB state reverts to bake time)
      4. update each machine.instance-id in mongo to the new VM UUID
      5. wait until the restored model returns to idle

    Step 4 is implemented in a separate command (`mongo-update-machines`)
    because the exact mongo collection layout is verified live.
    """
    baseline_path = pathlib.Path(args.baseline).expanduser()
    baseline = read_manifest(baseline_path)
    if baseline.get("type") != "baseline":
        raise CheckpointError(
            f"manifest is not a baseline: type={baseline.get('type')!r}"
        )
    if not baseline.get("controller_backup"):
        raise CheckpointError(
            "baseline manifest has no controller_backup; bake with "
            "--with-controller-backup"
        )

    controller = args.controller or baseline.get("controller_name")
    if not controller:
        raise CheckpointError(
            "controller name not in manifest; pass --controller"
        )

    backup_dir = (
        baseline_path if baseline_path.is_dir() else baseline_path.parent
    )
    backup_file = backup_dir / baseline["controller_backup"]
    juju_restore_tool = pathlib.Path(args.juju_restore_path).expanduser()
    if not juju_restore_tool.exists():
        raise CheckpointError(
            f"juju-restore tool not found at {juju_restore_tool}"
        )

    # 1. boot new VMs from baseline images
    new_instances: dict[str, str] = {}
    timestamp = utc_timestamp()
    machines = baseline.get("machines") or {}
    for machine_id, mv in sorted(machines.items()):
        attrs = mv.get("nova_attrs") or {}
        if not attrs.get("flavor_id"):
            raise CheckpointError(
                f"machine {machine_id} has no flavor_id in baseline; "
                "re-bake to capture nova_attrs"
            )
        raw_networks = attrs.get("networks") or {}
        networks = list(raw_networks.keys())
        fixed_ips: dict[str, list[str]] | None = None
        if not args.no_keep_ips:
            fixed_ips = {
                net: [ip for ip in (raw_networks.get(net) or []) if ip]
                for net in networks
            }
        if args.skip_security_groups:
            sgs: list[str] = []
        else:
            sgs = attrs.get("security_groups") or []
        name = (
            f"restore-{slug(baseline.get('name', 'baseline'))}-"
            f"m{slug(machine_id)}-{timestamp}"
        )
        ip_info = (
            ",".join(
                ip for net in networks
                for ip in (fixed_ips or {}).get(net, [])
            )
            if fixed_ips else "(any)"
        )
        print(
            f"boot {name} from {mv['snapshot_image_id']} "
            f"(sgs={sgs or 'default'}, ips={ip_info})"
        )
        new_uuid = nova_boot_from_image(
            image_id=mv["snapshot_image_id"],
            name=name,
            flavor_id=attrs["flavor_id"],
            networks=networks,
            fixed_ips=fixed_ips,
            security_groups=sgs,
            key_name=attrs.get("key_name"),
            availability_zone=attrs.get("availability_zone"),
        )
        new_instances[machine_id] = new_uuid
        print(f"  machine {machine_id} -> new instance {new_uuid}")

    # write a sidecar mapping for later mongo-update-machines step
    map_file = backup_dir / f"new-instances-{timestamp}.json"
    write_json(map_file, {
        "source_model_uuid": baseline.get("source_model_uuid"),
        "controller_name": controller,
        "new_instances": new_instances,
    })
    print(f"new-instance map written to {map_file}")

    # 2-3. juju-restore (or dry-run)
    juju_restore_on_controller(
        controller,
        backup_file,
        juju_restore_tool,
        dry_run=args.restore_dry_run,
    )

    print(
        "next step: run `mongo-update-machines` with the new-instance map "
        f"({map_file}) to retarget juju machines to the new Nova instances."
    )
    return 0


def command_apply_baseline(args: argparse.Namespace) -> int:
    """Inject baseline image-ids into a bundle yaml and write a new file."""
    baseline_path = pathlib.Path(args.baseline).expanduser()
    baseline = read_manifest(baseline_path)
    if baseline.get("type") != "baseline":
        raise CheckpointError(
            f"manifest is not a baseline: type={baseline.get('type')!r}"
        )
    bundle_path = pathlib.Path(args.bundle).expanduser()
    bundle = load_yaml(bundle_path)
    inject_image_id_into_bundle(bundle, baseline)
    out_path = pathlib.Path(args.output).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dump_yaml(bundle, out_path)
    print(f"baseline-injected bundle written to {out_path}")
    return 0


def verify_restore_target(
    manifest: dict[str, Any],
    model: str,
) -> dict[str, Any]:
    """Verify current model still matches the checkpoint target."""
    info, _status, machines = validate_model(model, allow_busy=True)
    if model_uuid(info) != manifest.get("model_uuid"):
        raise CheckpointError(
            "current model UUID does not match checkpoint: "
            f"{model_uuid(info)} != {manifest.get('model_uuid')}"
        )
    expected = {
        machine_id: machine["instance_id"]
        for machine_id, machine in manifest.get("machines", {}).items()
    }
    if machines != expected:
        raise CheckpointError(
            "current machine instance map does not match checkpoint:\n"
            f"current={machines}\nexpected={expected}"
        )
    return info


def wait_for_model(args: argparse.Namespace) -> None:
    """Wait for Juju units to become idle after restore."""
    deadline = time.monotonic() + args.wait_timeout
    while time.monotonic() < deadline:
        try:
            status = juju_status(args.model)
            is_idle, blockers = model_idle(status)
            if is_idle:
                print("juju model is idle")
                return
            print(f"waiting for Juju idle: {'; '.join(blockers)}")
        except CheckpointError as exc:
            print(f"waiting for Juju status: {exc}")
        time.sleep(args.wait_interval)
    raise CheckpointError(
        f"model did not become idle within {args.wait_timeout} seconds"
    )


def command_restore(args: argparse.Namespace) -> int:
    """Restore a model in place from a checkpoint."""
    path = pathlib.Path(args.checkpoint).expanduser()
    manifest = read_manifest(path)
    if manifest.get("backend") != "openstack":
        raise CheckpointError(
            f"unsupported backend: {manifest.get('backend')}"
        )
    verify_restore_target(manifest, args.model)

    if not args.yes:
        print("dry-run restore plan; pass --yes to rebuild Nova servers")
    for machine_id, machine in sorted(manifest.get("machines", {}).items()):
        instance_id = machine["instance_id"]
        image_id = machine["snapshot_image_id"]
        print(
            f"restore machine {machine_id}: "
            f"rebuild {instance_id} from {image_id}"
        )
        if args.yes:
            openstack_rebuild_server(
                instance_id,
                image_id,
                reimage_boot_volume=args.reimage_boot_volume,
            )

    if args.yes and args.wait:
        wait_for_model(args)
    return 0


def command_delete(args: argparse.Namespace) -> int:
    """Delete checkpoint images and metadata."""
    path = pathlib.Path(args.checkpoint).expanduser()
    manifest = read_manifest(path)
    if not args.yes:
        print("dry-run delete plan; pass --yes to delete images and metadata")
    for machine_id, machine in sorted(manifest.get("machines", {}).items()):
        image_id = machine["snapshot_image_id"]
        print(f"delete machine {machine_id} snapshot image {image_id}")
        if args.yes:
            openstack_delete_image(image_id)
    if args.yes:
        target = path if path.is_dir() else path.parent
        shutil.rmtree(target)
        print(f"deleted checkpoint metadata {target}")
    return 0


def command_list(args: argparse.Namespace) -> int:
    """List locally recorded checkpoints."""
    root = checkpoint_root(args.output_dir)
    if not root.exists():
        print(f"no checkpoint directory: {root}")
        return 0
    for manifest_path in sorted(root.glob(f"*/{MANIFEST}")):
        manifest = read_manifest(manifest_path)
        print(
            f"{manifest_path.parent} model={manifest.get('model')} "
            f"uuid={manifest.get('model_uuid')} "
            f"machines={len(manifest.get('machines', {}))}"
        )
    return 0


def command_list_baselines(args: argparse.Namespace) -> int:
    """Catalogue all bake baselines under output-dir.

    Reads each baseline-*/manifest.json and prints a single-line summary
    keyed on the schema v2 metadata (workload, release, series, bundle).
    Supports --release, --workload, --series filters so the catalogue
    can be narrowed when many bakes accumulate.
    """
    root = checkpoint_root(args.output_dir)
    if not root.exists():
        print(f"no checkpoint directory: {root}")
        return 0

    def short(value: Any, width: int) -> str:
        if value is None:
            return "-".ljust(width)
        text = str(value)
        if len(text) > width:
            text = text[: width - 1] + "…"
        return text.ljust(width)

    rows: list[dict[str, Any]] = []
    for manifest_path in sorted(root.glob("baseline-*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        baseline_dir = manifest_path.parent
        backup_file = baseline_dir / "controller-backup.tar.gz"
        rows.append({
            "dir": baseline_dir.name,
            "name": manifest.get("name"),
            "workload": manifest.get("workload"),
            "release": manifest.get("openstack_release"),
            "series": manifest.get("ubuntu_series"),
            "machines": len(manifest.get("machines", {}) or {}),
            "created_at": manifest.get("created_at"),
            "has_backup": backup_file.is_file(),
            "bundle_path": manifest.get("bundle_path"),
            "bundle_sha256": manifest.get("bundle_sha256"),
        })

    def passes(row: dict[str, Any]) -> bool:
        if args.workload and (row["workload"] or "") != args.workload:
            return False
        if args.release and (row["release"] or "") != args.release:
            return False
        if args.series and (row["series"] or "") != args.series:
            return False
        return True

    rows = [r for r in rows if passes(r)]

    if not rows:
        print("no baselines matched the filter")
        return 0

    header = (
        f"{short('NAME', 28)} "
        f"{short('WORKLOAD', 12)} "
        f"{short('SERIES', 8)} "
        f"{short('RELEASE', 10)} "
        f"{short('MACHINES', 9)} "
        f"{short('BACKUP', 7)} "
        f"{short('CREATED', 19)} "
        f"DIR"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{short(row['name'], 28)} "
            f"{short(row['workload'], 12)} "
            f"{short(row['series'], 8)} "
            f"{short(row['release'], 10)} "
            f"{short(row['machines'], 9)} "
            f"{short('yes' if row['has_backup'] else 'no', 7)} "
            f"{short(row['created_at'], 19)} "
            f"{row['dir']}"
        )
    if args.verbose:
        print()
        for row in rows:
            print(f"--- {row['dir']} ---")
            print(f"  bundle: {row['bundle_path']}")
            print(f"  bundle_sha256: {row['bundle_sha256']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(
        description="OpenStack-backed Juju model checkpoint PoC"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="validate model support")
    check.add_argument("-m", "--model", required=True)
    check.add_argument("--force", action="store_true",
                       help="allow a non-idle model")
    check.set_defaults(func=command_check)

    create = subparsers.add_parser("create", help="create a checkpoint")
    create.add_argument("-m", "--model", required=True)
    create.add_argument("--name", help="checkpoint name")
    create.add_argument("--output-dir", help="metadata directory")
    create.add_argument("--force", action="store_true",
                        help="allow a non-idle model")
    create.add_argument(
        "--allow-volume-backed",
        action="store_true",
        help="allow servers with attached volumes",
    )
    create.add_argument(
        "--verify-images",
        action="store_true",
        help="download each snapshot image and verify Glance hash metadata",
    )
    create.set_defaults(func=command_create)

    bake = subparsers.add_parser(
        "bake",
        help="bake baseline images for fast redeploy",
    )
    bake.add_argument("-m", "--model", required=True)
    bake.add_argument("--name", help="baseline name")
    bake.add_argument("--output-dir", help="metadata directory")
    bake.add_argument("--force", action="store_true",
                      help="allow a non-idle model")
    bake.add_argument(
        "--allow-volume-backed",
        action="store_true",
        help="allow servers with attached volumes",
    )
    bake.add_argument("--ssh-user", default="ubuntu",
                      help="ssh user for in-VM cleanup")
    bake.add_argument(
        "--skip-cleanup",
        action="store_true",
        help="skip ssh-based in-VM cleanup (debugging only)",
    )
    bake.add_argument(
        "--with-controller-backup",
        action="store_true",
        help="also run `juju create-backup` and store the archive next to "
             "the baseline manifest (required for restore-v2)",
    )
    bake.add_argument(
        "--verify-images",
        action="store_true",
        help="download each snapshot image and verify Glance hash to catch "
             "corruption at bake time",
    )
    bake.add_argument(
        "--snapshot-retries", type=int, default=3,
        help="how many times to retry snapshot+verify on a single machine "
             "before giving up (default: 3)",
    )
    bake.add_argument(
        "--no-stop-source",
        action="store_true",
        help="do NOT stop the source server while snapshotting "
             "(debugging only; live snapshots corrupt ext4 inodes on "
             "stsstack — see snapshot_with_verify_retry docstring)",
    )
    bake.add_argument(
        "--workload",
        help="logical name of the deployed stack (e.g. designate, cinder). "
             "Stored in manifest so list-baselines can group by workload.",
    )
    bake.add_argument(
        "--release",
        help="OpenStack release the source model targets (e.g. yoga, zed, "
             "2023.1). Stored in manifest for catalog/filter use.",
    )
    bake.add_argument(
        "--ubuntu-series",
        help="ubuntu series of the source model machines (e.g. jammy, focal). "
             "Stored in manifest.",
    )
    bake.add_argument(
        "--bundle-path",
        help="path to the zaza/juju bundle yaml used to deploy this model. "
             "Stored in manifest along with its sha256 so re-bakes can be "
             "tied back to a specific bundle version.",
    )
    bake.set_defaults(func=command_bake)

    mongo_update = subparsers.add_parser(
        "mongo-update-machines",
        help="update machine.instance-id in controller mongo from a map file",
    )
    mongo_update.add_argument("--map-file", required=True,
                              help="new-instances JSON written by restore-v2")
    mongo_update.add_argument("--controller",
                              help="override controller name from map file")
    mongo_update.add_argument("--model-uuid",
                              help="override model UUID from map file")
    mongo_update.set_defaults(func=command_mongo_update_machines)

    post_actions = subparsers.add_parser(
        "run-post-restore-actions",
        help="run charm actions needed after a checkpoint restore "
             "(mysql-innodb-cluster reboot-cluster-from-complete-outage, etc.)",
    )
    post_actions.add_argument(
        "-m", "--model", required=True,
        help="target juju model in <controller>:<user>/<model> form",
    )
    post_actions.add_argument(
        "--action", action="append", default=[],
        metavar="UNIT=ACTION",
        help="add a charm action to run (e.g. "
             "mysql-innodb-cluster/leader=reboot-cluster-from-complete-outage). "
             "May be passed multiple times.",
    )
    post_actions.add_argument(
        "--no-auto-detect", action="store_true",
        help="skip the built-in KNOWN_POST_RESTORE_ACTIONS sweep over "
             "the model; only run explicit --action entries",
    )
    post_actions.add_argument(
        "--wait", default="10m",
        help="--wait value passed to juju run (default: 10m)",
    )
    post_actions.set_defaults(func=command_run_post_restore_actions)

    restore_v2 = subparsers.add_parser(
        "restore-v2",
        help="boot new VMs from baseline + run juju-restore (same controller)",
    )
    restore_v2.add_argument("--baseline", required=True,
                            help="baseline manifest directory or file")
    restore_v2.add_argument("--controller",
                            help="target juju controller (default: from "
                                 "manifest)")
    restore_v2.add_argument(
        "--juju-restore-path", default="/home/ubuntu/juju-restore",
        help="path to juju-restore binary on the local box",
    )
    restore_v2.add_argument(
        "--restore-dry-run", action="store_true",
        help="pass --dry-run to juju-restore (no actual mongo restore)",
    )
    restore_v2.add_argument(
        "--skip-security-groups", action="store_true",
        help="don't pass --security-group to nova; useful when the "
             "baseline's juju-created SGs were deleted with the model",
    )
    restore_v2.add_argument(
        "--no-keep-ips", action="store_true",
        help="do NOT request the original v4 fixed IPs for new VMs. "
             "Default behaviour is to keep IPs so stateful charms "
             "(group replication, etc.) see the same cluster members.",
    )
    restore_v2.set_defaults(func=command_restore_v2)

    redeploy = subparsers.add_parser(
        "redeploy",
        help="redeploy a bundle into a model using a baseline",
    )
    redeploy.add_argument("--baseline", required=True,
                          help="baseline manifest directory or file")
    redeploy.add_argument("--model", required=True,
                          help="target juju model (must already exist)")
    redeploy.add_argument("--bundle", required=True,
                          help="input bundle yaml")
    redeploy.add_argument("--machine-timeout", type=int, default=1800,
                          help="seconds to wait for machines to start")
    redeploy.add_argument("--idle-timeout", type=int, default=3600,
                          help="seconds to wait for all units to be idle")
    redeploy.add_argument("--wait-interval", type=int, default=30)
    redeploy.add_argument("--no-wait", dest="wait_for_idle",
                          action="store_false",
                          help="do not wait for model idle after deploy")
    redeploy.set_defaults(func=command_redeploy, wait_for_idle=True)

    apply_baseline = subparsers.add_parser(
        "apply-baseline",
        help="(deprecated, juju rejects bundle image-id) inject image-id "
             "constraints from a baseline into a bundle",
    )
    apply_baseline.add_argument(
        "--baseline", required=True,
        help="baseline manifest directory or file",
    )
    apply_baseline.add_argument(
        "--bundle", required=True, help="input bundle yaml",
    )
    apply_baseline.add_argument(
        "--output", required=True, help="output bundle yaml",
    )
    apply_baseline.set_defaults(func=command_apply_baseline)

    restore = subparsers.add_parser("restore", help="restore in place")
    restore.add_argument("-m", "--model", required=True)
    restore.add_argument("checkpoint")
    restore.add_argument("--yes", action="store_true",
                         help="perform the destructive rebuild")
    restore.add_argument(
        "--reimage-boot-volume",
        action="store_true",
        help="pass --reimage-boot-volume to openstack server rebuild",
    )
    restore.add_argument("--no-wait", dest="wait", action="store_false",
                         help="do not wait for Juju idle after restore")
    restore.add_argument("--wait-timeout", type=int, default=900)
    restore.add_argument("--wait-interval", type=int, default=30)
    restore.set_defaults(func=command_restore, wait=True)

    delete = subparsers.add_parser(
        "delete",
        help="delete checkpoint artifacts",
    )
    delete.add_argument("checkpoint")
    delete.add_argument("--yes", action="store_true",
                        help="delete images and metadata")
    delete.set_defaults(func=command_delete)

    list_cmd = subparsers.add_parser("list", help="list local checkpoints")
    list_cmd.add_argument("--output-dir", help="metadata directory")
    list_cmd.set_defaults(func=command_list)

    list_b = subparsers.add_parser(
        "list-baselines",
        help="catalogue baked baselines with workload/release/series filter",
    )
    list_b.add_argument("--output-dir", help="metadata directory")
    list_b.add_argument("--workload",
                        help="filter rows by workload value in manifest")
    list_b.add_argument("--release",
                        help="filter rows by openstack_release value in manifest")
    list_b.add_argument("--series",
                        help="filter rows by ubuntu_series value in manifest")
    list_b.add_argument("--verbose", action="store_true",
                        help="also dump bundle_path and bundle_sha256")
    list_b.set_defaults(func=command_list_baselines)

    return parser


def main() -> int:
    """Run the command line interface."""
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except CheckpointError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
