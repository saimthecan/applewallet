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
    """Damga yuvalarını 'Buzlu Cam' (Glass-Marble) stilinde çizer."""
    center_x = x + icon_size // 2
    center_y = y + icon_size // 2
    radius = icon_size // 2 + 8
    bbox = [center_x - radius, center_y - radius, center_x + radius, center_y + radius]
    
    # Şeffaf katman oluştur (Cam efekti için)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    l_draw = ImageDraw.Draw(layer)

    if filled:
        # DOLU DAMGA: Kehribar Parıltılı Cam
        _draw_blurred_ellipse(img, [bbox[0]-4, bbox[1]-4, bbox[2]+4, bbox[3]+4], (251, 191, 36, 40), 10)
        l_draw.ellipse(bbox, fill=(255, 255, 255, 45), outline=(255, 255, 255, 120), width=1)
        inner_r = radius - 10
        l_draw.ellipse([center_x - inner_r, center_y - inner_r, center_x + inner_r, center_y + inner_r], fill=(251, 191, 36, 80))
        icon_alpha = 255
    else:
        # BOŞ DAMGA: Belirgin Füme Cam
        l_draw.ellipse(bbox, fill=(0, 0, 0, 40), outline=(0, 0, 0, 80), width=1)
        inner_r = radius - 2
        l_draw.ellipse([center_x - inner_r, center_y - inner_r, center_x + inner_r, center_y + inner_r], outline=(255, 255, 255, 30), width=1)
        icon_alpha = 70

    # Cam katmanını ana resme ekle
    img.alpha_composite(layer)
    
    return (x, y, icon_alpha)

def _get_emoji_font(size: int):
    """Sistemde mevcut olan en iyi emoji fontunu bulmaya çalışır."""
    paths = [
        "C:/Windows/Fonts/seguiemj.ttf",  # Windows Emoji
        "seguiemj.ttf",
        "Apple Color Emoji.ttc",         # macOS
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf", # Linux
    ]
    for path in paths:
        try:
            if Path(path).exists() or path == "seguiemj.ttf":
                return ImageFont.truetype(path, size)
        except:
            continue
    return _get_bold_font(size)

def _make_stamp_icon(size: int, label_color: str, symbol: str, primary_color: str, is_filled: bool):
    """Şık bir damga ikonu oluşturur (Emoji desteğiyle)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    # Daire Arka Planı
    if is_filled:
        draw.ellipse([2, 2, size-2, size-2], fill=primary_color, outline=label_color, width=2)
    else:
        draw.ellipse([2, 2, size-2, size-2], outline=label_color, width=2)
    
    # Emoji Sembolü
    s_font = _get_emoji_font(int(size * 0.55))
    s_bbox = draw.textbbox((0, 0), symbol, font=s_font)
    s_w = s_bbox[2] - s_bbox[0]
    s_h = s_bbox[3] - s_bbox[1]
    
    # Emojiyi merkeze çiz
    draw.text(((size - s_w) // 2, (size - s_h) // 2 - 2), symbol, font=s_font, fill=label_color, embedded_color=True)
    
    return img

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
    """Orijinal Temiz ve Premium Damga Şeridi (Ortalı Düzen)."""
    width, height = 750, 300
    goal = _clamp_stamp_goal(goal)
    current_stamps = max(0, min(goal, int(current_stamps)))
    
    bg_img = _load_strip_bg()
    if bg_img:
        img = bg_img.resize((width, height), Image.LANCZOS)
    else:
        bg_rgb = _hex_to_rgb(primary_color)
        img = Image.new("RGBA", (width, height), color=(*bg_rgb, 255))

    draw = ImageDraw.Draw(img)
    primary_rgb = _hex_to_rgb(primary_color)
    fg_rgb = _hex_to_rgb(label_color)

    # 1. KAMPANYA METNİ (Üst Orta)
    if campaign_name:
        c_font = _get_bold_font(24)
        c_text = tr_upper(campaign_name)
        c_bbox = draw.textbbox((0, 0), c_text, font=c_font)
        c_w = c_bbox[2] - c_bbox[0]
        # Siyah metin
        draw.text(((width - c_w) // 2, 15), c_text, fill=(0, 0, 0), font=c_font)

    # 2. DAMGALAR (Merkezi Premium Düzen)
    layout = _STAMP_LAYOUTS[goal]
    row_counts = layout["rows"]
    icon_size = layout["icon"]
    spacing_x = layout["gap_x"]
    spacing_y = layout["gap_y"]

    grid_h = (len(row_counts) - 1) * spacing_y + icon_size
    grid_w = (max(row_counts) - 1) * spacing_x + icon_size
    
    # Y ekseni hesaplama
    stamp_area_top = 62 if campaign_name else 34
    stamp_area_bottom = height - 60
    start_y = stamp_area_top + max(0, (stamp_area_bottom - stamp_area_top - grid_h) // 2)

    filled_icon = _fetch_twemoji(stamp_symbol, icon_size)
    stamp_index = 0
    for row_index, row_count in enumerate(row_counts):
        row_w = (row_count - 1) * spacing_x + icon_size
        y = start_y + row_index * spacing_y
        # TAM MERKEZE HİZALI (Orijinal)
        row_x = (width - row_w) // 2

        for col_index in range(row_count):
            x = row_x + col_index * spacing_x
            is_filled = stamp_index < current_stamps
            
            # Premium Slot Çizimi
            _, _, icon_alpha = _draw_premium_stamp_slot(img, draw, int(x), int(y), icon_size, primary_rgb, is_filled)

            if filled_icon and is_filled:
                stamp_img = filled_icon.copy()
                if icon_alpha < 255:
                    alpha = stamp_img.split()[3].point(lambda p: int(p * icon_alpha / 255))
                    stamp_img.putalpha(alpha)
                pad = 9
                stamp_img = stamp_img.resize((icon_size - pad*2, icon_size - pad*2), Image.LANCZOS)
                img.paste(stamp_img, (int(x) + pad, int(y) + pad), stamp_img)
            
            stamp_index += 1

    # 3. ALT ROZETLER (Glassmorphism / Buzlu Cam Etkisi)
    # Şeffaf katman oluştur
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ol_draw = ImageDraw.Draw(overlay)

    # Instagram
    if instagram:
        ig_text = f"@{instagram}" if not instagram.startswith("@") else instagram
        ig_font = _get_bold_font(18)
        ig_bbox = ol_draw.textbbox((0, 0), ig_text, font=ig_font)
        pill_w = (ig_bbox[2]-ig_bbox[0]) + 30
        pill_x, pill_y = 20, height - 46
        
        # Buzlu Cam Arka Planı (Yarı Şeffaf Beyaz + İnce Kontür)
        ol_draw.rounded_rectangle([pill_x, pill_y, pill_x + pill_w, pill_y + 36], radius=18, fill=(255, 255, 255, 40), outline=(255, 255, 255, 100), width=1)
        ol_draw.text((pill_x + 15, pill_y + 6), ig_text, font=ig_font, fill="#000000")

    # Puan
    puan_text = f"PUAN: {current_stamps} / {goal}"
    p_font = _get_bold_font(18)
    p_bbox = ol_draw.textbbox((0, 0), puan_text, font=p_font)
    p_pill_w = (p_bbox[2]-p_bbox[0] + 30)
    p_pill_x = (width - p_pill_w) // 2
    p_pill_y = height - 46
    
    # Buzlu Cam Arka Planı
    ol_draw.rounded_rectangle([p_pill_x, p_pill_y, p_pill_x + p_pill_w, p_pill_y + 36], radius=18, fill=(255, 255, 255, 40), outline=(255, 255, 255, 100), width=1)
    ol_draw.text((p_pill_x + 15, p_pill_y + 6), puan_text, font=p_font, fill="#000000")

    # Ödül Hazır (Golden Glass Etkisi)
    if is_reward_ready:
        badge_text = "ÖDÜLÜNÜZ HAZIR! "
        b_font = _get_bold_font(18)
        e_font = _get_emoji_font(18)
        
        # Genişlik hesaplama
        b_bbox = ol_draw.textbbox((0, 0), badge_text, font=b_font)
        e_bbox = ol_draw.textbbox((0, 0), "🎁", font=e_font)
        text_w = b_bbox[2] - b_bbox[0]
        emoji_w = e_bbox[2] - e_bbox[0]
        
        pill_w = text_w + emoji_w + 40
        pill_x = width - pill_w - 20
        pill_y = height - 46
        
        # Premium Altın Cam (Yarı Şeffaf Kehribar + Parlak Kontür)
        ol_draw.rounded_rectangle([pill_x, pill_y, pill_x + pill_w, pill_y + 36], radius=18, fill=(251, 191, 36, 160), outline=(251, 191, 36, 255), width=1)
        
        # Metin ve emojiyi kutu içinde ORTALA
        content_total_w = text_w + emoji_w
        start_x = pill_x + (pill_w - content_total_w) // 2
        
        ol_draw.text((start_x, pill_y + 6), badge_text, font=b_font, fill="#000000")
        ol_draw.text((start_x + text_w, pill_y + 6), "🎁", font=e_font, fill="#000000", embedded_color=True)

    # Katmanı ana resme işle
    img.alpha_composite(overlay)

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
        # Rozet mantığı: Eğer harcanmamış ödül varsa göster
        has_available_rewards = (total_rewards - used_rewards) > 0
        strip = _make_stamp_strip(current_stamps, goal, primary_color, label_color, stamp_symbol, campaign_name, is_reward_ready=has_available_rewards, instagram=instagram)
        zf.writestr("strip.png", strip); zf.writestr("strip@2x.png", strip)
        
        # Tier (Seviye) Mantığı - İngilizce ve Büyük Harf
        if total_rewards >= 51:
            tier_name = "DIAMOND BEAR"
        elif total_rewards >= 16:
            tier_name = "GOLDEN BEAR"
        elif total_rewards >= 6:
            tier_name = "SILVER BEAR"
        else:
            tier_name = "BRONZE BEAR"

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
                    {"key": "total_earned", "label": "KAZANILAN ÖDÜL", "value": f"{total_rewards}"}
                ],
                "primaryFields": [], 
                "secondaryFields": [
                    {"key": "reward", "label": "ÖDÜL", "value": reward_text},
                    {"key": "remaining", "label": "KALAN ÖDÜLÜM", "value": f"{total_rewards - used_rewards} ADET"}
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
    # Rozet mantığı: Eğer harcanmamış ödül varsa göster
    has_available_rewards = (total_rewards - used_rewards) > 0
    icon_bytes = _make_icon_png(60, primary_color, merchant_name[0])
    strip_bytes = _make_stamp_strip(
        current_stamps=current_stamps,
        goal=goal,
        primary_color=primary_color,
        label_color=label_color,
        stamp_symbol=stamp_symbol,
        campaign_name=campaign_name,
        is_reward_ready=has_available_rewards,
        instagram=instagram
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
        "is_reward_ready": has_available_rewards,
        "instagram": instagram,
        "used_rewards": used_rewards,
        "total_rewards": total_rewards
    }
