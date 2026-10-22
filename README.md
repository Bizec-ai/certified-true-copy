# certified-true-copy

Turn a scanned PDF into a **certified true copy** — the thing you hand to a bank, a registry, or a counterparty when they want "a copy, certified".

Four steps, in this order:

1. **Drop blank pages.** Duplex scanners produce a blank back side for every sheet; a certified bundle should not contain them.
2. **Stamp every page.** A translucent diagonal `CERTIFIED TRUE COPY` stamp, placed over the largest empty area of each page so it does not bury text.
3. **Certify on the last page.** A block with the heading, date, signature line, and the certifier's name and membership/registration line.
4. **Lock it.** AES-256 with an empty user password (so anyone can open and print) and print-only permissions — no editing, no annotating, no text copying, no page reassembly.

```bash
pip install pymupdf
python -m ctc.cli bundle.pdf --name "A. Person" --membership "Membership No. 0000000"
```

```
written : bundle_certified.pdf
pages   : 17 -> 9
removed : blank page(s) [2, 4, 6, 8, 10, 12, 14, 16]
lock    : print-only (opens without a password)
owner pw: 4sKd...  <- keep this if you ever need to edit
```

## Why this exists

Company secretarial and compliance work runs on paper trails: certified copies of registers, resolutions, identity documents, audited accounts. Doing it by hand means opening a 30-page scan, deleting the blank backs one at a time, stamping, typing the same block, and exporting — for every copy, every time. It is mechanical, it is easy to get subtly wrong, and it is the sort of thing that quietly eats an afternoon.

This is the automated version. It is deliberately small: one file of drawing logic, one CLI, no service, no database, no cloud. The document never leaves the machine it runs on.

## Python API

```python
from ctc import Certifier, certify_pdf

result = certify_pdf(
    "register.pdf",
    "register_certified.pdf",
    certifier=Certifier(
        name="A. Person",
        membership="Membership No. 0000000",
        heading="Certified True Copy",
    ),
    signature="signature.png",
    date="5 Aug 2026",
)
print(result["pages_out"], result["owner_password"])
```

`Certifier` renders only the fields you fill in, so the same class produces both the short form (`Certified True Copy` / `Date:` / signature / name / membership) and the verbose form some institutions demand (institution, capacity, company, address, phone).

| Argument | Default | Notes |
| --- | --- | --- |
| `strip_blanks` | `True` | Remove pages with no text layer and almost no ink |
| `stamp_pages` | `True` | Diagonal stamp on every remaining page |
| `blank_threshold` | `0.01` | Ink fraction below which an image-only page counts as blank |
| `owner_password` | random | Printed once by the CLI; returned by the API |
| `block_pos`, `stamp_pos` | auto | Relative (0-1) centre, detected from empty space |

## What "locked" does and does not mean

`permissions=PDF_PERM_PRINT` with an owner password means a PDF reader will refuse to edit, annotate, extract text, or reorder pages. It is a speed bump for honest people and a clear signal of intent — **it is not cryptography against a determined party**, because anyone holding the owner password, or a PDF library that ignores permission flags, can still change the file. If you need tamper evidence rather than tamper prevention, hash the output and record the hash somewhere the recipient can check (or put it on a signature/verification service).

## Limitations

- The blank-page test trusts the text layer first and ink coverage second. A scanned page with faint pencil marks but no text layer may be dropped — pass `--keep-blanks` when the source is a low-quality scan.
- No OCR, no redaction, no signature validation. It draws and it locks.
- The signature image is placed above the line at a fixed height; crop your signature to roughly 3:1 rather than relying on the tool to resize it.

## Tests

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
```

The suite builds its own PDFs (text page / blank page / all-blank), then asserts blank removal, stamp placement per page, block contents, print-only permissions, and that the owner password is the only way back to an editable document.

## Licence

MIT — see [LICENSE](LICENSE).
