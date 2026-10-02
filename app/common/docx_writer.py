"""Markdown → Word (.docx) without third-party packages (the 2026-10-02 test pasted a user's contract summary into an
online converter because OMuse had no way to make a Word file).

Supported: # / ## / ### headings, paragraphs, **bold**, *italic*, `code`, bullet (- * •) and numbered (1.) lists,
Markdown tables, > quotes, --- rules and ![alt](workspace image) pictures (PNG/JPEG). Chinese text uses a CJK font."""
from __future__ import annotations

import os
import re
import struct
import zipfile
from xml.sax.saxutils import escape

FONT_LATIN, FONT_CJK = "Calibri", "Microsoft YaHei"


class DocxError(Exception):
    pass


def _runs(text: str) -> str:
    """Inline Markdown → runs (bold, italic, code); links keep their text and show the URL in brackets."""
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1 (\2)", text)
    out = []
    for part in re.split(r"(\*\*[^*]+\*\*|`[^`]+`|(?<!\*)\*[^*\s][^*]*\*(?!\*))", text):
        if not part:
            continue
        props = ""
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            part, props = part[2:-2], "<w:b/>"
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            part, props = part[1:-1], '<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/><w:shd w:val="clear" w:fill="F2F2F2"/>'
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            part, props = part[1:-1], "<w:i/>"
        out.append(f'<w:r><w:rPr>{props}</w:rPr><w:t xml:space="preserve">{escape(part)}</w:t></w:r>')
    return "".join(out)


def _p(text: str, style: str = "", num: tuple[int, int] | None = None, align: str = "") -> str:
    ppr = ""
    if style:
        ppr += f'<w:pStyle w:val="{style}"/>'
    if num:
        ppr += f'<w:numPr><w:ilvl w:val="{num[1]}"/><w:numId w:val="{num[0]}"/></w:numPr>'
    if align:
        ppr += f'<w:jc w:val="{align}"/>'
    return f"<w:p><w:pPr>{ppr}</w:pPr>{_runs(text)}</w:p>"


def _table(rows: list[list[str]]) -> str:
    ncol = max(len(r) for r in rows)
    w = int(9000 / max(1, ncol))
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for _ in range(ncol))
    xml = ['<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="0" w:type="auto"/>'
           '<w:tblBorders>' + "".join(f'<w:{b} w:val="single" w:sz="4" w:color="BFBFBF"/>'
                                      for b in ("top", "left", "bottom", "right", "insideH", "insideV")) +
           f'</w:tblBorders></w:tblPr><w:tblGrid>{grid}</w:tblGrid>']
    for i, r in enumerate(rows):
        cells = []
        for j in range(ncol):
            txt = r[j] if j < len(r) else ""
            shade = '<w:shd w:val="clear" w:fill="EAF1FB"/>' if i == 0 else ""
            body = _runs(f"**{txt}**" if i == 0 and txt and not txt.startswith("**") else txt)
            cells.append(f'<w:tc><w:tcPr><w:tcW w:w="{w}" w:type="dxa"/>{shade}</w:tcPr><w:p>{body}</w:p></w:tc>')
        xml.append("<w:tr>" + "".join(cells) + "</w:tr>")
    xml.append("</w:tbl><w:p/>")
    return "".join(xml)


def _png_size(data: bytes) -> tuple[int, int]:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])
    if data[:2] == b"\xff\xd8":   # JPEG: walk the segments to the SOF marker
        i = 2
        while i < len(data) - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            m = data[i + 1]
            if m in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return 800, 600


def _image(rid: str, idx: int, w: int, h: int) -> str:
    max_w = 6.0 * 914400   # 6 inches
    cx = min(max_w, w * 9525)
    cy = int(cx * h / max(1, w))
    return (f'<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:drawing><wp:inline><wp:extent cx="{int(cx)}" cy="{cy}"/>'
            f'<wp:docPr id="{idx}" name="Picture {idx}"/><a:graphic><a:graphicData '
            'uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic><pic:nvPicPr>'
            f'<pic:cNvPr id="{idx}" name="img{idx}"/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill><a:blip r:embed="{rid}"/>'
            '<a:stretch><a:fillRect/></a:stretch></pic:blipFill><pic:spPr><a:xfrm><a:off x="0" y="0"/>'
            f'<a:ext cx="{int(cx)}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
            '</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>')


def markdown_to_docx(md: str, out_path: str, title: str = "", base_dir: str = "") -> dict:
    lines = (md or "").replace("\r\n", "\n").split("\n")
    body, images, i = [], [], 0
    if title and not (lines and lines[0].lstrip().startswith("# ")):
        body.append(_p(title, "Title"))
    while i < len(lines):
        ln = lines[i].rstrip()
        s = ln.strip()
        if not s:
            i += 1
            continue
        if s.startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                if not re.match(r"^\s*\|?\s*:?-{2,}", lines[i]):
                    rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            body.append(_table(rows))
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", s)
        img = re.match(r"^!\[([^\]]*)\]\(([^)\s]+)\)", s)
        if m:
            lvl = len(m.group(1))
            body.append(_p(m.group(2).strip(), "Title" if lvl == 1 and not body else f"Heading{min(lvl, 3)}"))
        elif img and base_dir:
            p = os.path.realpath(os.path.join(base_dir, img.group(2)))
            if p.startswith(os.path.realpath(base_dir) + os.sep) and os.path.isfile(p) and p.lower().endswith((".png", ".jpg", ".jpeg")):
                data = open(p, "rb").read()
                images.append((data, os.path.splitext(p)[1].lower().lstrip(".").replace("jpg", "jpeg")))
                w, h = _png_size(data)
                body.append(_image(f"rIdImg{len(images)}", len(images), w, h))
                if img.group(1):
                    body.append(_p(img.group(1), "Caption", align="center"))
            else:
                body.append(_p(f"[{img.group(1) or 'image'}]"))
        elif re.match(r"^[-*•]\s+", s):
            indent = (len(ln) - len(ln.lstrip())) // 2
            body.append(_p(re.sub(r"^[-*•]\s+", "", s), "ListParagraph", num=(1, min(indent, 2))))
        elif re.match(r"^\d+[.)、]\s*", s):
            body.append(_p(re.sub(r"^\d+[.)、]\s*", "", s), "ListParagraph", num=(2, 0)))
        elif s.startswith(">"):
            body.append(_p(s.lstrip("> ").strip(), "Quote"))
        elif re.match(r"^(-{3,}|\*{3,}|_{3,})$", s):
            body.append('<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="BFBFBF"/></w:pBdr></w:pPr></w:p>')
        else:
            body.append(_p(s))
        i += 1
    if not body:
        raise DocxError("内容为空 (empty document)")
    ns = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
          'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
          'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
          'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"')
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {ns}><w:body>{"".join(body)}'
                '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1200" w:right="1200" w:bottom="1200" '
                'w:left="1200" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr></w:body></w:document>')
    fonts = f'<w:rFonts w:ascii="{FONT_LATIN}" w:hAnsi="{FONT_LATIN}" w:eastAsia="{FONT_CJK}" w:cs="{FONT_LATIN}"/>'

    def style(sid, name, size, bold=False, color="", spacing=120, extra=""):
        b = "<w:b/>" if bold else ""
        c = f'<w:color w:val="{color}"/>' if color else ""
        return (f'<w:style w:type="paragraph" w:styleId="{sid}"><w:name w:val="{name}"/><w:basedOn w:val="Normal"/>'
                f'<w:pPr><w:spacing w:before="{spacing}" w:after="80"/>{extra}</w:pPr><w:rPr>{b}{c}<w:sz w:val="{size}"/></w:rPr></w:style>')
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
              f'<w:docDefaults><w:rPrDefault><w:rPr>{fonts}<w:sz w:val="22"/><w:lang w:val="en-US" w:eastAsia="zh-CN"/></w:rPr>'
              '</w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="100" w:line="300" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
              '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
              + style("Title", "Title", 36, True, "1F3864", 0)
              + style("Heading1", "heading 1", 30, True, "1F3864", 240)
              + style("Heading2", "heading 2", 26, True, "2E5597", 200)
              + style("Heading3", "heading 3", 23, True, "2E5597", 160)
              + style("ListParagraph", "List Paragraph", 22, spacing=0)
              + style("Quote", "Quote", 22, color="595959", extra='<w:ind w:left="567"/>')
              + style("Caption", "caption", 18, color="7F7F7F", spacing=0)
              + '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/></w:style></w:styles>')

    def absnum(aid, fmt, txt):
        lv = "".join(f'<w:lvl w:ilvl="{k}"><w:start w:val="1"/><w:numFmt w:val="{fmt}"/><w:lvlText w:val="{txt if fmt != "decimal" else "%" + str(k + 1) + "."}"/>'
                     f'<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="{420 + 420 * k}" w:hanging="300"/></w:pPr></w:lvl>' for k in range(3))
        return f'<w:abstractNum w:abstractNumId="{aid}">{lv}</w:abstractNum>'
    numbering = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                 + absnum(0, "bullet", "•") + absnum(1, "decimal", "")
                 + '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num><w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num></w:numbering>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '<Relationship Id="rIdNum" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>'
            + "".join(f'<Relationship Id="rIdImg{k + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                      f'Target="media/image{k + 1}.{ext}"/>' for k, (_d, ext) in enumerate(images))
            + "</Relationships>")
    ctypes = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
              '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
              '<Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/>'
              '<Default Extension="jpeg" ContentType="image/jpeg"/>'
              '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
              '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
              '<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
              '</Types>')
    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                 '</Relationships>')
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ctypes)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", styles)
        z.writestr("word/numbering.xml", numbering)
        z.writestr("word/_rels/document.xml.rels", rels)
        for k, (data, ext) in enumerate(images):
            z.writestr(f"word/media/image{k + 1}.{ext}", data)
    return {"path": out_path, "size": os.path.getsize(out_path), "paragraphs": len(body), "images": len(images)}
