// SPDX-License-Identifier: Apache-2.0
// Port of the Keiyoushi `ManwaManhua` (漫蛙漫画) extension. See NOTICE.
// The site's JSON API is used directly. The frontend JS file is fetched
// only to read the current AES key string (never executed), mirroring the
// Mihon manwayuplus extension's getAesKey.
#![no_std]

mod crypto;

use aidoku::{
    BaseUrlProvider, Chapter, FilterValue, Listing,
    ListingProvider, Manga, MangaPageResult, MangaStatus, Page, PageContent, PageContext,
    Result, Source,
    alloc::{String, Vec, format, vec},
    imports::{
        defaults::defaults_get,
        net::Request,
    },
    prelude::*,
};
use serde::{Deserialize, Serialize};

const DEFAULT_BASE_URL: &str = "https://manwadz.cc";
const SEARCH_PAGE_SIZE: i32 = 20;
const CATE_PAGE_SIZE: i32 = 36;
const IMAGE_PAGE_SIZE: i32 = 100;

// Image CDN lines, mirroring the Mihon manwayuplus extension's LINE1-6.
// Every line serves the same AES-encrypted bytes for the same path, so a
// line is only an availability fallback, never a plaintext shortcut.
const IMAGE_HOSTS: [&str; 5] = [
    "tu.mhttu.cc",
    "tu.mwzu.cc",
    "fm.mwtt.cc",
    "ce.mwzu.cc",
    "ce.mhttu.cc",
];

// ---------------------------------------------------------------- json models
#[derive(Deserialize)]
struct Response<T> {
    data: T,
}

#[derive(Deserialize)]
struct List<T> {
    total: i32,
    list: Vec<T>,
}

#[derive(Deserialize)]
struct ComicItem {
    #[serde(default)]
    id: Option<i64>,
    #[serde(default)]
    url: Option<String>,
    title: String,
    #[serde(default, alias = "pic")]
    cover: String,
    #[serde(default)]
    author: String,
    #[serde(default)]
    tags: String,
}

#[derive(Deserialize)]
struct ComicDetail {
    id: i64,
    title: String,
    #[serde(default)]
    alias: String,
    cover: String,
    status: i32,
    #[serde(default)]
    author: String,
    #[serde(default)]
    tags: String,
    #[serde(default)]
    intro: String,
    #[serde(default)]
    area_id: i32,
}

#[derive(Deserialize)]
struct ChapterItem {
    id: i64,
    #[serde(default, rename = "sortId")]
    sort_id: f64,
    #[serde(default, rename = "comicId")]
    comic_id: i64,
    title: String,
    #[serde(default, rename = "createTime")]
    create_time: i64,
}

#[derive(Deserialize)]
struct ChapterList {
    list: Vec<ChapterItem>,
}

#[derive(Deserialize)]
struct ImageItem {
    url: String,
    #[serde(default)]
    width: i32,
}

#[derive(Deserialize)]
struct Pagination {
    #[serde(default, rename = "total_pages")]
    total_pages: i32,
}

#[derive(Deserialize)]
struct ImagePage {
    images: Vec<ImageItem>,
    #[serde(default)]
    pagination: Option<Pagination>,
}

#[derive(Serialize)]
struct CateRequest {
    page: PageRequest,
    sort: i32,
    comic: ComicFilter,
}

#[derive(Serialize)]
struct PageRequest {
    page: i32,
    #[serde(rename = "pageSize")]
    page_size: i32,
}

#[derive(Serialize)]
struct ComicFilter {
    status: i32,
    day: i32,
    level: i32,
    #[serde(rename = "areaId")]
    area_id: i32,
}

// -------------------------------------------------------------------- helpers
fn base_url() -> String {
    let raw = defaults_get::<String>("baseURL").unwrap_or_default();
    let trimmed = raw.trim().trim_end_matches('/');
    if trimmed.is_empty() || !(trimmed.starts_with("https://") || trimmed.starts_with("http://")) {
        DEFAULT_BASE_URL.into()
    } else {
        trimmed.into()
    }
}

fn get_json<T: for<'de> Deserialize<'de>>(url: &str, referer: &str) -> Result<T> {
    let response = Request::get(url)?
        .header("Referer", referer)
        .header("Accept", "application/json")
        .send()?;
    let code = response.status_code();
    if !(200..300).contains(&code) {
        bail!("漫蛙漫画接口返回 HTTP {}，站点可能换了域名（mwmw.cc 有最新地址）", code);
    }
    let bytes = response.get_data().map_err(|_| error!("漫蛙漫画接口没有返回内容"))?;
    serde_json::from_slice(&bytes).map_err(|_| error!("漫蛙漫画接口返回的不是预期 JSON"))
}

fn post_json<T: for<'de> Deserialize<'de>>(url: &str, body: &str, referer: &str) -> Result<T> {
    let response = Request::post(url)?
        .header("Referer", referer)
        .header("Content-Type", "application/json")
        .header("Accept", "application/json")
        .body(body)
        .send()?;
    let code = response.status_code();
    if !(200..300).contains(&code) {
        bail!("漫蛙漫画分类接口返回 HTTP {}", code);
    }
    let bytes = response.get_data().map_err(|_| error!("漫蛙漫画分类接口没有返回内容"))?;
    serde_json::from_slice(&bytes).map_err(|_| error!("漫蛙漫画分类接口返回的不是预期 JSON"))
}

fn select_value(filters: &[FilterValue], id: &str) -> Option<i32> {
    for filter in filters {
        if let FilterValue::Select { id: key, value } = filter {
            if key == id {
                return value.parse::<i32>().ok();
            }
        }
    }
    None
}

fn tags_to_genres(tags: &str) -> Vec<String> {
    tags.split(',')
        .map(str::trim)
        .filter(|t| !t.is_empty())
        .map(|t| t.into())
        .collect()
}

fn area_name(id: i32) -> Option<&'static str> {
    match id {
        2 => Some("韩漫"),
        3 => Some("日漫"),
        4 => Some("国漫"),
        5 => Some("台漫"),
        _ => None,
    }
}

fn cate_body(page: i32, filters: &[FilterValue]) -> String {
    let body = CateRequest {
        page: PageRequest { page, page_size: CATE_PAGE_SIZE },
        sort: select_value(filters, "sort").unwrap_or(0),
        comic: ComicFilter {
            status: select_value(filters, "status").unwrap_or(-1),
            day: select_value(filters, "day").unwrap_or(0),
            level: select_value(filters, "level").unwrap_or(0),
            area_id: select_value(filters, "area").unwrap_or(0),
        },
    };
    serde_json::to_string(&body).unwrap_or_else(|_| "{}".into())
}

fn listing_page(
    base: &str, page: i32, query: Option<&str>, filters: &[FilterValue], sort: i32,
) -> Result<MangaPageResult> {
    let referer = format!("{base}/cate/");
    match query.map(str::trim).filter(|q| !q.is_empty()) {
        Some(keyword) => {
            let field = filters.iter().find_map(|f| match f {
                FilterValue::Select { id, value } if id == "field" => Some(value.clone()),
                _ => None,
            }).unwrap_or_else(|| "keyword".into());
            let url = format!(
                "{base}/api/search?type=mh&page={page}&pageSize={SEARCH_PAGE_SIZE}&{field}={}",
                aidoku::helpers::uri::encode_uri_component(keyword)
            );
            let data: Response<List<ComicItem>> = get_json(&url, &referer)?;
            Ok(MangaPageResult {
                entries: data.data.list.into_iter().filter_map(to_manga).collect(),
                has_next_page: page * SEARCH_PAGE_SIZE < data.data.total,
            })
        }
        None => {
            let mut filters_vec: Vec<FilterValue> = filters.to_vec();
            filters_vec.push(FilterValue::Select { id: "sort".into(), value: format!("{}", sort) });
            let body = cate_body(page, &filters_vec);
            let data: Response<List<ComicItem>> = post_json(&format!("{base}/api/cate"), &body, &referer)?;
            Ok(MangaPageResult {
                entries: data.data.list.into_iter().filter_map(to_manga).collect(),
                has_next_page: page * CATE_PAGE_SIZE < data.data.total,
            })
        }
    }
}

fn to_manga(item: ComicItem) -> Option<Manga> {
    let key = item.id.map(|id| format!("{}", id)).or_else(|| {
        item.url.as_deref().map(|u| format!("{}", u.trim_end_matches('/').rsplit('/').next().unwrap_or(u)))
    })?;
    if key.is_empty() {
        return None;
    }
    Some(Manga {
        key,
        title: item.title,
        cover: (!item.cover.is_empty()).then_some(item.cover),
        authors: (!item.author.is_empty()).then(|| vec![item.author]),
        tags: Some(tags_to_genres(&item.tags)).filter(|t| !t.is_empty()),
        ..Default::default()
    })
}

// --------------------------------------------------------------------- source
struct ManwaManhua;

impl Source for ManwaManhua {
    fn new() -> Self { Self }

    fn get_search_manga_list(
        &self, query: Option<String>, page: i32, filters: Vec<FilterValue>,
    ) -> Result<MangaPageResult> {
        listing_page(&base_url(), page, query.as_deref(), &filters, 3)
    }

    fn get_manga_update(
        &self, manga: Manga, needs_details: bool, needs_chapters: bool,
    ) -> Result<Manga> {
        let base = base_url();
        let mut updated = manga.clone();
        if needs_details {
            let referer = format!("{base}/comic/{}", manga.key);
            let data: Response<ComicDetail> = get_json(&format!("{base}/api/comic/{}", manga.key), &referer)?;
            let detail = data.data;
            updated.title = detail.title;
            // Inline the cover: the host's image-processor path is unreliable on
            // mobile, and the bytes are AES-CBC encrypted at rest.
            let key = fetch_aes_key(&base);
            updated.cover = Some(fetch_inline(&detail.cover, &referer, &key).unwrap_or_else(|_| detail.cover.clone()));
            updated.authors = (!detail.author.is_empty()).then(|| vec![detail.author]);
            updated.status = match detail.status {
                1 => MangaStatus::Ongoing,
                0 => MangaStatus::Completed,
                _ => MangaStatus::Unknown,
            };
            let mut tags = tags_to_genres(&detail.tags);
            if let Some(area) = area_name(detail.area_id) {
                if !tags.iter().any(|t| t == area) {
                    tags.push(area.into());
                }
            }
            updated.tags = Some(tags).filter(|t| !t.is_empty());
            let mut description = detail.intro;
            if !detail.alias.is_empty() {
                description.push_str("\n\n别名：");
                description.push_str(&detail.alias.replace('、', "、"));
            }
            updated.description = (!description.is_empty()).then_some(description);
        }
        if needs_chapters {
            let referer = format!("{base}/comic/{}", manga.key);
            let data: Response<ChapterList> =
                get_json(&format!("{base}/api/comic/{}/chapters", manga.key), &referer)?;
            let mut list = data.data.list;
            list.sort_by(|a, b| b.sort_id.partial_cmp(&a.sort_id).unwrap_or(core::cmp::Ordering::Equal)
                .then_with(|| b.id.cmp(&a.id)));
            updated.chapters = Some(list.into_iter().map(|c| Chapter {
                key: format!("{}/{}", c.comic_id, c.id),
                title: Some(c.title),
                chapter_number: None,
                date_uploaded: (c.create_time > 0).then_some(c.create_time * 1000),
                ..Default::default()
            }).collect());
        }
        Ok(updated)
    }

    fn get_page_list(&self, _manga: Manga, chapter: Chapter) -> Result<Vec<Page>> {
        let base = base_url();
        let chapter_id = format!("{}", chapter.key.rsplit('/').next().unwrap_or(&chapter.key));
        let referer = format!("{base}/comic/{chapter_id}");
        let key = fetch_aes_key(&base);
        let mut pages: Vec<Page> = Vec::new();
        let mut inlined = 0;
        let mut first_error: Option<String> = None;
        let mut page_no = 1;
        loop {
            let url = format!(
                "{base}/api/comic/image/{chapter_id}?page={page_no}&page_size={IMAGE_PAGE_SIZE}&image_source="
            );
            let data: Response<ImagePage> = get_json(&url, &referer)?;
            let total_pages = data.data.pagination.as_ref().map(|p| p.total_pages).unwrap_or(1).max(1);
            for image in data.data.images.into_iter().filter(|i| i.width != -1) {
                let url = match fetch_inline(&image.url, &referer, &key) {
                    Ok(data_url) => {
                        inlined += 1;
                        data_url
                    }
                    Err(reason) => {
                        if first_error.is_none() {
                            first_error = Some(reason);
                        }
                        image.url
                    }
                };
                pages.push(Page { content: PageContent::url(url), ..Default::default() });
            }
            if page_no >= total_pages || page_no > 50 {
                break;
            }
            page_no += 1;
        }
        if pages.is_empty() {
            bail!("漫蛙漫画本章节没有可读图片，可能需要登录或站点改版");
        }
        if inlined == 0 {
            bail!(
                "漫蛙漫画本章图片内联失败（0/{}）：{}",
                pages.len(),
                first_error.unwrap_or_default()
            );
        }
        Ok(pages)
    }
}

impl ListingProvider for ManwaManhua {
    fn get_manga_list(&self, listing: Listing, page: i32) -> Result<MangaPageResult> {
        let base = base_url();
        match listing.id.as_str() {
            "popular" => listing_page(&base, page, None, &[], 3),
            "latest" => listing_page(&base, page, None, &[], 0),
            _ => bail!("未知的漫蛙漫画列表"),
        }
    }
}

impl BaseUrlProvider for ManwaManhua {
    fn get_base_url(&self) -> Result<String> { Ok(base_url()) }
}

/// Fetch the live AES key from the site's own frontend JS
/// (`{base}/static/assets/js/base.js` contains `AES_KEY: '...'`), mirroring
/// the Mihon manwayuplus extension's getAesKey (regex `AES_KEY\s*:\s*['"]([^'"]+)['"]`,
/// cached value, compiled-in default). Any failure falls back to the
/// compiled-in key, so a missing or changed JS file can never break images
/// that the old key still decrypts.
fn fetch_aes_key(base: &str) -> [u8; 32] {
    let key = (|| -> Option<[u8; 32]> {
        let url = format!("{base}/static/assets/js/base.js");
        let referer = format!("{base}/");
        let response = Request::get(&url).ok()?
            .header("Referer", &referer)
            .send().ok()?;
        if !(200..300).contains(&response.status_code()) {
            return None;
        }
        let bytes = response.get_data().ok()?;
        let text = core::str::from_utf8(&bytes).ok()?;
        let key_str = extract_aes_key(text)?;
        if key_str.len() != 32 {
            return None;
        }
        let mut key = [0u8; 32];
        key.copy_from_slice(key_str.as_bytes());
        Some(key)
    })();
    key.unwrap_or(*crypto::KEY)
}

/// Find the `AES_KEY: '...'` definition in the frontend JS and return the
/// quoted value (exactly 32 chars). Occurrences that are not a definition
/// (e.g. `this.AES_KEY`) are skipped.
fn extract_aes_key(js: &str) -> Option<&str> {
    let marker = "AES_KEY";
    let mut rest = js;
    loop {
        let idx = rest.find(marker)?;
        let after = rest[idx + marker.len()..].trim_start();
        if after.starts_with(':') {
            let after_colon = after[1..].trim_start();
            let quote = after_colon.chars().next()?;
            if quote == '\'' || quote == '"' {
                let inner = &after_colon[quote.len_utf8()..];
                if let Some(end) = inner.find(quote) {
                    let value = &inner[..end];
                    if value.len() == 32 {
                        return Some(value);
                    }
                }
            }
        }
        rest = after;
    }
}

fn url_host(url: &str) -> &str {
    match url.find("://") {
        Some(i) => {
            let rest = &url[i + 3..];
            match rest.find('/') {
                Some(j) => &rest[..j],
                None => rest,
            }
        }
        None => "",
    }
}

fn with_host(url: &str, host: &str) -> String {
    match url.find("://") {
        Some(i) => {
            let rest = &url[i + 3..];
            match rest.find('/') {
                Some(j) => format!("{}://{}{}", &url[..i], host, &rest[j..]),
                None => format!("{}://{}", &url[..i], host),
            }
        }
        None => url.into(),
    }
}

/// Fetch an image, decrypt it and inline it as a data URL.
/// Tries the URL's own host first, then every other known image CDN line
/// with the same path (all lines serve identical bytes), so one dead or
/// blocked CDN no longer fails the image. Unlike the old silent Option,
/// failures carry a short stage tag so the caller can report why inlining
/// failed instead of falling back silently.
fn fetch_inline(url: &str, referer: &str, key: &[u8; 32]) -> core::result::Result<String, String> {
    if url.is_empty() {
        return Err("空地址".into());
    }
    let mut hosts: Vec<&str> = Vec::new();
    let original = url_host(url);
    if !original.is_empty() {
        hosts.push(original);
    }
    for host in IMAGE_HOSTS {
        if host != original {
            hosts.push(host);
        }
    }
    let mut first_error: Option<String> = None;
    for host in hosts {
        match fetch_inline_once(&with_host(url, host), referer, key) {
            Ok(data_url) => return Ok(data_url),
            Err(reason) => {
                if first_error.is_none() {
                    first_error = Some(reason);
                }
            }
        }
    }
    Err(first_error.unwrap_or_else(|| String::from("图床全部线路失败")))
}

fn fetch_inline_once(url: &str, referer: &str, key: &[u8; 32]) -> core::result::Result<String, String> {
    let response = Request::get(url)
        .map_err(|_| String::from("创建请求失败"))?
        .header("Referer", referer)
        .send()
        .map_err(|_| String::from("发送请求失败"))?;
    let code = response.status_code();
    if !(200..300).contains(&code) {
        return Err(format!("图床 HTTP {code}"));
    }
    let bytes = response.get_data().map_err(|_| String::from("读取图片数据失败"))?;
    crypto::data_url_with_key(&bytes, key).map_err(|_| String::from("解密失败"))
}

register_source!(ManwaManhua, ListingProvider, BaseUrlProvider);

