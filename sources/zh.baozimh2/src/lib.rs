#![no_std]
extern crate alloc;

mod decode;

use aidoku::{
	error::Result,
	helpers::uri::encode_uri,
	prelude::*,
	std::{
		json,
		net::{HttpMethod, Request},
		String, Vec,
	},
	Chapter, Filter, FilterType, Listing, Manga, MangaContentRating, MangaPageResult, MangaStatus,
	MangaViewer, Page,
};
use alloc::string::ToString;

const WWW_URL: &str = "https://baozimh.org";
const API_URL: &str = "https://api-get-v3.mgsearcher.com";
// Image hosts from the site reader (/_astro/getChapter.*.js): route "c"
// (the default), line 2 -> c-nd2, any other line -> c-nd3. The old
// f40-1-4.g-mh.online host is no longer referenced anywhere.
const IMG_URL_LINE2: &str = "https://c-nd2-1.6wm.top";
const IMG_URL_DEFAULT: &str = "https://c-nd3-1.6wm.top";

const FILTER_CATEGORY: [&str; 35] = [
	"",
	"cn",
	"kr",
	"jp",
	"fuchou",
	"gufeng",
	"qihuan",
	"nixi",
	"lianai",
	"yineng",
	"zhaixiang",
	"chuanyue",
	"rexue",
	"chunai",
	"xitong",
	"chongsheng",
	"maoxian",
	"lingyi",
	"danvzhu",
	"juqing",
	"lianai",
	"xuanhuan",
	"nvshen",
	"kehuan",
	"mohuan",
	"tuili",
	"lieqi",
	"zhiyu",
	"dushi",
	"yixing",
	"qingchun",
	"mori",
	"xuanyi",
	"xiuxian",
	"zhandou",
];

fn handle_cover_url(url: String) -> String {
	if url.contains("url=") {
		url.split("url=")
			.map(|a| a.to_string())
			.collect::<Vec<String>>()
			.pop()
			.unwrap()
			.replace("%3A", ":")
			.replace("%2F", "/")
			.replace("&w=250&q=60", "")
	} else {
		url
	}
}

/// Description for a manga. Prefers the chapter API's `data.desc` (the same
/// call the chapter list uses); falls back to the page's
/// `<meta name="description">`, stripping a leading "<first chapter title> "
/// prefix when the API told us the first chapter's title. The old
/// `.text-medium.my-unit-md` selector no longer exists on the site (the
/// detail page is now Astro-rendered and carries no such class).
fn manga_description(mid: &str, meta_desc: String) -> String {
	let api = (|| -> Option<(String, String)> {
		let url = format!("{}/api/manga/get?mid={}&mode=all", API_URL, mid);
		let json = Request::new(url, HttpMethod::Get)
			.header("Origin", &WWW_URL)
			.header("Referer", &WWW_URL)
			.json()
			.ok()?;
		let data = json.as_object().ok()?;
		let data = data.get("data").as_object().ok()?;
		let desc = data.get("desc").as_string().ok()?.read();
		let first_title = data
			.get("chapters")
			.as_array()
			.ok()
			.and_then(|list| list.get(0).as_object().ok())
			.and_then(|item| item.get("attributes").as_object().ok())
			.and_then(|attributes| attributes.get("title").as_string().ok())
			.map(|title| title.read())
			.unwrap_or_default();
		Some((desc, first_title))
	})();
	match api {
		Some((desc, first_title)) => {
			let desc = desc.trim().to_string();
			if !desc.is_empty() {
				return desc;
			}
			let title = first_title.trim();
			if title.is_empty() {
				return meta_desc;
			}
			let prefixed = format!("{} ", title);
			match meta_desc.strip_prefix(prefixed.as_str()) {
				Some(rest) => rest.trim().to_string(),
				None => meta_desc,
			}
		}
		None => meta_desc,
	}
}

#[get_manga_list]
fn get_manga_list(filters: Vec<Filter>, page: i32) -> Result<MangaPageResult> {
	let mut query = String::new();
	let mut category = String::new();

	for filter in filters {
		match filter.kind {
			FilterType::Title => {
				query = filter.value.as_string()?.read();
			}
			FilterType::Select => {
				let index = filter.value.as_int()? as usize;
				match filter.name.as_str() {
					"类型" => {
						category = FILTER_CATEGORY[index].to_string();
					}
					_ => continue,
				}
			}
			_ => continue,
		}
	}

	let has_more = true;
	let mut mangas: Vec<Manga> = Vec::new();

	let url = if query.is_empty() {
		let category_str = if category.is_empty() {
			String::from("manga")
		} else if category.len() <= 2 {
			format!("manga-genre/{}", category)
		} else {
			format!("manga-tag/{}", category)
		};
		format!("{}/{}/page/{}", WWW_URL, category_str, page)
	} else {
		format!("{}/s/{}?page={}", WWW_URL, encode_uri(query), page)
	};
	let html = Request::new(url, HttpMethod::Get).html()?;

	for item in html.select(".pb-2>a").array() {
		let item = match item.as_node() {
			Ok(node) => node,
			Err(_) => continue,
		};
		let id = item
			.attr("href")
			.read()
			.split("/")
			.map(|a| a.to_string())
			.collect::<Vec<String>>()
			.pop()
			.unwrap();
		let cover = handle_cover_url(item.select("div>img").attr("src").read());
		let title = item.select("div>h3").text().read();
		mangas.push(Manga {
			id,
			cover,
			title,
			..Default::default()
		});
	}

	Ok(MangaPageResult {
		manga: mangas,
		has_more,
	})
}

#[get_manga_listing]
fn get_manga_listing(listing: Listing, page: i32) -> Result<MangaPageResult> {
	let mut list = String::new();

	match listing.name.as_str() {
		"人气推荐" => {
			list.push_str("hots");
		}
		"热门更新" => {
			list.push_str("dayup");
		}
		"最新上架" => {
			list.push_str("newss");
		}
		_ => return get_manga_list(Vec::new(), page),
	}

	let url = format!("{}/{}/page/{}", WWW_URL, list, page);
	let html = Request::new(url, HttpMethod::Get).html()?;
	let has_more = true;
	let mut mangas: Vec<Manga> = Vec::new();

	for item in html.select(".pb-2>a").array() {
		let item = match item.as_node() {
			Ok(node) => node,
			Err(_) => continue,
		};
		let id = item
			.attr("href")
			.read()
			.split("/")
			.map(|a| a.to_string())
			.collect::<Vec<String>>()
			.pop()
			.unwrap();
		let cover = handle_cover_url(item.select("div>img").attr("src").read());
		let title = item.select("div>h3").text().read();
		mangas.push(Manga {
			id,
			cover,
			title,
			..Default::default()
		});
	}

	Ok(MangaPageResult {
		manga: mangas,
		has_more,
	})
}

#[get_manga_details]
fn get_manga_details(id: String) -> Result<Manga> {
	let ids = id.split("/").collect::<Vec<&str>>();
	let url = format!("{}/manga/{}", WWW_URL, ids[0]);
	let html = Request::new(url.clone(), HttpMethod::Get).html()?;
	let mid = html.select("#mangachapters").attr("data-mid").read();
	let cover = handle_cover_url(
		html.select("meta[property='og:image']")
			.attr("content")
			.read(),
	);
	let title = html
		.select("title")
		.text()
		.read()
		.replace("-🌈️包子漫畫", "")
		.replace("-包子漫畫", "");
	let author = html
		.select("a[href*=author]>span")
		.array()
		.map(|a| a.as_node().unwrap().text().read().replace(",", ""))
		.filter(|a| !a.is_empty())
		.collect::<Vec<String>>()
		.join(", ");
	let artist = String::new();
	let meta_desc = html
		.select("meta[name='description']")
		.attr("content")
		.read();
	let description = manga_description(&mid, meta_desc);
	let categories = html
		.select(".py-1>a:not([href*=author])>span")
		.array()
		.map(|a| {
			a.as_node()
				.unwrap()
				.text()
				.read()
				.replace(",", "")
				.replace("热门漫画", "")
				.replace("#", "")
				.replace("热门推荐", "")
				.trim()
				.to_string()
		})
		.filter(|a| !a.is_empty())
		.collect::<Vec<String>>();
	let status = MangaStatus::Ongoing;
	let nsfw = MangaContentRating::Safe;
	let viewer = MangaViewer::Scroll;

	Ok(Manga {
		id: format!("{}/{}", ids[0], mid),
		cover,
		title,
		author,
		artist,
		description,
		url,
		categories,
		status,
		nsfw,
		viewer,
	})
}

#[get_chapter_list]
fn get_chapter_list(id: String) -> Result<Vec<Chapter>> {
	let ids = id.split("/").collect::<Vec<&str>>();
	let url = format!("{}/api/manga/get?mid={}&mode=all", API_URL, ids[1]);
	let json = Request::new(url.clone(), HttpMethod::Get)
		.header("Origin", &WWW_URL)
		.header("Referer", &WWW_URL)
		.json()?;
	let data = json.as_object()?;
	let data = data.get("data").as_object()?;
	let list = data.get("chapters").as_array()?;
	let mut chapters: Vec<Chapter> = Vec::new();

	for (index, item) in list.enumerate() {
		let item = match item.as_object() {
			Ok(item) => item,
			Err(_) => continue,
		};
		let attributes = item.get("attributes").as_object()?;
		let id = item.get("id").as_int()?.to_string();
		let title = attributes.get("title").as_string()?.read();
		let slug = attributes.get("slug").as_string()?.read();
		let url = format!("{}/manga/{}/{}", WWW_URL, ids[0], slug);
		let chapter = (index + 1) as f32;
		chapters.push(Chapter {
			id,
			title,
			chapter,
			url,
			..Default::default()
		});
	}
	chapters.reverse();

	Ok(chapters)
}

#[get_page_list]
fn get_page_list(manga_id: String, chapter_id: String) -> Result<Vec<Page>> {
	let ids = manga_id.split("/").collect::<Vec<&str>>();
	let url = format!(
		"{}/api/v2/chapter/getinfo?m={}&c={}",
		API_URL,
		ids[1],
		chapter_id.clone()
	);
	let json = Request::new(url.clone(), HttpMethod::Get)
		.header("Origin", &WWW_URL)
		.header("Referer", &WWW_URL)
		.json()?;
	let data = json.as_object()?;
	let data = data.get("data").as_object()?;
	let info = data.get("info").as_object()?;
	let images = info.get("images").as_object()?;
	// The v2 endpoint no longer returns an array of {url} objects:
	// `images.images` is one obfuscated string, decoded in decode.rs,
	// and `images.line` selects the image host (2 -> c-nd2, else c-nd3).
	let images_str = images.get("images").as_string()?.read();
	let line = images.get("line").as_int().unwrap_or(0);
	let img_url = if line == 2 {
		IMG_URL_LINE2
	} else {
		IMG_URL_DEFAULT
	};
	let decoded = decode::decode_images(&images_str)?;
	let list = json::parse(decoded.as_bytes())?.as_array()?;
	let mut pages: Vec<Page> = Vec::new();

	for (index, item) in list.enumerate() {
		let item = match item.as_object() {
			Ok(item) => item,
			Err(_) => continue,
		};
		let index = index as i32;
		let url = match item.get("url").as_string() {
			Ok(url) => format!("{}{}", img_url, url.read()),
			Err(_) => continue,
		};
		pages.push(Page {
			index,
			url,
			..Default::default()
		})
	}

	Ok(pages)
}

#[modify_image_request]
fn modify_image_request(request: Request) {
	request.header("Referer", &WWW_URL);
}
