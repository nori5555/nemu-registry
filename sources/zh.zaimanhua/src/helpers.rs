use crate::{
	models::{ClassifyData, DetailData, RankItem},
	urls,
};
use aidoku::{
	Manga, MangaPageResult, Result,
	alloc::{String, Vec},
	imports::net::Request,
};
use core::cell::RefCell;
use zaimanhua_template::{
	Kind, Params,
	models::{ApiResponse, ClassifyTag, ListItem},
	net, settings,
};

pub use zaimanhua_template::helpers::*;

// the comic api has no author search, so the tag comes from one of the author's comics
pub fn search_author(
	cache: &RefCell<Option<(String, Option<i64>)>>,
	author: &str,
	page: i32,
) -> Result<MangaPageResult> {
	let search = |page| fetch_list(&urls::search(Kind::Comic, author, page));
	let cached = cache
		.borrow()
		.as_ref()
		.filter(|(name, _)| name == author)
		.map(|(_, tag_id)| *tag_id);
	let (tag_id, results) = match cached {
		Some(tag_id) => (tag_id, None),
		None => {
			let results = search(1)?;
			// guests can't open most search hits by id, but by_pinyin answers them
			let comic_py = results
				.list
				.iter()
				.filter(|item| is_by(item, author))
				.find_map(|item| item.comic_py.as_deref());
			let tag_id = match comic_py {
				Some(comic_py) => {
					net::fetch::<DetailData>(&urls::detail_by_pinyin(comic_py), None)?
						.data
						.and_then(|detail| detail.author_id(author))
				}
				None => None,
			};
			cache.replace(Some((author.into(), tag_id)));
			(tag_id, (page == 1).then_some(results))
		}
	};
	let Some(tag_id) = tag_id else {
		let results = match results {
			Some(results) => results,
			None => search(page)?,
		};
		return Ok(results.into_page_result(page));
	};
	// author lists leave out unlicensed comics for guests and level 0, but search has them
	let (list, mut results) = match results {
		Some(results) => (fetch_list(&urls::author(tag_id, page))?, results),
		None => {
			let token = settings::get_token();
			let requests = [
				urls::author(tag_id, page),
				urls::search(Kind::Comic, author, page),
			]
			.iter()
			.map(|url| net::request(url, token.as_deref()))
			.collect::<Result<Vec<_>>>()?;
			let mut responses = Request::send_all(requests).into_iter();
			(net::parse(responses.next())?, net::parse(responses.next())?)
		}
	};
	let mut result = list.into_page_result(page);
	results.list.retain(|item| is_by(item, author));
	let mut hits = results.into_page_result(page);
	hits.entries
		.retain(|manga| result.entries.iter().all(|entry| entry.key != manga.key));
	result.entries.append(&mut hits.entries);
	result.has_next_page |= hits.has_next_page;
	Ok(result)
}

fn is_by(item: &ListItem, author: &str) -> bool {
	item.authors
		.as_deref()
		.is_some_and(|authors| authors.split('/').any(|name| name == author))
}

pub fn rank_list(by_time: &str, page: i32) -> Result<MangaPageResult> {
	let entries: Vec<Manga> =
		net::fetch::<Vec<RankItem>>(&urls::rank(by_time, page), settings::get_token().as_deref())?
			.into_iter()
			.map(Into::into)
			.collect();
	Ok(MangaPageResult {
		has_next_page: !entries.is_empty(),
		entries,
	})
}

pub fn theme_tags() -> Result<Vec<ClassifyTag>> {
	Ok(net::fetch::<ClassifyData>(&urls::classify(), None)?
		.classify_list
		.into_iter()
		.find(|group| group.id == 1)
		.map(|group| group.list)
		.unwrap_or_default())
}

/// Fetch a comic's detail, working around the guest limit on detail-by-id.
///
/// The server answers errno 2 ("漫画不存在或已被删除") for some comics when
/// guests fetch `/comic/detail/{id}`, while `/comic/detail/by_pinyin` answers
/// the same comic fine. When the by-id call reports a hidden comic, search by
/// title, resolve the comic's `comic_py` from the search hit with the same id,
/// and fetch the detail by pinyin instead. Any failure in that fallback
/// (search error, no matching hit, by-pinyin error) falls through to the
/// original errno 2 error, so genuinely missing comics still report exactly
/// as before.
pub fn fetch_detail(params: &Params, manga: &Manga) -> Result<DetailData> {
	let response = with_access_response(|token| {
		net::request(&urls::detail(params.kind, &manga.key), token)?
			.json_owned::<ApiResponse<DetailData>>()
	})?;
	if is_hidden(&response) {
		if let Ok(Some(detail)) = try_detail_by_pinyin(&manga.key, &manga.title) {
			return Ok(detail);
		}
	}
	response.into_result()
}

/// Search by title for the comic with `key`, then fetch its detail by pinyin.
/// Returns `Ok(None)` when the comic cannot be resolved (empty title, no
/// matching search hit, or an empty `comic_py`).
fn try_detail_by_pinyin(key: &str, title: &str) -> Result<Option<DetailData>> {
	if title.trim().is_empty() {
		return Ok(None);
	}
	let results = fetch_list(&urls::search(Kind::Comic, title, 1))?;
	let comic_py = key
		.parse::<i64>()
		.ok()
		.and_then(|id| results.list.iter().find(|item| item.id == id))
		.and_then(|item| item.comic_py.as_deref());
	let Some(comic_py) = comic_py else {
		return Ok(None);
	};
	Ok(Some(
		net::fetch::<DetailData>(&urls::detail_by_pinyin(comic_py), None)?,
	))
}

pub fn with_access<T: Default>(
	fetch: impl Fn(Option<&str>) -> Result<ApiResponse<T>>,
) -> Result<T> {
	with_access_response(fetch)?.into_result()
}

pub fn with_access_response<T: Default>(
	fetch: impl Fn(Option<&str>) -> Result<ApiResponse<T>>,
) -> Result<ApiResponse<T>> {
	let token = settings::get_token();
	let response = fetch(token.as_deref())?;
	let Some(token) = token.filter(|_| is_hidden(&response)) else {
		return Ok(response);
	};
	// an expired token reads as a guest, and user_info renews it
	_ = net::user_info();
	match settings::get_token().filter(|renewed| *renewed != token) {
		Some(renewed) => fetch(Some(&renewed)),
		None => Ok(response),
	}
}

fn is_hidden<T>(response: &ApiResponse<T>) -> bool {
	response.errno == 2 && response.errmsg.as_deref() == Some("漫画不存在或已被删除")
}
