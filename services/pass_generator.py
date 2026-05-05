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


def _make_stamp_strip(
    current_stamps: int,
    goal: int,
    primary_color: str,
    label_color: str,
    stamp_icon: str = "★",
    pending_rewards: int = 0,
    campaign_name: str | None = None,
    reward_text: str | None = None,
    instagram: str | None = None,
) -> bytes:
    """
    Generate strip.png for Apple Wallet storeCard.  750×300 @2x.

    The strip is a BACKGROUND image — iOS renders primaryFields text
    ("0 / 5 Puan") ON TOP of it.  We keep the strip clean with just
    branding elements.  Stamp progress is shown by the native iOS text.

    Layout:
      ┌──────────────────────────────────────┐
      │  🐻 (mascot)         [IG] @merchant  │  top-right
      │  (subtle,                             │
      │   bottom-right) (iOS renders text)    │  ← primaryFields overlay
      │                                       │
      │  [🎁 badge if pending]                │  reward badge
      └──────────────────────────────────────┘
      iOS renders secondary/auxiliary fields below the strip.
    """
    width, height = 750, 300
    bg = _hex_to_rgb(primary_color)
    fg = _hex_to_rgb(label_color)

    img = Image.new("RGBA", (width, height), color=(*bg, 255))
    draw = ImageDraw.Draw(img)

    # ── Mascot watermark (bottom-right, subtle) ───────────────────────────────
    mascot_src = _load_mascot()
    if mascot_src:
        # Scale mascot to ~85% of strip height, positioned bottom-right
        mascot_h = int(height * 0.85)
        aspect = mascot_src.width / mascot_src.height
        mascot_w = int(mascot_h * aspect)
        mascot_resized = mascot_src.resize((mascot_w, mascot_h), Image.LANCZOS)

        # Reduce opacity to ~30% so it's visible but doesn't overwhelm iOS text
        alpha = mascot_resized.split()[3]
        alpha = alpha.point(lambda p: int(p * 0.30))
        mascot_resized.putalpha(alpha)

        # Position: bottom-right corner, slightly inset
        mx = width - mascot_w + 20  # let it bleed off the right edge slightly
        my = height - mascot_h + 10  # let it bleed off the bottom slightly
        img.paste(mascot_resized, (mx, my), mascot_resized)

    # ── Fonts ─────────────────────────────────────────────────────────────────
    small_font = _get_bold_font(18)
    tiny_font = _get_bold_font(15)

    # ── Campaign name subtitle (top-left, below iOS header bar) ───────────────
    if campaign_name:
        campaign_font = _get_bold_font(20)
        c_x = 16
        c_y = 10
        _draw_text_with_emoji(
            img, draw, (c_x, c_y), campaign_name,
            fill=(*fg, 200), font=campaign_font, emoji_size=22,
        )

    # ── Merchant Instagram (top-right corner) ─────────────────────────────────
    if instagram:
        ig_icon_size = 20
        ig_text = f"@{instagram}"
        ig_bbox = draw.textbbox((0, 0), ig_text, font=small_font)
        ig_tw = ig_bbox[2] - ig_bbox[0]
        ig_th = ig_bbox[3] - ig_bbox[1]
        ig_right_margin = 16
        ig_top_margin = 14
        ig_total_w = ig_icon_size + 6 + ig_tw
        ig_x = width - ig_right_margin - ig_total_w
        ig_y = ig_top_margin
        _draw_ig_icon(draw, ig_x, ig_y, ig_icon_size, (*fg, 255))
        draw.text(
            (ig_x + ig_icon_size + 6 - ig_bbox[0], ig_y + (ig_icon_size - ig_th) // 2 - ig_bbox[1]),
            ig_text, fill=(*fg, 255), font=small_font,
        )

    # ── Pending reward badge (bottom-left) ────────────────────────────────────
    if pending_rewards > 0:
        badge_font = _get_bold_font(22)
        # Build badge label (no emoji in PIL text — we composite the image separately)
        badge_label = f"{pending_rewards} Ödülünüz Hazır!" if pending_rewards > 1 else "Ödülünüz Hazır!"
        emoji_size = 26  # px for the gift emoji image
        gap = 8          # gap between emoji and text

        b_bbox = draw.textbbox((0, 0), badge_label, font=badge_font)
        btext_w = b_bbox[2] - b_bbox[0]
        btext_h = b_bbox[3] - b_bbox[1]

        pad_x = 14
        bh = 40
        bw = pad_x + emoji_size + gap + btext_w + pad_x
        bx = 14
        by = height - bh - 44  # sit just above the footer divider

        # Draw amber pill background
        draw.rounded_rectangle(
            [bx, by, bx + bw, by + bh],
            radius=bh // 2,
            fill=(*_hex_to_rgb("#F59E0B"), 235),
        )

        # Composite gift emoji PNG (Twemoji) — avoids broken □ glyph on Linux
        emoji_img = _fetch_twemoji("🎁", emoji_size)
        text_x = bx + pad_x
        emoji_y = by + (bh - emoji_size) // 2
        if emoji_img:
            img.paste(emoji_img, (text_x, emoji_y), emoji_img)
        else:
            # Fallback: draw a text star if CDN unavailable
            draw.text((text_x, emoji_y), "*", fill=(255, 255, 255), font=badge_font)

        # Draw the badge label text next to the emoji
        text_x_label = text_x + emoji_size + gap
        text_y_label = by + (bh - btext_h) // 2 - b_bbox[1]
        draw.text((text_x_label, text_y_label), badge_label, fill=(255, 255, 255), font=badge_font)

    # ── Subtle "Powered by" watermark — sits inside strip, well above the
    #    iOS-rendered secondary/auxiliary fields that appear below the strip.
    branding_font = _get_bold_font(13)
    branding_text = f"Powered by {settings.app_name}  ·  {settings.app_domain}"
    br_bbox = draw.textbbox((0, 0), branding_text, font=branding_font)
    br_w = br_bbox[2] - br_bbox[0]
    br_x = (width - br_w) // 2 - br_bbox[0]
    br_y = height - 28  # inside strip, above the iOS field boundary
    draw.text((br_x, br_y), branding_text, fill=(*fg, 60), font=branding_font)

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


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


def build_pkpass_source(
    pass_id: str,
    customer_identifier: str,
    campaign_id: str,
    current_stamps: int,
    merchant_name: str,
    primary_color: str,
    label_color: str,
    campaign_goal: int,
    reward_text: str,
    campaign_name: str,
    stamp_label: str,
    reward_label: str,
    loyalty_card_label: str,
    instagram: str | None = None,
    auth_token: str | None = None,
    qr_message_override: str | None = None,
    pending_rewards: int = 0,
    total_rewards_earned: int = 0,
    stamp_icon: str = "★",
    location_name: str | None = None,
    scope_location_id: str | None = None,
    business_display_name: str | None = None,
    location_latitude: float | None = None,
    location_longitude: float | None = None,
    language: str = "tr",
) -> dict:
    """Build the raw pass.json and image files used by a .pkpass archive.
    
    The QR barcode encodes "{customer_identifier}|{campaign_id}" for shared passes
    and "{customer_identifier}|{campaign_id}|{location_id}" for per-location passes.
    
    organizationName = business_display_name or "{location_name} - {merchant_name}"
    """

    r, g, b = _hex_to_rgb(primary_color)
    lr, lg, lb = _hex_to_rgb(label_color)

    qr_message = qr_message_override or (
        f"{customer_identifier}|{campaign_id}|{scope_location_id}"
        if scope_location_id
        else f"{customer_identifier}|{campaign_id}"
    )

    # organizationName used in pass list (metadata) — show formatted business name if provided
    display_name = business_display_name or (f"{location_name} - {merchant_name}" if location_name else merchant_name)

    # ── Secondary fields: reward (what they earn — front and centre) ────────
    secondary_fields: list[dict] = [
        {
            "key": "reward",
            "label": reward_label,
            "value": reward_text,
        },
    ]

    # ── Auxiliary fields: clean stats (no emoji clutter) ────────────────────
    pending_display = (f"{pending_rewards} Hazır!" if pending_rewards > 1 else "Hazır!") if pending_rewards > 0 else "—"
    auxiliary_fields: list[dict] = [
        {
            "key": "earned",
            "label": "KAZANILAN",
            "value": str(total_rewards_earned) if total_rewards_earned > 0 else "—",
        },
        {
            "key": "redeemed",
            "label": "KULLANILAN",
            "value": str(total_rewards_earned - pending_rewards) if total_rewards_earned > 0 else "—",
        },
        *(
            [{
                "key": "pending",
                "label": "BEKLEYEN",
                "value": pending_display,
                "changeMessage": "Ödül kazandın! %@",
            }]
            if pending_rewards > 0 else []
        ),
    ]

    # ── Back fields: loyalbear.co + instagram (for curious people) ────
    back_fields: list[dict] = [
        {
            "key": "website",
            "label": "Sadakat Platformu",
            "value": settings.app_domain,
            "dataDetectorTypes": ["PKDataDetectorTypeLink"],
            "attributedValue": f"<a href='https://{settings.app_domain}'>{settings.app_domain}</a>",
        },
        {
            "key": "sw_instagram",
            "label": f"{settings.app_name} Instagram",
            "value": "@loyalbear.co",
            "dataDetectorTypes": ["PKDataDetectorTypeLink"],
            "attributedValue": "<a href='https://instagram.com/loyalbear.co'>@loyalbear.co</a>",
        },
    ]
    if instagram:
        back_fields.append({
            "key": "instagram",
            "label": "Instagram",
            "value": f"@{instagram}",
            "dataDetectorTypes": ["PKDataDetectorTypeLink"],
            "attributedValue": f"<a href='https://instagram.com/{instagram}'>@{instagram}</a>",
        })

    pass_json: dict = {
        "formatVersion": 1,
        "passTypeIdentifier": settings.apple_pass_type_id,
        "serialNumber": pass_id,
        "teamIdentifier": settings.apple_team_id,
        "organizationName": display_name,
        "logoText": business_display_name or merchant_name,
        "description": f"{display_name} {loyalty_card_label}",
        "backgroundColor": f"rgb({r},{g},{b})",
        "labelColor": f"rgb({lr},{lg},{lb})",
        "foregroundColor": f"rgb({lr},{lg},{lb})",
        "storeCard": {
            "headerFields": [],
            "primaryFields": [
                {
                    "key": "stamps",
                    "label": stamp_label,
                    "value": f"{current_stamps} / {campaign_goal}",
                    "changeMessage": "Yeni puan eklendi! %@",
                }
            ],
            "secondaryFields": secondary_fields,
            "auxiliaryFields": auxiliary_fields,
            "backFields": back_fields,
        },
        "barcode": {
            "message": qr_message,
            "format": "PKBarcodeFormatQR",
            "messageEncoding": "iso-8859-1",
        },
        "barcodes": [
            {
                "message": qr_message,
                "format": "PKBarcodeFormatQR",
                "messageEncoding": "iso-8859-1",
            }
        ],
    }

    # ── Location-based lock screen notification (Apple Wallet proximity) ──
    if location_latitude is not None and location_longitude is not None:
        stamps_left = max(0, campaign_goal - current_stamps)
        display = location_name or merchant_name
        if language == "tr":
            if pending_rewards > 0:
                relevant_text = f"🎁 {display} — ödülünüz hazır! Uğrayın ve {reward_text} kazanın!"
            elif stamps_left == 1:
                relevant_text = f"🎯 {display} — sadece 1 puan kaldı! Bir alışveriş daha yapın ve {reward_text} kazanın!"
            elif stamps_left <= 3 and current_stamps > 0:
                relevant_text = (
                    f"☕ {display} yakınındasınız! {stamps_left} puan daha ile "
                    f"{reward_text} kazanabilirsiniz — uğramaya ne dersiniz?"
                )
            elif current_stamps > 0:
                relevant_text = (
                    f"☕ {display} yakınındasınız — {current_stamps}/{campaign_goal} puan topladınız! "
                    f"Hedefinize yaklaşın, {reward_text} sizi bekliyor!"
                )
            else:
                relevant_text = (
                    f"☕ {display} yakınındasınız — ilk alışverişinizi yapın ve "
                    f"{reward_text} kazanmaya başlayın!"
                )
        else:
            if pending_rewards > 0:
                relevant_text = f"🎁 {display} — your reward is ready! Stop by and claim your {reward_text}!"
            elif stamps_left == 1:
                relevant_text = f"🎯 {display} — just 1 more visit to earn {reward_text}! Don't miss out!"
            elif stamps_left <= 3 and current_stamps > 0:
                relevant_text = (
                    f"☕ You're near {display}! Only {stamps_left} more stamps to earn "
                    f"{reward_text} — why not stop by?"
                )
            elif current_stamps > 0:
                relevant_text = (
                    f"☕ You're near {display} — {current_stamps}/{campaign_goal} stamps collected! "
                    f"Keep going, {reward_text} is waiting for you!"
                )
            else:
                relevant_text = (
                    f"☕ You're near {display} — visit now and start earning "
                    f"your way to {reward_text}!"
                )

        pass_json["locations"] = [
            {
                "latitude": location_latitude,
                "longitude": location_longitude,
                "relevantText": relevant_text,
            }
        ]
        pass_json["maxDistance"] = 500  # meters

    # Add PassKit Web Service info only when we have an HTTPS URL
    # Apple rejects passes with non-HTTPS or unreachable webServiceURL
    base = settings.api_base_url.rstrip('/')
    if auth_token and base.startswith("https://"):
        pass_json["webServiceURL"] = f"{base}/passkit/"
        pass_json["authenticationToken"] = auth_token

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
