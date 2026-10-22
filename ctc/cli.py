"""Command line entry point: ``ctc input.pdf --name "A. Person"``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import Certifier, certify_pdf


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctc",
        description=(
            "Certify a PDF as a true copy: remove blank pages, stamp every page, "
            "add a signed certification block, lock the result to print-only."
        ),
    )
    parser.add_argument("input", help="input PDF")
    parser.add_argument("-o", "--out", help="output PDF (default: <input>_certified.pdf)")
    parser.add_argument("--name", required=True, help="certifier name, printed under the signature line")
    parser.add_argument("--membership", default="", help="membership / registration number")
    parser.add_argument("--institution", default="", help="institute or firm the certifier belongs to")
    parser.add_argument("--capacity", default="", help="capacity, e.g. 'Director'")
    parser.add_argument("--company", default="", help="company the copy is issued for")
    parser.add_argument("--address", default="", help="contact address")
    parser.add_argument("--phone", default="", help="contact phone")
    parser.add_argument("--heading", default="Certified True Copy", help="heading text")
    parser.add_argument("--statement", default="", help="replace the heading with a full statement")
    parser.add_argument("--sig", help="signature image (PNG) placed above the line")
    parser.add_argument("--date", help="date printed in the block (default: today, e.g. '5 Aug 2026')")
    parser.add_argument(
        "--bearer-signed",
        action="store_true",
        help="add the 'bearer signed in my presence' sentence (identity documents)",
    )
    parser.add_argument("--keep-blanks", action="store_true", help="do not remove blank pages")
    parser.add_argument("--no-stamp", action="store_true", help="skip the diagonal stamp")
    parser.add_argument(
        "--owner-password",
        help="password that unlocks editing (default: random, printed once)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    source = Path(args.input)
    if not source.exists():
        print(f"error: no such file: {source}", file=sys.stderr)
        return 2

    certifier = Certifier(
        name=args.name,
        membership=args.membership,
        institution=args.institution,
        capacity=args.capacity,
        company=args.company,
        address=args.address,
        phone=args.phone,
        heading=args.heading,
        statement=args.statement,
        bearer_signed=args.bearer_signed,
    )

    try:
        result = certify_pdf(
            source,
            args.out,
            certifier=certifier,
            signature=args.sig,
            date=args.date,
            strip_blanks=not args.keep_blanks,
            stamp_pages=not args.no_stamp,
            owner_password=args.owner_password,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"written : {result['output']}")
    print(f"pages   : {result['pages_in']} -> {result['pages_out']}")
    if result["blank_pages_removed"]:
        print(f"removed : blank page(s) {result['blank_pages_removed']}")
    print(f"lock    : {result['permissions']} (opens without a password)")
    if not args.owner_password:
        print(f"owner pw: {result['owner_password']}  <- keep this if you ever need to edit")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
