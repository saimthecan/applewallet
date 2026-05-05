import sys
import os
from pathlib import Path
from io import BytesIO

# Add the app directory to sys.path
sys.path.append(os.path.join(os.getcwd()))

from services.pass_generator import _make_stamp_strip, _make_icon_png

def generate_preview():
    # Simulate the same parameters
    primary_color = "#7C3AED" # Purple
    label_color = "#FFFFFF"
    merchant_name = "Bear Coffee"
    campaign_name = "8 KAHVE ALANA 1 ADET BİZDEN"
    
    icon = _make_icon_png(60, primary_color, merchant_name[0])
    strip_bytes = _make_stamp_strip(
        current_stamps=3,
        goal=8,
        primary_color=primary_color,
        label_color=label_color,
        stamp_symbol="☕",
        campaign_name=campaign_name,
        logo_icon=icon
    )
    
    with open("preview_strip.png", "wb") as f:
        f.write(strip_bytes)
    print("Preview saved to preview_strip.png")

if __name__ == "__main__":
    generate_preview()
