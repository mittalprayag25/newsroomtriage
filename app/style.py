from pathlib import Path


class StyleError(Exception):
    pass


def load_house_style(path: Path) -> str:
    if not path.is_file():
        raise StyleError(f"House style file is missing: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise StyleError(f"House style file is empty: {path}")
    return text
