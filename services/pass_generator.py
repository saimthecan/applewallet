import base64
import hashlib
import json
import logging
import re
import uuid
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Optional

import requests as _requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization.pkcs12 import (
    load_key_and_certificates,
)
from cryptography.hazmat.primitives.serialization.pkcs7 import (
    PKCS7Options,
    PKCS7SignatureBuilder,
)
from cryptography.x509 import load_der_x509_certificate, load_pem_x509_certificate
from PIL import Image, ImageDraw, ImageFont

from app.core.config import settings


# ── Turkish-safe uppercase ────────────────────────────────────────────────────
# Python's str.upper() is not locale-aware: 'i'.upper() == 'I' (wrong in Turkish).
# In Turkish: 'i' → 'İ'  and  'ı' → 'I'
_TR_UPPER_TABLE = str.maketrans('iı', 'İI')

def tr_upper(text: str) -> str:
    """Uppercase a string using Turkish locale rules.

    Handles the two special cases the default upper() gets wrong:
      - dotted lowercase i  → dotted uppercase İ  (not I)
      - dotless lowercase ı → dotless uppercase I  (correct, but noted for clarity)
    All other characters are uppercased by the standard unicode rules.
    """
    return text.translate(_TR_UPPER_TABLE).upper()

# ── Twemoji emoji image fetching & disk cache ─────────────────────────────────
# Twemoji is the same emoji set used by browsers/the web preview.
# We fetch the emoji as a 72×72 PNG from the CDN, cache it to disk so it
# is only downloaded ONCE (survives process restarts), and keep a fast
# in-memory cache on top.

_logger = logging.getLogger(__name__)
_TWEMOJI_BASE = "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72"
_TWEMOJI_DISK_CACHE = Path("/tmp/twemoji_cache")
_EMOJI_IMAGE_CACHE: dict[str, Optional[Image.Image]] = {}

# Regex that matches one or more consecutive emoji codepoints (including
# variation selectors and ZWJ sequences) as a single group.
_EMOJI_RE = re.compile(
    "(["
    "\U0001F600-\U0001F64F"  # Emoticons
    "\U0001F300-\U0001F5FF"  # Misc Symbols & Pictographs
    "\U0001F680-\U0001F6FF"  # Transport & Map
    "\U0001F1E0-\U0001F1FF"  # Flags
    "\U00002702-\U000027B0"  # Dingbats
    "\U0001F900-\U0001F9FF"  # Supplemental Symbols
    "\U0001FA00-\U0001FA6F"  # Chess Symbols
    "\U0001FA70-\U0001FAFF"  # Symbols Extended-A
    "\U00002600-\U000026FF"  # Misc Symbols
    "\U0000FE0F"             # Variation selector
    "\U0000200D"             # Zero-width joiner
    "]+)",
    re.UNICODE,
)


def _emoji_to_codepoint(emoji: str) -> str:
    """Convert an emoji string to its Twemoji codepoint filename (e.g. '1f355')."""
    # Strip variation selector U+FE0F; keep zero-width joiners for ZWJ sequences
    codes = [f"{ord(c):x}" for c in emoji if ord(c) != 0xFE0F]
    return "-".join(codes)


def _fetch_twemoji(emoji: str, size: int) -> Optional[Image.Image]:
    """Return a Twemoji RGBA image for *emoji* scaled to *size*×*size* px.

    Returns None if the emoji is not found or the network is unavailable.
    Results are cached to disk (downloaded once) and kept in memory.
    """
    codepoint = _emoji_to_codepoint(emoji)
    cache_key = f"{codepoint}_{size}"
    if cache_key in _EMOJI_IMAGE_CACHE:
        return _EMOJI_IMAGE_CACHE[cache_key]

    # ── Disk cache: check for previously downloaded original (72px) ─────────
    _TWEMOJI_DISK_CACHE.mkdir(parents=True, exist_ok=True)
    disk_path = _TWEMOJI_DISK_CACHE / f"{codepoint}.png"

    if disk_path.exists():
        try:
            img = Image.open(disk_path).convert("RGBA")
            result = img.resize((size, size), Image.LANCZOS)
            _EMOJI_IMAGE_CACHE[cache_key] = result
            return result
        except Exception:
            pass  # corrupted file, re-download

    # ── Download from CDN ────────────────────────────────────────────────
    codepoint_simple = "-".join(
        f"{ord(c):x}" for c in emoji if ord(c) not in (0xFE0F, 0x200D)
    )
    urls = [
        f"{_TWEMOJI_BASE}/{codepoint}.png",
        f"{_TWEMOJI_BASE}/{codepoint_simple}.png",
    ]

    raw_bytes: Optional[bytes] = None
    for url in urls:
        try:
            resp = _requests.get(url, timeout=4)
            if resp.status_code == 200:
                raw_bytes = resp.content
                break
        except Exception:
            continue

    result: Optional[Image.Image] = None
    if raw_bytes:
        # Save original 72px PNG to disk for future reuse
        try:
            disk_path.write_bytes(raw_bytes)
        except Exception as exc:
            _logger.debug("Failed to cache emoji to disk: %s", exc)
        img = Image.open(BytesIO(raw_bytes)).convert("RGBA")
        result = img.resize((size, size), Image.LANCZOS)

    _EMOJI_IMAGE_CACHE[cache_key] = result
    return result


def _draw_text_with_emoji(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    fill: tuple,
    font,
    emoji_size: int = 22,
) -> int:
    """Draw *text* at *xy* with inline Twemoji images for any emoji characters.

    Non-emoji segments are rendered with PIL's draw.text(); emoji characters
    are composited as Twemoji PNGs.  Returns the total width drawn.
    """
    segments = _EMOJI_RE.split(text)
    x, y = xy
    cursor_x = x

    # Measure text height for vertical centering of emoji
    ref_bbox = draw.textbbox((0, 0), "Ag", font=font)
    text_h = ref_bbox[3] - ref_bbox[1]

    for segment in segments:
        if not segment:
            continue

        is_emoji = bool(_EMOJI_RE.fullmatch(segment))

        if is_emoji:
            emoji_img = _fetch_twemoji(segment, emoji_size)
            if emoji_img:
                ey = int(y + (text_h - emoji_size) / 2)
                img.paste(emoji_img, (int(cursor_x), ey), emoji_img)
                cursor_x += emoji_size + 2
            # If fetch failed, just skip (no broken box)
        else:
            draw.text((cursor_x, y), segment, fill=fill, font=font)
            seg_bbox = draw.textbbox((0, 0), segment, font=font)
            cursor_x += seg_bbox[2] - seg_bbox[0]

    return int(cursor_x - x)

def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore


def _make_solid_png(width: int, height: int, hex_color: str) -> bytes:
    r, g, b = _hex_to_rgb(hex_color)
    img = Image.new("RGB", (width, height), color=(r, g, b))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# Candidate system fonts that PIL can load as TrueType for icon rendering
_TRUETYPE_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]


def _get_bold_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Try to load a decent TrueType bold font at the given pixel size."""
    for path in _TRUETYPE_FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _make_icon_png(size: int, hex_color: str, initial: str) -> bytes:
    """Generate a square PNG icon with a large centered initial letter.

    iOS Wallet clips the icon into a circle, so we fill most of the
    square with a big, bold letter to ensure it is clearly visible.
    """
    r, g, b = _hex_to_rgb(hex_color)
    img = Image.new("RGB", (size, size), color=(r, g, b))
    draw = ImageDraw.Draw(img)
    text = tr_upper(initial)[:1]

    # Use ~70 % of the icon height as font size so the letter fills the circle
    font_size = max(int(size * 0.70), 10)
    font = _get_bold_font(font_size)

    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - tw) / 2 - bbox[0]
    y = (size - th) / 2 - bbox[1]
    draw.text((x, y), text, fill=(255, 255, 255), font=font)

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_logo_png(width: int, height: int, hex_color: str, label_color: str, text: str) -> bytes:
    """Generate a logo PNG with the merchant name rendered as text.

    Apple Wallet shows this in the top-left of the pass.  We render the
    merchant name in a color that contrasts against the pass background so it
    is always legible.
    """
    r, g, b = _hex_to_rgb(hex_color)
    lr, lg, lb = _hex_to_rgb(label_color)
    img = Image.new("RGBA", (width, height), color=(r, g, b, 0))  # transparent bg
    draw = ImageDraw.Draw(img)

    # Try progressively smaller font sizes until the text fits within the width
    max_font_size = int(height * 0.65)
    font = None
    tw, th = width + 1, height + 1
    for size in range(max_font_size, 8, -2):
        f = _get_bold_font(size)
        bbox = draw.textbbox((0, 0), text, font=f)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if tw <= width - 8:  # 4px padding each side
            font = f
            break

    if font is None:
        font = _get_bold_font(8)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

    bbox = draw.textbbox((0, 0), text, font=font)
    x = (width - tw) / 2 - bbox[0]
    y = (height - th) / 2 - bbox[1]
    draw.text((x, y), text, fill=(lr, lg, lb, 255), font=font)

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _draw_ig_icon(draw: ImageDraw.ImageDraw, x: int, y: int, size: int, color: tuple) -> None:
    """Draw a simplified Instagram icon (rounded rect + circle + dot) at (x, y)."""
    r = size // 4  # corner radius
    draw.rounded_rectangle([x, y, x + size, y + size], radius=r, outline=color, width=max(2, size // 12))
    # Inner circle (camera lens)
    cx, cy_icon = x + size // 2, y + size // 2
    lens_r = size // 4
    draw.ellipse(
        [cx - lens_r, cy_icon - lens_r, cx + lens_r, cy_icon + lens_r],
        outline=color, width=max(2, size // 12),
    )
    # Flash dot (top-right)
    dot_r = max(1, size // 10)
    dot_cx = x + size - size // 4
    dot_cy = y + size // 4
    draw.ellipse([dot_cx - dot_r, dot_cy - dot_r, dot_cx + dot_r, dot_cy + dot_r], fill=color)


_MASCOT_PATH = Path(__file__).parent / "assets" / "mascot-default.png"
_MASCOT_IMAGE_CACHE: Optional[Image.Image] = None


def _load_mascot() -> Optional[Image.Image]:
    """Load the app mascot image from disk (cached)."""
    global _MASCOT_IMAGE_CACHE
    if _MASCOT_IMAGE_CACHE is not None:
        return _MASCOT_IMAGE_CACHE
    if _MASCOT_PATH.exists():
        try:
            _MASCOT_IMAGE_CACHE = Image.open(_MASCOT_PATH).convert("RGBA")
            return _MASCOT_IMAGE_CACHE
        except Exception:
            pass
    return None


def build_pkpass_source(
    pass_id: str,
    serial: str,
    user_name: str = "Değerli Müşterimiz",
    current_stamps: int = 0,
    goal: int = 10,
    stamp_symbol: str = "☕",
    merchant_name: str = "Lounge Club",
    campaign_name: str = "5 Kahve Alana 1 Hediye",
    reward_text: str = "Filtre Kahve",
    primary_color: str = "#064E3B",
    label_color: str = "#FFFFFF",
    foreground_color: str = "#FFFFFF",
    instagram: str | None = "loyalbear.co",
    auth_token: str | None = None,
    language: str = "tr",
) -> BytesIO:
    """
    Generate the full .pkpass bundle in memory.
    """
    # 1. Create a temporary folder structure (in-memory zip)
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Assets (Icons/Logo/Strip)
        icon_data = _make_icon_png(58, primary_color, merchant_name[0])
        zf.writestr("icon.png", _make_icon_png(29, primary_color, merchant_name[0]))
        zf.writestr("icon@2x.png", icon_data)

        logo_data = _make_icon_png(58, primary_color, merchant_name[0])
        zf.writestr("logo.png", _make_icon_png(29, primary_color, merchant_name[0]))
        zf.writestr("logo@2x.png", logo_data)

        # Dynamic Strip (The Stamp Grid)
        strip_data = _make_stamp_strip(
            current_stamps=current_stamps,
            goal=goal,
            primary_color=primary_color,
            label_color=label_color,
            stamp_symbol=stamp_symbol,
            campaign_name=campaign_name,
            reward_text=reward_text,
            instagram=instagram,
        )
        zf.writestr("strip.png", strip_data)
        zf.writestr("strip@2x.png", strip_data)

        # pass.json
        pass_json = {
            "formatVersion": 1,
            "passTypeIdentifier": settings.apple_pass_type_id,
            "serialNumber": serial,
            "teamIdentifier": settings.apple_team_id,
            "organizationName": merchant_name,
            "description": campaign_name,
            "logoText": merchant_name,
            "backgroundColor": primary_color,
            "foregroundColor": foreground_color,
            "labelColor": label_color,
            "storeCard": {
                "primaryFields": [
                    {
                        "key": "balance",
                        "label": "PUAN",
                        "value": f"{current_stamps} / {goal}",
                    }
                ],
                "secondaryFields": [
                    {
                        "key": "customer",
                        "label": "AD SOYAD",
                        "value": user_name,
                    }
                ],
                "auxiliaryFields": [
                    {
                        "key": "reward",
                        "label": "HEDİYE",
                        "value": reward_text,
                    },
                    {
                        "key": "stamps_left",
                        "label": "KALAN",
                        "value": f"{max(0, goal - current_stamps)}",
                    }
                ],
                "backFields": [
                    {
                        "key": "info",
                        "label": "KAMPANYA DETAYI",
                        "value": f"Bu dijital sadakat kartı ile {goal} adet {reward_text} alımınızda 1 adet hediye kazanırsınız. QR kodu her alışverişinizde okutmayı unutmayın!",
                    }
                ],
            },
            "barcode": {
                "message": f"{settings.api_base_url}/redeem/{pass_id}",
                "format": "PKBarcodeFormatQR",
                "messageEncoding": "iso-8859-1",
            },
        }
        zf.writestr("pass.json", json.dumps(pass_json, ensure_ascii=False).encode())

    return buf


def _load_signing_assets() -> tuple:
    """Load the Pass cert + key + WWDR cert from env (base64 encoded)."""
    p12_bytes = base64.b64decode(settings.apple_pass_cert_p12_b64)
    password = settings.apple_pass_cert_password.encode()
    key, cert, _ = load_key_and_certificates(p12_bytes, password)

    wwdr_raw = base64.b64decode(settings.apple_wwdr_cert_b64)
    try:
        wwdr_cert = load_pem_x509_certificate(wwdr_raw)
    except Exception:
        # Fallback: cert stored as DER (binary .cer) instead of PEM
        wwdr_cert = load_der_x509_certificate(wwdr_raw)

    return key, cert, wwdr_cert


def _sign_manifest(manifest_bytes: bytes) -> bytes:
    key, cert, wwdr_cert = _load_signing_assets()
    signature = (
        PKCS7SignatureBuilder()
        .set_data(manifest_bytes)
        .add_signer(
            cert, key, hashes.SHA256()
        )  # SHA256 — accepted by Apple, required by cryptography>=42
        .add_certificate(wwdr_cert)
        .sign(serialization.Encoding.DER, [PKCS7Options.DetachedSignature])
    )
    return signature


    pass_json_bytes = json.dumps(pass_json, ensure_ascii=False).encode()
    # Icon: branch initial for the Wallet pass list
    initial = (location_name or merchant_name)[:1] if (location_name or merchant_name) else "S"
    icon_1x = _make_icon_png(29, primary_color, initial)
    icon_2x = _make_icon_png(58, primary_color, initial)
    # Logo: small square with the initial — rendered next to logoText by iOS
    logo_1x = _make_icon_png(29, primary_color, initial)
    logo_2x = _make_icon_png(58, primary_color, initial)
    strip_2x = _make_stamp_strip(
        current_stamps, campaign_goal, primary_color, label_color,
        stamp_icon, pending_rewards,
        campaign_name=campaign_name,
        reward_text=reward_text,
        instagram=instagram,
    )
    # @1x is half resolution
    strip_1x_img = Image.open(BytesIO(strip_2x)).resize((375, 150))
    strip_1x_buf = BytesIO()
    strip_1x_img.save(strip_1x_buf, format="PNG")
    strip_1x = strip_1x_buf.getvalue()

    files: dict[str, bytes] = {
        "pass.json": pass_json_bytes,
        "icon.png": icon_1x,
        "icon@2x.png": icon_2x,
        "logo.png": logo_1x,
        "logo@2x.png": logo_2x,
        "strip.png": strip_1x,
        "strip@2x.png": strip_2x,
    }

    return {
        "pass_json": pass_json,
        "files": files,
    }


def package_pkpass_files(files: dict[str, bytes]) -> bytes:
    manifest = {
        name: hashlib.sha1(data).hexdigest() for name, data in files.items()
    }  # noqa: S324
    manifest_bytes = json.dumps(manifest).encode()

    signature = _sign_manifest(manifest_bytes)

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        zf.writestr("manifest.json", manifest_bytes)
        zf.writestr("signature", signature)

    return buf.getvalue()


def build_pkpass(*args, **kwargs) -> bytes:
    """Build and sign a .pkpass file. Returns raw bytes of the ZIP archive."""
    source = build_pkpass_source(*args, **kwargs)
    return package_pkpass_files(source["files"])
