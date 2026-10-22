"""Certify a PDF as a true copy.

The three things that make a scanned bundle look "certified" instead of
"photocopied": the blank pages are gone, every page carries a stamp, and the
last page ends with a signed certification block. This module does those three
things and then locks the result read-only so the copy cannot be edited after
it leaves your hands.
"""

from __future__ import annotations

import datetime as _dt
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

import fitz  # PyMuPDF

STAMP_TEXT = "CERTIFIED TRUE COPY"

__all__ = [
    "Certifier",
    "certify_pdf",
    "strip_blank_pages",
    "page_is_blank",
    "find_blank_region",
    "add_stamp",
    "add_cert_block",
    "STAMP_TEXT",
]


@dataclass
class Certifier:
    """Who is signing the certification.

    Only ``name`` is required. Everything else is optional and simply left out
    of the block when empty — so the same class covers a two-line certification
    and the verbose institutional wording some bodies ask for.
    """

    name: str
    membership: str = ""
    institution: str = ""
    capacity: str = ""
    company: str = ""
    address: str = ""
    phone: str = ""
    heading: str = "Certified True Copy"
    statement: str = ""
    bearer_signed: bool = False

    def detail_lines(self) -> list[str]:
        lines = [self.name]
        if self.institution:
            lines.append(self.institution)
        if self.membership:
            lines.append(self.membership)
        if self.company:
            lines.append(f"Company: {self.company}")
        if self.capacity:
            lines.append(f"Capacity: {self.capacity}")
        if self.address:
            lines.append(self.address)
        if self.phone:
            lines.append(self.phone)
        return [ln for ln in lines if ln]


# --------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------
def _cjk_font(doc: fitz.Document) -> str:
    """Pick a built-in font that can render Latin and CJK."""
    for name in ("china-ss", "china-s", "helv"):
        try:
            doc.load_font(name)
            return name
        except Exception:
            continue
    return "helv"


def page_is_blank(page: fitz.Page, threshold: float = 0.01) -> bool:
    """True when the page carries almost no ink.

    ``threshold`` is the fraction of sampled non-white pixels. The default
    ignores scanner speckle and JPEG noise but still keeps a page holding a
    single small stamp. A page with a text layer is never blank, however
    little ink it carries.
    """
    if page.get_text().strip():
        return False

    pix = page.get_pixmap(dpi=40)
    samples, channels = pix.samples, max(pix.n, 3)
    total = inked = 0
    for i in range(0, len(samples) - channels + 1, channels):
        r, g, b = samples[i], samples[i + 1], samples[i + 2]
        total += 1
        if r < 245 or g < 245 or b < 245:
            inked += 1
    return (inked / max(total, 1)) < threshold


def strip_blank_pages(doc: fitz.Document, threshold: float = 0.01) -> list[int]:
    """Delete blank pages in place. Returns the 1-based numbers removed."""
    blanks = [i for i in range(len(doc)) if page_is_blank(doc[i], threshold)]
    for index in reversed(blanks):
        doc.delete_page(index)
    return [i + 1 for i in blanks]


def find_blank_region(
    page: fitz.Page,
    min_area_ratio: float = 0.06,
    bottom_bias: float = 0.0,
) -> Optional[tuple[float, float]]:
    """Find the largest empty square on a page.

    Returns the centre as relative (0-1, 0-1) coordinates, or ``None`` when the
    page is too full to place anything. ``bottom_bias`` (0-1) restricts the
    search to the lower part of the page, which is where a certification block
    belongs.
    """
    pix = page.get_pixmap(dpi=72)
    width, height, channels, samples = pix.width, pix.height, pix.n, pix.samples
    cols, rows = width // 4 + 1, height // 4 + 1

    ink = [[0] * cols for _ in range(rows)]
    for y in range(0, height, 4):
        row = ink[y // 4]
        base = y * pix.stride
        for x in range(0, width, 4):
            i = base + x * channels
            if samples[i] < 250 or samples[i + 1] < 250 or samples[i + 2] < 250:
                row[x // 4] = 1

    # integral image so any rectangle's ink cost is O(1)
    integral = [[0] * (cols + 1) for _ in range(rows + 1)]
    for y in range(rows):
        running = 0
        current, previous = integral[y + 1], integral[y]
        row = ink[y]
        for x in range(cols):
            running += row[x]
            current[x + 1] = previous[x + 1] + running

    def ink_in(x0: int, y0: int, x1: int, y1: int) -> int:
        return integral[y1][x1] - integral[y0][x1] - integral[y1][x0] + integral[y0][x0]

    best_area, best = 0, None
    for window in (cols // 3, cols // 4, cols // 5):
        if window < 1:
            continue
        y_start = int(rows * bottom_bias) if bottom_bias else 0
        for cy in range(y_start, max(y_start, rows - window + 1)):
            for cx in range(cols - window + 1):
                if ink_in(cx, cy, cx + window, cy + window) == 0:
                    area = window * window
                    if area > best_area:
                        best_area = area
                        best = ((cx + window / 2) / cols, (cy + window / 2) / rows)
    if best is None or best_area < min_area_ratio * cols * rows:
        return None
    return best


# --------------------------------------------------------------------------
# drawing
# --------------------------------------------------------------------------
def add_stamp(
    page: fitz.Page,
    text: str = STAMP_TEXT,
    fontname: str = "helv",
    color: Sequence[float] = (0.85, 0.10, 0.10),
    opacity: float = 0.55,
    size: float = 34,
    pos: Optional[Sequence[float]] = None,
    angle: int = 25,
) -> None:
    """Draw the diagonal stamp. ``pos`` is the relative centre of the text."""
    rect = page.rect
    cx, cy = rect.width * (pos or (0.5, 0.5))[0], rect.height * (pos or (0.5, 0.5))[1]
    font = fitz.Font(fontname)
    writer = fitz.TextWriter(rect)
    writer.append((cx - size, cy), text, font=font, fontsize=size)
    writer.write_text(
        page,
        color=tuple(color),
        opacity=opacity,
        morph=(fitz.Point(cx, cy), fitz.Matrix(angle).prerotate(angle)),
    )


def add_cert_block(
    page: fitz.Page,
    certifier: Certifier,
    date_str: str,
    signature: Optional[str] = None,
    fontname: str = "helv",
    pos: Optional[Sequence[float]] = None,
    line_width: float = 260,
) -> None:
    """Draw the certification block: heading, date, signature line, details."""
    rect = page.rect
    if pos:
        left = rect.width * pos[0] - line_width / 2
        baseline = rect.height * pos[1] - 105
    else:
        left = 60                          # left margin
        baseline = rect.height - 250       # top of block

    details = certifier.detail_lines()
    block_height = (24 + 50 + 22 + 14 * max(len(details), 1)
                    + (15 if certifier.bearer_signed else 0))
    # never draw outside the printable area — a clipped signature block is
    # worse than a slightly crowded one
    left = min(max(left, 40), max(40, rect.width - line_width - 40))
    baseline = min(max(baseline, 80), max(80, rect.height - block_height - 10))

    page.insert_text(
        (left, baseline),
        certifier.statement or certifier.heading,
        fontname="hebo",
        fontsize=15,
    )
    baseline += 24
    if certifier.bearer_signed:
        page.insert_text(
            (left, baseline),
            "And the bearer of the document has signed in my presence.",
            fontname=fontname,
            fontsize=9,
        )
        baseline += 15
    page.insert_text((left, baseline), f"Date: {date_str}", fontname=fontname, fontsize=11)
    baseline += 50

    if signature and Path(signature).exists():
        try:
            image = fitz.Pixmap(signature)
            if image.n > 4:
                image = fitz.Pixmap(fitz.csRGB, image)
            ratio = image.width / max(image.height, 1)
            height = 30
            page.insert_image(
                fitz.Rect(left, baseline - height + 4, left + height * ratio, baseline + 4),
                pixmap=image,
            )
        except Exception as exc:  # a broken signature image must not kill the run
            print(f"[warn] signature not inserted: {exc}")

    page.draw_line(
        fitz.Point(left, baseline + 6),
        fitz.Point(left + line_width, baseline + 6),
        color=(0, 0, 0),
        width=1,
    )
    baseline += 22

    for i, line in enumerate(certifier.detail_lines()):
        page.insert_text(
            (left, baseline),
            line,
            fontname=fontname,
            fontsize=11 if i == 0 else 9,
        )
        baseline += 14


# --------------------------------------------------------------------------
# main entry point
# --------------------------------------------------------------------------
def certify_pdf(
    input_path: str | Path,
    output_path: Optional[str | Path] = None,
    *,
    certifier: Certifier,
    signature: Optional[str | Path] = None,
    date: Optional[str] = None,
    strip_blanks: bool = True,
    stamp_pages: bool = True,
    blank_threshold: float = 0.01,
    owner_password: Optional[str] = None,
    block_pos: Optional[Sequence[float]] = None,
    stamp_pos: Optional[Sequence[float]] = None,
) -> dict:
    """Certify ``input_path`` and save a read-only copy.

    The output opens with no password and allows printing only — editing,
    annotating, copying text and reassembling pages are blocked. The owner
    password is generated unless you pass one; it is returned so the caller can
    store it, and it is never written into the PDF metadata.
    """
    input_path = Path(input_path)
    output_path = Path(output_path or input_path.with_name(f"{input_path.stem}_certified.pdf"))
    date = date or _dt.date.today().strftime("%-d %b %Y")
    owner_password = owner_password or secrets.token_urlsafe(12)

    doc = fitz.open(input_path)
    pages_before = len(doc)
    removed = strip_blank_pages(doc, blank_threshold) if strip_blanks else []
    pages_after = len(doc)

    if pages_after == 0:
        doc.close()
        raise ValueError("every page was blank — refusing to produce an empty PDF")

    fontname = _cjk_font(doc)

    if block_pos is None:
        detected = find_blank_region(doc[-1], bottom_bias=0.3)
        block_pos = detected or (0.5, 0.88)
    if stamp_pos is None:
        stamp_pos = find_blank_region(doc[0]) or (0.5, 0.5)

    for index, page in enumerate(doc):
        if stamp_pages:
            spot = stamp_pos
            if spot == (0.5, 0.5):
                spot = find_blank_region(page) or (0.5, 0.5)
            add_stamp(page, fontname=fontname, pos=spot)
        if index == pages_after - 1:
            add_cert_block(
                page,
                certifier,
                date,
                signature=str(signature) if signature else None,
                fontname=fontname,
                pos=block_pos,
            )

    doc.save(
        output_path,
        garbage=3,
        deflate=True,
        encryption=fitz.PDF_ENCRYPT_AES_256,
        user_pw="",
        owner_pw=owner_password,
        permissions=fitz.PDF_PERM_PRINT,
    )
    doc.close()

    return {
        "output": str(output_path),
        "pages_in": pages_before,
        "pages_out": pages_after,
        "blank_pages_removed": removed,
        "stamped": stamp_pages,
        "permissions": "print-only",
        "owner_password": owner_password,
    }
