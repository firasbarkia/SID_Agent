"""Identity removal for candidate free text, and stable per-company pseudonyms."""

import hashlib
import hmac
import re

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
LINK = re.compile(r"(?:https?://|www\.)\S+", re.I)
PHONE = re.compile(r"\+?\d[\d\s().-]{6,}\d")
YEARS = re.compile(r"(?:19|20)\d\d\s*[-–/]\s*(?:19|20)\d\d")


def _phone(found):
    text = found.group(0)
    if sum(c.isdigit() for c in text) < 8 or YEARS.fullmatch(text.strip()):
        return text
    return "[phone]"


def scrub(text: str, name: str = "") -> str:
    """Remove emails, links, phone numbers and the person's own name tokens."""
    text = LINK.sub("[link]", text)
    text = EMAIL.sub("[email]", text)
    text = PHONE.sub(_phone, text)
    tokens = [t for t in re.findall(r"\w+", name or "") if len(t) >= 2]
    if tokens:
        pattern = r"\b(?:" + "|".join(re.escape(t) for t in tokens) + r")\b"
        text = re.sub(pattern, "[name]", text, flags=re.I)
        text = re.sub(r"\[name\](?:\s+\[name\])+", "[name]", text)
    return text


class Anonymizer:
    """Pseudonyms are stable for one company and unlinkable across companies."""

    def __init__(self, secret: str):
        if not secret:
            raise ValueError("anonymization_not_configured")
        self.key = hmac.new(secret.encode(), b"sid-candidate-ref-v1", hashlib.sha256).digest()

    def pseudonym(self, company_id: str, candidate_id: str) -> str:
        message = f"{company_id}\x00{candidate_id}".encode()
        return "C-" + hmac.new(self.key, message, hashlib.sha256).hexdigest()[:10].upper()
