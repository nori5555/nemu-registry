#!/usr/bin/env python3
"""mihon2aidoku - convert Mihon/Tachiyomi/Keiyoushi repository indexes into
Aidoku source lists, and cross-map them against existing Aidoku (.aix) sources.

Design rule: never invent installable packages. An Aidoku source list entry must
point at a real `.aix`. Mihon APK/JAR files are NOT `.aix`; they are recorded in
catalog.json only and listed as unsupported for installation.

Stdlib only. Python 3.9+.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Tuple

USER_AGENT = "mihon2aidoku/1.0 (+https://github.com/keiyoushi/extensions)"
DEFAULT_MIHON = "https://raw.githubusercontent.com/keiyoushi/extensions/repo/index.json"
DEFAULT_AIDOKU = "https://aidoku-community.github.io/sources/index.min.json"
FORMAT_CATALOG = "mihon2aidoku-catalog/1"


# --------------------------------------------------------------------------- io
def fetch(url: str, cache_dir: Optional[str] = None, refresh: bool = False,
          timeout: int = 60, retries: int = 3) -> bytes:
    """Download a URL (or read a local path / file:// URL)."""
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        with open(os.path.expanduser(url), "rb") as fh:
            return fh.read()
    if url.startswith("file://"):
        with open(urllib.parse.urlparse(url).path, "rb") as fh:
            return fh.read()

    cache_path = None
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        key = hashlib.sha256(url.encode()).hexdigest()[:16]
        cache_path = os.path.join(cache_dir, key + ".json")
        if not refresh and os.path.exists(cache_path):
            with open(cache_path, "rb") as fh:
                return fh.read()

    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, */*",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            if cache_path:
                with open(cache_path, "wb") as fh:
                    fh.write(data)
            return data
        except Exception as exc:  # noqa: BLE001 - retry any transport error
            last = exc
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"failed to fetch {url}: {last}")


def load_json_bytes(data: bytes, origin: str) -> Any:
    try:
        return json.loads(data.decode("utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"invalid JSON from {origin}: {exc}") from exc


def write_json(path: str, payload: Any, minify: bool = False) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        if minify:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")


# ---------------------------------------------------------------------- helpers
def norm_name(value: str) -> str:
    """Casefold, strip accents and non-alphanumerics. Keeps CJK characters."""
    value = unicodedata.normalize("NFKD", value or "").casefold()
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return "".join(ch for ch in value if ch.isalnum())


def host_of(url: Optional[str]) -> str:
    if not url:
        return ""
    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = (urllib.parse.urlsplit(raw).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def base_lang(code: str) -> str:
    return (code or "").strip().replace("_", "-").split("-")[0].lower()


def languages_compatible(mihon_lang: str, aidoku_langs: Iterable[str]) -> bool:
    """'all' matches anything; otherwise require the primary subtag to match."""
    langs = {base_lang(x) for x in (aidoku_langs or []) if x}
    if not langs or {"multi", "all"} & langs:
        return True
    lang = base_lang(mihon_lang)
    if lang in ("", "all", "multi"):
        return True
    return lang in langs


def absolutize(value: Optional[str], base_url: str) -> Optional[str]:
    if not value:
        return None
    return urllib.parse.urljoin(base_url, value)


def nsfw_from_warning(warning: Optional[str]) -> int:
    """Map Mihon CONTENT_WARNING_* to Aidoku SourceContentRating raw values."""
    mapping = {
        "CONTENT_WARNING_SAFE": 0,
        "CONTENT_WARNING_MIXED": 1,
        "CONTENT_WARNING_NSFW": 2,
    }
    return mapping.get((warning or "").upper(), 1)


# --------------------------------------------------------------- mihon parsing
def parse_mihon(payload: Any) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Normalise any known Mihon/Tachiyomi index shape into (repo, extensions)."""
    repo: Dict[str, Any] = {"name": "Mihon", "contact": {}, "signingKey": None, "url": None}
    if isinstance(payload, list):
        raw_extensions = payload
    elif isinstance(payload, dict):
        for key in ("name", "badgeLabel", "signingKey", "contact"):
            if key in payload:
                repo[key] = payload[key]
        if isinstance(payload.get("extensionList"), dict):
            repo["name"] = payload.get("name") or repo["name"]
            raw_extensions = payload["extensionList"].get("extensions") or []
        elif isinstance(payload.get("extensions"), list):
            raw_extensions = payload["extensions"]
        elif payload.get("packageName") or payload.get("pkg"):
            raw_extensions = [payload]
        else:
            raw_extensions = []
    else:
        raise RuntimeError("unrecognised Mihon index: expected object or array")

    extensions = []
    for raw in raw_extensions:
        if not isinstance(raw, dict):
            continue
        res = raw.get("resources") or {}
        sources = []
        for src in raw.get("sources") or []:
            if not isinstance(src, dict):
                continue
            sources.append({
                "id": str(src.get("id", "")),
                "name": src.get("name") or raw.get("name") or "",
                "language": src.get("language") or raw.get("lang") or "all",
                "homeUrl": src.get("homeUrl") or src.get("baseUrl") or "",
            })
        version_code = raw.get("versionCode", raw.get("version"))
        try:
            version_code_int = int(version_code) if version_code is not None else 0
        except (TypeError, ValueError):
            version_code_int = 0
        extensions.append({
            "name": raw.get("name") or "",
            "packageName": raw.get("packageName") or raw.get("pkg") or "",
            "versionName": str(raw.get("versionName") or raw.get("version") or ""),
            "versionCode": version_code_int,
            "contentWarning": raw.get("contentWarning") or raw.get("nsfw"),
            "resources": {
                "apkUrl": raw.get("apkUrl") or res.get("apkUrl") or raw.get("apk"),
                "jarUrl": raw.get("jarUrl") or res.get("jarUrl"),
                "iconUrl": raw.get("iconUrl") or res.get("iconUrl") or raw.get("icon"),
            },
            "sources": sources,
        })
    return repo, extensions


# --------------------------------------------------------------- aidoku parsing
def parse_aidoku(payload: Any, list_url: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Normalise a modern or legacy Aidoku source list, absolutizing URLs."""
    if isinstance(payload, list):
        meta: Dict[str, Any] = {"name": "Legacy source list"}
        raw_sources = payload
    elif isinstance(payload, dict):
        meta = {"name": payload.get("name") or "Source list"}
        if payload.get("feedbackURL"):
            meta["feedbackURL"] = payload["feedbackURL"]
        raw_sources = payload.get("sources") or []
    else:
        raise RuntimeError("unrecognised Aidoku list: expected object or array")

    sources = []
    for raw in raw_sources:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        langs = raw.get("languages")
        if not langs and raw.get("lang"):
            langs = [raw["lang"]]
        rating = raw.get("contentRating")
        if rating is None:
            rating = raw.get("nsfw")
        try:
            rating_int = int(rating) if rating is not None else 0
        except (TypeError, ValueError):
            rating_int = {"safe": 0, "containsNsfw": 1, "primarilyNsfw": 2}.get(str(rating), 0)
        try:
            version = int(raw.get("version") or 0)
        except (TypeError, ValueError):
            version = 0
        # Legacy keys are bare filenames: the app prefixes them with icons/ or sources/.
        icon_ref = raw.get("iconURL")
        if icon_ref is None and raw.get("icon"):
            icon_ref = "icons/" + str(raw["icon"]).lstrip("/")
        download_ref = raw.get("downloadURL")
        if download_ref is None and raw.get("file"):
            download_ref = "sources/" + str(raw["file"]).lstrip("/")

        sources.append({
            "id": str(raw["id"]),
            "name": raw.get("name") or raw["id"],
            "altNames": [x for x in (raw.get("altNames") or []) if x],
            "version": version,
            "iconURL": absolutize(icon_ref, list_url),
            "downloadURL": absolutize(download_ref, list_url),
            "languages": [x for x in (langs or []) if x],
            "contentRating": rating_int,
            "baseURL": raw.get("baseURL"),
            "minAppVersion": raw.get("minAppVersion"),
            "maxAppVersion": raw.get("maxAppVersion"),
        })
    return meta, sources


# --------------------------------------------------------------------- matching
def score_match(src: Dict[str, Any], entry: Dict[str, Any]) -> Tuple[float, str]:
    """Return (score, method). 0 means no usable match."""
    if not languages_compatible(src.get("language", "all"), entry.get("languages")):
        return 0.0, "language-mismatch"
    h1 = host_of(src.get("homeUrl"))
    h2 = host_of(entry.get("baseURL"))
    n1 = norm_name(src.get("name", ""))
    names = [norm_name(entry.get("name", ""))] + [norm_name(x) for x in entry.get("altNames") or []]
    names = [x for x in names if x]
    name_eq = bool(n1) and n1 in names
    ratio = max((difflib.SequenceMatcher(None, n1, n).ratio() for n in names), default=0.0)
    host_eq = bool(h1) and h1 == h2

    if host_eq and name_eq:
        return 1.0, "host+name"
    if host_eq and ratio >= 0.75:
        return 0.90, "host+fuzzy-name"
    if host_eq:
        return 0.86, "host"
    if name_eq:
        return 0.82, "name"
    if ratio >= 0.94:
        return 0.60, "fuzzy-name"
    return 0.0, "no-match"


def match_sources(extensions: List[Dict[str, Any]], aidoku: List[Dict[str, Any]],
                  min_score: float = 0.8) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Greedy one-to-one matching. Returns (mapping records, best match per source key)."""
    candidates = []
    for ei, ext in enumerate(extensions):
        for si, src in enumerate(ext["sources"]):
            for ai, entry in enumerate(aidoku):
                score, method = score_match(src, entry)
                if score >= min_score:
                    candidates.append((score, method, ei, si, ai))

    # Highest score first; prefer sources with a real language over "all",
    # then prefer identical names, then stable ordering.
    def rank(item: Tuple[float, str, int, int, int]):
        score, _method, ei, si, ai = item
        lang = extensions[ei]["sources"][si].get("language", "all")
        return (-score, 0 if lang != "all" else 1, norm_name(aidoku[ai]["name"]) != norm_name(extensions[ei]["sources"][si]["name"]), ei, si, ai)

    candidates.sort(key=rank)
    used_sources = set()
    used_entries = set()
    records = []
    for score, method, ei, si, ai in candidates:
        if (ei, si) in used_sources or ai in used_entries:
            continue
        used_sources.add((ei, si))
        used_entries.add(ai)
        ext = extensions[ei]
        src = ext["sources"][si]
        entry = aidoku[ai]
        records.append({
            "packageName": ext["packageName"],
            "extensionName": ext["name"],
            "mihonSource": src,
            "aidokuSourceId": entry["id"],
            "aidokuName": entry["name"],
            "aidokuVersion": entry["version"],
            "downloadURL": entry["downloadURL"],
            "score": round(score, 3),
            "method": method,
        })
    for rec in records:
        rec["confidence"] = "high" if rec["score"] >= 0.9 else "medium"
    return records, {f"{r['packageName']}#{r['mihonSource']['id']}": r for r in records}


# --------------------------------------------------------------------- catalog
def build_catalog(repo: Dict[str, Any], input_url: str, extensions: List[Dict[str, Any]],
                  matches: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    catalog_exts = []
    stats = {"extensions": len(extensions), "sources": 0, "matchedSources": 0, "matchedExtensions": 0}
    for ext in extensions:
        sources = []
        ext_matched = False
        for src in ext["sources"]:
            key = f"{ext['packageName']}#{src['id']}"
            rec = matches.get(key)
            if rec:
                ext_matched = True
                stats["matchedSources"] += 1
            stats["sources"] += 1
            sources.append({
                "id": src["id"],
                "name": src["name"],
                "language": src["language"],
                "homeUrl": src["homeUrl"],
                "aidokuSourceId": rec["aidokuSourceId"] if rec else None,
                "installableInAidoku": bool(rec),
            })
        if ext_matched:
            stats["matchedExtensions"] += 1
        catalog_exts.append({
            "name": ext["name"],
            "packageName": ext["packageName"],
            "versionName": ext["versionName"],
            "versionCode": ext["versionCode"],
            "contentWarning": ext["contentWarning"],
            "nsfw": nsfw_from_warning(ext["contentWarning"]),
            "resources": ext["resources"],
            "aidokuReady": ext_matched,
            "sources": sources,
        })
    return {
        "format": FORMAT_CATALOG,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repository": {
            "name": repo.get("name"),
            "badgeLabel": repo.get("badgeLabel"),
            "url": input_url,
            "signingKey": repo.get("signingKey"),
            "contact": repo.get("contact"),
        },
        "stats": stats,
        "note": (
            "Mihon/Tachiyomi extensions ship JAR/APK bytecode and cannot be loaded by Aidoku. "
            "Only sources with aidokuSourceId set have a real .aix package and appear in index.json."
        ),
        "extensions": catalog_exts,
    }


def build_unsupported(catalog: Dict[str, Any]) -> Dict[str, Any]:
    items = []
    for ext in catalog["extensions"]:
        if ext["aidokuReady"]:
            continue
        items.append({
            "name": ext["name"],
            "packageName": ext["packageName"],
            "versionName": ext["versionName"],
            "contentWarning": ext["contentWarning"],
            "languages": sorted({s["language"] for s in ext["sources"]}),
            "sources": [{"id": s["id"], "name": s["name"], "language": s["language"], "homeUrl": s["homeUrl"]}
                        for s in ext["sources"]],
            "reason": "no Aidoku (.aix) implementation found",
            "nextStep": "port the extension to a Rust/WASM Aidoku source, then re-run with --aidoku <new list>",
        })
    return {
        "generatedAt": catalog["generatedAt"],
        "count": len(items),
        "totalExtensions": len(catalog["extensions"]),
        "items": items,
    }


def build_index(name: str, entries: List[Dict[str, Any]], feedback: Optional[str] = None) -> Dict[str, Any]:
    payload = {"name": name}
    if feedback:
        payload["feedbackURL"] = feedback
    payload["sources"] = sorted(entries, key=lambda x: (x["id"].split(".")[0], x["name"].casefold()))
    return payload


def build_report(catalog: Dict[str, Any], index: Dict[str, Any], mapping: List[Dict[str, Any]],
                 input_url: str, aidoku_url: str, target: str = "aidoku") -> str:
    stats = catalog["stats"]
    by_lang: Dict[str, int] = {}
    for rec in mapping:
        lang = base_lang(rec["mihonSource"].get("language", "all"))
        by_lang[lang] = by_lang.get(lang, 0) + 1
    top = sorted(by_lang.items(), key=lambda kv: (-kv[1], kv[0]))[:12]
    lines = [
        "# Mihon → Aidoku 转换报告",
        "",
        f"- 生成时间：{catalog['generatedAt']}",
        f"- Mihon 仓库：{input_url}",
        f"- Aidoku 参照列表：{aidoku_url}",
        f"- 扩展总数：**{stats['extensions']}**，站点源：**{stats['sources']}**",
        f"- 已有 Aidoku `.aix` 可安装：**{len(index['sources'])}** 个源（覆盖 {stats['matchedExtensions']} 个扩展）",
        f"- 仅元数据、需移植：**{stats['extensions'] - stats['matchedExtensions']}** 个扩展",
        "",
        "## 可安装源按语言分布",
        "",
    ]
    lines += [f"- `{lang}`：{count}" for lang, count in top] or ["- 无"]
    lines += [
        "",
        "## 安装",
        "",
    ]
    if target == "nemu":
        lines += [
            "Nemu 通过「源目录」加载本目录，要求 **HTTPS**、不接受 localhost/私网地址：",
            "",
            "1. 把本目录（`index.json`、`index.min.json`、`icons/`）发布到 GitHub Pages / Cloudflare Pages；",
            "2. Nemu → 设置 → 源 → 添加源目录，填目录 URL（如 `https://<user>.github.io/<repo>/`）；",
            "3. 应用会自动补 `/index.min.json`；若填完整 JSON 文件地址也可。",
            "",
            "注意：Nemu 内置 `aidoku-community` 目录已包含上面这些 `.aix` 源，本目录的价值是"
            "「Keiyoushi 交集快照 + 统一命名」，不会凭空增加可安装源。",
            "",
            "`meta/` 下的 `catalog.json` / `unsupported.json` 无需发布。",
        ]
    else:
        lines += [
            "把本目录发布到任意 HTTPS 静态托管（GitHub Pages / Cloudflare Pages / 自建），然后在 Aidoku：",
            "",
            "1. 设置 → 源列表 → 添加源列表，填入 `index.json` 的 URL；",
            "2. 或直接打开深链 `aidoku://addSourceList?url=<index.json URL>`。",
            "",
            "本机预览可用 `python3 -m http.server` 起服务；Aidoku 不支持 `file://`。",
        ]
    lines += [
        "",
        "## 边界",
        "",
        "- `index.json` 只包含已验证存在 `.aix` 的条目，全部 URL 已绝对化，换托管位置不会断链；",
        "- Mihon 的 APK/JAR 是 JVM 字节码，Aidoku 不能执行，因此未移植的源只存在于 `catalog.json` / `unsupported.json`；",
        "- 匹配依据为站点域名 + 名称（含 altNames），报告中的 `method` / `score` 可人工复核；",
        "- 匹配到的是同名站点的 Aidoku 独立实现，章节解析逻辑与 Mihon 扩展并非同一份代码。",
        "",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ nemu icons
def bundle_icons(entries: List[Dict[str, Any]], out_dir: str, cache_dir: Optional[str],
                 refresh: bool = False, workers: int = 8) -> Dict[str, Any]:
    """Download icons next to the index and rewrite iconURL to a relative path.

    nemu concatenates `${registryBaseUrl}/${iconPath}` when saving an installed
    source, so an absolute icon URL produces a broken link. Relative paths are
    the only safe form for a nemu directory.
    """
    icons_dir = os.path.join(out_dir, "icons")
    os.makedirs(icons_dir, exist_ok=True)
    planned: List[Tuple[Dict[str, Any], str, str]] = []
    used = set()
    for entry in entries:
        url = entry.get("iconURL")
        if not url:
            continue
        base = os.path.basename(urllib.parse.urlparse(url).path) or ""
        if not base or not re.search(r"\.(png|jpe?g|webp|svg)$", base, re.I):
            base = f"{entry['id']}.png"
        name = base
        if name.casefold() in used:
            name = f"{entry['id']}-{base}"
        used.add(name.casefold())
        planned.append((entry, url, name))

    failures: List[str] = []

    def download(item: Tuple[Dict[str, Any], str, str]) -> Optional[str]:
        entry, url, name = item
        try:
            data = fetch(url, cache_dir, refresh)
            if len(data) < 64:
                raise RuntimeError("suspiciously small icon payload")
            with open(os.path.join(icons_dir, name), "wb") as fh:
                fh.write(data)
            return None
        except Exception as exc:  # noqa: BLE001
            return f"{entry['id']}: {exc}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for result in pool.map(download, planned):
            if result:
                failures.append(result)
    failed_ids = {item.split(":", 1)[0] for item in failures}
    for entry, _url, name in planned:
        if entry["id"] not in failed_ids:
            entry["iconURL"] = "icons/" + name
    return {"bundled": len(planned) - len(failures), "failed": failures,
            "directory": icons_dir if planned else None}


# ---------------------------------------------------------------- validation
def validate_index(payload: Dict[str, Any], check_urls: bool = False,
                   timeout: int = 20, target: str = "aidoku") -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []
    sources = payload.get("sources")
    if not isinstance(payload, dict) or not isinstance(sources, list):
        return {"ok": False, "errors": ["not an Aidoku source list with a 'sources' array"], "warnings": []}
    if not payload.get("name"):
        warnings.append("missing top-level name")
    seen_ids, seen_files = set(), set()
    for i, src in enumerate(sources):
        tag = src.get("id") or f"#{i}"
        if not src.get("id"):
            errors.append(f"{tag}: missing id")
        elif src["id"] in seen_ids:
            errors.append(f"{tag}: duplicate id")
        seen_ids.add(src.get("id"))
        if not isinstance(src.get("name"), str) or not src.get("name"):
            errors.append(f"{tag}: missing name")
        if not isinstance(src.get("version"), int) or src.get("version", 0) <= 0:
            errors.append(f"{tag}: version must be a positive integer")
        if src.get("contentRating") not in (0, 1, 2):
            errors.append(f"{tag}: contentRating must be 0, 1 or 2")
        if not src.get("languages"):
            warnings.append(f"{tag}: empty languages array")
        for field in ("downloadURL", "iconURL"):
            url = src.get(field)
            if url is None:
                if field == "downloadURL":
                    errors.append(f"{tag}: missing downloadURL")
                continue
            absolute = bool(re.match(r"^https?://", url, re.I))
            if target == "nemu" and field == "iconURL":
                # nemu builds the installed icon URL as base + "/" + iconPath,
                # so a relative path is the correct form and an absolute one breaks.
                if not absolute and not re.match(r"^[A-Za-z0-9._~%!$&'()*+,;=:@/-]+$", url):
                    errors.append(f"{tag}: iconURL is not a usable relative path: {url}")
                continue
            if not absolute:
                errors.append(f"{tag}: {field} not absolute http(s): {url}")
            elif target == "nemu" and field == "downloadURL" and url.lower().startswith("http://"):
                warnings.append(f"{tag}: downloadURL uses http; nemu pages are https (mixed content)")
        dl = src.get("downloadURL") or ""
        if dl and not dl.split("?")[0].endswith(".aix"):
            errors.append(f"{tag}: downloadURL does not end with .aix")
        if dl:
            if dl in seen_files:
                warnings.append(f"{tag}: duplicate downloadURL {dl}")
            seen_files.add(dl)

    if target == "nemu":
        for src_entry in sources:
            icon = src_entry.get("iconURL") or ""
            if icon and re.match(r"^[a-z][a-z0-9+.-]*://", icon, re.I):
                warnings.append(
                    f"{src_entry.get('id')}: absolute iconURL; nemu concatenates "
                    "registry base + icon path, bundle icons instead")

    if check_urls and sources:
        def probe(url: str) -> Tuple[str, str]:
            last = "unknown"
            for attempt in range(3):
                for method in ("HEAD", "GET"):
                    try:
                        req = urllib.request.Request(url, method=method,
                                                     headers={"User-Agent": USER_AGENT})
                        with urllib.request.urlopen(req, timeout=timeout) as resp:
                            if 200 <= resp.status < 400:
                                return url, f"ok {resp.status}"
                    except Exception as exc:  # noqa: BLE001
                        last = str(exc)
                time.sleep(1.5 * (attempt + 1))
            return url, f"fail {last}"
        urls = sorted({s["downloadURL"] for s in sources if s.get("downloadURL")})
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            for url, status in pool.map(probe, urls):
                if not status.startswith("ok"):
                    errors.append(f"unreachable: {url} ({status})")
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "count": len(sources), "checkedUrls": check_urls}


def diff_lists(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    def keyed(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        return {s["id"]: s for s in payload.get("sources", []) if s.get("id")}
    a, b = keyed(old), keyed(new)
    added = [b[k] for k in sorted(b.keys() - a.keys())]
    removed = [a[k] for k in sorted(a.keys() - b.keys())]
    updated = []
    for k in sorted(a.keys() & b.keys()):
        before, after = a[k], b[k]
        changed = {f: [before.get(f), after.get(f)] for f in
                   ("name", "version", "downloadURL", "iconURL", "languages", "contentRating", "baseURL")
                   if before.get(f) != after.get(f)}
        if changed:
            updated.append({"id": k, "changed": changed})
    return {
        "summary": {"added": len(added), "removed": len(removed), "updated": len(updated),
                    "unchanged": len(a.keys() & b.keys()) - len(updated)},
        "added": added, "removed": removed, "updated": updated,
    }

# ------------------------------------------------------------ runtime probing
PROBE_BUNDLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe-runtime.cjs")


def classify_probe(report: Dict[str, Any]) -> str:
    """Turn a probe report into a coarse status.

    ok       every core stage worked
    partial  search worked, details/chapters/pages did not
    blocked  Cloudflare or an HTTP 4xx wall in front of the source
    failed   nothing usable
    """
    stages = report.get("stages") or {}
    core = ("search", "details", "chapters", "pages")
    if report.get("ok") or all(stages.get(s) for s in core):
        return "ok"
    text = " ".join(str(e.get("message", "")) for e in report.get("errors") or []).lower()
    if "cloudflare" in text or "challenge" in text or "captcha" in text:
        return "blocked"
    if re.search(r"\b(400|403|429|451)\b|bad request|forbidden|too many requests", text):
        return "blocked"
    if "no results" in text:
        return "empty"
    if stages.get("search"):
        return "partial"
    return "failed"


def probe_source(entry: Dict[str, Any], cache_dir: Optional[str], query: str,
                 image: bool, timeout: int, node: str, refresh: bool) -> Dict[str, Any]:
    started = time.time()
    record: Dict[str, Any] = {
        "id": entry["id"],
        "name": entry.get("name"),
        "version": entry.get("version"),
        "downloadURL": entry.get("downloadURL"),
    }
    aix_path = None
    try:
        if not os.path.exists(PROBE_BUNDLE):
            raise RuntimeError(f"missing probe runtime: {PROBE_BUNDLE}")
        data = fetch(entry["downloadURL"], cache_dir, refresh)
        aix_dir = os.path.join(cache_dir or "/tmp", "aix")
        os.makedirs(aix_dir, exist_ok=True)
        aix_path = os.path.join(aix_dir, re.sub(r"[^A-Za-z0-9._-]", "_", entry["id"]) + ".aix")
        with open(aix_path, "wb") as fh:
            fh.write(data)
        cmd = [node, PROBE_BUNDLE, aix_path, entry["id"], query, f"--timeout-ms={timeout * 1000}"]
        if image:
            cmd.append("--image")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
        payload = None
        for line in reversed((proc.stdout or "").strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    payload = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        if payload is None:
            raise RuntimeError((proc.stderr or proc.stdout or "no probe output").strip()[-400:])
        record.update(payload)
    except subprocess.TimeoutExpired:
        record.update({"ok": False, "stages": {}, "errors": [{"stage": "timeout",
                        "message": f"probe exceeded {timeout}s"}]})
    except Exception as exc:  # noqa: BLE001
        record.update({"ok": False, "stages": {}, "errors": [{"stage": "probe",
                        "message": str(exc)[:400]}]})
    finally:
        if aix_path and os.path.exists(aix_path):
            try:
                os.unlink(aix_path)
            except OSError:
                pass
    record["status"] = classify_probe(record)
    record["checkedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    record["elapsedMs"] = int((time.time() - started) * 1000)
    return record


def cmd_probe(args: argparse.Namespace) -> int:
    payload = load_json_bytes(fetch(args.list), args.list)
    _, entries = parse_aidoku(payload, args.list)
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        entries = [e for e in entries if e["id"] in wanted]
    if args.limit:
        entries = entries[: args.limit]
    if not entries:
        print(json.dumps({"ok": False, "error": "no sources selected"}, ensure_ascii=False))
        return 2
    cache_dir = args.cache_dir or os.path.join(os.path.dirname(os.path.abspath(args.list)), ".cache")

    results: List[Dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(probe_source, entry, cache_dir, args.query, args.image,
                        args.timeout, args.node, args.refresh): entry["id"]
            for entry in entries
        }
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
            done = len(results)
            if args.progress and done % 10 == 0:
                print(f"progress {done}/{len(entries)}", file=sys.stderr)
    results.sort(key=lambda r: (r["status"], r["id"]))

    summary = {
        "total": len(results),
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "partial": sum(1 for r in results if r["status"] == "partial"),
        "blocked": sum(1 for r in results if r["status"] == "blocked"),
        "empty": sum(1 for r in results if r["status"] == "empty"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
    }
    report = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "query": args.query,
        "imageCheck": args.image,
        "summary": summary,
        "results": results,
    }
    if args.out:
        write_json(args.out, report)
    print(json.dumps({
        "ok": summary["ok"] > 0,
        **summary,
        "out": args.out,
        "failures": [
            {"id": r["id"], "status": r["status"],
             "error": (r["errors"][0]["message"][:120] if r.get("errors") else "")}
            for r in results if r["status"] != "ok"
        ][: args.show_failures],
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_filter(args: argparse.Namespace) -> int:
    payload = load_json_bytes(fetch(args.list), args.list)
    meta, entries = parse_aidoku(payload, args.list)
    health = load_json_bytes(fetch(args.health), args.health)
    by_id = {r["id"]: r for r in health.get("results") or []}
    keep = {x.strip() for x in args.status.split(",") if x.strip()}

    kept, dropped = [], []
    for entry in entries:
        record = by_id.get(entry["id"])
        status = record["status"] if record else "unchecked"
        if status in keep:
            kept.append(entry)
        else:
            dropped.append({"id": entry["id"], "name": entry["name"], "status": status,
                            "error": (record["errors"][0]["message"][:160]
                                      if record and record.get("errors") else None)})

    # Icons travel with the filtered list: rewrite to relative names under
    # out/icons/, which the caller copies next to the index.
    for entry in kept:
        icon = entry.get("iconURL") or ""
        basename = os.path.basename(urllib.parse.urlparse(icon).path)
        entry["iconURL"] = f"icons/{basename}" if basename else None

    index = build_index(meta.get("name") or args.list_name, kept)
    out_dir = os.path.abspath(os.path.expanduser(args.out))
    os.makedirs(out_dir, exist_ok=True)
    write_json(os.path.join(out_dir, "index.json"), index)
    write_json(os.path.join(out_dir, "index.min.json"), index, minify=True)
    write_json(os.path.join(out_dir, "meta", "dropped.json"),
               {"generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "kept": len(kept), "dropped": dropped})
    print(json.dumps({"ok": True, "kept": len(kept), "dropped": len(dropped),
                      "out": out_dir, "statuses": sorted(keep)}, ensure_ascii=False, indent=2))
    return 0


# ----------------------------------------------------------------------- CLI
def cmd_convert(args: argparse.Namespace) -> int:
    out_dir = os.path.abspath(os.path.expanduser(args.out))
    cache_dir = args.cache_dir or os.path.join(out_dir, "..", "cache")
    os.makedirs(out_dir, exist_ok=True)

    raw = fetch(args.input, cache_dir, args.refresh)
    repo, extensions = parse_mihon(load_json_bytes(raw, args.input))
    repo["url"] = args.input
    if not extensions:
        print(json.dumps({"ok": False, "error": "no extensions parsed from input"}, ensure_ascii=False))
        return 2

    aidoku_entries: List[Dict[str, Any]] = []
    registries: List[Dict[str, Any]] = []
    specs = [s.strip() for s in (args.aidoku or "").split(",") if s.strip()]
    if specs and specs != ["none"]:
        for spec in specs:
            if not re.match(r"^[a-z][a-z0-9+.-]*://", spec, re.I) and not args.aidoku_base:
                print(json.dumps({
                    "ok": False,
                    "error": "local Aidoku list needs --aidoku-base <public index URL> "
                             "so relative .aix paths can be resolved",
                }, ensure_ascii=False))
                return 2
            base = args.aidoku_base or spec
            aidoku_raw = fetch(spec, cache_dir, args.refresh)
            meta, entries = parse_aidoku(load_json_bytes(aidoku_raw, spec), base)
            registries.append({"url": spec, "name": meta.get("name"), "count": len(entries)})
            aidoku_entries.extend(entries)

        # Merge duplicates across directories: newest version wins.
        best: Dict[str, Dict[str, Any]] = {}
        for entry in aidoku_entries:
            current = best.get(entry["id"])
            if current is None or entry["version"] > current["version"]:
                best[entry["id"]] = entry
        aidoku_entries = list(best.values())

    mapping, by_source = match_sources(extensions, aidoku_entries, min_score=args.min_score)
    catalog = build_catalog(repo, args.input, extensions, by_source)

    if args.scope == "none":
        chosen: List[Dict[str, Any]] = []
    elif args.scope == "all":
        chosen = list(aidoku_entries)
    else:
        matched_ids = {r["aidokuSourceId"] for r in mapping}
        chosen = [e for e in aidoku_entries if e["id"] in matched_ids]

    # Safety: an Aidoku list may only reference real .aix packages.
    bad = [e["id"] for e in chosen if not (e.get("downloadURL") or "").endswith(".aix")]
    if bad:
        print(json.dumps({"ok": False, "error": "refusing to write index with non-.aix packages",
                          "ids": bad[:20]}, ensure_ascii=False))
        return 2

    index = build_index(args.list_name, chosen, args.feedback)
    icon_stats: Dict[str, Any] = {"bundled": 0, "failed": [], "directory": None}
    if args.target == "nemu" and not args.no_icons and index["sources"]:
        icon_stats = bundle_icons(index["sources"], out_dir, cache_dir, args.refresh,
                                  workers=args.icon_workers)

    unsupported = build_unsupported(catalog)
    report = build_report(catalog, index, mapping, args.input,
                          args.aidoku if args.aidoku and args.aidoku.lower() != "none" else "disabled",
                          target=args.target)

    meta_dir = os.path.join(out_dir, "meta")
    os.makedirs(meta_dir, exist_ok=True)
    write_json(os.path.join(out_dir, "index.json"), index)
    write_json(os.path.join(out_dir, "index.min.json"), index, minify=True)
    write_json(os.path.join(meta_dir, "catalog.json"), catalog)
    write_json(os.path.join(meta_dir, "unsupported.json"), unsupported)
    write_json(os.path.join(meta_dir, "mapping.json"), {
        "generatedAt": catalog["generatedAt"],
        "target": args.target,
        "minScore": args.min_score,
        "count": len(mapping),
        "matches": mapping,
    })
    with open(os.path.join(meta_dir, "report.md"), "w", encoding="utf-8") as fh:
        fh.write(report)

    summary = {
        "ok": True,
        "out": out_dir,
        "extensions": catalog["stats"]["extensions"],
        "sources": catalog["stats"]["sources"],
        "matchedExtensions": catalog["stats"]["matchedExtensions"],
        "installableSources": len(index["sources"]),
        "unsupportedExtensions": unsupported["count"],
        "target": args.target,
        "registries": registries,
        "icons": icon_stats,
        "publishable": ["index.json", "index.min.json"] + (["icons/"] if icon_stats["bundled"] else []),
        "meta": ["meta/catalog.json", "meta/unsupported.json", "meta/mapping.json", "meta/report.md"],
    }
    if args.target == "nemu":
        summary["registryUrlHint"] = "https://<user>.github.io/<repo>/  (nemu appends /index.min.json)"
    if icon_stats["failed"]:
        summary["iconWarnings"] = icon_stats["failed"][:10]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    old = load_json_bytes(fetch(args.old), args.old)
    new = load_json_bytes(fetch(args.new), args.new)
    if isinstance(old, dict) and old.get("sources") is None:
        print(json.dumps({"ok": False, "error": "old file is not an Aidoku source list"}, ensure_ascii=False))
        return 2
    result = diff_lists(old, new)
    if args.out:
        write_json(args.out, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    payload = load_json_bytes(fetch(args.list), args.list)
    result = validate_index(payload, check_urls=args.check_urls, timeout=args.timeout,
                            target=args.target)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mihon2aidoku",
        description="Convert Mihon/Tachiyomi/Keiyoushi indexes to Aidoku source lists.")
    sub = parser.add_subparsers(dest="command")

    conv = sub.add_parser("convert", help="build an Aidoku source list + full metadata catalog")
    conv.add_argument("--input", default=DEFAULT_MIHON, help="Mihon index URL or local path")
    conv.add_argument("--aidoku", default=DEFAULT_AIDOKU,
                      help="existing Aidoku list(s) used for .aix matching, comma-separated; "
                           "'none' disables matching")
    conv.add_argument("--out", default="./out", help="output directory")
    conv.add_argument("--scope", choices=("matched", "all", "none"), default="matched",
                      help="which sources go into index.json (default: only verified .aix matches)")
    conv.add_argument("--min-score", type=float, default=0.8, help="match threshold 0..1 (default 0.8)")
    conv.add_argument("--list-name", default="Keiyoushi → Aidoku", help="name shown in Aidoku")
    conv.add_argument("--feedback", default=None, help="optional feedbackURL")
    conv.add_argument("--aidoku-base", default=None,
                      help="public index URL to resolve a local Aidoku snapshot's relative paths")
    conv.add_argument("--target", choices=("aidoku", "nemu"), default="aidoku",
                      help="output flavour: aidoku (default) or nemu (relative icons bundled)")
    conv.add_argument("--no-icons", action="store_true",
                      help="nemu target: keep remote icon URLs instead of bundling icons/")
    conv.add_argument("--icon-workers", type=int, default=8, help="parallel icon downloads")
    conv.add_argument("--cache-dir", default=None)
    conv.add_argument("--refresh", action="store_true", help="ignore cached downloads")
    conv.set_defaults(func=cmd_convert)

    prb = sub.add_parser("probe", help="run each source through the Aidoku WASM runtime")
    prb.add_argument("--list", required=True, help="index.json to probe")
    prb.add_argument("--out", default=None, help="write the full health report here")
    prb.add_argument("--only", default=None, help="comma-separated source ids")
    prb.add_argument("--limit", type=int, default=0)
    prb.add_argument("--query", default="one piece", help="search query used for the probe")
    prb.add_argument("--image", action="store_true", help="also fetch cover and first page image")
    prb.add_argument("--workers", type=int, default=3)
    prb.add_argument("--timeout", type=int, default=90, help="per-source seconds")
    prb.add_argument("--node", default="node")
    prb.add_argument("--cache-dir", default=None)
    prb.add_argument("--refresh", action="store_true")
    prb.add_argument("--progress", action="store_true")
    prb.add_argument("--show-failures", type=int, default=20)
    prb.set_defaults(func=cmd_probe)

    flt = sub.add_parser("filter", help="keep only sources that passed a health report")
    flt.add_argument("--list", required=True)
    flt.add_argument("--health", required=True)
    flt.add_argument("--status", default="ok", help="comma list: ok,partial,blocked,failed,unchecked")
    flt.add_argument("--list-name", default="Filtered sources")
    flt.add_argument("--out", required=True)
    flt.set_defaults(func=cmd_filter)

    dif = sub.add_parser("diff", help="compare two Aidoku source lists")
    dif.add_argument("--old", required=True)
    dif.add_argument("--new", required=True)
    dif.add_argument("--out", default=None)
    dif.set_defaults(func=cmd_diff)

    ver = sub.add_parser("verify", help="validate an Aidoku source list")
    ver.add_argument("--list", required=True)
    ver.add_argument("--check-urls", action="store_true")
    ver.add_argument("--timeout", type=int, default=20)
    ver.add_argument("--target", choices=("aidoku", "nemu"), default="aidoku")
    ver.set_defaults(func=cmd_verify)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    commands = {"convert", "diff", "verify", "probe", "filter"}
    if not any(token in commands for token in argv[:1]) and not (argv and argv[0] in commands):
        argv.insert(0, "convert")
    if not argv:
        parser.print_help()
        return 2
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
