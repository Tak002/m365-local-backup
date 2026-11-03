"""공통 출력 도우미: HTML→Markdown, 시각 형식, 파일명 정리, 원자적 쓰기, YAML front-matter."""
import datetime
import html
import json
import os
import pathlib
import re
import urllib.parse

import html2text

_DROP_BLOCKS = re.compile(r"(?is)<(style|script|head|title|xml)\b.*?</\1\s*>")
_COMMENTS = re.compile(r"(?s)<!--.*?-->")
_BLANKS = re.compile(r"\n{3,}")
_SAFELINK = re.compile(r"https://[\w.-]*safelinks\.protection\.outlook\.com/\?url=([^&\s)\]>]+)[^\s)\]>]*")
_WEEKDAY = "월화수목금토일"


def html_to_md(s: str) -> str:
    if not s:
        return ""
    s = _COMMENTS.sub("", _DROP_BLOCKS.sub("", s))
    h = html2text.HTML2Text()
    h.body_width = 0
    h.ignore_images = True
    h.images_to_alt = True
    h.unicode_snob = True
    h.wrap_links = False
    h.wrap_list_items = False
    h.pad_tables = False
    out = h.handle(s)
    out = _SAFELINK.sub(lambda m: urllib.parse.unquote(m.group(1)), out)
    out = "\n".join(line.rstrip() for line in out.splitlines())
    return _BLANKS.sub("\n\n", out).strip()


def plain_to_md(s: str) -> str:
    return html.unescape(s or "").strip()


def ms_to_dt(ms) -> datetime.datetime | None:
    try:
        ms = float(ms)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    return datetime.datetime.fromtimestamp(ms / 1000).astimezone()


def iso_to_dt(s) -> datetime.datetime | None:
    if not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone() if dt.tzinfo else dt.astimezone()


def iso(dt: datetime.datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def day_label(dt: datetime.datetime) -> str:
    return f"{dt:%Y-%m-%d} ({_WEEKDAY[dt.weekday()]})"


_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(s: str, limit: int = 40, default: str = "untitled") -> str:
    s = _BAD_CHARS.sub("", s or "")
    s = re.sub(r"\s+", " ", s).strip().strip(".")
    if len(s) > limit:
        s = s[:limit].rstrip(" .")
    if not s or s.upper() in _RESERVED:
        s = default
    return s


def safe_file_name(name: str, limit: int = 100) -> str:
    stem, ext = os.path.splitext(name or "")
    ext = _BAD_CHARS.sub("", ext)[:16]
    return safe_name(stem, limit - len(ext), default="file") + ext


def write_atomic(path: pathlib.Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def dump_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1) + "\n"


def dump_jsonl(rows) -> str:
    return "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in rows)


def front_matter(meta: dict) -> str:
    """JSON 스칼라·배열은 그대로 유효한 YAML이다."""
    lines = ["---"]
    for k, v in meta.items():
        if v is None or v == [] or v == "":
            continue
        lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def human_size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
