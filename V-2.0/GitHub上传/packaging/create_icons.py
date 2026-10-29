"""Render the DJI campus typographic app icon and export native icon formats (macOS)."""
from pathlib import Path
import subprocess
import AppKit
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'static' / 'branding'
OUT.mkdir(parents=True, exist_ok=True)
size = 1024
image = AppKit.NSImage.alloc().initWithSize_((size, size))
image.lockFocus()
AppKit.NSColor.clearColor().setFill()
AppKit.NSRectFill(((0, 0), (size, size)))
AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.04, 0.09, 0.10, 1).setFill()
AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(((18, 18), (988, 988)), 220, 220).fill()


def text(value, font_size, y, color, bold=True):
    font = AppKit.NSFont.boldSystemFontOfSize_(font_size) if bold else AppKit.NSFont.systemFontOfSize_(font_size)
    attributes = {AppKit.NSFontAttributeName: font, AppKit.NSForegroundColorAttributeName: color}
    string = AppKit.NSAttributedString.alloc().initWithString_attributes_(value, attributes)
    width = string.size().width
    string.drawAtPoint_(((size-width)/2, y))


text('DJI', 310, 380, AppKit.NSColor.whiteColor())
text('campus', 125, 230, AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.65, 0.96, 0.53, 1), False)
image.unlockFocus()
bitmap = AppKit.NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
png = bitmap.representationUsingType_properties_(AppKit.NSPNGFileType, {})
(OUT / 'dji-campus.png').write_bytes(bytes(png))
source = Image.open(OUT / 'dji-campus.png').convert('RGBA')
source.save(OUT / 'dji-campus.ico', format='ICO', sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
iconset = OUT / 'dji-campus.iconset'
iconset.mkdir(exist_ok=True)
for point_size in (16, 32, 128, 256, 512):
    for scale in (1, 2):
        pixels = point_size * scale
        name = f'icon_{point_size}x{point_size}' + ('@2x' if scale == 2 else '') + '.png'
        source.resize((pixels, pixels), Image.Resampling.LANCZOS).save(iconset / name)
subprocess.run(['iconutil', '-c', 'icns', str(iconset), '-o', str(OUT / 'dji-campus.icns')], check=True)
print(OUT)
