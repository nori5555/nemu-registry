#!/usr/bin/env python3
"""Auto-port Mihon (Tachiyomi/Keiyoushi) extensions to Aidoku Rust sources.

Semi-automatic by design: a script cannot turn arbitrary Kotlin into Rust.
What it CAN do, reliably:

  T1  Theme subclass. The extension is just `class X : SomeTheme()` and all
      logic lives in the shared theme (lib-multisrc/<theme>). Once the theme
      is ported to Rust once, every site on it is a mechanical instantiation:
      name + baseUrl from build.gradle.kts. v1 implements the `mmlook` theme
      (Dumanwu, Rumanhua). Other themes report "not ported yet" with the
      Kotlin theme path so a human can port it next.
  T2  Custom KeiSource (plain Jsoup/JSON scraping, no JS/crypto/login).
      The script extracts URL templates + CSS selectors + field mappings and
      emits a Rust skeleton with TODO markers; a human finishes it.
  T3  QuickJS / WebView / login / custom crypto / interceptors.
      Not auto-portable. The script only lists the blocking feature.

Classification (2026-11-12, keiyoushi zh extensions not matched to any
community Aidoku source): 43 zh packages -> 11 T1 (6 themes: mmlook x2,
mccmsweb x3, sinmh x3, mccms x1, dm5 x1, manga18 x1), 32 T2 (of which
~6 carry Interceptor/WebView/QuickJS flags and are effectively T3).

Usage:
  python3 tools/autoport.py classify <package> [--src <extensions-source clone>]
  python3 tools/autoport.py scaffold <package> --out sources/ [--src ...]
  python3 tools/autoport.py batch [--src ...]   # classify every unmatched zh ext

The classifier reads keiyoushi's index.json (cached under .cache/) and the
extension's build.gradle.kts + main .kt from a local extensions-source
clone (~/workspace/keiyoushi-src-ref by default) or from
raw.githubusercontent.com when no clone is given.
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

KEIYOUSHI_INDEX = "https://raw.githubusercontent.com/keiyoushi/extensions/repo/index.json"
EXT_SRC_RAW = "https://raw.githubusercontent.com/keiyoushi/extensions-source/main"
CACHE = Path(__file__).resolve().parent / ".cache" / "autoport"
DEFAULT_SRC = Path.home() / "workspace" / "keiyoushi-src-ref"

# theme -> (rust template name, status). Only "mmlook" is ported in v1.
KNOWN_THEMES = {
    "mmlook": "ported",
    "mccms": "todo",
    "mccmsweb": "todo",
    "sinmh": "todo",
    "dm5": "todo",
    "manga18": "todo",
}

HARD_FLAGS = ("QuickJS", "WebView", "Login")


def fetch(url, cache_name=None):
    if cache_name:
        p = CACHE / cache_name
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace")
        p.parent.mkdir(parents=True, exist_ok=True)
        data = urllib.request.urlopen(url, timeout=60).read()
        p.write_bytes(data)
        return data.decode("utf-8", errors="replace")
    return urllib.request.urlopen(url, timeout=60).read().decode("utf-8", errors="replace")


def keiyoushi_entry(pkg):
    idx = json.loads(fetch(KEIYOUSHI_INDEX, "keiyoushi.json"))
    exts = idx["extensionList"]["extensions"] if isinstance(idx, dict) else idx
    for e in exts:
        if e.get("id") == pkg:
            return e
    return None


def parse_gradle(text):
    """Extract theme + source metadata from an extension's build.gradle.kts."""
    out = {}
    m = re.search(r'theme\s*=\s*"([^"]+)"', text)
    if m:
        out["theme"] = m.group(1)
    m = re.search(r'contentWarning\s*=\s*ContentWarning\.(\w+)', text)
    if m:
        out["contentWarning"] = m.group(1)
    m = re.search(r'source\s*\{(.*?)\}', text, re.S)
    if m:
        body = m.group(1)
        for key in ("name", "lang", "baseUrl"):
            mm = re.search(rf'{key}\s*=\s*"([^"]+)"', body)
            if mm:
                out[key] = mm.group(1)
    return out


def find_source_class(kt_text):
    """Return (class_name, base_class) for the @Source-annotated class."""
    m = re.search(
        r"@Source\s*\n\s*(?:abstract\s+)?class\s+(\w+)\s*:\s*(\w+)\s*\(",
        kt_text,
    )
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"(?:abstract\s+)?class\s+(\w+)\s*:\s*(\w+)\s*\(", kt_text)
    return (m.group(1), m.group(2)) if m else (None, None)


def read_extension(pkg, src_root):
    """Return (gradle_meta, kt_texts dict, dir_name) for a Mihon package."""
    d = pkg.split(".")[-1]
    gradle, kts = None, {}
    if src_root:
        base = Path(src_root) / "src" / "zh" / d
        g = base / "build.gradle.kts"
        if g.exists():
            gradle = g.read_text(encoding="utf-8")
        for p in sorted(base.glob("src/**/*.kt")):
            kts[p.name] = p.read_text(encoding="utf-8", errors="replace")
    if gradle is None:
        gradle = fetch(f"{EXT_SRC_RAW}/src/zh/{d}/build.gradle.kts")
    if not kts:
        # main class file: <Class>.kt, guess from dir name
        cap = d[:1].upper() + d[1:]
        for cand in (cap, d):
            try:
                kts[cand + ".kt"] = fetch(
                    f"{EXT_SRC_RAW}/src/zh/{d}/src/eu/kanade/tachiyomi/extension/zh/{d}/{cand}.kt"
                )
                break
            except Exception:
                continue
    return gradle, kts, d


def classify(pkg, src_root=None):
    entry = keiyoushi_entry(pkg)
    gradle_text, kts, d = read_extension(pkg, src_root)
    meta = parse_gradle(gradle_text)
    kt_all = "\n".join(kts.values())
    cls, base = find_source_class(kt_all)
    flags = []
    if "QuickJs" in kt_all or "quickjs" in kt_all.lower():
        flags.append("QuickJS")
    if "Interceptor" in kt_all:
        flags.append("Interceptor")
    if re.search(r"\bCipher\b|\bAES\b|decrypt", kt_all):
        flags.append("Crypto")
    if re.search(r"login", kt_all, re.I):
        flags.append("Login")
    if "WebView" in kt_all:
        flags.append("WebView")
    theme = (meta.get("theme") or "").lower()
    if theme in KNOWN_THEMES:
        tier = "T1"
        note = "theme ported" if KNOWN_THEMES[theme] == "ported" else "theme NOT ported yet"
    elif base in ("KeiSource", "HttpSource", "ParsedHttpSource"):
        tier = "T3" if any(f in HARD_FLAGS for f in flags) else "T2"
        note = "custom source" + ("; hard flags: " + ",".join(flags) if flags else "")
    else:
        tier = "T2"
        note = f"base class {base} (unreviewed)"
    return {
        "package": pkg,
        "dir": d,
        "name": entry.get("name") if entry else meta.get("name"),
        "iconUrl": entry.get("iconUrl") if entry else None,
        "homeUrl": (entry.get("sources") or [{}])[0].get("homeUrl") if entry else None,
        "theme": theme or None,
        "base_class": base,
        "source_name": meta.get("name"),
        "lang": meta.get("lang"),
        "baseUrl": meta.get("baseUrl"),
        "contentWarning": meta.get("contentWarning"),
        "kt_files": sorted(kts),
        "kt_lines": kt_all.count("\n") + 1,
        "flags": flags,
        "tier": tier,
        "note": note,
    }


def extract_t2_spec(kt_text):
    """Heuristic extraction for a custom KeiSource: URL templates + selectors."""
    spec = {"gets": [], "posts": [], "selectors": [], "attrs": []}
    for m in re.finditer(r'client\.get\(\s*"?([^"\n]{0,120})', kt_text):
        spec["gets"].append(m.group(1))
    for m in re.finditer(r'client\.post\(\s*"?([^"\n]{0,120})', kt_text):
        spec["posts"].append(m.group(1))
    for m in re.finditer(r'\.(?:select|selectFirst)\("([^"]+)"\)', kt_text):
        if m.group(1) not in spec["selectors"]:
            spec["selectors"].append(m.group(1))
    for m in re.finditer(r'\.attr\("([^"]+)"\)', kt_text):
        if m.group(1) not in spec["attrs"]:
            spec["attrs"].append(m.group(1))
    return spec

# ---------------------------------------------------------------------------
# MMLook theme port (Rust, aidoku 0.2.0) — the only theme implemented in v1.
# Original Kotlin: lib-multisrc/mmlook/{MMLook.kt, Dto.kt, Filters.kt,
# PageDecrypt.kt} + lib/unpacker/Unpacker.kt (Dean Edwards P.A.C.K.E.R.).
# Placeholders: {ID} zh.<dir>, {NAME} source name, {CRATE} crate name,
# {BASE} mobile base URL, {DESKTOP} www. base URL, {PKG} Mihon package,
# {DIR} extension dir, {RATING} content rating (0 safe / 1 suggestive / 2 nsfw).
# ---------------------------------------------------------------------------
MMLOOK_LIB_RS = r'''// SPDX-License-Identifier: Apache-2.0
// Auto-ported from Keiyoushi @@PKG@@ by tools/autoport.py (MMLook theme, v1).
// Original Kotlin: lib-multisrc/mmlook (MMLook.kt, Dto.kt, Filters.kt,
// PageDecrypt.kt) + lib/unpacker/Unpacker.kt. Site: @@BASE@@.
// Differences from the Kotlin original:
// - Unknown detail fields are ignored instead of throwing.
// - Chapter links are filtered to hrefs ending in .html instead of relying
//   on the .chapterlistload child structure.
// - The packed page script is located by string search instead of the
//   :containsData() selector (not available in the WASM HTML engine).
// - No viewer is declared on purpose: aidoku 0.2.0's MangaViewer labels are
//   swapped against what the app renders (Rtl = 1 opens left-to-right in
//   nemu, matching aidoku 0.3.0's Viewer::LeftToRight = 1), so declaring
//   one from the 0.2.0 label would be a coin flip. The app default applies.
#![no_std]
extern crate alloc;

use aidoku::{
    error::{AidokuError, AidokuErrorKind, Result},
    prelude::*,
    std::{
        net::{HttpMethod, Request},
        String, Vec,
    },
    Chapter, Filter, FilterType, Listing, Manga, MangaPageResult, MangaStatus, Page,
};
use alloc::string::ToString;
use base64::{Engine as _, engine::general_purpose::STANDARD as B64};

fn err() -> AidokuError {
    AidokuError {
        reason: AidokuErrorKind::Unimplemented,
    }
}

const BASE_URL: &str = "@@BASE@@";
const DESKTOP_URL: &str = "@@DESKTOP@@";

// Ranking ids, in filters.json option order (index 0 = not used).
const RANK_IDS: [&str; 7] = ["", "1", "2", "3", "4", "5", "6"];
// Category ids, in filters.json option order (index 0 = not used).
const CATE_IDS: [&str; 17] = [
    "", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15", "16",
];

// Page-decrypt keys (PageDecrypt.kt): index = .readerContainer data-id.
const DECRYPT_KEYS: [&str; 10] = [
    "smkhy258", "smkd95fv", "md496952", "cdcsdwq", "vbfsa256", "cawf151c", "cd56cvda", "8kihnt9",
    "dso15tlo", "5ko6plhy",
];

fn get_html(url: &str) -> Result<aidoku::std::html::Node> {
    Request::new(url, HttpMethod::Get).html()
}

fn select_index(filters: &[Filter], name: &str) -> usize {
    for f in filters {
        if f.name.as_str() == name {
            if let FilterType::Select = f.kind {
                if let Ok(v) = f.value.as_int() {
                    return v.max(0) as usize;
                }
            }
        }
    }
    0
}

fn title_query(filters: &[Filter]) -> String {
    for f in filters {
        if let FilterType::Title = f.kind {
            if let Ok(v) = f.value.as_string() {
                return v.read();
            }
        }
    }
    String::new()
}

/// Collapse runs of whitespace to single spaces (the Kotlin original relies
/// on jsoup's text() doing this; the WASM HTML engine may not).
fn normalize_ws(text: &str) -> String {
    let mut out = String::new();
    let mut last_ws = false;
    for c in text.chars() {
        if c.is_whitespace() {
            if !last_ws {
                out.push(' ');
            }
            last_ws = true;
        } else {
            out.push(c);
            last_ws = false;
        }
    }
    out
}

fn form_encode(value: &str) -> String {
    let mut out = String::new();
    for b in value.as_bytes() {
        match b {
            b'0'..=b'9' | b'a'..=b'z' | b'A'..=b'Z' | b'-' | b'_' | b'.' | b'~' => {
                out.push(*b as char)
            }
            _ => {
                out.push_str(&alloc::format!("%{:02X}", b));
            }
        }
    }
    out
}

fn parse_ranking(html: aidoku::std::html::Node) -> Result<MangaPageResult> {
    let mut manga = Vec::new();
    for el in html.select(".likedata").array() {
        let node = el.as_node().unwrap();
        let href = node.select("a").attr("href").read();
        let id = href.trim_matches('/').to_string();
        if id.is_empty() {
            continue;
        }
        let title = node.select(".le-t").text().read();
        if title.is_empty() {
            continue;
        }
        let author = node
            .select(".likeinfo > p")
            .text()
            .read()
            .trim_start_matches("作者：")
            .to_string();
        let description = node.select(".le-j").text().read();
        let cover = node.select("img").attr("data-src").read();
        manga.push(Manga {
            id,
            cover,
            title,
            author,
            description,
            ..Default::default()
        });
    }
    Ok(MangaPageResult { manga, has_more: false })
}

fn fetch_ranking(id: &str) -> Result<MangaPageResult> {
    let html = get_html(&format!("{DESKTOP_URL}/rank/{id}"))?;
    parse_ranking(html)
}

fn parse_search(html: aidoku::std::html::Node) -> Result<MangaPageResult> {
    let mut manga = Vec::new();
    for el in html.select(".item-data > div").array() {
        let node = el.as_node().unwrap();
        let href = node.select("a").attr("href").read();
        let id = href.trim_matches('/').to_string();
        if id.is_empty() {
            continue;
        }
        let mut title = node.select(".e-title").text().read();
        if title.is_empty() {
            title = node.select(".title").text().read();
        }
        if title.is_empty() {
            continue;
        }
        let author = node.select(".tip").text().read();
        let cover = node.select("img").attr("data-src").read();
        manga.push(Manga {
            id,
            cover,
            title,
            author,
            ..Default::default()
        });
    }
    Ok(MangaPageResult { manga, has_more: false })
}

#[get_manga_list]
fn get_manga_list(filters: Vec<Filter>, _page: i32) -> Result<MangaPageResult> {
    let query = title_query(&filters);
    if !query.is_empty() {
        let q12: String = query.chars().take(12).collect();
        let body = format!("k={}", form_encode(&q12));
        let html = Request::new(format!("{DESKTOP_URL}/s"), HttpMethod::Post)
            .header("Content-Type", "application/x-www-form-urlencoded")
            .body(body.as_bytes())
            .html()?;
        return parse_search(html);
    }
    let rank = select_index(&filters, "排行榜");
    if rank > 0 && rank < RANK_IDS.len() {
        return fetch_ranking(RANK_IDS[rank]);
    }
    let cate = select_index(&filters, "分类");
    if cate > 0 && cate < CATE_IDS.len() {
        let html = get_html(&format!("{DESKTOP_URL}/sort/{}", CATE_IDS[cate]))?;
        return parse_ranking(html);
    }
    fetch_ranking("1")
}

#[get_manga_listing]
fn get_manga_listing(listing: Listing, _page: i32) -> Result<MangaPageResult> {
    match listing.name.as_str() {
        "精品榜" => fetch_ranking("1"),
        "最近更新" => fetch_ranking("5"),
        _ => fetch_ranking("1"),
    }
}

#[get_manga_details]
fn get_manga_details(id: String) -> Result<Manga> {
    let html = get_html(&format!("{DESKTOP_URL}/{id}/"))?;
    let info = html.select(".comicInfo");
    let cover = info.select("img").attr("data-src").read();
    let container = info.select(".detinfo");
    let title = container.select("h1").text().read();
    let mut author = String::new();
    let mut description = String::new();
    let mut status = MangaStatus::Unknown;
    let mut categories: Vec<String> = Vec::new();
    for span in container.select("span").array() {
        let node = span.as_node().unwrap();
        let text = normalize_ws(&node.text().read());
        let mut chars = text.chars();
        let key: String = chars.by_ref().take(4).collect();
        let value: String = chars.collect::<String>().trim_start().to_string();
        match key.as_str() {
            "作 者：" => author = value,
            "更新时间" => description = format!("{text}\n\n"),
            "标 签：" => {
                categories = value
                    .split(' ')
                    .filter(|s| !s.is_empty())
                    .map(|s| s.to_string())
                    .collect();
            }
            "状 态：" => {
                status = match value.as_str() {
                    "连载中" => MangaStatus::Ongoing,
                    "已完结" => MangaStatus::Completed,
                    _ => MangaStatus::Unknown,
                };
            }
            _ => {} // unknown field: ignore (Kotlin throws here)
        }
    }
    let content = container.select(".content").text().read();
    description.push_str(&content);
    Ok(Manga {
        id: id.clone(),
        cover,
        title,
        author,
        description,
        url: format!("{BASE_URL}/{id}/"),
        categories,
        status,
        ..Default::default()
    })
}

#[get_chapter_list]
fn get_chapter_list(id: String) -> Result<Vec<Chapter>> {
    let html = get_html(&format!("{DESKTOP_URL}/{id}/"))?;
    let mut chapters: Vec<Chapter> = Vec::new();
    for a in html.select(".chapterlistload a").array() {
        let node = a.as_node().unwrap();
        let href = node.attr("href").read();
        if !href.ends_with(".html") {
            continue;
        }
        let cid = href
            .trim_matches('/')
            .strip_suffix(".html")
            .unwrap_or("")
            .to_string();
        if cid.is_empty() {
            continue;
        }
        let name = node.text().read();
        chapters.push(Chapter {
            id: cid.clone(),
            title: name,
            url: format!("{BASE_URL}/{cid}.html"),
            ..Default::default()
        });
    }
    if !html.select(".chaplist-more").array().is_empty() {
        let body = format!("id={}", form_encode(&id));
        let json = Request::new(format!("{DESKTOP_URL}/morechapter"), HttpMethod::Post)
            .header("Content-Type", "application/x-www-form-urlencoded")
            .body(body.as_bytes())
            .json()?;
        let data = json.as_object()?;
        let list = data.get("data").as_array()?;
        for item in list {
            let item = match item.as_object() {
                Ok(o) => o,
                Err(_) => continue,
            };
            let chapterid = match item.get("chapterid").as_string() {
                Ok(s) => s.read(),
                Err(_) => continue,
            };
            let chaptername = match item.get("chaptername").as_string() {
                Ok(s) => s.read(),
                Err(_) => continue,
            };
            let cid = format!("{id}/{chapterid}");
            chapters.push(Chapter {
                id: cid.clone(),
                title: chaptername,
                url: format!("{BASE_URL}/{cid}.html"),
                ..Default::default()
            });
        }
    }
    Ok(chapters)
}

// --- P.A.C.K.E.R. unpack (lib/unpacker/Unpacker.kt) ---------------------
fn parse_radix62(s: &str) -> usize {
    let mut result: usize = 0;
    for c in s.chars() {
        result = result.saturating_mul(62).saturating_add(match c {
            '0'..='9' => c as usize - '0' as usize,
            'a'..='z' => c as usize - 'a' as usize + 10,
            'A'..='Z' => c as usize - 'A' as usize + 36,
            _ => return usize::MAX,
        });
    }
    result
}

fn unpack(script: &str) -> Option<String> {
    let start = script.find("}('")? + 3;
    let rest = &script[start..];
    let end = rest.find(".split('|'),0,{}))")?;
    let packed = rest[..end].replace("\\'", "\"");
    let comma = packed.find("',")?;
    let data = &packed[..comma];
    let after = &packed[comma + 2..];
    // after = "62,52,'DICT'.split(...)": skip the radix args; the dict is
    // the quoted string after them (SubstringExtractor cursor semantics in
    // the Kotlin Unpacker — a naive first-quote split returns "62,52,").
    let q1 = after.find('\'')? + 1;
    let rest2 = &after[q1..];
    let q2 = rest2.find('\'')?;
    let dict: Vec<&str> = rest2[..q2].split('|').collect();
    let mut out = String::new();
    let mut chars = data.char_indices().peekable();
    while let Some((i, c)) = chars.next() {
        if c.is_ascii_alphanumeric() || c == '_' {
            let mut j = i + c.len_utf8();
            while let Some((k, nc)) = chars.peek() {
                if nc.is_ascii_alphanumeric() || *nc == '_' {
                    j = *k + nc.len_utf8();
                    chars.next();
                } else {
                    break;
                }
            }
            let word = &data[i..j];
            let idx = parse_radix62(word);
            if idx < dict.len() && !dict[idx].is_empty() {
                out.push_str(dict[idx]);
            } else {
                out.push_str(word);
            }
        } else {
            out.push(c);
        }
    }
    Some(out)
}

// --- Page decrypt (lib-multisrc/mmlook/PageDecrypt.kt) -------------------
// base64(data) -> XOR with key[index] -> base64 decode -> JSON image list.
fn decrypt(data: &str, index: usize) -> Result<String> {
    let key = DECRYPT_KEYS.get(index).ok_or_else(err)?;
    let compact: Vec<u8> = data.bytes().filter(|b| !b.is_ascii_whitespace()).collect();
    let mut bytes = B64.decode(&compact).map_err(|_| err())?;
    let kb = key.as_bytes();
    for (i, b) in bytes.iter_mut().enumerate() {
        *b ^= kb[i % kb.len()];
    }
    let inner: Vec<u8> = bytes.into_iter().filter(|b| !b.is_ascii_whitespace()).collect();
    let plain = B64.decode(&inner).map_err(|_| err())?;
    String::from_utf8(plain).map_err(|_| err())
}

fn parse_json_string_array(json: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut chars = json.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '"' {
            let mut s = String::new();
            loop {
                match chars.next() {
                    Some('\\') => match chars.next() {
                        Some('n') => s.push('\n'),
                        Some('t') => s.push('\t'),
                        Some('r') => s.push('\r'),
                        Some('u') => {
                            let mut code = 0u32;
                            for _ in 0..4 {
                                if let Some(h) = chars.next() {
                                    code = code * 16 + h.to_digit(16).unwrap_or(0);
                                }
                            }
                            if let Some(ch) = char::from_u32(code) {
                                s.push(ch);
                            }
                        }
                        Some(e) => s.push(e),
                        None => break,
                    },
                    Some('"') => break,
                    Some(ch) => s.push(ch),
                    None => break,
                }
            }
            out.push(s);
        }
    }
    out
}

fn extract_data_id(html: &str) -> Option<usize> {
    let pos = html.find("data-id=\"")? + 9;
    let rest = &html[pos..];
    let end = rest.find('"')?;
    rest[..end].parse::<usize>().ok()
}

fn extract_eval_script(html: &str) -> Option<String> {
    let pos = html.find("eval(function(p,a,c,k,e,d")?;
    let start = html[..pos].rfind("<script")?;
    let end = html[pos..].find("</script>")? + pos;
    Some(html[start..end].to_string())
}

#[get_page_list]
fn get_page_list(_manga_id: String, chapter_id: String) -> Result<Vec<Page>> {
    let url = format!("{BASE_URL}/{chapter_id}.html");
    let raw = Request::new(&url, HttpMethod::Get).string()?;
    let data_id = extract_data_id(&raw).ok_or_else(err)?;
    let script = extract_eval_script(&raw).ok_or_else(err)?;
    let unpacked = unpack(&script).ok_or_else(err)?;
    let payload = unpacked
        .strip_prefix("var __c0rst96=\"")
        .and_then(|s| s.strip_suffix('"'))
        .ok_or_else(err)?;
    let json = decrypt(payload, data_id)?;
    let urls = parse_json_string_array(&json);
    if urls.is_empty() {
        return Err(err());
    }
    Ok(urls
        .into_iter()
        .map(|u| Page {
            url: u,
            ..Default::default()
        })
        .collect())
}

#[modify_image_request]
fn modify_image_request(request: Request) {
    request.header("Referer", BASE_URL).header(
        "User-Agent",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) \
         AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    );
}
'''

MMLOOK_CARGO_TOML = '''[package]
name = "@@CRATE@@"
version = "0.1.0"
edition = "2021"

[lib]
crate-type = ["cdylib"]

[profile.dev]
panic = "abort"

[profile.release]
panic = "abort"
opt-level = "s"
strip = true
lto = true

[dependencies]
# Pinned to the exact aidoku 0.2.0 rev the other forked sources in this
# repo build against (see Cargo.lock). Current aidoku-rs main (0.3.0)
# removed the #[get_manga_list] macro API this source is written against,
# so the dependency must NOT float.
aidoku = { git = "https://github.com/Aidoku/aidoku-rs", rev = "bae338b15a3840dc0428c7af64fe9fc92abfa2fb", version = "=0.2.0", features = ["helpers"] }
base64 = { version = "0.22", default-features = false, features = ["alloc"] }
'''

MMLOOK_SOURCE_JSON = """{
\t"info": {
\t\t"id": "@@ID@@",
\t\t"name": "@@NAME@@",
\t\t"version": 1,
\t\t"url": "@@META_URL@@",
\t\t"languages": [
\t\t\t"zh"
\t\t],
\t\t"contentRating": @@RATING@@,
\t\t"minAppVersion": "0.7.1"
\t},
\t"listings": [
\t\t{
\t\t\t"id": "rank1",
\t\t\t"name": "精品榜"
\t\t},
\t\t{
\t\t\t"id": "rank5",
\t\t\t"name": "最近更新"
\t\t}
\t]
}
"""

MMLOOK_FILTERS_JSON = """[
\t{
\t\t"type": "select",
\t\t"id": "rank",
\t\t"title": "排行榜",
\t\t"options": [
\t\t\t"不查看",
\t\t\t"精品榜",
\t\t\t"人气榜",
\t\t\t"推荐榜",
\t\t\t"黑马榜",
\t\t\t"最近更新",
\t\t\t"新漫画"
\t\t],
\t\t"ids": [
\t\t\t"",
\t\t\t"1",
\t\t\t"2",
\t\t\t"3",
\t\t\t"4",
\t\t\t"5",
\t\t\t"6"
\t\t]
\t},
\t{
\t\t"type": "select",
\t\t"id": "cate",
\t\t"title": "分类",
\t\t"options": [
\t\t\t"全部",
\t\t\t"冒险",
\t\t\t"热血",
\t\t\t"都市",
\t\t\t"玄幻",
\t\t\t"悬疑",
\t\t\t"耽美",
\t\t\t"恋爱",
\t\t\t"生活",
\t\t\t"搞笑",
\t\t\t"穿越",
\t\t\t"修真",
\t\t\t"后宫",
\t\t\t"女主",
\t\t\t"古风",
\t\t\t"连载",
\t\t\t"完结"
\t\t],
\t\t"ids": [
\t\t\t"",
\t\t\t"1",
\t\t\t"2",
\t\t\t"3",
\t\t\t"4",
\t\t\t"5",
\t\t\t"6",
\t\t\t"7",
\t\t\t"8",
\t\t\t"9",
\t\t\t"10",
\t\t\t"11",
\t\t\t"12",
\t\t\t"13",
\t\t\t"14",
\t\t\t"15",
\t\t\t"16"
\t\t]
\t}
]
"""

MMLOOK_SETTINGS_JSON = "[]\n"

PACKAGE_PY = '''#!/usr/bin/env python3
"""Package an already built @@NAME@@ WASM. Never downloads or publishes anything."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parent
p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--wasm", type=Path, default=root / "../../target/wasm32-unknown-unknown/release/@@CRATE@@.wasm")
p.add_argument("--out", type=Path, default=root / "package.aix")
a = p.parse_args()
wasm = a.wasm.read_bytes()
if not wasm.startswith(b"\\0asm\\x01\\0\\0\\0"):
    raise SystemExit("Not a WebAssembly 1 binary")
manifest = json.loads((root / "res/source.json").read_text())
assert manifest["info"]["id"] == "@@ID@@"
version = manifest["info"]["version"]
assert version > 0
for name in ("filters.json", "settings.json"):
    json.loads((root / "res" / name).read_text())
icon = (root / "res/icon.png").read_bytes()
assert icon.startswith(b"\\x89PNG\\r\\n\\x1a\\n")
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
'''

# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------
NOTICE_TMPL = """@@NAME@@ Aidoku/Rust port

Auto-ported from Keiyoushi @@PKG@@ (theme: @@THEME@@) by tools/autoport.py.
Original work: Keiyoushi extensions-source.
Copyright remains with the original contributors.
License: Apache License, Version 2.0 (included verbatim in LICENSE).
Original project: https://github.com/keiyoushi/extensions-source
Original icon, retained unmodified (when the scaffold could fetch it):
  src/zh/@@DIR@@/res/mipmap-xxxhdpi/ic_launcher.png

Aidoku API reference:
  https://github.com/Aidoku/aidoku-rs
  bae338b15a3840dc0428c7af64fe9fc92abfa2fb (aidoku 0.2.0)
Aidoku SDK and the base64 crate are third-party dependencies licensed
MIT OR Apache-2.0. They are not represented as newly authored by this port.
"""

BUILD_FRAGMENT_TMPL = """    # --- autoport: @@ID@@ (MMLook theme) ---
    - name: Build @@ID@@
      working-directory: sources/@@ID@@
      run: |
        rustup toolchain install nightly
        rustup target add wasm32-unknown-unknown --toolchain nightly
        cargo +nightly build --release --target wasm32-unknown-unknown
        python3 package.py --wasm ../../target/wasm32-unknown-unknown/release/@@CRATE@@.wasm --out ../@@ID@@-v@@VERSION@@.aix
      env:
        RUSTFLAGS: "-C link-arg=--allow-undefined"
"""

RATING_MAP = {"SAFE": 0, "MIXED": 1, "NSFW": 2}


def sub(tmpl, subs):
    out = tmpl
    for k, v in subs.items():
        out = out.replace("@@" + k + "@@", str(v))
    return out


def content_rating(meta):
    return RATING_MAP.get((meta or {}).get("contentWarning", "SAFE"), 0)


def desktop_url(base):
    return base.replace("https://m.", "https://www.", 1)


def request_base(base):
    """Request base URL. MMLook sites are notorious for broken TLS (the
    Kotlin theme ships an accept-all hostname verifier plus an https->http
    downgrade for browser URLs); the site serves everything over plain
    HTTP, verified live 2026-11-12. Requests go over http, the metadata
    URL in source.json keeps the https baseUrl."""
    return base.replace("https://", "http://", 1)


def scaffold(pkg, out_dir, src_root=None):
    info = classify(pkg, src_root)
    aidoku_id = "zh." + info["dir"]
    crate = info["dir"].replace("-", "_")
    base = info["baseUrl"]
    if not base:
        raise SystemExit(f"{pkg}: no baseUrl in build.gradle.kts, cannot scaffold")
    if info["tier"] != "T1" or info["theme"] != "mmlook":
        raise SystemExit(
            f"{pkg}: tier {info['tier']}, theme {info['theme']} — {info['note']}. "
            "v1 only scaffolds the mmlook theme."
        )
    root = Path(out_dir) / f"sources/{aidoku_id}"
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "res").mkdir(parents=True, exist_ok=True)
    subs = {
        "ID": aidoku_id, "NAME": info["source_name"] or info["name"] or info["dir"],
        "CRATE": crate, "BASE": request_base(base), "DESKTOP": request_base(desktop_url(base)),
        "META_URL": base,
        "PKG": pkg, "DIR": info["dir"], "RATING": str(content_rating(info)),
        "THEME": info["theme"], "VERSION": "1",
    }
    (root / "src" / "lib.rs").write_text(sub(MMLOOK_LIB_RS, subs), encoding="utf-8")
    (root / "Cargo.toml").write_text(sub(MMLOOK_CARGO_TOML, subs), encoding="utf-8")
    (root / "package.py").write_text(sub(PACKAGE_PY, subs), encoding="utf-8")
    (root / "res" / "source.json").write_text(sub(MMLOOK_SOURCE_JSON, subs), encoding="utf-8")
    (root / "res" / "filters.json").write_text(MMLOOK_FILTERS_JSON, encoding="utf-8")
    (root / "res" / "settings.json").write_text(MMLOOK_SETTINGS_JSON, encoding="utf-8")
    (root / "NOTICE").write_text(sub(NOTICE_TMPL, subs), encoding="utf-8")
    # LICENSE: Apache-2.0 verbatim from apache.org (needed by package.py).
    try:
        lic = urllib.request.urlopen("https://www.apache.org/licenses/LICENSE-2.0.txt", timeout=60).read()
        (root / "LICENSE").write_bytes(lic)
        lic_ok = True
    except Exception as e:
        lic_ok = False
        print(f"WARNING: could not fetch Apache-2.0 LICENSE text ({e}); add it before building", file=sys.stderr)
    # Icon: the extension's launcher icon (local clone first, then raw).
    icon_ok = False
    icon_bytes = None
    if src_root:
        ip = Path(src_root) / "src" / "zh" / info["dir"] / "res" / "mipmap-xxxhdpi" / "ic_launcher.png"
        if ip.exists():
            icon_bytes = ip.read_bytes()
    if icon_bytes is None:
        try:
            icon_bytes = urllib.request.urlopen(
                f"{EXT_SRC_RAW}/src/zh/{info['dir']}/res/mipmap-xxxhdpi/ic_launcher.png",
                timeout=60).read()
        except Exception as e:
            print(f"WARNING: could not fetch icon ({e})", file=sys.stderr)
    if icon_bytes and icon_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        (root / "res" / "icon.png").write_bytes(icon_bytes)
        icon_ok = True
    elif icon_bytes:
        print("WARNING: fetched icon is not a PNG; not saved", file=sys.stderr)
    frag = sub(BUILD_FRAGMENT_TMPL, subs)
    (Path(out_dir) / f"{aidoku_id}-build-fragment.yml").write_text(frag, encoding="utf-8")
    print(json.dumps({
        "scaffolded": str(root), "id": aidoku_id, "baseUrl": base,
        "license": lic_ok, "icon": icon_ok,
        "next": [
            f"review {root}/src/lib.rs (esp. the TODO-free MMLook port)",
            "add the build fragment to .github/workflows/build-source.yml",
            "push -> build -> sync -> verify Pages -> phone test",
        ],
    }, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("classify", help="classify one Mihon package")
    c.add_argument("package")
    c.add_argument("--src", default=None)
    s = sub.add_parser("scaffold", help="generate a Rust source dir (T1 mmlook only in v1)")
    s.add_argument("package")
    s.add_argument("--out", default=".")
    s.add_argument("--src", default=None)
    b = sub.add_parser("batch", help="classify every unmatched zh extension")
    b.add_argument("--src", default=None)
    b.add_argument("--unmatched", default=None,
                   help="unmatched_zh_v2.json path (default: workspace nemu-12/bakamad)")
    args = ap.parse_args()
    src = args.src or (str(DEFAULT_SRC) if DEFAULT_SRC.exists() else None)
    if args.cmd == "classify":
        print(json.dumps(classify(args.package, src), ensure_ascii=False, indent=2))
    elif args.cmd == "scaffold":
        scaffold(args.package, args.out, src)
    elif args.cmd == "batch":
        path = args.unmatched or str(
            Path.home() / "workspace" / "nemu-12" / "bakamad" / "unmatched_zh_v2.json")
        unmatched = json.load(open(path, encoding="utf-8"))
        rows = []
        for e in unmatched:
            if not e["packageName"].startswith("eu.kanade.tachiyomi.extension.zh."):
                continue
            try:
                rows.append(classify(e["packageName"], src))
            except Exception as ex:
                rows.append({"package": e["packageName"], "tier": "ERROR", "note": str(ex)})
        from collections import Counter
        print(Counter(r["tier"] for r in rows))
        for r in rows:
            print(f"{r['tier']:3} {r.get('name') or r['package']} | theme={r.get('theme')} "
                  f"base={r.get('base_class')} lines={r.get('kt_lines')} "
                  f"flags={','.join(r.get('flags') or []) or '-'} | {r.get('note')}")
        json.dump(rows, open("port_tiers.json", "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
