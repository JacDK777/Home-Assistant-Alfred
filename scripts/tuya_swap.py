"""Swap Tuya cloud entities on a dashboard for their Tuya Local twins.

Dry run by default: prints the mapping. With --apply it backs up the
dashboard file and writes the swapped version (restart HA afterwards).
"""
import difflib, json, os, re, shutil, sys, time

ST = os.environ.get("HA_STORAGE", "/homeassistant/.storage")
WANT = os.environ.get("DASH", "main")
APPLY = "--apply" in sys.argv


def load(name):
    with open(os.path.join(ST, name), encoding="utf-8") as f:
        return json.load(f)


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


ents = load("core.entity_registry")["data"]["entities"]
devs = {d["id"]: d for d in load("core.device_registry")["data"]["devices"]}

# Find the dashboard
dash_file, dash_title = None, None
try:
    items = load("lovelace_dashboards")["data"]["items"]
except FileNotFoundError:
    items = []
for it in items:
    if WANT in norm(it.get("title")) or WANT in norm(it.get("url_path")):
        dash_file, dash_title = "lovelace." + it["id"], it.get("title")
        break
if not dash_file:
    for fn in sorted(os.listdir(ST)):
        if fn.startswith("lovelace") and fn != "lovelace_dashboards" and not fn.endswith(".bak"):
            cfg = load(fn)["data"].get("config", {})
            if WANT in norm(cfg.get("title")):
                dash_file, dash_title = fn, cfg.get("title")
                break
if not dash_file:
    print("Could not find a dashboard matching", repr(WANT))
    print("Dashboards:", [(i.get("title"), i.get("url_path")) for i in items])
    sys.exit(1)

doc = load(dash_file)
text = json.dumps(doc["data"]["config"], ensure_ascii=False)
print(f"Dashboard: {dash_title}  ({dash_file})\n")

by_id = {e["entity_id"]: e for e in ents}
used = [e for e in sorted(set(re.findall(r"\b[a-z_]+\.[a-z0-9_]+\b", text))) if e in by_id]
tuya_used = [e for e in used if by_id[e]["platform"] == "tuya"]
local = [e for e in ents if e["platform"] == "tuya_local" and not e.get("disabled_by")]


def tuya_ids(dev):
    return {i[1] for i in (dev or {}).get("identifiers", []) if i[0] in ("tuya", "tuya_local")}


def dname(dev):
    return norm((dev or {}).get("name_by_user") or (dev or {}).get("name"))


def ename(e):
    return norm(e.get("name") or e.get("original_name") or e["entity_id"].split(".", 1)[1])


SWAPPABLE = ("switch", "light")


def gang(e):
    m = re.search(r"(\d+)$", e["entity_id"]) or re.search(r"(\d+)$", ename(e))
    return m and m.group(1)


def dev_matches(cdev, ldev):
    if tuya_ids(cdev) & tuya_ids(ldev):
        return True
    return bool(dname(cdev)) and dname(cdev) == dname(ldev)


mapping, unmatched, dev_map = {}, [], {}
for eid in tuya_used:
    ce = by_id[eid]
    dom = eid.split(".")[0]
    cdev = devs.get(ce.get("device_id"))
    on_dev = [l for l in local if dev_matches(cdev, devs.get(l.get("device_id")))]
    cands = [l for l in on_dev if l["entity_id"].split(".")[0] == dom]
    note = ""
    if not cands and dom in SWAPPABLE:  # Tuya Local often makes switch gangs into lights
        cands = [l for l in on_dev if l["entity_id"].split(".")[0] in SWAPPABLE]
        if cands:
            note = "  [switch/light swap, check]"
    if not cands and on_dev:  # last resort: any entity on the same physical device
        cands, note = on_dev, "  [different type on same device, check]"
    if not cands:  # fall back to whole-name match across all Tuya Local entities
        full = lambda e, d: norm(f"{dname(d)} {ename(e)}")
        target = full(ce, cdev)
        scored = sorted(((difflib.SequenceMatcher(None, target, full(l, devs.get(l.get("device_id")))).ratio(), l)
                         for l in local if l["entity_id"].split(".")[0] == dom), key=lambda x: -x[0])
        if scored and scored[0][0] >= 0.85:
            cands, note = [scored[0][1]], f"  [name match {scored[0][0]:.0%}, check]"
    if len(cands) > 1:
        num = gang(ce)
        same = [l for l in cands if num and gang(l) == num]
        if len(same) == 1:
            cands, note = same, note or f"  [gang {num} of {len(cands)}]"
        else:
            scored = sorted(cands, key=lambda l: -difflib.SequenceMatcher(None, ename(ce), ename(l)).ratio())
            cands, note = scored[:1], f"  [picked from {len(scored)} on device, check]"
    if cands:
        le = cands[0]
        mapping[eid] = le["entity_id"]
        if ce.get("device_id") and le.get("device_id"):
            dev_map[ce["device_id"]] = le["device_id"]
        print(f"  {eid:45} -> {le['entity_id']}{note}")
    else:
        unmatched.append(eid)

print(f"\n{len(mapping)} of {len(tuya_used)} Tuya cloud entities matched "
      f"({len(used) - len(tuya_used)} other entities left as they are).")
if unmatched:
    print("No Tuya Local match (left unchanged):")
    for eid in unmatched:
        print("  ", eid, "-", by_id[eid].get("original_name") or "")

# Tuya cloud devices referenced directly (device cards, tile features, ...)
for did in set(re.findall(r'"device_id":\s*"([0-9a-f]{32})"', text)):
    cdev = devs.get(did)
    if did in dev_map or not cdev or not any(i[0] == "tuya" for i in cdev.get("identifiers", [])):
        continue
    twin = [d for d in devs.values() if d["id"] != did and any(i[0] == "tuya_local" for i in d.get("identifiers", []))
            and dev_matches(cdev, d)]
    if twin:
        dev_map[did] = twin[0]["id"]
        print(f"  device {dname(cdev)!r:40} -> Tuya Local device {dname(twin[0])!r}")
    else:
        print(f"  device {dname(cdev)!r:40} has no Tuya Local device (left unchanged)")

new = text
for old, rep in {**mapping, **dev_map}.items():
    new = re.sub(r"(?<![\w.])" + re.escape(old) + r"(?![\w])", rep, new)

left = [e for e in set(re.findall(r"\b[a-z_]+\.[a-z0-9_]+\b", new)) if e in by_id and by_id[e]["platform"] == "tuya"]
print(f"After the swap, {len(left)} Tuya cloud entities would remain on the dashboard"
      + (": " + ", ".join(sorted(left)) if left else "."))

if not APPLY:
    print("\nDry run only, nothing changed. Re-run with --apply to swap them.")
    sys.exit(0)
bak = os.path.join(ST, f"{dash_file}.{time.strftime('%Y%m%d-%H%M%S')}.bak")
shutil.copy2(os.path.join(ST, dash_file), bak)
doc["data"]["config"] = json.loads(new)
with open(os.path.join(ST, dash_file), "w", encoding="utf-8") as f:
    json.dump(doc, f, indent=4, ensure_ascii=False)
print(f"\nSwapped. Backup saved as {bak}. Restart Home Assistant to load it.")
