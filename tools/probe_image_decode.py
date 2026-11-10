#!/usr/bin/env python3
"""Read one image from stdin; fully decode it. No images are displayed or saved."""
import io
import json
import sys
import warnings
from PIL import Image

Image.MAX_IMAGE_PIXELS = 60_000_000
warnings.simplefilter('error', Image.DecompressionBombWarning)
try:
    data = sys.stdin.buffer.read(25 * 1024 * 1024 + 1)
    if len(data) > 25 * 1024 * 1024:
        raise ValueError('image exceeds 25 MiB probe limit')
    with Image.open(io.BytesIO(data)) as image:
        kind = image.format
        width, height = image.size
        image.load()
        if width < 16 or height < 16:
            raise ValueError('image is smaller than 16x16')
    print(json.dumps(dict(ok=True, format=kind, width=width, height=height,
                          bytes=len(data))))
except Exception as error:
    print(json.dumps(dict(ok=False, error=str(error)[:300])))
    sys.exit(1)
