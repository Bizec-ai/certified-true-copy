"""End-to-end tests: build a PDF, certify it, inspect the result."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import fitz

from ctc import Certifier, certify_pdf, find_blank_region, page_is_blank


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


if __name__ == "__main__":
    unittest.main()
