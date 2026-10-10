"""
Owner-supplied photographs.

Users can attach their own images to a scan — interiors of the envelope Street
View cannot see, a facade behind a hedge, damage they already know about, or
any property Google's cars have never driven past.

Everything arriving here is untrusted input from the public internet, so
nothing is stored as uploaded. Each file is decoded, bounded, re-encoded to a
plain JPEG and written under a name we chose. That single step handles the
whole category at once: no attacker-controlled filenames or extensions, no
polyglot files pretending to be images, no EXIF (which routinely carries the
GPS coordinates of the photographer's home), and no 20-megapixel originals
inflating the vision-model bill.
"""

import asyncio
import io
import logging
from pathlib import Path
from typing import List, Sequence, Tuple

from PIL import Image, ImageOps, UnidentifiedImageError

from . import config

log = logging.getLogger(__name__)

# iPhones shoot HEIC by default, and a browser that cannot decode it cannot
# downscale it either, so these arrive as-is. pillow-heif teaches PIL to read
# them; without it they are refused with the usual message.
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF = {"HEIF"}
except Exception:  # noqa: BLE001 - optional dependency
    HEIF = set()

# Formats we will decode. A file claiming any other type is rejected before PIL
# is asked to parse it.
ACCEPTED = {"JPEG", "PNG", "WEBP", "BMP", "GIF", "MPO"} | HEIF
ACCEPTED_HINT = "JPEG, PNG, WebP or HEIC" if HEIF else "JPEG, PNG or WebP"

# Pillow warns above ~89 megapixels and raises above twice that. Neither is a
# useful ceiling for a phone photo, and a crafted file that decompresses to
# gigabytes is the classic way to take a service down with one small upload.
Image.MAX_IMAGE_PIXELS = 64_000_000


class UploadRejected(Exception):
    """A user-fixable problem with an attachment. The message is shown verbatim."""


def _too_big(name: str) -> UploadRejected:
    return UploadRejected(
        f"{name} is larger than {config.MAX_UPLOAD_MB:g} MB. "
        "Resize it or pick a smaller photo."
    )


async def _read_bounded(upload, limit: int) -> bytes:
    """
    Read at most `limit` bytes.

    Content-Length is a claim by the client, so the cap is enforced on what is
    actually read rather than on what the header promises.
    """
    chunks, total = [], 0
    while True:
        chunk = await upload.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise _too_big(upload.filename or "That image")
        chunks.append(chunk)
    return b"".join(chunks)


# Edge of the thumbnail written beside each upload. The live feed shows every
# photo while the scan runs, and fifty full-size images is a lot to pull to
# draw fifty small squares.
THUMB_EDGE = 480


def _normalise(raw: bytes, label: str) -> Tuple[bytes, bytes]:
    """Decode, orient, bound, re-encode as JPEG. Returns (photo, thumbnail) bytes."""
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            fmt = (probe.format or "").upper()
            if fmt not in ACCEPTED:
                raise UploadRejected(
                    f"{label} is a {fmt or 'unrecognised'} file. Please upload {ACCEPTED_HINT}."
                )
            # Phone photos are usually stored unrotated with an EXIF orientation
            # flag. Applying it before we discard EXIF keeps them upright — the
            # model reads a sideways facade as a sideways facade.
            image = ImageOps.exif_transpose(probe)
            image = image.convert("RGB")
    except UploadRejected:
        raise
    except Image.DecompressionBombError:
        raise UploadRejected(f"{label} has implausibly large dimensions and was not read.")
    except (UnidentifiedImageError, OSError) as exc:
        raise UploadRejected(
            f"{label} could not be read as an image. It may be corrupt or "
            f"a renamed file of another type."
        ) from exc

    # Downscale only — never upscale a small photo into false detail.
    edge = config.UPLOAD_MAX_EDGE
    if max(image.size) > edge:
        image.thumbnail((edge, edge), Image.LANCZOS)

    out = io.BytesIO()
    image.save(out, "JPEG", quality=88, optimize=True)

    image.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.LANCZOS)
    thumb = io.BytesIO()
    image.save(thumb, "JPEG", quality=82, optimize=True)
    return out.getvalue(), thumb.getvalue()


async def collect(uploads: Sequence) -> List[Tuple[str, bytes, bytes]]:
    """
    Validate and normalise every attachment, in memory.

    Deliberately returns bytes rather than writing to disk: this runs *before*
    the credit is charged, so a rejected attachment costs the user nothing and
    leaves nothing behind.
    """
    files = [u for u in uploads if u is not None and getattr(u, "filename", "")]
    if not files:
        return []

    if len(files) > config.MAX_UPLOAD_IMAGES:
        raise UploadRejected(
            f"You attached {len(files)} photos. The limit is {config.MAX_UPLOAD_IMAGES}."
        )

    limit = int(config.MAX_UPLOAD_MB * 1024 * 1024)
    out: List[Tuple[str, bytes, bytes]] = []

    for i, upload in enumerate(files, start=1):
        label = upload.filename or f"Image {i}"
        raw = await _read_bounded(upload, limit)
        if not raw:
            raise UploadRejected(f"{label} is empty.")
        # The raw bytes go out of scope each iteration, so peak memory is one
        # original plus the normalised set, not every original at once. Decoding
        # is CPU work — half a second for a HEIC — so it runs off the event loop,
        # or fifty of them would stall every other request for half a minute.
        photo, thumb = await asyncio.to_thread(_normalise, raw, label)
        out.append((f"owner_photo_{i}.jpg", photo, thumb))
        log.info("Accepted attachment %s (%d KB -> %d KB)",
                 label, len(raw) // 1024, len(photo) // 1024)

    return out


def write(files: Sequence[Tuple[str, bytes, bytes]], dest: Path) -> List[str]:
    """Write collected attachments (and their thumbnails) into a scan directory.
    Returns the photos' names."""
    dest.mkdir(parents=True, exist_ok=True)
    for name, blob, thumb in files:
        (dest / name).write_bytes(blob)
        (dest / f"thumb_{name}").write_bytes(thumb)
    return [name for name, _, _ in files]
