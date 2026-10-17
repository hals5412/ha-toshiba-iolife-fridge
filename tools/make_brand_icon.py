"""統合のアイコン (brand/icon.png, icon@2x.png) を生成する.

冷蔵庫と水滴を組み合わせたオリジナルの図柄。背景は透過で、ライト/ダーク両方で見えるよう
塗りと濃い縁取りを使う。

実行: python tools/make_brand_icon.py  (Pillow が必要)
"""

from pathlib import Path

from PIL import Image, ImageDraw

S = 2048  # 描画キャンバス (縮小してアンチエイリアスをかける)

BODY = (79, 195, 247, 255)  # 冷蔵庫の本体
BODY_SHADE = (41, 170, 230, 255)  # 下段 (冷凍室) を少し濃く
OUTLINE = (1, 87, 155, 255)
HANDLE = (1, 87, 155, 255)
DROP = (2, 136, 209, 255)
HIGHLIGHT = (255, 255, 255, 230)


def u(v: float) -> int:
    return round(v * S)


def draw() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    stroke = u(0.035)

    # 本体
    left, top, right, bottom = u(0.20), u(0.06), u(0.72), u(0.94)
    radius = u(0.07)
    d.rounded_rectangle((left, top, right, bottom), radius, fill=BODY, outline=OUTLINE, width=stroke)

    # 下段 (冷凍室) の塗り分け
    split = u(0.56)
    d.rounded_rectangle(
        (left + stroke, split, right - stroke, bottom - stroke),
        radius - stroke,
        fill=BODY_SHADE,
    )
    d.rectangle((left + stroke, split, right - stroke, split + radius), fill=BODY_SHADE)

    # 上段は観音開き
    center = (left + right) // 2
    d.line((center, top + stroke // 2, center, split), fill=OUTLINE, width=stroke)
    d.line((left, split, right, split), fill=OUTLINE, width=stroke)

    # 取っ手
    handle_w = u(0.03)
    d.rounded_rectangle(
        (center - u(0.075), u(0.24), center - u(0.075) + handle_w, u(0.44)),
        handle_w // 2,
        fill=HANDLE,
    )
    d.rounded_rectangle(
        (center + u(0.045), u(0.24), center + u(0.045) + handle_w, u(0.44)),
        handle_w // 2,
        fill=HANDLE,
    )
    d.rounded_rectangle(
        (u(0.34), u(0.64), u(0.58), u(0.64) + handle_w),
        handle_w // 2,
        fill=HANDLE,
    )

    # 右下の水滴 (給水・製氷のモチーフ)
    cx, cy, r = u(0.75), u(0.74), u(0.17)
    tip = u(0.36)
    drop = [
        (cx, cy - tip),
        (cx - r * 0.93, cy - r * 0.35),
        (cx + r * 0.93, cy - r * 0.35),
    ]
    d.ellipse((cx - r - stroke, cy - r - stroke, cx + r + stroke, cy + r + stroke), fill=(255, 255, 255, 255))
    d.polygon(
        [(cx, cy - tip - stroke * 1.6), (cx - r - stroke * 0.9, cy - r * 0.25), (cx + r + stroke * 0.9, cy - r * 0.25)],
        fill=(255, 255, 255, 255),
    )
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=DROP)
    d.polygon(drop, fill=DROP)
    d.ellipse(
        (cx - r * 0.55, cy - r * 0.15, cx - r * 0.20, cy + r * 0.45),
        fill=HIGHLIGHT,
    )
    return img


def main() -> None:
    out = Path(__file__).parents[1] / "custom_components" / "toshiba_iolife_fridge" / "brand"
    out.mkdir(parents=True, exist_ok=True)
    img = draw()
    img.resize((512, 512), Image.LANCZOS).save(out / "icon@2x.png", optimize=True)
    img.resize((256, 256), Image.LANCZOS).save(out / "icon.png", optimize=True)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
