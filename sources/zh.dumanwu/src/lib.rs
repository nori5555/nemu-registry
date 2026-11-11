// SPDX-License-Identifier: Apache-2.0
// Auto-ported from Keiyoushi eu.kanade.tachiyomi.extension.zh.dumanwu by tools/autoport.py (MMLook theme, v1).
// Original Kotlin: lib-multisrc/mmlook (MMLook.kt, Dto.kt, Filters.kt,
// PageDecrypt.kt) + lib/unpacker/Unpacker.kt. Site: http://m.dumanwu1.com.
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

const BASE_URL: &str = "http://m.dumanwu1.com";
const DESKTOP_URL: &str = "http://www.dumanwu1.com";

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

fn title_query(filters: Vec<Filter>) -> String {
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
    let rank = select_index(&filters, "排行榜");
    let cate = select_index(&filters, "分类");
    let query = title_query(filters);
    if !query.is_empty() {
        let q12: String = query.chars().take(12).collect();
        let body = format!("k={}", form_encode(&q12));
        let html = Request::new(format!("{DESKTOP_URL}/s"), HttpMethod::Post)
            .header("Content-Type", "application/x-www-form-urlencoded")
            .body(body.as_bytes())
            .html()?;
        return parse_search(html);
    }
    if rank > 0 && rank < RANK_IDS.len() {
        return fetch_ranking(RANK_IDS[rank]);
    }
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
