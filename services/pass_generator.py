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

from core.config import settings


# ── Turkish-safe uppercase ────────────────────────────────────────────────────
_TR_UPPER_TABLE = str.maketrans('iı', 'İI')

def tr_upper(text: str) -> str:
    return text.translate(_TR_UPPER_TABLE).upper()

# ── Twemoji emoji image fetching & disk cache ─────────────────────────────────
_logger = logging.getLogger(__name__)
_TWEMOJI_BASE = "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72"
_EMOJI_IMAGE_CACHE: dict = {}

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
    codes = [f"{ord(c):x}" for c in emoji if ord(c) != 0xFE0F]
    return "-".join(codes)

def _fetch_twemoji(emoji: str, size: int) -> Optional[Image.Image]:
    codepoint = _emoji_to_codepoint(emoji)
    cache_key = f"{codepoint}_{size}"
    if cache_key in _EMOJI_IMAGE_CACHE:
        return _EMOJI_IMAGE_CACHE[cache_key]

    codepoint_simple = "-".join(f"{ord(c):x}" for c in emoji if ord(c) not in (0xFE0F, 0x200D))
    urls = [f"{_TWEMOJI_BASE}/{codepoint}.png", f"{_TWEMOJI_BASE}/{codepoint_simple}.png"]

    raw_bytes = None
    for url in urls:
        try:
            resp = _requests.get(url, timeout=4)
            if resp.status_code == 200:
                raw_bytes = resp.content
                break
        except Exception:
            continue

    result = None
    if raw_bytes:
        img = Image.open(BytesIO(raw_bytes)).convert("RGBA")
        result = img.resize((size, size), Image.LANCZOS)

    _EMOJI_IMAGE_CACHE[cache_key] = result
    return result

def _hex_to_rgb(hex_color: str) -> tuple:
    h = hex_color.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))

_TRUETYPE_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]

def _get_bold_font(size: int) -> ImageFont.FreeTypeFont:
    for path in _TRUETYPE_FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()

def _make_icon_png(size: int, hex_color: str, initial: str) -> bytes:
    r, g, b = _hex_to_rgb(hex_color)
    img = Image.new("RGB", (size, size), color=(r, g, b))
    draw = ImageDraw.Draw(img)
    text = tr_upper(initial)[:1]
    font_size = max(int(size * 0.70), 10)
    font = _get_bold_font(font_size)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]), text, fill=(255, 255, 255), font=font)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

_MASCOT_PATH = Path(__file__).parent / "assets" / "mascot-default.png"
_STRIP_BG_PATH = Path(__file__).parent / "assets" / "strip_bg.png"
_MASCOT_IMAGE_CACHE: Optional[Image.Image] = None

def _load_strip_bg() -> Optional[Image.Image]:
    if _STRIP_BG_PATH.exists():
        try:
            return Image.open(_STRIP_BG_PATH).convert("RGBA")
        except Exception:
            pass
    return None

def _make_stamp_strip(
    current_stamps: int,
    goal: int,
    primary_color: str,
    label_color: str,
    stamp_symbol: str = "☕",
    campaign_name: Optional[str] = None,
    reward_text: Optional[str] = None,
    instagram: Optional[str] = None,
    logo_icon: Optional[bytes] = None,
) -> bytes:
    width, height = 750, 300
    
    # Load custom background or fallback to solid color
    bg_img = _load_strip_bg()
    if bg_img:
        img = bg_img.resize((width, height), Image.LANCZOS)
        fg_rgb = (40, 30, 20) # Deep coffee brown
    else:
        bg_rgb = _hex_to_rgb(primary_color)
        img = Image.new("RGBA", (width, height), color=(*bg_rgb, 255))
        fg_rgb = _hex_to_rgb(label_color)

    draw = ImageDraw.Draw(img)

    # Grid logic
    cols = goal if goal <= 6 else (goal + 1) // 2
    rows = 1 if goal <= 6 else 2
    icon_size = 72 if rows == 1 else 60
    padding_x = 40
    spacing_x = (width - padding_x * 2) // max(1, cols - 1) if cols > 1 else 0
    spacing_y = 80
    start_y = 110 if rows == 1 else 95

    filled_icon = _fetch_twemoji(stamp_symbol, icon_size)
    empty_icon = None
    if filled_icon:
        empty_icon = filled_icon.copy()
        alpha = empty_icon.split()[3].point(lambda p: int(p * 0.20))
        empty_icon.putalpha(alpha)

    for i in range(goal):
        r, c = i // cols, i % cols
        row_count = cols if (r < rows - 1) else (goal - r * cols)
        row_w = (row_count - 1) * spacing_x + icon_size
        x = (width - row_w) // 2 + c * spacing_x
        y = start_y + r * spacing_y

        circle_r = icon_size // 2 + 8
        draw.ellipse([x+icon_size//2-circle_r, y+icon_size//2-circle_r, x+icon_size//2+circle_r, y+icon_size//2+circle_r], fill=(*fg_rgb, 40))

        is_filled = i < current_stamps
        stamp_img = filled_icon if is_filled else empty_icon
        if stamp_img:
            img.paste(stamp_img, (int(x), int(y)), stamp_img)

    if campaign_name:
        campaign_font = _get_bold_font(24)
        c_bbox = draw.textbbox((0, 0), tr_upper(campaign_name), font=campaign_font)
        c_w = c_bbox[2] - c_bbox[0]
        draw.text(((width - c_w) // 2, 15), tr_upper(campaign_name), fill=(*fg_rgb, 200), font=campaign_font)

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def sign_pkpass(pkpass_source: BytesIO) -> bytes:
    p12_bytes = base64.b64decode(settings.apple_pass_cert_p12_b64)
    key, cert, _ = load_key_and_certificates(p12_bytes, settings.apple_pass_cert_password.encode())
    wwdr_raw = base64.b64decode(settings.apple_wwdr_cert_b64)
    try:
        wwdr_cert = load_pem_x509_certificate(wwdr_raw)
    except Exception:
        wwdr_cert = load_der_x509_certificate(wwdr_raw)

    with zipfile.ZipFile(pkpass_source, "a") as zf:
        manifest = {name: hashlib.sha1(zf.read(name)).hexdigest() for name in zf.namelist()}
        manifest_bytes = json.dumps(manifest).encode()
        zf.writestr("manifest.json", manifest_bytes)
        signature = PKCS7SignatureBuilder().set_data(manifest_bytes).add_signer(cert, key, hashes.SHA256()).add_certificate(wwdr_cert).sign(serialization.Encoding.DER, [PKCS7Options.DetachedSignature])
        zf.writestr("signature", signature)

    return pkpass_source.getvalue()

def build_pkpass(
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
    instagram: Optional[str] = "loyalbear.co",
    auth_token: Optional[str] = None,
    language: str = "tr",
) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        icon = _make_icon_png(60, primary_color, merchant_name[0])
        zf.writestr("icon.png", icon); zf.writestr("icon@2x.png", icon)
        zf.writestr("logo.png", icon); zf.writestr("logo@2x.png", icon)
        
        # Include campaign_name in the strip generation
        strip = _make_stamp_strip(current_stamps, goal, primary_color, label_color, stamp_symbol, campaign_name)
        zf.writestr("strip.png", strip); zf.writestr("strip@2x.png", strip)
        
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
                "headerFields": [], # Removed bear from here
                "primaryFields": [], 
                "secondaryFields": [
                    {"key": "puan", "label": "PUAN", "value": f"{current_stamps} / {goal}"}
                ],
                "auxiliaryFields": [
                    {"key": "reward", "label": "HEDİYE", "value": reward_text}
                ],
                "backFields": [
                    {"key": "info", "label": "BİLGİ", "value": f"{goal} damgada 1 {reward_text} hediye!"}
                ]
            },
            "barcode": {"message": f"{settings.api_base_url}/redeem/{pass_id}", "format": "PKBarcodeFormatQR", "messageEncoding": "iso-8859-1"}
        }
        zf.writestr("pass.json", json.dumps(pass_json, ensure_ascii=False).encode())
    
    return sign_pkpass(buf)
