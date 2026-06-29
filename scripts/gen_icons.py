"""Генерирует минимальные иконки для Tauri: icon.png (512×512) и icon.ico.

Использует только PIL (Pillow), без внешних зависимостей.
Простой дизайн: фиолетовый круг с белой буквой "A" в центре.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("ERROR: Pillow not installed. Run: pip install Pillow", file=sys.stderr)
    sys.exit(1)

ICONS_DIR = Path(__file__).resolve().parents[1] / "rust" / "icons"
ICONS_DIR.mkdir(parents=True, exist_ok=True)

# Фиолетовый градиент (как в web_ui/styles.css --accent: #7c5cff)
BG_COLOR = (124, 92, 255, 255)  # #7c5cff
FG_COLOR = (255, 255, 255, 255)  # white


def make_icon(size: int) -> Image.Image:
    """Создаёт иконку size×size с буквой A."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Круг на весь размер
    draw.ellipse([0, 0, size - 1, size - 1], fill=BG_COLOR)
    # Буква A в центре
    # Используем дефолтный шрифт, масштабируем под size
    font_size = int(size * 0.6)
    try:
        # Пробуем системный шрифт
        font_paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ]
        font = None
        for fp in font_paths:
            if Path(fp).exists():
                font = ImageFont.truetype(fp, font_size)
                break
        if font is None:
            font = ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()
    # Центрируем букву
    text = "A"
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        tx = (size - tw) // 2 - bbox[0]
        ty = (size - th) // 2 - bbox[1]
    except AttributeError:
        # старый PIL без textbbox
        tw, th = font.getsize(text)
        tx = (size - tw) // 2
        ty = (size - th) // 2
    draw.text((tx, ty), text, fill=FG_COLOR, font=font)
    return img


def main():
    # PNG 512×512 (Tauri использует для Linux/macOS)
    png_path = ICONS_DIR / "icon.png"
    img512 = make_icon(512)
    img512.save(png_path, "PNG")
    print(f"  created {png_path} ({png_path.stat().st_size} bytes)")

    # ICO с несколькими размерами (Windows). PIL сохраняет каждый размер
    # как отдельный слой в одном .ico файле.
    ico_path = ICONS_DIR / "icon.ico"
    sizes_ico = [16, 32, 48, 64, 128, 256]
    icons = [make_icon(s) for s in sizes_ico]
    # Передаём sizes= для указания размеров, append_images для доп. слоёв
    icons[-1].save(  # последний (256×256) как базовый
        ico_path, format="ICO",
        sizes=[(s, s) for s in sizes_ico],
        append_images=icons[:-1],
    )
    print(f"  created {ico_path} ({ico_path.stat().st_size} bytes)")

    # Также 128×128 PNG (часто требуется для Linux .desktop)
    img128 = make_icon(128)
    img128.save(ICONS_DIR / "icon_128.png", "PNG")
    print(f"  created {ICONS_DIR / 'icon_128.png'}")

    print("\nDone. Icons ready for Tauri dev and build.")


if __name__ == "__main__":
    main()
