// SPDX-License-Identifier: Apache-2.0
// Port of Keiyoushi `ManwaManhua` (see NOTICE). No website JavaScript is executed.
//
// Reader and cover images are AES-256-CBC with the IV prepended (first 16 bytes).
// They are decrypted in-source and handed to the host as `data:` URLs, so no
// host canvas / image processor is involved (that path is unreliable on mobile).

use aidoku::{
    Result, alloc::{String, Vec, format},
    prelude::*,
};
use base64::Engine;
use base64::engine::general_purpose::STANDARD as B64;

pub const KEY: &[u8; 32] = b"0B6666A0-BB59-1381-B746-a0E4C9AC";

pub fn is_image(data: &[u8]) -> bool {
    data.starts_with(b"\xff\xd8\xff")
        || data.starts_with(b"\x89PNG\r\n\x1a\n")
        || data.starts_with(b"GIF87a")
        || data.starts_with(b"GIF89a")
        || (data.len() >= 12 && &data[..4] == b"RIFF" && &data[8..12] == b"WEBP")
}

pub fn mime_of(data: &[u8]) -> &'static str {
    if data.starts_with(b"\xff\xd8\xff") { "image/jpeg" }
    else if data.starts_with(b"\x89PNG") { "image/png" }
    else if data.starts_with(b"GIF") { "image/gif" }
    else if data.len() >= 12 && &data[..4] == b"RIFF" { "image/webp" }
    else { "application/octet-stream" }
}

pub fn decrypt(data: &[u8]) -> Result<Vec<u8>> {
    decrypt_with_key(data, KEY)
}

/// Same as `decrypt`, but with an explicitly supplied key (fetched live from
/// the site's frontend JS when available; falls back to `KEY`).
pub fn decrypt_with_key(data: &[u8], key: &[u8; 32]) -> Result<Vec<u8>> {
    use aes::Aes256;
    use cbc::cipher::{BlockDecryptMut, KeyIvInit, block_padding::NoPadding};

    if is_image(data) {
        return Ok(data.to_vec());
    }
    if data.len() <= 16 || (data.len() - 16) % 16 != 0 {
        bail!("漫蛙图片长度不符合 AES-CBC 结构，可能被验证页替换");
    }
    let (iv, payload) = data.split_at(16);
    let mut buffer = payload.to_vec();
    cbc::Decryptor::<Aes256>::new(key.into(), iv.into())
        .decrypt_padded_mut::<NoPadding>(&mut buffer)
        .map_err(|_| error!("漫蛙图片 AES-CBC 解密失败"))?;
    if !is_image(&buffer) {
        bail!("漫蛙图片解密后没有有效图片签名，站点可能更换了密钥");
    }
    Ok(buffer)
}

/// Decrypt (when needed) and inline as a data URL the client can render directly.
pub fn data_url(data: &[u8]) -> Result<String> {
    data_url_with_key(data, KEY)
}

pub fn data_url_with_key(data: &[u8], key: &[u8; 32]) -> Result<String> {
    let bytes = decrypt_with_key(data, key)?;
    Ok(format!("data:{};base64,{}", mime_of(&bytes), B64.encode(&bytes)))
}
