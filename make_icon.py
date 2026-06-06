"""Generate build/icon.ico from the app's runtime-painted logo.

The app paints its own icon (make_app_pixmap) instead of shipping an image
file, so there's no .ico to hand PyInstaller / Inno Setup. This renders that
logo to a multi-resolution .ico at build time. Called by build.ps1.

Needs Pillow (build.ps1 installs it).
"""
import os
from io import BytesIO

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray
from PySide6.QtWidgets import QApplication
import VideoAudioDownloader_UI as app
from PIL import Image

# QPixmap/QPainter need a running Q*Application even when only rendering offscreen.
_qapp = QApplication.instance() or QApplication([])

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build")
os.makedirs(OUT_DIR, exist_ok=True)


def _png_bytes(pixmap):
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    pixmap.save(buf, "PNG")
    buf.close()
    return bytes(ba)


def main():
    base = Image.open(BytesIO(_png_bytes(app.make_app_pixmap(256)))).convert("RGBA")
    ico = os.path.join(OUT_DIR, "icon.ico")
    base.save(ico, format="ICO",
              sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                     (64, 64), (128, 128), (256, 256)])
    print("wrote", ico)


if __name__ == "__main__":
    main()
