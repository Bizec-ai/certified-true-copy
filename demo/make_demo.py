"""Build a small demo bundle so the certifier can be tried without a real file.

    python3 demo/make_demo.py            # writes demo/demo_bundle_original.pdf

The bundle deliberately contains a mostly-blank page in the middle: the tool
strips those, which is the first thing a reviewer should see happen.
"""

from pathlib import Path

import fitz

OUT = Path(__file__).resolve().parent / "demo_bundle_original.pdf"
W, H = 595, 842


def text_page(doc, title, lines, footer):
    page = doc.new_page(width=W, height=H)
    page.insert_text((60, 80), title, fontsize=16, fontname="hebo")
    page.draw_line(fitz.Point(60, 92), fitz.Point(W - 60, 92))
    y = 120
    for line in lines:
        page.insert_text((60, y), line, fontsize=10.5, fontname="helv")
        y += 17
    page.insert_text((60, H - 50), footer, fontsize=8, fontname="helv")
    return page


doc = fitz.open()

text_page(
    doc,
    "SAMPLE HOLDINGS LIMITED",
    [
        "Certificate of Incorporation extract",
        "",
        "Company number: 0000000",
        "Date of incorporation: 1 January 2020",
        "",
        "This extract is prepared for delivery to a bank in support of a",
        "know-your-customer review. It is a specimen and carries no legal",
        "effect whatsoever.",
    ],
    "SAMPLE HOLDINGS LIMITED - page 1",
)

text_page(doc, "Register of Members", [
    "1.  CHAN Tai Man        1,000 ordinary shares",
    "2.  CHAN Siu Ming         500 ordinary shares",
    "",
    "Total issued: 1,500 ordinary shares",
], "SAMPLE HOLDINGS LIMITED - page 2")

# intentionally blank scan page - the tool removes it
doc.new_page(width=W, height=H)

text_page(doc, "Register of Charges", [
    "No charges are registered against the company at the date of this",
    "extract.",
], "SAMPLE HOLDINGS LIMITED - page 4")

doc.save(OUT)
doc.close()
print(f"wrote {OUT} (4 pages, one blank)")
