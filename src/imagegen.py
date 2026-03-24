from io import BytesIO
from typing import Iterable, Optional
from PIL import Image, ImageDraw, ImageFont


def _font(size: int) -> ImageFont.ImageFont:
    try:
        # Try a common sans font if available in the container; fall back to default
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size)
    except Exception:
        return ImageFont.load_default()


def render_calc_card(*,
                     title: str,
                     server_settings: str,
                     user_input: str,
                     joining_text: str,
                     calling_text: str,
                     footer: Optional[str] = None,
                     heroes: Optional[Iterable[str]] = None,
                     width: int = 1000,
                     height: int = 700,
                     ) -> BytesIO:
    """Render a simple PNG card containing the calculation results.

    Returns a BytesIO positioned at start, ready to be sent as a Discord file.
    """
    img = Image.new("RGB", (width, height), color=(24, 26, 27))  # dark background
    draw = ImageDraw.Draw(img)

    title_font = _font(40)
    h2_font = _font(28)
    body_font = _font(24)
    small_font = _font(20)

    margin = 30
    x = margin
    y = margin

    # Title
    draw.text((x, y), title, fill=(255, 255, 255), font=title_font)
    y += 60

    # Heroes row (optional)
    if heroes:
        hero_line = "Heroes: " + ", ".join([h for h in heroes if (h or "").strip()])
        if hero_line.strip() != "Heroes:":
            draw.text((x, y), hero_line, fill=(200, 220, 255), font=h2_font)
            y += 40

    # Server settings and user input columns
    left_col_x = x
    right_col_x = width // 2 + 10

    draw.text((left_col_x, y), "Server Settings", fill=(255, 210, 120), font=h2_font)
    y_left = y + 36
    for line in server_settings.split("\n"):
        draw.text((left_col_x, y_left), line, fill=(230, 230, 230), font=body_font)
        y_left += 30

    draw.text((right_col_x, y), "Your Input", fill=(255, 210, 120), font=h2_font)
    y_right = y + 36
    for line in user_input.split("\n"):
        draw.text((right_col_x, y_right), line, fill=(230, 230, 230), font=body_font)
        y_right += 30

    y = max(y_left, y_right) + 10

    # Horizontal separator
    draw.line([(margin, y), (width - margin, y)], fill=(80, 80, 80), width=2)
    y += 20

    # Joining / Calling blocks
    block_w = (width - margin * 3) // 2
    block_h = 200

    # Joining block
    draw.rectangle([margin, y, margin + block_w, y + block_h], outline=(90, 120, 90), width=2)
    draw.text((margin + 10, y + 10), "Joining March (per march)", fill=(170, 255, 170), font=h2_font)
    j_y = y + 50
    for line in joining_text.split("\n"):
        draw.text((margin + 20, j_y), line, fill=(230, 230, 230), font=body_font)
        j_y += 32

    # Calling block
    cx = margin * 2 + block_w
    draw.rectangle([cx, y, cx + block_w, y + block_h], outline=(120, 120, 180), width=2)
    draw.text((cx + 10, y + 10), "Calling March", fill=(170, 200, 255), font=h2_font)
    c_y = y + 50
    for line in calling_text.split("\n"):
        draw.text((cx + 20, c_y), line, fill=(230, 230, 230), font=body_font)
        c_y += 32

    y = y + block_h + 20

    if footer:
        draw.text((margin, y), footer, fill=(180, 180, 180), font=small_font)

    # Export
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
