#!/usr/bin/env python3
"""Build a Chinese-only Aidoku directory from a converted index.

Keeps entries whose language list is entirely zh* (no multi-language or foreign
sources) and copies the matching icons next to the output index.
"""
import argparse
import json
import os
import shutil
import urllib.parse

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--index", default="index.json", help="converted Aidoku index")
parser.add_argument("--icons", default="icons", help="local icon directory")
parser.add_argument("--out", default="zh", help="output directory")
parser.add_argument("--name", default="中文源", help="list name")
args = parser.parse_args()

with open(args.index, encoding="utf-8") as fh:
    payload = json.load(fh)


def pure_chinese(entry):
    langs = [str(x).lower() for x in (entry.get("languages") or [])]
    return bool(langs) and all(x.startswith("zh") for x in langs)


kept = [s for s in payload.get("sources", []) if pure_chinese(s)]
if not kept:
    raise SystemExit("no Chinese sources found")

shutil.rmtree(args.out, ignore_errors=True)
os.makedirs(os.path.join(args.out, "icons"), exist_ok=True)

missing = []
for src in kept:
    icon = src.get("iconURL") or ""
    name = os.path.basename(urllib.parse.urlparse(icon).path)
    local = os.path.join(args.icons, name)
    if name and os.path.exists(local):
        shutil.copy2(local, os.path.join(args.out, "icons", name))
        src["iconURL"] = "icons/" + name
    else:
        missing.append(src["id"])
        src["iconURL"] = None

out = {"name": args.name, "sources": kept}
with open(os.path.join(args.out, "index.json"), "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
with open(os.path.join(args.out, "index.min.json"), "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))

print(json.dumps({"kept": len(kept), "missingIcons": missing, "out": args.out},
                 ensure_ascii=False))
