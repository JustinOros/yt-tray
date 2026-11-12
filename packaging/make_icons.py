import sys
from pathlib import Path

from PIL import Image, ImageDraw

out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent
size = 1024
img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((40, 40, size - 40, size - 40), radius=220, fill=(24, 28, 36, 255))
d.ellipse((190, 190, size - 190, size - 190), outline=(46, 184, 92, 255), width=48)
d.polygon([(410, 330), (410, 694), (720, 512)], fill=(46, 184, 92, 255))
img.save(out / "icon.png")
img.save(out / "icon.ico", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
img.save(out / "icon.icns")
