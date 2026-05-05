import os
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

# Add the app directory to sys.path
sys.path.append(os.path.join(os.getcwd()))

from services.pass_generator import _make_icon_png, _make_stamp_strip


def generate_preview():
    primary_color = "#7C3AED"
    label_color = "#FFFFFF"
    merchant_name = "Bear Coffee"

    icon = _make_icon_png(60, primary_color, merchant_name[0])
    preview_dir = Path("previews")
    preview_dir.mkdir(exist_ok=True)
    strips = []

    for goal in range(2, 11):
        strip_bytes = _make_stamp_strip(
            current_stamps=min(3, goal),
            goal=goal,
            primary_color=primary_color,
            label_color=label_color,
            stamp_symbol="☕",
            campaign_name=f"{goal} KAHVE ALANA 1 ADET BIZDEN",
            logo_icon=icon,
        )
        output_path = preview_dir / f"preview_strip_{goal}.png"
        output_path.write_bytes(strip_bytes)
        strips.append((goal, strip_bytes))

    Path("preview_strip.png").write_bytes((preview_dir / "preview_strip_8.png").read_bytes())

    sheet = Image.new("RGB", (750, 9 * 334), "#111111")
    sheet_draw = ImageDraw.Draw(sheet)
    for index, (goal, strip_bytes) in enumerate(strips):
        y = index * 334
        sheet_draw.text((16, y + 10), f"Goal {goal}", fill="#ffffff")
        strip_img = Image.open(BytesIO(strip_bytes)).convert("RGB")
        sheet.paste(strip_img, (0, y + 34))
    sheet.save(preview_dir / "preview_strip_all_goals.jpg", quality=92)

    print("Previews saved to previews/ and preview_strip.png")


if __name__ == "__main__":
    generate_preview()
