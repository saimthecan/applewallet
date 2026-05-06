from urllib.parse import quote
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response

try:
    from core.config import settings
    from services.pass_generator import build_pkpass, get_pass_data_for_preview
except ModuleNotFoundError:
    from .core.config import settings
    from .services.pass_generator import build_pkpass, get_pass_data_for_preview


app = FastAPI(title="Wallet Pass Test", version="0.1.0")


def _require_signing_config() -> None:
    missing = [
        name
        for name, value in {
            "APPLE_PASS_TYPE_ID": settings.apple_pass_type_id,
            "APPLE_TEAM_ID": settings.apple_team_id,
            "APPLE_PASS_CERT_P12_B64": settings.apple_pass_cert_p12_b64,
            "APPLE_PASS_CERT_PASSWORD": settings.apple_pass_cert_password,
            "APPLE_WWDR_CERT_B64": settings.apple_wwdr_cert_b64,
        }.items()
        if not value
    ]
    if missing:
        raise HTTPException(status_code=503, detail=f"Apple signing config missing: {', '.join(missing)}")


def _pass_url() -> str:
    return f"{settings.api_base_url.rstrip('/')}/pass"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index():
    pass_url = _pass_url()
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=280x280&data={quote(pass_url)}"
    return f"""
<!doctype html>
<html lang="tr">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Apple Wallet Test Pass</title>
    <style>
      body {{
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        background: #0f172a;
        color: white;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }}
      main {{
        width: min(92vw, 420px);
        text-align: center;
      }}
      img {{
        width: 280px;
        height: 280px;
        background: white;
        padding: 14px;
        border-radius: 20px;
      }}
      a {{
        display: block;
        margin-top: 20px;
        border-radius: 14px;
        background: white;
        color: #0f172a;
        padding: 14px 18px;
        font-weight: 800;
        text-decoration: none;
      }}
      p {{
        color: #cbd5e1;
        line-height: 1.5;
      }}
      code {{
        word-break: break-all;
        color: #93c5fd;
      }}
    </style>
  </head>
  <body>
    <main>
      <h1>Apple Wallet Test Pass</h1>
      <p>iPhone Kamera ile QR'i okut veya direkt linke bas.</p>
      <img src="{qr_url}" alt="Apple Wallet test pass QR" />
      <a href="{pass_url}">Test .pkpass indir</a>
      <a href="/preview" style="background: #7c3aed; color: white; margin-top: 10px;">💻 Tarayıcıda Önizle (Canlı)</a>
      <p><code>{pass_url}</code></p>
    </main>
  </body>
</html>
"""


@app.get("/pass")
def pass_file():
    _require_signing_config()
    
    # Yeni sistem: 3/8 kahve, kahve emojisi ile
    pkpass = build_pkpass(
        pass_id=f"test-{uuid.uuid4()}",
        serial=f"SER-{uuid.uuid4().hex[:8].upper()}",
        user_name="Saim Can Özgen",
        current_stamps=3,
        goal=8,
        stamp_symbol="☕",
        merchant_name="Bear Coffee",
        campaign_name="8 KAHVE ALANA 1 ADET BİZDEN - 2. Promo ARsenal dün maçı kazandı mesela",
        reward_text="3 Adet Filtre Kahve diğer kısmları da uzatayım bakalım nasıl olacak",
        primary_color="#7C3AED",
        label_color="#FFFFFF",
        foreground_color="#FFFFFF",
        instagram="loyalbear.co bu da 2. satır uzadığında noluyor",
        language="tr",
    )
    
    return Response(
        content=pkpass,
        media_type="application/vnd.apple.pkpass",
        headers={
            "Content-Disposition": 'inline; filename="wallet-pass-test.pkpass"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/preview", response_class=HTMLResponse)
def preview_pass():
    data = get_pass_data_for_preview(
        current_stamps=8,
        goal=8,
        reward_text="3 Adet Filtre Kahve",
        primary_color="#7C3AED",
        merchant_name="Bear Coffee",
    )
    
    return f"""
<!doctype html>
<html lang="tr">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Apple Wallet Preview</title>
    <style>
        body {{
            background: #0f172a;
            color: white;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 40px 20px;
            margin: 0;
        }}
        .wallet-card {{
            width: 350px;
            background-color: {data['primary_color']};
            border-radius: 20px;
            overflow: hidden;
            box-shadow: 0 20px 50px rgba(0,0,0,0.5);
            position: relative;
        }}
        .header {{
            padding: 15px 20px;
            display: flex;
            align-items: center;
            gap: 12px;
        }}
        .logo {{
            width: 30px;
            height: 30px;
            border-radius: 6px;
            background: white;
            object-fit: cover;
        }}
        .merchant-name {{
            font-weight: 600;
            font-size: 16px;
            color: {data['label_color']};
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        .strip-container {{
            width: 100%;
            height: 140px;
            background: #000;
            overflow: hidden;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .strip-img {{
            width: 100%;
            height: 100%;
            object-fit: cover;
        }}
        .fields {{
            padding: 20px;
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 15px;
        }}
        .field-label {{
            font-size: 10px;
            font-weight: 700;
            color: {data['label_color']};
            opacity: 0.8;
            margin-bottom: 2px;
            text-transform: uppercase;
        }}
        .field-value {{
            font-size: 18px;
            font-weight: 600;
            color: {data['foreground_color']};
        }}
        .barcode-section {{
            background: white;
            margin: 10px 20px 20px;
            padding: 15px;
            border-radius: 12px;
            display: flex;
            flex-direction: column;
            align-items: center;
        }}
        .qr-placeholder {{
            width: 120px;
            height: 120px;
            background: #eee;
            border-radius: 4px;
            margin-bottom: 5px;
        }}
        .back-btn {{
            margin-top: 30px;
            color: #94a3b8;
            text-decoration: none;
            font-size: 14px;
        }}
        .back-btn:hover {{ color: white; }}
    </style>
</head>
<body>
    <h2 style="margin-bottom: 30px;">Apple Wallet Önizleme</h2>
    
    <div class="wallet-card">
        <div class="header">
            <img class="logo" src="data:image/png;base64,{data['logo_base64']}" alt="logo">
            <div class="merchant-name">{data['merchant_name']}</div>
            <div style="margin-left: auto; text-align: right;">
                <div class="field-label">PUAN</div>
                <div class="field-value" style="font-size: 14px;">{data['current_stamps']} / {data['goal']}</div>
            </div>
        </div>
        
        <div class="strip-container">
            <img class="strip-img" src="data:image/png;base64,{data['strip_base64']}" alt="strip">
        </div>
        
        <div class="fields">
            {f'''
            <div style="grid-column: span 2;">
                <div class="field-label"></div>
                <div class="field-value" style="font-size: 20px; color: #fbbf24; text-align: left;">{data['reward_text']} Ödülünüz Hazır! 🎁</div>
            </div>
            ''' if data['is_reward_ready'] else f'''
            <div>
                <div class="field-label">HEDİYE</div>
                <div class="field-value">{data['reward_text']}</div>
            </div>
            <div style="text-align: right;"></div>
            '''}
        </div>

        {f'''
        <div class="fields" style="margin-top: 10px; border-top: 1px solid rgba(255,255,255,0.1); padding-top: 10px;">
            <div>
                <div class="field-label">SOSYAL MEDYA</div>
                <div class="field-value" style="font-size: 14px;">@{data['instagram']}</div>
            </div>
        </div>
        ''' if data['instagram'] else ''}
        
        <div class="barcode-section">
            <img class="qr-placeholder" src="https://api.qrserver.com/v1/create-qr-code/?size=120x120&data=preview" alt="QR">
            <div style="color: #000; font-size: 10px; font-weight: bold; margin-top: 5px;">KART KODU</div>
        </div>
    </div>

    <a href="/" class="back-btn">← Ana Sayfaya Dön</a>
    <p style="margin-top: 40px; font-size: 12px; color: #64748b; text-align: center;">
        Bu sayfa yerel tasarım sürecini hızlandırmak için oluşturulmuştur.<br>
        Kodda yaptığınız değişiklikleri görmek için sayfayı yenileyin.
    </p>
</body>
</html>
"""
