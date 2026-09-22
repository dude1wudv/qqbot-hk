"""Small code-drawn character stickers; no network or model image requests."""

from __future__ import annotations

from pathlib import Path
import os
import tempfile

MOODS = {"开心", "疑惑", "无语", "鼓励", "晚安"}


def render_expression(
    mood: str, directory: str = "/opt/data/cache/character"
) -> str | None:
    """Return a deterministic PNG, or let callers fall back to plain text."""
    if mood not in MOODS:
        return None
    try:
        from PIL import Image, ImageDraw

        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        name = {
            "开心": "happy",
            "疑惑": "curious",
            "无语": "unamused",
            "鼓励": "cheer",
            "晚安": "sleep",
        }[mood]
        destination = root / (name + "-v1.png")
        if destination.is_file():
            return str(destination)
        image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        ink = "#263447"
        draw.ellipse((29, 46, 227, 231), fill="#9adbd2", outline=ink, width=7)
        draw.line((128, 46, 141, 20), fill=ink, width=6)
        draw.ellipse((132, 10, 151, 29), fill="#ffcf66", outline=ink, width=3)
        for x in (69, 161):
            draw.ellipse((x - 8, 155, x + 22, 170), fill="#efa5ab")
        if mood in {"晚安", "无语"}:
            for x in (74, 154):
                draw.line((x, 125, x + 28, 125), fill=ink, width=6)
        elif mood in {"开心", "鼓励"}:
            for x in (74, 154):
                draw.arc((x, 107, x + 28, 139), 185, 355, fill=ink, width=6)
        else:
            draw.ellipse((77, 111, 90, 135), fill=ink)
            draw.ellipse((165, 104, 178, 135), fill=ink)
        if mood == "无语":
            draw.line((111, 177, 143, 177), fill=ink, width=5)
        elif mood == "疑惑":
            draw.ellipse((119, 166, 137, 184), outline=ink, width=4)
        else:
            draw.arc((107, 154, 149, 190), 0, 180, fill=ink, width=5)
        if mood == "晚安":
            draw.text((198, 39), "Z z", fill=ink, stroke_width=1)
        elif mood == "鼓励":
            draw.line((19, 116, 7, 76), fill=ink, width=7)
            draw.ellipse((1, 60, 24, 85), fill="#ffcf66", outline=ink, width=3)
        fd, temporary = tempfile.mkstemp(dir=root, suffix=".png")
        try:
            with os.fdopen(fd, "wb") as handle:
                image.save(handle, format="PNG")
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return str(destination)
    except (ImportError, OSError):
        return None
