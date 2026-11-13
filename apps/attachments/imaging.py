"""Thumbnail generation for photo attachments (Pillow, dependency-light).

The app has exactly one attachment model and one thumbnail size, so we
use Pillow directly rather than pulling in sorl-thumbnail/imagekit. A
~300 px JPEG (~15-30 KB) replaces a full-size original (up to 10 MB) in
list/grid views, dropping a five-photo case list from ~50 MB of page
weight to well under 200 KB.
"""

import io

from PIL import Image, ImageOps, UnidentifiedImageError

#: Long-edge cap for the grid thumbnail, in pixels.
THUMBNAIL_MAX_EDGE = 300
#: JPEG quality for the generated thumbnail.
THUMBNAIL_QUALITY = 80
#: Largest source the thumbnailer will decode (a 50 Mpx phone photo is
#: ~7100x7100); anything bigger gets no thumbnail rather than a worker OOM.
MAX_SOURCE_PIXELS = 50_000_000


def build_thumbnail_bytes(source, max_edge=THUMBNAIL_MAX_EDGE):
    """Return JPEG thumbnail bytes for an image file object, or ``None``.

    ``source`` is any binary file-like object positioned anywhere (it is
    rewound first). Returns ``None`` if the bytes cannot be decoded as an
    image, so callers can treat non-images and corrupt uploads uniformly
    without the thumbnail step ever breaking an upload.

    EXIF orientation is baked into the pixels (``exif_transpose``) so a
    portrait phone photo is not shown sideways once the EXIF tag is
    dropped by the JPEG re-encode. The image is flattened to RGB so PNGs
    with alpha and CMYK/palette modes all encode as JPEG cleanly.

    ``max_edge`` overrides the grid default: video posters
    (``video.POSTER_MAX_EDGE``) pass a larger cap because they render in
    a tile roughly three times wider than a photo thumbnail's. Sizing and
    quality stay in this one module so posters and thumbnails come out of
    the same encoder and match visually.
    """
    try:
        source.seek(0)
        with Image.open(source) as img:
            # Pillow's own bomb threshold (~179 Mpx) still lets a few
            # hundred KB of PNG decode into gigabytes of bitmap inside a
            # request worker; cap the source first, and shrink it before
            # the transpose/convert copies are made.
            if img.width * img.height > MAX_SOURCE_PIXELS:
                return None
            img.thumbnail((max_edge, max_edge))
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=THUMBNAIL_QUALITY, optimize=True)
            return buffer.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None
