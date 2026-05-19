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


def ssh_cleanup_machine(
    address: str,
    machine_id: str,
    ssh_user: str,
) -> None:
    """Run cleanup commands inside a juju machine over ssh."""
    target = f"{ssh_user}@{address}"
    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=15",
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
            f"ssh cleanup of machine {machine_id} ({target}) failed "
            f"({proc.returncode}):\nstdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )


def openstack_server_show(server_id: str) -> dict[str, Any]:
    """Return OpenStack server metadata."""
    return run([OPENSTACK_CMD, "server", "show", server_id, "-f", "json"],
               json_output=True)


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
    manifest: dict[str, Any] = {
        "schema": 1,
        "type": "baseline",
        "backend": "openstack",
        "created_at": timestamp,
        "name": name,
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
                f"cleanup machine {machine_id} via ssh {meta['address']}"
            )
            ssh_cleanup_machine(meta["address"], machine_id, args.ssh_user)

        image_name = (
            f"baseline-{slug(name)}-m{slug(machine_id)}-{timestamp}"
        )
        properties = {
            "juju_baseline": slug(name),
            "juju_source_model_uuid": model_uuid(info),
            "juju_machine_id": machine_id,
            "juju_source_instance_id": instance_id,
            "juju_base": meta["base"],
        }
        print(f"snapshot machine {machine_id}: {instance_id} -> {image_name}")
        image_id = openstack_server_snapshot(
            instance_id,
            image_name,
            properties,
        )

        manifest["machines"][machine_id] = {
            "source_instance_id": instance_id,
            "base": meta["base"],
            "series": meta.get("series"),
            "snapshot_image_id": image_id,
            "snapshot_image_name": image_name,
        }
        write_json(baseline_dir / MANIFEST, manifest)

    print(f"baseline written to {baseline_dir}")
    print(
        "note: source model machines have been cleaned in place; "
        "destroy the source model before deploying from this baseline."
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
    bake.set_defaults(func=command_bake)

    apply_baseline = subparsers.add_parser(
        "apply-baseline",
        help="inject image-id constraints from a baseline into a bundle",
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
