"""Attachment sanitising: type sniffing, re-encoding (drops EXIF/metadata), server-side blur, text masking."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

from PIL import Image, ImageFilter

from branchops.domain.services.masking import SensitiveDataMasker

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"
MAX_BLUR_REGIONS = 20
EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "text/plain": ".txt"}


class AttachmentRejectedError(ValueError):
    """The upload violates a limit or is not what it claims to be."""


@dataclass(frozen=True)
class ProcessedAttachment:
    data: bytes
    content_type: str
    blurred_regions: int
    width: int | None = None
    height: int | None = None


def sniff(data: bytes) -> str | None:
    if data.startswith(PNG_MAGIC):
        return "image/png"
    if data.startswith(JPEG_MAGIC):
        return "image/jpeg"
    return None


def safe_filename(name: str, content_type: str) -> str:
    """Basename without path separators or control characters, with the extension of the real type."""
    base = re.split(r"[\\/]", name or "")[-1]
    base = re.sub(r"[\x00-\x1f\x7f]", "", base).strip(" .") or "ek"
    stem = base.rsplit(".", 1)[0] if "." in base else base
    stem = re.sub(r"[^\w.\- ()]+", "_", stem)[:100] or "ek"
    return stem + EXTENSIONS.get(content_type, "")


def _blur(image: Image.Image, regions: list[list[int]]) -> int:
    applied = 0
    width, height = image.size
    for region in regions[:MAX_BLUR_REGIONS]:
        if len(region) != 4:
            raise AttachmentRejectedError("blur region must be [x, y, w, h]")
        x, y, w, h = (int(v) for v in region)
        left, top = max(0, x), max(0, y)
        right, bottom = min(width, x + max(0, w)), min(height, y + max(0, h))
        if right <= left or bottom <= top:
            continue
        box = (left, top, right, bottom)
        patch = image.crop(box)
        # Pixelate first (irreversible), then blur so the area reads as "redacted", not as a mosaic.
        small = patch.resize(
            (max(1, patch.width // 16), max(1, patch.height // 16)), Image.Resampling.BILINEAR
        )
        patch = small.resize(patch.size, Image.Resampling.NEAREST).filter(ImageFilter.GaussianBlur(8))
        image.paste(patch, box)
        applied += 1
    return applied


def process_image(
    data: bytes, content_type: str, blur_regions: list[list[int]] | None, *, max_pixels: int
) -> ProcessedAttachment:
    if sniff(data) != content_type:
        raise AttachmentRejectedError(f"content is not a valid {content_type}")
    if len(blur_regions or []) > MAX_BLUR_REGIONS:
        raise AttachmentRejectedError(f"at most {MAX_BLUR_REGIONS} blur regions")
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > max_pixels:
                raise AttachmentRejectedError("image dimensions too large")
            source.load()
            # Copy pixels only: EXIF, ICC, text chunks and other metadata are dropped.
            mode = "RGB" if content_type == "image/jpeg" else ("RGBA" if "A" in source.getbands() else "RGB")
            image = Image.new(mode, source.size)
            image.paste(source.convert(mode))
    except AttachmentRejectedError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise AttachmentRejectedError(f"unreadable image: {type(exc).__name__}") from exc
    blurred = _blur(image, blur_regions or [])
    out = io.BytesIO()
    if content_type == "image/png":
        image.save(out, format="PNG", optimize=True)
    else:
        image.save(out, format="JPEG", quality=85, optimize=True)
    return ProcessedAttachment(out.getvalue(), content_type, blurred, image.width, image.height)


def process_text(data: bytes, masker: SensitiveDataMasker) -> ProcessedAttachment:
    if sniff(data) is not None or b"\x00" in data:
        raise AttachmentRejectedError("content is not plain text")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AttachmentRejectedError("text attachments must be UTF-8") from exc
    return ProcessedAttachment((masker.mask_text(text) or "").encode("utf-8"), "text/plain", 0)
