"""Files the user attaches in the chat (and files the agent reads): store, classify, extract text, pick images.

Uploads live in the workspace under uploads/<YYYY-MM>/. Nothing here executes a file: documents are parsed as data,
videos are only decoded by ffmpeg into still frames / audio, and every extracted text is handed to the model as
untrusted content.
"""
from __future__ import annotations

import base64
import io
import mimetypes
import os
import re
import subprocess
import time
import zipfile
from xml.etree import ElementTree as ET

UPLOAD_MAX = 50 * 1024 * 1024
UPLOAD_MAX_FILES = 10
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".heif"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
AUDIO_EXT = {".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac", ".opus"}
TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".yaml", ".yml", ".xml", ".ini", ".py", ".js",
            ".ts", ".html", ".htm", ".sql", ".sh"}
DOC_EXT = {".pdf", ".docx", ".xlsx", ".xlsm", ".pptx"}
ALLOWED_EXT = IMAGE_EXT | VIDEO_EXT | AUDIO_EXT | TEXT_EXT | DOC_EXT | {".doc", ".xls", ".ppt", ".zip", ".rtf"}

for _ext, _mime in ((".md", "text/markdown"), (".heic", "image/heic"), (".webp", "image/webp"), (".m4a", "audio/mp4"),
                    (".opus", "audio/ogg"), (".mkv", "video/x-matroska"), (".docx",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
                    (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                    (".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation")):
    mimetypes.add_type(_mime, _ext)


class AttachmentError(ValueError):
    pass


def kind_of(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext == ".pdf":
        return "pdf"
    if ext in (".docx",):
        return "docx"
    if ext in (".xlsx", ".xlsm"):
        return "xlsx"
    if ext == ".pptx":
        return "pptx"
    if ext in TEXT_EXT:
        return "text"
    return "other"


def mime_of(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def safe_name(name: str) -> str:
    name = os.path.basename(str(name or "").replace("\\", "/")).strip()
    name = re.sub(r"[\x00-\x1f/<>:\"|?*]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = "file"
    base, ext = os.path.splitext(name)
    return base[:80] + ext[:10].lower()


def save_upload(workspace: str, name: str, data: bytes) -> dict:
    """Write an uploaded file under uploads/<YYYY-MM>/ with a unique name. Returns its info."""
    if not data:
        raise AttachmentError("文件是空的 (empty file)")
    if len(data) > UPLOAD_MAX:
        raise AttachmentError(f"文件超过 {UPLOAD_MAX // 1024 // 1024} MB (file too large)")
    name = safe_name(name)
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_EXT:
        raise AttachmentError(f"不支持这种文件类型 {ext or '(none)'} (unsupported file type)")
    folder = os.path.join(workspace, "uploads", time.strftime("%Y-%m"))
    os.makedirs(folder, exist_ok=True)
    base, ext = os.path.splitext(name)
    path, n = os.path.join(folder, name), 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(folder, f"{base} ({n}){ext}")
    with open(path, "wb") as f:
        f.write(data)
    return info(workspace, path)


def info(workspace: str, path: str) -> dict:
    return {"path": os.path.relpath(path, workspace), "name": os.path.basename(path), "size": os.path.getsize(path),
            "mime": mime_of(path), "kind": kind_of(path)}


# ------------------------------------------------------------------ text extraction
def _xml_text(data: bytes, para_tag: str, text_tag: str) -> list[str]:
    out = []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return out
    for p in root.iter(para_tag):
        s = "".join(t.text or "" for t in p.iter(text_tag))
        if s.strip():
            out.append(s)
    return out


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def docx_text(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if "word/document.xml" not in names:
            raise AttachmentError("不是有效的 Word 文件 (not a .docx)")
        root = ET.fromstring(z.read("word/document.xml"))
        body = root.find(f"{W}body")
        lines: list[str] = []
        for el in list(body) if body is not None else []:
            if el.tag == f"{W}p":
                s = "".join(t.text or "" for t in el.iter(f"{W}t"))
                style = el.find(f"{W}pPr/{W}pStyle")
                lvl = (style.get(f"{W}val") or "") if style is not None else ""
                if s.strip():
                    m = re.match(r"(?i)heading\s*(\d)|标题\s*(\d)", lvl)
                    lines.append(("#" * int(m.group(1) or m.group(2)) + " " if m else "") + s)
            elif el.tag == f"{W}tbl":
                for tr in el.iter(f"{W}tr"):
                    cells = ["".join(t.text or "" for t in tc.iter(f"{W}t")).strip() for tc in tr.iter(f"{W}tc")]
                    lines.append("| " + " | ".join(cells) + " |")
        for extra in sorted(n for n in names if re.match(r"word/(footnotes|endnotes)\.xml$", n)):
            lines += _xml_text(z.read(extra), f"{W}p", f"{W}t")
    return "\n".join(lines)


def pptx_text(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                        key=lambda n: int(re.findall(r"\d+", n)[-1]))
        out = []
        for i, n in enumerate(slides, 1):
            out.append(f"## Slide {i}")
            out += _xml_text(z.read(n), f"{A}p", f"{A}t")
    return "\n".join(out)


def pdf_text(path: str, max_pages: int = 80) -> tuple[str, int]:
    from pypdf import PdfReader
    r = PdfReader(path)
    pages = r.pages[:max_pages]
    parts = []
    for i, pg in enumerate(pages, 1):
        try:
            t = (pg.extract_text() or "").strip()
        except Exception:
            t = ""
        if t:
            parts.append(f"--- page {i} ---\n{t}")
    return "\n".join(parts), len(r.pages)


def text_of(path: str, limit: int = 40000) -> str:
    """Readable text of a document, or '' when it has none (images, scanned PDFs, media)."""
    k = kind_of(path)
    if k == "pdf":
        return pdf_text(path)[0][:limit]
    if k == "docx":
        return docx_text(path)[:limit]
    if k == "pptx":
        return pptx_text(path)[:limit]
    if k == "xlsx":
        from app.common import xlsx
        return xlsx.read_text(path, limit)
    if k == "text":
        with open(path, "rb") as f:
            raw = f.read(limit * 4)
        if b"\x00" in raw[:2000]:
            return ""
        return raw.decode("utf-8", errors="replace")[:limit]
    return ""


# ------------------------------------------------------------------ images, PDF scans, video frames
_HEIF = False


def _pil():
    global _HEIF
    try:
        from PIL import Image  # noqa: F401
    except Exception:
        return None
    if not _HEIF:
        try:
            from pillow_heif import register_heif_opener
            register_heif_opener()       # iPhone photos (.heic)
        except Exception:
            pass
        _HEIF = True
    return Image


def to_jpeg(data: bytes, max_side: int = 1280) -> tuple[bytes, str]:
    """Resize/convert an image for the vision model. Falls back to the original bytes without Pillow."""
    Image = _pil()
    if not Image:
        return data, "image/jpeg"
    try:
        im = Image.open(io.BytesIO(data))
        try:
            from PIL import ImageOps
            im = ImageOps.exif_transpose(im)
        except Exception:
            pass
        if getattr(im, "n_frames", 1) > 1:
            im.seek(0)
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        raise AttachmentError(f"无法读取这张图片 (cannot read image): {str(e)[:120]}")


def image_b64(path: str, max_side: int = 1280) -> tuple[str, str]:
    with open(path, "rb") as f:
        data, mime = to_jpeg(f.read(), max_side)
    return base64.b64encode(data).decode(), mime


def pdf_page_images(path: str, max_pages: int = 4) -> list[bytes]:
    """Scanned PDFs: the page images embedded in the first pages (no renderer needed)."""
    from pypdf import PdfReader
    out: list[bytes] = []
    for pg in PdfReader(path).pages[:max_pages]:
        try:
            imgs = list(pg.images)
        except Exception:
            imgs = []
        if imgs:
            biggest = max(imgs, key=lambda im: len(im.data))
            out.append(biggest.data)
    return out


def ffmpeg_exe() -> str | None:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        from shutil import which
        return which("ffmpeg")


def media_duration(path: str) -> float:
    exe = ffmpeg_exe()
    if not exe:
        return 0.0
    p = subprocess.run([exe, "-hide_banner", "-i", path], capture_output=True, text=True, timeout=60)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", p.stderr)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


def video_frames(path: str, n: int = 6, max_side: int = 640) -> list[tuple[float, bytes]]:
    """n still frames spread evenly over the video, as JPEG bytes with their timestamp (seconds)."""
    exe = ffmpeg_exe()
    if not exe:
        raise AttachmentError("没有可用的 ffmpeg，无法读取视频 (ffmpeg not available)")
    dur = media_duration(path)
    times = [dur * (i + 0.5) / n for i in range(n)] if dur > 0 else [0.0]
    out = []
    for ts in times:
        p = subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-ss", f"{ts:.2f}", "-i", path, "-frames:v", "1",
                            "-vf", f"scale='min({max_side},iw)':-2", "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
                           capture_output=True, timeout=60)
        if p.returncode == 0 and p.stdout:
            out.append((ts, p.stdout))
    if not out:
        raise AttachmentError("无法从视频中取出画面 (could not decode the video)")
    return out


def contact_sheet(frames: list[tuple[float, bytes]], cols: int = 3, cell: int = 420) -> tuple[bytes, str]:
    """Combine frames into one labelled grid image (one vision call instead of many)."""
    Image = _pil()
    if not Image:
        return frames[0][1], "image/jpeg"
    from PIL import ImageDraw
    ims = []
    for ts, data in frames:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        im.thumbnail((cell, cell))
        ims.append((ts, im))
    rows = (len(ims) + cols - 1) // cols
    h = max(im.height for _, im in ims)
    sheet = Image.new("RGB", (cols * cell, rows * (h + 4)), "white")
    d = ImageDraw.Draw(sheet)
    for i, (ts, im) in enumerate(ims):
        x, y = (i % cols) * cell, (i // cols) * (h + 4)
        sheet.paste(im, (x, y))
        label = f"#{i + 1}  {int(ts // 60)}:{int(ts % 60):02d}"
        d.rectangle([x, y, x + 8 * len(label) + 8, y + 18], fill="black")
        d.text((x + 4, y + 3), label, fill="white")
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=82)
    return buf.getvalue(), "image/jpeg"


def audio_wav(path: str, max_seconds: int = 600) -> bytes:
    """The soundtrack as 16 kHz mono WAV (for speech-to-text), at most max_seconds."""
    exe = ffmpeg_exe()
    if not exe:
        raise AttachmentError("没有可用的 ffmpeg (ffmpeg not available)")
    p = subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-i", path, "-t", str(max_seconds), "-vn", "-ac", "1",
                        "-ar", "16000", "-f", "wav", "pipe:1"], capture_output=True, timeout=180)
    if p.returncode != 0 or len(p.stdout) < 1000:
        raise AttachmentError("这个文件没有可用的声音 (no audio track)")
    return p.stdout
