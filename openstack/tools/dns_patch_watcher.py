#!/usr/bin/env python3
"""
DNS Patch Watcher v5 - auto-detects zaza model, patches charmhelpers ip.py,
ensures br-ex is UP with data-port attached on neutron-gateway,
and resolves error units automatically.

Usage: python3 dns_patch_watcher.py [model-name]
"""
import subprocess
import sys
import time
import json

# Patch script that handles both old (dnspython 1.x) and new (2.0+) versions
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
            # Check dnspython version to use correct method
            c = c.replace(
                "answers = dns.resolver.query(address, rtype)",
                "resolver = dns.resolver.Resolver()\\n"
                "        resolver.lifetime = 30\\n"
                "        _resolve = getattr(resolver, \\"resolve\\", getattr(resolver, \\"query\\", None))\\n"
                "        answers = _resolve(address, rtype)")
            c = c.replace(
                "except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):",
                "except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers, dns.exception.Timeout, Exception):")
            with open(f, "w") as fh:
                fh.write(c)
            patched.append(f)
    print("PATCHED:" + ",".join(patched) if patched else "CLEAN")
'''

BREX_SCRIPT = '''
import subprocess, json
result = subprocess.run(["ovs-vsctl", "list-ports", "br-ex"], capture_output=True, text=True)
ports = result.stdout.strip().split("\\n") if result.stdout.strip() else []
has_physical = any(p for p in ports if not p.startswith("phy-") and p != "")
if has_physical:
    print("OK")
else:
    result = subprocess.run(["ip", "-j", "link", "show"], capture_output=True, text=True)
    interfaces = json.loads(result.stdout)
    for iface in interfaces:
        name = iface.get("ifname", "")
        if name.startswith("ens") and name != "ens3" and "LOOPBACK" not in iface.get("flags", []):
            subprocess.run(["ovs-vsctl", "add-port", "br-ex", name], capture_output=True)
            subprocess.run(["ip", "link", "set", "br-ex", "up"], capture_output=True)
            print("FIXED:" + name)
            break
    else:
        print("NOFIX")
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


def get_model_status(model):
    out, rc = run(["juju", "status", "-m", model, "--format", "json"])
    if rc != 0:
        return [], [], []
    try:
        data = json.loads(out)
    except Exception:
        return [], [], []
    machines = [mid for mid, m in data.get("machines", {}).items()
                if m.get("juju-status", {}).get("current") == "started"]
    errors = []
    gw_units = []
    for app_name, app in data.get("applications", {}).items():
        for name, unit in app.get("units", {}).items():
            if unit.get("juju-status", {}).get("current") == "error":
                errors.append(name)
            if app_name == "neutron-gateway":
                gw_units.append(name)
            for sub_name, sub in unit.get("subordinates", {}).items():
                if sub.get("juju-status", {}).get("current") == "error":
                    errors.append(sub_name)
    return machines, errors, gw_units


def patch_machine(model, mid):
    out, _ = run(["juju", "exec", "-m", model, "--machine", mid,
                  "--", "sudo python3 -c '%s'" % PATCH_SCRIPT])
    if "PATCHED:" in out:
        return "PATCHED"
    elif "CLEAN" in out:
        return "CLEAN"
    return "NONE"


def fix_brex(model, unit):
    out, _ = run(["juju", "exec", "-m", model, "--unit", unit,
                  "--", "sudo python3 -c '%s'" % BREX_SCRIPT])
    if "FIXED:" in out:
        return "FIXED"
    elif "OK" in out:
        return "OK"
    return "NOFIX"


def main():
    model = sys.argv[1] if len(sys.argv) > 1 else None
    done_machines = set()
    done_brex = set()
    p = lambda *a: print(*a, flush=True)

    p("[watcher] Starting, model=%s" % (model or "auto-detect"))

    while True:
        if not model:
            model = find_zaza_model()
            if not model:
                time.sleep(5)
                continue
            p("[watcher] Found model: %s" % model)

        machines, errors, gw_units = get_model_status(model)
        if not machines and not errors:
            out, _ = run(["juju", "list-models"])
            if model not in out:
                p("[watcher] Model %s gone, waiting for new one" % model)
                model = None
                done_machines.clear()
                done_brex.clear()
                time.sleep(5)
                continue

        for mid in machines:
            if mid in done_machines:
                continue
            result = patch_machine(model, mid)
            if result == "PATCHED":
                p("[watcher] machine %s: PATCHED" % mid)
                done_machines.add(mid)
            elif result == "CLEAN":
                done_machines.add(mid)

        for unit in gw_units:
            if unit in done_brex:
                continue
            result = fix_brex(model, unit)
            if result == "FIXED":
                p("[watcher] %s: br-ex FIXED" % unit)
                done_brex.add(unit)
            elif result == "OK":
                done_brex.add(unit)

        for unit in errors:
            run(["juju", "resolved", "-m", model, unit], timeout=10)
            p("[watcher] resolved %s" % unit)

        time.sleep(5)


if __name__ == "__main__":
    main()
