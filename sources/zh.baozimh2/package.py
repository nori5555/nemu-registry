#!/usr/bin/env python3
"""Package an already built Baozimh2 WASM. Never downloads or publishes anything."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parent
p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--wasm", type=Path, default=root / "target/wasm32-unknown-unknown/release/baozimh2.wasm")
p.add_argument("--out", type=Path, default=root / "package.aix")
a = p.parse_args()
wasm = a.wasm.read_bytes()
if not wasm.startswith(b"\0asm\x01\0\0\0"):
    raise SystemExit("Not a WebAssembly 1 binary")
manifest = json.loads((root / "res/source.json").read_text())
assert manifest["info"]["id"] == "zh.baozimh2"
version = manifest["info"]["version"]
assert version > 0
for name in ("filters.json", "settings.json"):
    json.loads((root / "res" / name).read_text())
icon = (root / "res/icon.png").read_bytes()
assert icon.startswith(b"\x89PNG\r\n\x1a\n")
entries = {"Payload/main.wasm": wasm}
for f in sorted((root / "res").iterdir()):
    if f.is_file():
        entries["Payload/" + f.name] = f.read_bytes()
for name in ("LICENSE", "NOTICE"):
    entries["Payload/" + name] = (root / name).read_bytes()
a.out.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(a.out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for name, data in sorted(entries.items()):
        info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        z.writestr(info, data)
with zipfile.ZipFile(a.out) as z:
    assert z.testzip() is None
print(json.dumps({"file": str(a.out), "bytes": a.out.stat().st_size,
    "sha256": hashlib.sha256(a.out.read_bytes()).hexdigest(),
    "wasm_sha256": hashlib.sha256(wasm).hexdigest(), "files": sorted(entries)}, indent=2))
