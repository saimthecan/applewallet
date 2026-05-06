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
from PIL import Image, ImageDraw, ImageFilter, ImageFont

try:
    from core.config import settings
except ModuleNotFoundError:
    from ..core.config import settings


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

_STAMP_LAYOUTS = {
    2: {"rows": [2], "icon": 88, "gap_x": 170, "gap_y": 0},
    3: {"rows": [3], "icon": 86, "gap_x": 150, "gap_y": 0},
    4: {"rows": [4], "icon": 80, "gap_x": 130, "gap_y": 0},
    5: {"rows": [5], "icon": 74, "gap_x": 114, "gap_y": 0},
    6: {"rows": [3, 3], "icon": 78, "gap_x": 148, "gap_y": 100},
    7: {"rows": [4, 3], "icon": 74, "gap_x": 126, "gap_y": 98},
    8: {"rows": [4, 4], "icon": 72, "gap_x": 128, "gap_y": 96},
    9: {"rows": [5, 4], "icon": 68, "gap_x": 114, "gap_y": 94},
    10: {"rows": [5, 5], "icon": 66, "gap_x": 112, "gap_y": 92},
}

def _clamp_stamp_goal(goal: int) -> int:
    return max(2, min(10, int(goal)))

def _blend_rgb(color_a: tuple, color_b: tuple, amount: float) -> tuple:
    amount = max(0.0, min(1.0, amount))
    return tuple(int(a * (1 - amount) + b * amount) for a, b in zip(color_a, color_b))

def _draw_blurred_ellipse(img: Image.Image, bbox: list, fill: tuple, blur: int) -> None:
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    layer_draw = ImageDraw.Draw(layer)
    layer_draw.ellipse(bbox, fill=fill)
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(blur)))

def _draw_premium_stamp_slot(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    icon_size: int,
    primary_rgb: tuple,
    filled: bool,
) -> tuple:
    center_x = x + icon_size // 2
    center_y = y + icon_size // 2
    radius = icon_size // 2 + 9
    bbox = [center_x - radius, center_y - radius, center_x + radius, center_y + radius]
    shadow_box = [bbox[0] + 2, bbox[1] + 5, bbox[2] + 2, bbox[3] + 5]

    if filled:
        base = (*primary_rgb, 232)
        rim = (*_blend_rgb(primary_rgb, (255, 255, 255), 0.24), 235)
        inner = (*_blend_rgb(primary_rgb, (30, 20, 55), 0.20), 240)
        shine = (255, 255, 255, 54)
        _draw_blurred_ellipse(img, shadow_box, (34, 20, 58, 88), 8)
        _draw_blurred_ellipse(img, [bbox[0] - 5, bbox[1] - 5, bbox[2] + 5, bbox[3] + 5], (*primary_rgb, 42), 10)
        draw.ellipse(bbox, fill=base, outline=rim, width=3)
        inset = 7
        draw.ellipse([bbox[0] + inset, bbox[1] + inset, bbox[2] - inset, bbox[3] - inset], outline=inner, width=2)
        draw.arc([bbox[0] + 12, bbox[1] + 10, bbox[2] - 12, bbox[3] - 12], 205, 335, fill=shine, width=3)
        icon_alpha = 255
    else:
        base = (*primary_rgb, 118)
        rim = (*_blend_rgb(primary_rgb, (255, 255, 255), 0.34), 96)
        inner = (*primary_rgb, 62)
        _draw_blurred_ellipse(img, shadow_box, (34, 20, 58, 42), 7)
        draw.ellipse(bbox, fill=base, outline=rim, width=2)
        inset = 8
        draw.ellipse([bbox[0] + inset, bbox[1] + inset, bbox[2] - inset, bbox[3] - inset], outline=inner, width=2)
        icon_alpha = 22

    return (x, y, icon_alpha)

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
    is_reward_ready: bool = False,
) -> bytes:
    width, height = 750, 300
    goal = _clamp_stamp_goal(goal)
    current_stamps = max(0, min(goal, int(current_stamps)))
    
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

    layout = _STAMP_LAYOUTS[goal]
    row_counts = layout["rows"]
    icon_size = layout["icon"]
    spacing_x = layout["gap_x"]
    spacing_y = layout["gap_y"]
    primary_rgb = _hex_to_rgb(primary_color)

    grid_h = (len(row_counts) - 1) * spacing_y + icon_size
    stamp_area_top = 62 if campaign_name else 34
    stamp_area_bottom = height - (65 if is_reward_ready else 22)
    start_y = stamp_area_top + max(0, (stamp_area_bottom - stamp_area_top - grid_h) // 2)

    filled_icon = _fetch_twemoji(stamp_symbol, icon_size)
    stamp_index = 0
    for row_index, row_count in enumerate(row_counts):
        row_w = (row_count - 1) * spacing_x + icon_size
        y = start_y + row_index * spacing_y

        for col_index in range(row_count):
            x = (width - row_w) // 2 + col_index * spacing_x
            is_filled = stamp_index < current_stamps
            _, _, icon_alpha = _draw_premium_stamp_slot(img, draw, int(x), int(y), icon_size, primary_rgb, is_filled)

            if filled_icon:
                stamp_img = filled_icon.copy()
                if icon_alpha < 255:
                    alpha = stamp_img.split()[3].point(lambda p: int(p * icon_alpha / 255))
                    stamp_img.putalpha(alpha)
                icon_padding = 4 if is_filled else 9
                if icon_padding:
                    stamp_img = stamp_img.resize((icon_size - icon_padding * 2, icon_size - icon_padding * 2), Image.LANCZOS)
                paste_x = int(x) + icon_padding
                paste_y = int(y) + icon_padding
                img.paste(stamp_img, (paste_x, paste_y), stamp_img)
            stamp_index += 1

    if campaign_name:
        campaign_font = _get_bold_font(24)
        c_bbox = draw.textbbox((0, 0), tr_upper(campaign_name), font=campaign_font)
        c_w = c_bbox[2] - c_bbox[0]
        draw.text(((width - c_w) // 2, 15), tr_upper(campaign_name), fill=(*fg_rgb, 200), font=campaign_font)

    # Ödül Hazır Rozeti (Eğer kazanıldıysa)
    if is_reward_ready:
        badge_text = "ÖDÜLÜNÜZ HAZIR!"
        badge_font = _get_bold_font(18)
        b_bbox = draw.textbbox((0, 0), badge_text, font=badge_font)
        b_w = b_bbox[2] - b_bbox[0]
        
        icon_size = 18
        pill_w = b_w + icon_size + 45
        pill_h = 32
        pill_x = width - pill_w - 25
        pill_y = height - pill_h - 10
        
        # Sarı kapsül
        draw.rounded_rectangle([pill_x, pill_y, pill_x + pill_w, pill_y + pill_h], radius=16, fill="#FBBF24")
        
        # EL YAPIMI HEDİYE PAKETİ ÇİZİMİ (Karakter değil, çizim!)
        ix = pill_x + 15
        iy = pill_y + 7
        # Kutu gövdesi
        draw.rectangle([ix, iy+4, ix+icon_size-4, iy+icon_size], fill="#000", outline="#000")
        # Kurdele (Dikey ve Yatay çizgiler)
        draw.line([ix + (icon_size-4)//2, iy+4, ix + (icon_size-4)//2, iy+icon_size], fill="#FBBF24", width=2)
        draw.line([ix, iy + (icon_size+4)//2, ix+icon_size-4, iy + (icon_size+4)//2], fill="#FBBF24", width=2)
        # Fiyonk (Üst kısım)
        draw.ellipse([ix+2, iy, ix+(icon_size-4)//2, iy+5], outline="#000", width=2)
        draw.ellipse([ix+(icon_size-4)//2, iy, ix+icon_size-6, iy+5], outline="#000", width=2)
        
        # Siyah metin
        draw.text((pill_x + 20 + icon_size, pill_y + 5), badge_text, font=badge_font, fill="#000000")

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def sign_pkpass(pkpass_source: BytesIO) -> bytes:
    p12_bytes = base64.b64decode(settings.apple_pass_cert_p12_b64.strip())
    key, cert, _ = load_key_and_certificates(p12_bytes, settings.apple_pass_cert_password.strip().encode())
    wwdr_raw = base64.b64decode(settings.apple_wwdr_cert_b64.strip())
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
    logo_image: Optional[bytes] = None,
    used_rewards: int = 1,
    total_rewards: int = 3,
) -> bytes:
    goal = _clamp_stamp_goal(goal)
    current_stamps = max(0, min(goal, int(current_stamps)))

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if logo_image:
            # Use provided merchant logo
            zf.writestr("icon.png", logo_image); zf.writestr("icon@2x.png", logo_image)
            zf.writestr("logo.png", logo_image); zf.writestr("logo@2x.png", logo_image)
            icon_for_strip = logo_image # If we want to use it in strip as well
        else:
            # Fallback to generated initial letter
            icon = _make_icon_png(60, primary_color, merchant_name[0])
            zf.writestr("icon.png", icon); zf.writestr("icon@2x.png", icon)
            zf.writestr("logo.png", icon); zf.writestr("logo@2x.png", icon)
        
        # Include campaign_name and reward status in the strip generation
        is_reward_ready = current_stamps >= goal
        strip = _make_stamp_strip(current_stamps, goal, primary_color, label_color, stamp_symbol, campaign_name, is_reward_ready=is_reward_ready)
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
                "headerFields": [
                    {"key": "puan", "label": "PUAN", "value": f"{current_stamps} / {goal}"}
                ],
                "primaryFields": [], 
                "secondaryFields": [
                    {"key": "reward", "label": "ÖDÜL", "value": reward_text},
                    {"key": "used_count", "label": "KULLANILAN ÖDÜL", "value": f"{used_rewards} / {total_rewards}"}
                ],
                "auxiliaryFields": [],
                "backFields": [
                    {"key": "info", "label": "BİLGİ", "value": f"{goal} damgada 1 {reward_text} hediye!"}
                ]
            },
            "barcode": {"message": f"{settings.api_base_url}/redeem/{pass_id}", "format": "PKBarcodeFormatQR", "messageEncoding": "iso-8859-1"}
        }
        zf.writestr("pass.json", json.dumps(pass_json, ensure_ascii=False).encode())
    
    return sign_pkpass(buf)


def get_pass_data_for_preview(
    current_stamps: int,
    goal: int,
    merchant_name: str = "Bear Coffee",
    campaign_name: str = "8 KAHVE ALANA 1 ADET BİZDEN",
    reward_text: str = "Filtre Kahve",
    primary_color: str = "#7C3AED",
    label_color: str = "#FFFFFF",
    foreground_color: str = "#FFFFFF",
    stamp_symbol: str = "☕",
    instagram: str = None,
    used_rewards: int = 1,
    total_rewards: int = 3,
) -> dict:
    """Web önizlemesi için gerekli görselleri ve metinleri hazırlar."""
    is_reward_ready = current_stamps >= goal
    icon_bytes = _make_icon_png(60, primary_color, merchant_name[0])
    strip_bytes = _make_stamp_strip(
        current_stamps, goal, primary_color, label_color, stamp_symbol, campaign_name, is_reward_ready=is_reward_ready, instagram=instagram
    )

    return {
        "merchant_name": merchant_name,
        "campaign_name": campaign_name,
        "reward_text": reward_text,
        "current_stamps": current_stamps,
        "goal": goal,
        "primary_color": primary_color,
        "label_color": label_color,
        "foreground_color": foreground_color,
        "logo_base64": base64.b64encode(icon_bytes).decode(),
        "strip_base64": base64.b64encode(strip_bytes).decode(),
        "is_reward_ready": is_reward_ready,
        "instagram": instagram,
        "used_rewards": used_rewards,
        "total_rewards": total_rewards
    }
