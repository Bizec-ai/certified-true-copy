"""Certify a PDF as a true copy.

The three things that make a scanned bundle look "certified" instead of
"photocopied": the blank pages are gone, every page carries a stamp, and the
last page ends with a signed certification block. This module does those three
things and then locks the result read-only so the copy cannot be edited after
it leaves your hands.
"""

from __future__ import annotations

import datetime as _dt
import math
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
    "best_spot_for_box",
    "stamp_box",
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


def _ink_integral(
    page: fitz.Page,
    dpi: int = 72,
) -> tuple[int, int, "callable"]:
    """Downsample a page 4x and return (cols, rows, ink_in).

    ``ink_in(x0, y0, x1, y1)`` is an O(1) ink count over a pixel-aligned
    rectangle, backed by an integral image. Both the blank-square search and
    the size-aware box search run on this, so they stay fast on scanned pages.
    """
    pix = page.get_pixmap(dpi=dpi)
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

    return cols, rows, ink_in


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

    Note: a big empty square is not proof that a wide object fits — use
    :func:`best_spot_for_box` when you know the real size of what you are
    placing.
    """
    cols, rows, ink_in = _ink_integral(page)

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


def stamp_box(
    page: fitz.Page,
    text: str = STAMP_TEXT,
    size: float = 34.0,
    angle: int = 25,
) -> tuple[float, float]:
    """On-paper width and height of the rotated diagonal stamp, in points.

    Mirrors the geometry in :func:`add_stamp`: the text is shrunk to fit the
    page diagonal first, then its rotated bounding box is measured. Feed the
    result to :func:`best_spot_for_box` so the stamp is placed where it
    actually fits, not where an equally-sized square happens to be empty.
    """
    rect = page.rect
    fontname = _cjk_font(page.parent) if page.parent is not None else "helv"
    font = fitz.Font(fontname)

    length = font.text_length(text, fontsize=size)
    room = (rect.width**2 + rect.height**2) ** 0.5 * 0.60
    if length > room:
        size = max(10.0, size * room / length)
        length = font.text_length(text, fontsize=size)

    rad = math.radians(angle)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    return (
        length * cos_a + size * sin_a,
        length * sin_a + size * cos_a,
    )


def best_spot_for_box(
    page: fitz.Page,
    box_w_pt: float,
    box_h_pt: float,
    prefer: Sequence[float] = (0.82, 0.82),
    tol: float = 0.004,
) -> tuple[float, float]:
    """Find the emptiest place on a page for a box of a *given real size*.

    A large empty square says nothing about whether a 390pt-wide rotated stamp
    fits there — that mistake puts the stamp on top of body text. This slides
    the real box size over the page, keeps the windows with the least ink, and
    among those picks the one closest to ``prefer`` (relative coordinates), so
    a mostly-empty bottom-right page still puts the stamp bottom-right.

    Returns the relative centre ``(cx, cy)``.
    """
    rect = page.rect
    cols, rows, ink_in = _ink_integral(page)
    box_w = max(1, min(cols, int(round(box_w_pt / rect.width * cols))))
    box_h = max(1, min(rows, int(round(box_h_pt / rect.height * rows))))
    step = max(1, min(box_w, box_h) // 6)

    scores: list[tuple[float, int, int]] = []
    for y0 in range(0, rows - box_h + 1, step):
        for x0 in range(0, cols - box_w + 1, step):
            ink = ink_in(x0, y0, x0 + box_w, y0 + box_h)
            scores.append((ink / float(box_w * box_h), x0, y0))
    if not scores:
        return tuple(prefer)

    # Ink density alone is not enough: a thin footer line is a tiny fraction of
    # the box area yet still gets covered by the stamp.  So score first, then
    # reject any window whose rectangle actually touches a word box.
    words = [fitz.Rect(word[:4]) for word in page.get_text("words")]
    scale_x = rect.width / cols
    scale_y = rect.height / rows

    def centre(score: tuple[float, int, int]) -> tuple[float, float]:
        return ((score[1] + box_w / 2.0) * scale_x, (score[2] + box_h / 2.0) * scale_y)

    def distance(score: tuple[float, int, int]) -> float:
        cx, cy = centre(score)
        return ((cx - prefer[0] * rect.width) / rect.width) ** 2 + (
            (cy - prefer[1] * rect.height) / rect.height
        ) ** 2

    ordered = sorted(scores, key=lambda s: (s[0], distance(s)))

    def box_for(score: tuple[float, int, int]) -> fitz.Rect:
        cx, cy = centre(score)
        return fitz.Rect(
            cx - box_w_pt / 2.0,
            cy - box_h_pt / 2.0,
            cx + box_w_pt / 2.0,
            cy + box_h_pt / 2.0,
        )

    clean = [s for s in ordered[:400] if not any(box_for(s).intersects(w) for w in words)]
    if clean:
        floor = min(s[0] for s in clean)
        pool = [s for s in clean if s[0] <= floor + tol]
        pick = min(pool, key=distance)
    else:
        pick = ordered[0]

    cx, cy = centre(pick)
    return (cx / rect.width, cy / rect.height)


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
    """Draw the diagonal stamp, centred on ``pos`` (relative coords, default centre).

    The text is measured first and shrunk if it would not fit the page, then
    rotated about its own baseline midpoint so the glyph body stays inside the
    paper instead of running off the right edge.
    """
    rect = page.rect
    cx, cy = rect.width * (pos or (0.5, 0.5))[0], rect.height * (pos or (0.5, 0.5))[1]
    font = fitz.Font(fontname)

    length = font.text_length(text, fontsize=size)
    room = (rect.width**2 + rect.height**2) ** 0.5 * 0.60
    if length > room:
        size = max(10.0, size * room / length)
        length = font.text_length(text, fontsize=size)

    baseline = cy + size * 0.28           # optical centre of the glyph body

    # Rotation happens about the baseline midpoint, so work out the extents of
    # the rotated text and slide the whole thing back inside the paper.
    rad = math.radians(angle)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    asc, desc = 0.80 * size, 0.20 * size
    xs, ys = [], []
    for dx, dy in (
        (-length / 2, -asc),
        (length / 2, -asc),
        (length / 2, desc),
        (-length / 2, desc),
    ):
        xs.append(dx * cos_a - dy * sin_a)
        ys.append(dx * sin_a + dy * cos_a)
    margin = 18.0
    bx = min(max(cx, margin - min(xs)), rect.width - margin - max(xs))
    by = min(max(baseline, margin - min(ys)), rect.height - margin - max(ys))
    pivot = fitz.Point(bx, by)
    writer = fitz.TextWriter(rect)
    writer.append((bx - length / 2, by), text, font=font, fontsize=size)
    writer.write_text(
        page,
        color=tuple(color),
        opacity=opacity,
        morph=(pivot, fitz.Matrix(angle)),
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
    stamp_text: str = STAMP_TEXT,
    stamp_size: float = 34.0,
    stamp_angle: int = 25,
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
        # Auto placement for the block: measure the real block size and slide
        # it over the last page, preferring the lower-left where a signature
        # block belongs. Falls back to the old square search, then to a corner.
        try:
            block_pos = best_spot_for_box(doc[-1], 312.0, 162.0, prefer=(0.22, 0.80))
        except Exception:
            detected = find_blank_region(doc[-1], bottom_bias=0.3)
            block_pos = detected or (0.5, 0.88)

    for index, page in enumerate(doc):
        if stamp_pages:
            spot = stamp_pos
            if spot is None:
                # Size-aware: the rotated stamp is wide, so search with its
                # real on-paper dimensions instead of an arbitrary square.
                try:
                    box_w, box_h = stamp_box(page, stamp_text, size=stamp_size, angle=stamp_angle)
                    # 12pt of clearance: the real glyph box is a few points
                    # tighter than the rotated-text estimate, and a stamp that
                    # merely touches a footer still looks sloppy.
                    spot = best_spot_for_box(page, box_w + 12, box_h + 12,
                                             prefer=(0.82, 0.82))
                except Exception:
                    spot = find_blank_region(page) or (0.5, 0.5)
            add_stamp(
                page,
                text=stamp_text,
                fontname=fontname,
                pos=spot,
                size=stamp_size,
                angle=stamp_angle,
            )
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
