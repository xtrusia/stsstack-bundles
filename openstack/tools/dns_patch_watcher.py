#!/usr/bin/env python3
"""
DNS Patch Watcher v2 - auto-detects zaza model, patches charmhelpers ip.py
as soon as charm is installed, resolves error units automatically.

Usage: python3 dns_patch_watcher.py [model-name]
  If no model given, auto-detects zaza-* model.
"""
import subprocess
import sys
import time
import json

PATCH_SCRIPT = '''
import glob
files = glob.glob("/var/lib/juju/agents/*/charm/**/network/ip.py", recursive=True) + \
        glob.glob("/var/lib/juju/agents/*/charm/hooks/**/network/ip.py", recursive=True)
if not files:
    print("NONE")
else:
    patched = []
    for f in files:
        with open(f) as fh:
            c = fh.read()
        if "dns.resolver.query(address, rtype)" in c:
            c = c.replace(
                "answers = dns.resolver.query(address, rtype)",
                "resolver = dns.resolver.Resolver()\\n        resolver.lifetime = 30\\n        answers = resolver.resolve(address, rtype)")
            c = c.replace(
                "except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):",
                "except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers, dns.exception.Timeout):")
            with open(f, "w") as fh:
                fh.write(c)
            patched.append(f)
    print("PATCHED:" + ",".join(patched) if patched else "CLEAN")
'''


def run(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except Exception:
        return "", 1


def find_zaza_model():
    out, rc = run(["juju", "list-models", "--format", "json"])
    if rc != 0:
        return None
    try:
        for m in json.loads(out).get("models", []):
            if "zaza" in m["name"]:
                return m["name"]
    except Exception:
        pass
    return None


def get_started_machines(model):
    out, rc = run(["juju", "status", "-m", model, "--format", "json"])
    if rc != 0:
        return [], []
    try:
        data = json.loads(out)
    except Exception:
        return [], []
    machines = [mid for mid, m in data.get("machines", {}).items()
                if m.get("juju-status", {}).get("current") == "started"]
    errors = []
    for app in data.get("applications", {}).values():
        for name, unit in app.get("units", {}).items():
            if unit.get("juju-status", {}).get("current") == "error":
                errors.append(name)
            for sub_name, sub in unit.get("subordinates", {}).items():
                if sub.get("juju-status", {}).get("current") == "error":
                    errors.append(sub_name)
    return machines, errors


def patch_machine(model, mid):
    out, _ = run(["juju", "exec", "-m", model, "--machine", mid,
                  "--", "sudo python3 -c '%s'" % PATCH_SCRIPT])
    if "PATCHED:" in out:
        return "PATCHED"
    elif "CLEAN" in out:
        return "CLEAN"
    return "NONE"


def main():
    model = sys.argv[1] if len(sys.argv) > 1 else None
    done = set()
    p = lambda *a: print(*a, flush=True)

    p("[watcher] Starting, model=%s" % (model or "auto-detect"))

    while True:
        # Auto-detect model if not given or model disappeared
        if not model:
            model = find_zaza_model()
            if not model:
                time.sleep(5)
                continue
            p("[watcher] Found model: %s" % model)

        machines, errors = get_started_machines(model)
        if not machines and not errors:
            # Model might have been destroyed
            out, _ = run(["juju", "list-models"])
            if model not in out:
                p("[watcher] Model %s gone, waiting for new one" % model)
                model = None
                done.clear()
                time.sleep(5)
                continue

        for mid in machines:
            if mid in done:
                continue
            result = patch_machine(model, mid)
            if result == "PATCHED":
                p("[watcher] machine %s: PATCHED" % mid)
                done.add(mid)
            elif result == "CLEAN":
                done.add(mid)
            # NONE = no ip.py yet, retry next loop

        for unit in errors:
            run(["juju", "resolved", "-m", model, unit], timeout=10)
            p("[watcher] resolved %s" % unit)

        time.sleep(5)


if __name__ == "__main__":
    main()
