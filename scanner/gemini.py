"""
The one place the pipeline talks to Gemini.

A 50-photo inspection makes sixty-odd model calls, several of them at once,
and at that volume the rare 429 or 503 stops being rare. Every call therefore
goes through `call()`, which retries transient failures with jittered backoff,
and structured calls go through `structured()`, which also retries the
occasional reply that is not valid JSON for its schema. A single flaky call
should cost a few seconds, not a paid scan.
"""

import io
import json
import logging
import random
import time
from typing import Any, List, Optional, Type, TypeVar

from google import genai
from google.genai import errors, types
from PIL import Image
from pydantic import BaseModel, ValidationError

from . import config

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Gemini would otherwise try to call our pydantic models as tools.
NO_AFC = types.AutomaticFunctionCallingConfig(disable=True)

# Statuses worth another attempt. Everything else (400, 403, 404) is a bug or a
# configuration problem that will fail identically on retry.
TRANSIENT_CODES = {408, 429, 500, 502, 503, 504}


class EmptyResponse(Exception):
    """The model returned no usable content (often a safety block)."""


def make_client(api_key: str, timeout_seconds: Optional[int] = None) -> genai.Client:
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=(timeout_seconds or config.GEMINI_TIMEOUT_SECONDS) * 1000),
    )


def _transient(exc: Exception) -> bool:
    if isinstance(exc, errors.APIError):
        return getattr(exc, "code", None) in TRANSIENT_CODES
    # Network-level trouble from the HTTP client: timeouts, resets, DNS blips.
    name = type(exc).__name__.lower()
    return any(k in name for k in ("timeout", "connect", "remoteprotocol", "readerror"))


def _sleep(attempt: int) -> None:
    # 2s, 5s, 11s ... with jitter, so six parallel workers that hit the same
    # 429 do not all come back in the same instant.
    time.sleep(min(30.0, (2 ** attempt) * 1.6 + random.uniform(0.2, 1.2)))


def jpeg_part(image: Image.Image, quality: int = 88) -> types.Part:
    """An image as a JPEG part.

    The SDK sends a PIL image as JPEG only if it is an untouched JPEG straight
    from disk; anything converted, copied or resized goes as PNG, five to ten
    times larger. Every image this pipeline sends has been through one of
    those, so left alone a fifty-photo request would run past Gemini's inline
    size limit and fail. Encoding here keeps every request a JPEG request.
    """
    buf = io.BytesIO()
    image.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True)
    return types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg")


def _encode(contents: List[Any]) -> List[Any]:
    return [jpeg_part(c) if isinstance(c, Image.Image) else c for c in contents]


def call(client: genai.Client, model: str, contents: List[Any],
         cfg: Optional[types.GenerateContentConfig] = None, label: str = ""):
    """generate_content with retries on transient failures."""
    attempts = config.GEMINI_ATTEMPTS
    contents = _encode(contents)
    for attempt in range(attempts):
        try:
            return client.models.generate_content(model=model, contents=contents, config=cfg)
        except Exception as exc:  # noqa: BLE001 - classified below
            if attempt == attempts - 1 or not _transient(exc):
                raise
            log.info("Gemini %s transient failure (%s), retrying", label or model, exc)
            _sleep(attempt)
    raise RuntimeError("unreachable")


def _strip_fences(text: str) -> str:
    """Models occasionally wrap JSON in a markdown fence despite the mime type."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def structured(client: genai.Client, schema: Type[T], contents: List[Any], *,
               model: Optional[str] = None, temperature: float = 0.2,
               media_resolution: Optional[types.MediaResolution] = None,
               system: Optional[str] = None, label: str = "") -> T:
    """
    One structured call: the schema doubles as the response contract and as
    prompt text (field descriptions are read by the model), and the reply is
    validated against it before anything downstream sees it.
    """
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=schema,
        temperature=temperature,
        automatic_function_calling=NO_AFC,
        media_resolution=media_resolution,
        system_instruction=system,
    )
    last: Optional[Exception] = None
    # Malformed replies get one extra try on top of the transport retries in
    # call(): a different sample usually parses.
    for attempt in range(2):
        try:
            resp = call(client, model or config.TEXT_MODEL, contents, cfg, label)
        except errors.ClientError as exc:
            # media_resolution is a cost knob, not a correctness one. A model
            # that does not accept it should cost us the saving, not the call.
            if cfg.media_resolution is None or getattr(exc, "code", None) != 400:
                raise
            log.warning("Gemini %s rejected media_resolution (%s); retrying without it",
                        label or schema.__name__, exc)
            cfg = cfg.model_copy(update={"media_resolution": None})
            resp = call(client, model or config.TEXT_MODEL, contents, cfg, label)
        text = getattr(resp, "text", None)
        if not text:
            last = EmptyResponse(f"{label or schema.__name__}: model returned no content")
        else:
            try:
                return schema.model_validate_json(_strip_fences(text))
            except (ValidationError, json.JSONDecodeError, ValueError) as exc:
                last = exc
        log.info("Gemini %s unusable reply (attempt %d): %s", label or schema.__name__,
                 attempt + 1, str(last)[:200])
    assert last is not None
    raise last


def edit_image(client: genai.Client, image: Image.Image, instruction: str,
               label: str = "render") -> Optional[Image.Image]:
    """
    Image-to-image with the image model. Returns None rather than raising when
    the model declines or returns text only: a missing render is a gap in a
    report, never a reason to fail it.
    """
    for attempt in range(2):
        try:
            resp = call(client, config.IMAGE_MODEL, [image, instruction], None, label)
            for cand in resp.candidates or []:
                for part in (cand.content.parts if cand.content else None) or []:
                    data = getattr(getattr(part, "inline_data", None), "data", None)
                    if data:
                        return Image.open(io.BytesIO(data)).convert("RGB")
            log.info("No image returned for %s (attempt %d)", label, attempt + 1)
        except Exception as exc:  # noqa: BLE001
            log.warning("Render %s failed: %s", label, exc)
            return None
    return None


def shrink(image: Image.Image, edge: int) -> Image.Image:
    """A downscaled copy for calls that need the gist, not the grain."""
    if max(image.size) <= edge:
        return image
    copy = image.copy()
    copy.thumbnail((edge, edge), Image.LANCZOS)
    return copy
