// SPDX-License-Identifier: Apache-2.0
// Port of Keiyoushi `ManwaManhua` (see NOTICE). No website JavaScript is executed.
//
// The site encrypts reader images: AES-256-CBC with the IV prepended to the
// payload (first 16 bytes), key given below. Decryption happens in
// `process_page_image`, so the host never needs a canvas.

use aidoku::{
    Result, alloc::Vec,
    imports::canvas::ImageRef,
    prelude::*,
};

const KEY: &[u8; 32] = b"0B6666A0-BB59-1381-B746-a0E4C9AC";

pub fn is_image(data: &[u8]) -> bool {
    data.starts_with(b"\xff\xd8\xff")
        || data.starts_with(b"\x89PNG\r\n\x1a\n")
        || data.starts_with(b"GIF87a")
        || data.starts_with(b"GIF89a")
        || (data.len() >= 12 && &data[..4] == b"RIFF" && &data[8..12] == b"WEBP")
}

pub fn decrypt_prepended_iv(data: &[u8]) -> Result<Vec> {
    use aes::Aes256;
    use cbc::cipher::{BlockDecryptMut, KeyIvInit, block_padding::NoPadding};

    // Mirrors from time to time serve already-decoded bytes; keep idempotent.
    if is_image(data) {
        return Ok(data.to_vec());
    }
    if data.len() <= 16 || (data.len() - 16) % 16 != 0 {
        bail!("漫蛙图片长度不符合 AES-CBC 结构，可能被验证页替换");
    }
    let (iv, payload) = data.split_at(16);
    let mut buffer = payload.to_vec();
    cbc::Decryptor::<Aes256>::new(KEY.into(), iv.into())
        .decrypt_padded_mut::<NoPadding>(&mut buffer)
        .map_err(|_| error!("漫蛙图片 AES-CBC 解密失败"))?;
    if !is_image(&buffer) {
        bail!("漫蛙图片解密后没有有效图片签名，站点可能更换了密钥");
    }
    Ok(buffer)
}

pub fn image_ref(bytes: Vec) -> Result<ImageRef> {
    Ok(ImageRef::new(&bytes))
}
