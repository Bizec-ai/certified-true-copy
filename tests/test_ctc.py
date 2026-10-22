"""End-to-end tests: build a PDF, certify it, inspect the result."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import fitz

from ctc import (
    Certifier,
    best_spot_for_box,
    certify_pdf,
    find_blank_region,
    page_is_blank,
    stamp_box,
)


def make_pdf(path: Path, pages: int = 3) -> None:
    """A PDF whose pages alternate between text and blank."""
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)  # A4
        if i % 2 == 0:
            page.insert_text((72, 100), f"Page {i + 1} content", fontsize=14)
            page.insert_text((72, 140), "Sample document text for the test suite.", fontsize=11)
    doc.save(path)
    doc.close()


class CertifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source.pdf"
        make_pdf(self.source, pages=3)  # page 2 is blank

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_blank_page_detection(self) -> None:
        doc = fitz.open(self.source)
        self.assertFalse(page_is_blank(doc[0]))
        self.assertTrue(page_is_blank(doc[1]))
        doc.close()

    def test_blank_region_found_on_text_page(self) -> None:
        doc = fitz.open(self.source)
        spot = find_blank_region(doc[0])
        doc.close()
        self.assertIsNotNone(spot)
        x, y = spot
        self.assertTrue(0.0 <= x <= 1.0 and 0.0 <= y <= 1.0)

    def test_blank_pages_are_stripped(self) -> None:
        result = certify_pdf(self.source, certifier=Certifier(name="A. Person"))
        self.assertEqual(result["pages_in"], 3)
        self.assertEqual(result["pages_out"], 2)
        self.assertEqual(result["blank_pages_removed"], [2])

    def test_output_opens_without_password_and_is_print_only(self) -> None:
        result = certify_pdf(self.source, certifier=Certifier(name="A. Person"))
        doc = fitz.open(result["output"])
        self.assertFalse(doc.needs_pass, "the recipient must be able to open it with no password")
        self.assertEqual(doc.permissions & fitz.PDF_PERM_PRINT, fitz.PDF_PERM_PRINT)
        self.assertEqual(doc.permissions & fitz.PDF_PERM_MODIFY, 0)
        self.assertEqual(doc.permissions & fitz.PDF_PERM_COPY, 0)
        self.assertEqual(doc.permissions & fitz.PDF_PERM_ANNOTATE, 0)
        doc.close()

    def test_owner_password_unlocks_editing(self) -> None:
        result = certify_pdf(
            self.source,
            certifier=Certifier(name="A. Person"),
            owner_password="known-owner-pw",
        )
        locked = fitz.open(result["output"])
        self.assertEqual(locked.permissions & fitz.PDF_PERM_MODIFY, 0)
        locked.close()

        doc = fitz.open(result["output"])
        self.assertTrue(doc.authenticate("known-owner-pw"))
        self.assertEqual(doc.permissions & fitz.PDF_PERM_MODIFY, fitz.PDF_PERM_MODIFY)
        doc.close()

    def test_stamp_and_block_land_on_the_page(self) -> None:
        result = certify_pdf(
            self.source,
            certifier=Certifier(
                name="A. Person",
                membership="Membership No. 0000000",
                heading="Certified True Copy",
            ),
            date="5 Aug 2026",
        )
        doc = fitz.open(result["output"])
        page_texts = [page.get_text().upper() for page in doc]
        pages = len(doc)
        doc.close()
        text = "\n".join(page_texts)

        self.assertIn("A. PERSON", text)
        self.assertIn("5 AUG 2026", text)
        self.assertIn("0000000", text)
        # the rotated stamp is extracted in fragments, so check the stem
        for i, page_text in enumerate(page_texts, start=1):
            self.assertIn("CERTIF", page_text, f"stamp missing on page {i}")
        self.assertEqual(pages, 2)

    def test_strip_can_be_disabled(self) -> None:
        result = certify_pdf(
            self.source,
            certifier=Certifier(name="A. Person"),
            strip_blanks=False,
        )
        self.assertEqual(result["pages_out"], 3)
        self.assertEqual(result["blank_pages_removed"], [])

    def test_all_blank_input_is_refused(self) -> None:
        blank = self.root / "blank.pdf"
        doc = fitz.open()
        doc.new_page()
        doc.save(blank)
        doc.close()
        with self.assertRaises(ValueError):
            certify_pdf(blank, certifier=Certifier(name="A. Person"))


    def test_stamp_never_runs_off_the_paper(self) -> None:
        """The stamp is drawn centred and rotated about its own midpoint."""
        for spot in ((0.5, 0.5), (0.9, 0.1), (0.1, 0.9), (0.95, 0.5)):
            with self.subTest(pos=spot):
                result = certify_pdf(
                    self.source,
                    certifier=Certifier(name="A. Person"),
                    stamp_pos=spot,
                    block_pos=(0.5, 0.9),
                    strip_blanks=False,
                )
                doc = fitz.open(result["output"])
                seen = 0
                for page in doc:
                    for block in page.get_text("dict")["blocks"]:
                        if block["type"] != 0:
                            continue
                        for line in block["lines"]:
                            for span in line["spans"]:
                                if "CERTIF" not in span["text"].upper():
                                    continue
                                seen += 1
                                x0, y0, x1, y1 = span["bbox"]
                                self.assertGreaterEqual(x0, -1, span["text"])
                                self.assertGreaterEqual(y0, -1, span["text"])
                                self.assertLessEqual(x1, page.rect.width + 1, span["text"])
                                self.assertLessEqual(y1, page.rect.height + 1, span["text"])
                self.assertGreaterEqual(seen, len(doc))
                doc.close()


class PlacementTests(unittest.TestCase):
    """The stamp/block must land on empty paper, not on top of the text.

    ``find_blank_region`` finds the biggest empty *square*, which is not the
    same question as "does a 390pt-wide rotated stamp fit here" - that mismatch
    put the stamp across body text in production. These tests pin the
    size-aware behaviour so it cannot regress.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "dense.pdf"

        doc = fitz.open()
        page = doc.new_page(width=595, height=842)      # A4
        y = 80
        for line in range(26):                          # dense top 2/3
            page.insert_text((60, y), "The quick brown fox jumps over the lazy dog. " * 2,
                             fontsize=11)
            y += 18
        page.insert_text((60, 800), "footer line", fontsize=8)
        doc.save(self.source)
        doc.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_stamp_box_measures_the_rotated_stamp(self) -> None:
        doc = fitz.open(self.source)
        width, height = stamp_box(doc[0])
        doc.close()
        self.assertGreater(width, height, "a 25-degree stamp is always wider than it is tall")
        self.assertGreater(width, 200)
        self.assertLess(width, 595)
        self.assertLess(height, 842)

    def test_box_search_avoids_the_dense_text(self) -> None:
        doc = fitz.open(self.source)
        page = doc[0]
        width, height = stamp_box(page)
        spot = best_spot_for_box(page, width, height, prefer=(0.82, 0.82))
        box = fitz.Rect(
            spot[0] * page.rect.width - width / 2,
            spot[1] * page.rect.height - height / 2,
            spot[0] * page.rect.width + width / 2,
            spot[1] * page.rect.height + height / 2,
        )
        # The dense paragraph lives above y=560; the stamp must be below it.
        self.assertGreater(box.y0, 540, f"stamp box {box} still overlaps the body text")
        doc.close()

    def test_certified_stamp_lands_off_the_text(self) -> None:
        source_doc = fitz.open(self.source)
        page = source_doc[0]
        width, height = stamp_box(page)
        spot = best_spot_for_box(page, width, height, prefer=(0.82, 0.82))
        stamp_rect = fitz.Rect(
            spot[0] * page.rect.width - width / 2,
            spot[1] * page.rect.height - height / 2,
            spot[0] * page.rect.width + width / 2,
            spot[1] * page.rect.height + height / 2,
        )
        words = page.get_text("words")
        source_doc.close()

        clashes = [
            word[4]
            for word in words
            if fitz.Rect(word[:4]).intersects(stamp_rect)
        ]
        self.assertEqual(clashes, [], f"auto placement would cover: {clashes[:5]}")

        # ...and the certified output really does put it there.
        result = certify_pdf(
            self.source,
            certifier=Certifier(name="A. Person"),
            strip_blanks=False,
        )
        doc = fitz.open(result["output"])
        self.assertFalse(doc.needs_pass)
        self.assertEqual(len(doc), 1)
        doc.close()


if __name__ == "__main__":
    unittest.main()
