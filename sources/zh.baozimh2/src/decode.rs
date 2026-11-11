//! Decode the obfuscated chapter image list returned by
//! `/api/v2/chapter/getinfo` (`data.info.images.images`).
//!
//! Reverse-engineered from the site's shipped reader JavaScript
//! (`/_astro/getChapter.*.js` loading `/assets/runtime/chapter-decoder.js`,
//! which exposes the decoder as `window.__cimg.r`). The "encryption" is
//! only a fixed re-ordering plus an alphabet substitution, all constants
//! visible in that shipped file; no key material is involved:
//!
//! 1. The string is wrapped in the markers `J7r` (prefix) and `nQ`
//!    (suffix); the inside is split into three segments around the
//!    literal markers `kD` and `W4s`, with lengths derived from the
//!    total length: third = total / 3, first = (total - third) / 2,
//!    second = the remainder.
//! 2. The segments are re-ordered to third + first + second.
//! 3. Every odd 7-character chunk is reversed.
//! 4. Each character is translated from the site's custom alphabet to
//!    the standard base64url alphabet.
//! 5. The result is base64url-decoded to a JSON array of
//!    `{"order": N, "url": "/scomic/..."}` objects.
//!
//! This module only performs steps 1-5 up to the JSON text; the caller
//! parses the JSON with the host JSON parser.

use aidoku::{
	error::{AidokuError, AidokuErrorKind, Result},
	std::String,
};
use alloc::{string::ToString, vec::Vec};
use base64::{engine::general_purpose::URL_SAFE, Engine as _};

const PREFIX: &str = "J7r";
const SUFFIX: &str = "nQ";
const MARK_A: &str = "kD";
const MARK_B: &str = "W4s";
const CHUNK_LEN: usize = 7;
const CUSTOM_ALPHABET: &[u8; 64] =
	b"_-9876543210abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ";
const STANDARD_ALPHABET: &[u8; 64] =
	b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

fn decode_error() -> AidokuError {
	AidokuError {
		reason: AidokuErrorKind::JsonParseError,
	}
}

/// Decode the `images.images` string into the JSON text of the page list.
pub fn decode_images(input: &str) -> Result<String> {
	if !input.is_ascii() || !input.starts_with(PREFIX) || !input.ends_with(SUFFIX) {
		return Err(decode_error());
	}
	let inner = &input[PREFIX.len()..input.len() - SUFFIX.len()];
	let total = match inner.len().checked_sub(MARK_A.len() + MARK_B.len()) {
		Some(total) if total > 0 => total,
		_ => return Err(decode_error()),
	};
	// Mirror the reader's length arithmetic exactly.
	let third_len = total / 3;
	let first_len = (total - third_len) / 2;
	let second_len = total - third_len - first_len;
	let mut rest = inner;
	let first = &rest[..first_len];
	rest = &rest[first_len..];
	if !rest.starts_with(MARK_A) {
		return Err(decode_error());
	}
	rest = &rest[MARK_A.len()..];
	let second = &rest[..second_len];
	rest = &rest[second_len..];
	if !rest.starts_with(MARK_B) {
		return Err(decode_error());
	}
	rest = &rest[MARK_B.len()..];
	let third = rest;
	if third.len() != third_len {
		return Err(decode_error());
	}
	// Re-order: third + first + second.
	let mut chars: Vec<char> = Vec::with_capacity(total);
	chars.extend(third.chars());
	chars.extend(first.chars());
	chars.extend(second.chars());
	// Reverse every odd 7-character chunk.
	for (index, chunk) in chars.chunks_mut(CHUNK_LEN).enumerate() {
		if index % 2 == 1 {
			chunk.reverse();
		}
	}
	// Translate the custom alphabet to standard base64url.
	let mut encoded: Vec<u8> = Vec::with_capacity(total);
	for ch in chars {
		if !ch.is_ascii() {
			return Err(decode_error());
		}
		let byte = ch as u8;
		match CUSTOM_ALPHABET.iter().position(|&b| b == byte) {
			Some(position) => encoded.push(STANDARD_ALPHABET[position]),
			None => return Err(decode_error()),
		}
	}
	// Pad to a multiple of 4, exactly like the reader does before atob.
	let pad = (4 - encoded.len() % 4) % 4;
	for _ in 0..pad {
		encoded.push(b'=');
	}
	let bytes = URL_SAFE.decode(&encoded).map_err(|_| decode_error())?;
	let text = core::str::from_utf8(&bytes)?.to_string();
	Ok(text)
}
