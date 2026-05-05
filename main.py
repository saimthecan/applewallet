from urllib.parse import quote
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response

from app.core.config import settings
from app.services.pass_generator import build_pkpass


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
      <a href="{pass_url}">Test .pkpass ac</a>
      <p><code>{pass_url}</code></p>
    </main>
  </body>
</html>
"""


@app.get("/pass")
def pass_file():
    _require_signing_config()
    pkpass = build_pkpass(
        pass_id=f"test-{uuid.uuid4()}",
        customer_identifier="TEST_ONLY",
        campaign_id="test-campaign",
        current_stamps=0,
        merchant_name="Bear Coffee",
        primary_color="#8b00b8",
        label_color="#ffffff",
        campaign_goal=5,
        reward_text="FILTRE KAHVE",
        campaign_name="5 KAHVE ALANA 1 ADET BIZDEN",
        stamp_label="Puan",
        reward_label="ODUL",
        loyalty_card_label="Test Kart",
        instagram=None,
        auth_token=None,
        qr_message_override="TEST_ONLY",
        pending_rewards=1,
        total_rewards_earned=1,
        stamp_icon="*",
        location_name=None,
        scope_location_id=None,
        business_display_name="Bear Coffee",
        location_latitude=None,
        location_longitude=None,
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
