"""
Certification Guidance Parser — reads ANY certification guidance document
(ASCENT, HKCGI, bank-specific, etc.) and extracts the certification
requirements so the CTC tool can automatically insert what is needed.

Usage:
    from ctc.guidance import parse_requirements, summarize
    reqs = parse_requirements(guidance_text)
    print(summarize(reqs))
"""
import re
from dataclasses import dataclass, field


@dataclass
class CertificationRequirements:
    statement_wording: str = (
        "This is to certify that this document is a true copy of the original."
    )
    requires_signature_and_date: bool = True
    requires_position: bool = True
    requires_institution: bool = True
    requires_membership_number: bool = True
    requires_contact_address: bool = True
    requires_phone: bool = True
    requires_stamp: bool = False
    requires_bearer_signed_clause: bool = False
    requires_english_translation: bool = False
    max_age_months: int | None = 3
    allowed_certifier_roles: list[str] = field(default_factory=lambda: [
        "lawyer", "notary public", "accountant", "corporate secretary",
        "member of the judiciary", "senior civil servant",
        "director or manager of regulated financial institution",
    ])
    source_name: str = ""

    def to_dict(self) -> dict:
        return {
            "statement_wording": self.statement_wording,
            "requires_signature_and_date": self.requires_signature_and_date,
            "requires_position": self.requires_position,
            "requires_institution": self.requires_institution,
            "requires_membership_number": self.requires_membership_number,
            "requires_contact_address": self.requires_contact_address,
            "requires_phone": self.requires_phone,
            "requires_stamp": self.requires_stamp,
            "requires_bearer_signed_clause": self.requires_bearer_signed_clause,
            "requires_english_translation": self.requires_english_translation,
            "max_age_months": self.max_age_months,
            "allowed_certifier_roles": self.allowed_certifier_roles,
            "source_name": self.source_name,
        }


def _has(text: str, *patterns: str) -> bool:
    return any(re.search(p, text) for p in patterns)


def parse_requirements(text: str, source_name: str = "") -> CertificationRequirements:
    """Extract certification requirements from guidance text (keyword rules)."""
    t = (text or "").lower()
    req = CertificationRequirements(source_name=source_name)

    # Statement wording — the crucial "true copy of the original" phrase
    if _has(t, r"true copy of the original", r"true copy"):
        req.statement_wording = (
            "This is to certify that this document is a true copy of the original."
        )

    # Signed and dated
    req.requires_signature_and_date = _has(
        t, r"sign\w*\s+and\s+dat\w*", r"dat\w*\s+and\s+sign\w*",
        r"signed and dated",
    )

    # Position / capacity of certifying person
    req.requires_position = _has(t, r"position", r"capa?city")

    # Professional designation institution
    req.requires_institution = _has(
        t, r"designation institution", r"professional designation", r"institution",
    )

    # Membership / designation number
    req.requires_membership_number = _has(
        t, r"membership", r"professional designation number", r"designation number",
    )

    # Contact address
    req.requires_contact_address = _has(
        t, r"contact address", r"address and phone", r"contact.*address",
    )

    # Phone number
    req.requires_phone = _has(t, r"phone number", r"contact.*phone", r"tel(ephone)?")

    # Stamp / seal
    req.requires_stamp = _has(t, r"stamp", r"seal of")

    # Bearer signed in my presence (for ID docs without signature)
    req.requires_bearer_signed_clause = _has(
        t, r"signed in my presence", r"bearer of the document has signed",
        r"bearer.*sign\w*",
    )

    # English translation
    req.requires_english_translation = _has(t, r"english translation", r"notaris\w*\s+translation")

    # Max age of certification
    m = re.search(r"(?:no more than|within|not more than|prior to)\s*(?:three|3)\s*months?", t)
    if m:
        req.max_age_months = 3
    else:
        m = re.search(r"(\d+)\s*months?", t)
        if m:
            req.max_age_months = int(m.group(1))

    # Allowed certifying bodies
    roles = []
    role_map = [
        (r"lawyer", "lawyer"),
        (r"notary public|notary", "notary public"),
        (r"accountant", "accountant"),
        (r"corporate secretary", "corporate secretary"),
        (r"judiciary|judge", "member of the judiciary"),
        (r"senior civil servant", "senior civil servant"),
        (r"regulated financial institution", "director or manager of regulated financial institution"),
    ]
    for pat, label in role_map:
        if re.search(pat, t):
            roles.append(label)
    if roles:
        req.allowed_certifier_roles = roles

    return req


def summarize(req: CertificationRequirements) -> list[str]:
    """Human-readable checklist of detected requirements."""
    lines = []
    lines.append(f"📋 {req.source_name or 'Certification'} 要求分析:")
    lines.append(f"  • 聲明：{req.statement_wording}")
    if req.requires_signature_and_date:
        lines.append("  • 需要簽名 + 日期")
    if req.requires_position:
        lines.append("  • 需要職位/身份（position/capacity）")
    if req.requires_institution:
        lines.append("  • 需要專業資格機構")
    if req.requires_membership_number:
        lines.append("  • 需要 Membership 號碼")
    if req.requires_contact_address:
        lines.append("  • 需要聯絡地址")
    if req.requires_phone:
        lines.append("  • 需要電話")
    if req.requires_stamp:
        lines.append("  • 需要蓋印/印章")
    if req.requires_bearer_signed_clause:
        lines.append("  • 需要 'bearer signed in my presence' 聲明")
    if req.requires_english_translation:
        lines.append("  • 需要英文翻譯（或經公證）")
    if req.max_age_months:
        lines.append(f"  • 認證有效期：{req.max_age_months} 個月內")
    lines.append(f"  • 可認證身份：{'、'.join(req.allowed_certifier_roles)}")
    return lines


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        import fitz
        doc = fitz.open(sys.argv[1])
        text = "\n".join(p.get_text() for p in doc)
        doc.close()
        reqs = parse_requirements(text, source_name=sys.argv[1])
        print("\n".join(summarize(reqs)))
    else:
        sample = (
            "The certified document must be signed and dated; indicate the printed name "
            "and position of certifying person, professional designation institution and "
            "designation number, and provide their contact address and phone number; "
            "include a stamp if applicable; include the statement 'true copy of the original'; "
            "bearer must sign in my presence. Not older than three (3) months."
        )
        reqs = parse_requirements(sample, source_name="sample")
        print("\n".join(summarize(reqs)))
