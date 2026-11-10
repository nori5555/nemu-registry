#!/usr/bin/env python3
"""Mirror every .aix referenced by a registry index onto local hosting.

raw.githubusercontent.com is unreachable for many users; the built-in Aidoku
Community directory (GitHub Pages) works. This copies each package next to the
index and rewrites `downloadURL` to a relative path, so every client downloads
from the same reachable host.

Usage: python3 tools/mirror_zh.py --index zh/index.json --out zh/sources
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
import zipfile

UA = "mihon2aidoku/1.0"
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--index", default="zh/index.json")
parser.add_argument("--out", default="zh/sources")
args = parser.parse_args()

with open(args.index, encoding="utf-8") as fh:
    payload = json.load(fh)

os.makedirs(args.out, exist_ok=True)
errors = []
for src in payload.get("sources", []):
    url = src.get("downloadURL") or ""
    if not url.startswith("http"):
        continue  # already relative
    name = os.path.basename(urllib.parse.urlparse(url).path)
    target = os.path.join(args.out, name)
    if not os.path.exists(target):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read()
            with open(target, "wb") as fh:
                fh.write(data)
        except Exception as error:  # noqa: BLE001
            errors.append(f"{src.get('id')}: {error}")
            continue
    try:
        with zipfile.ZipFile(target) as archive:
            if "Payload/main.wasm" not in archive.namelist():
                raise ValueError("missing Payload/main.wasm")
    except Exception as error:  # noqa: BLE001
        errors.append(f"{src.get('id')}: invalid package ({error})")
        os.remove(target)
        continue
    src["downloadURL"] = f"sources/{name}"

with open(args.index, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
min_path = args.index.replace("index.json", "index.min.json")
if min_path != args.index:
    with open(min_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))

print(json.dumps({"mirrored": len(payload.get("sources", [])) - len(errors),
                  "errors": errors[:10]}, ensure_ascii=False))
if errors:
    sys.exit(1)
