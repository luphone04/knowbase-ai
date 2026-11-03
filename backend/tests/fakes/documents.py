"""Real PDF and DOCX files for tests, built in memory.

`make_pdf` writes a minimal but valid PDF (Helvetica text, correct xref
offsets) that pypdf extracts page by page. `make_docx` uses python-docx
itself. Neither is a mock: the production parsers read these files as-is.
"""

import io
import zipfile
from collections.abc import Sequence
from typing import Any

import docx
from pypdf import PdfReader, PdfWriter


def make_pdf(pages: Sequence[Sequence[str]], *, title: str | None = None) -> bytes:
    """One text line per string; an empty page list entry makes an image-only-like page."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    contents = []
    for lines in pages:
        ops = ["BT", "/F1 11 Tf", "72 740 Td", "14 TL"]
        for line in lines:
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"({escaped}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        contents.append(add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"))
    pages_id = len(objects) + len(pages) + 1
    page_ids = [
        add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>" % (pages_id, font, c)
        )
        for c in contents
    ]
    kids = b" ".join(b"%d 0 R" % p for p in page_ids)
    assert add(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids))) == pages_id
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    info = add(b"<< /Title (%s) >>" % title.encode("latin-1")) if title else None

    out = io.BytesIO()
    out.write(b"%PDF-1.7\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    trailer = b"trailer\n<< /Size %d /Root %d 0 R" % (len(objects) + 1, catalog)
    if info is not None:
        trailer += b" /Info %d 0 R" % info
    out.write(trailer + b" >>\nstartxref\n%d\n%%%%EOF\n" % xref)
    return out.getvalue()


def encrypt_pdf(data: bytes, password: str = "secret") -> bytes:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    writer.encrypt(user_password=password, algorithm="AES-256")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


DocxBlock = tuple[str, Any]  # ("heading", (text, level)) | ("paragraph", text) | ("table", rows)


def make_docx(blocks: Sequence[DocxBlock], *, title: str | None = None) -> bytes:
    document = docx.Document()
    if title:
        document.core_properties.title = title
    for kind, value in blocks:
        if kind == "heading":
            heading_text, level = value
            document.add_heading(str(heading_text), level=int(level))
        elif kind == "paragraph":
            document.add_paragraph(str(value))
        elif kind == "table":
            rows: list[list[str]] = [list(map(str, row)) for row in value]
            table = document.add_table(rows=len(rows), cols=len(rows[0]))
            for r, row in enumerate(rows):
                for c, cell in enumerate(row):
                    table.cell(r, c).text = cell
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def make_zip(entries: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return out.getvalue()


RETURNS_POLICY = (
    "# Returns policy\n\n"
    "Northstar Outfitters accepts returns of unused laptops within 30 days of delivery. "
    "Items must include all original accessories.\n\n"
    "## Damaged packaging\n\n"
    "If the outer packaging is damaged but the laptop is unused, the return is still accepted. "
    "Contact support with photos within 48 hours.\n"
)


def make_shared_stream_pdf(pages: int, chars_per_page: int) -> bytes:
    """A tiny PDF whose pages all draw one compressed content stream (audit H2).

    The file stays a few KB while every page extracts to about
    `chars_per_page` characters: the decompression-amplification shape.
    """
    import zlib

    op = b"BT /F1 12 Tf 10 10 Td (Lorem ipsum dolor sit amet) Tj ET\n"
    content = zlib.compress(op * max(1, chars_per_page // 26), 9)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids ["
        + b" ".join(b"%d 0 R" % (5 + i) for i in range(pages))
        + b"] /Count %d >>" % pages,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(content)
        + content
        + b"\nendstream",
    ]
    objects += [
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 3 0 R >> >> >>"
    ] * pages
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    )
    return out.getvalue()


def set_zip_flags(data: bytes, flag_bits: int) -> bytes:
    """Set general-purpose flag bits on every ZIP entry (0x1 = 'encrypted')."""
    out = bytearray(data)
    for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        start = 0
        while (index := out.find(signature, start)) != -1:
            flags = int.from_bytes(out[index + offset : index + offset + 2], "little")
            out[index + offset : index + offset + 2] = (flags | flag_bits).to_bytes(2, "little")
            start = index + 4
    return bytes(out)
