"""
Encrypted-file detection — explains a parse failure when the real cause is
"the file is password-protected", instead of leaving the user with a cryptic
"document is not valid".

Called by the pipeline ONLY AFTER a parse has already failed, so normal files
pay zero extra cost — the detection round-trip happens only on the (rare)
failure path.

Design — detect by ENCRYPTION MECHANISM, not by file extension. There are only
three encryption schemes across everything DocIngest ingests, so three checks
cover all current and future formats that use one of them (vs. a brittle
per-extension if-chain):

  1. PDF standard encryption          → pymupdf `needs_pass`         (definite)
  2. MS-OFFCRYPTO (OLE + EncryptedPackage stream) → olefile          (definite)
     Covers EVERY encrypted MS Office file — modern OOXML (docx/xlsx/pptx) AND
     legacy binary (xls/doc/ppt) alike: both are re-wrapped as an OLE compound
     document carrying an `EncryptedPackage` stream when password-protected.
     Keyed on that stream (not just "is it OLE"), so a normal legacy .xls — an
     OLE file with NO EncryptedPackage — is correctly NOT flagged.
  3. ZIP entry encryption             → zip flag bit 0x1            (definite)

All three checks are content-based (read a header / stream), not
extension-based, so a mislabelled file is still judged correctly. Returns a
human-readable (Japanese, matching the GUI) reason, or None when not encrypted.
NEVER raises — detection is a diagnostic nicety; on any error it returns None
and the caller keeps the original parse error.
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

# OLE/CDF compound-document magic. Both legacy binary Office and any
# password-encrypted MS Office file start with these 8 bytes.
_OLE_MAGIC = bytes([0xD0, 0xCF, 0x11, 0xE0, 0xA1, 0xB1, 0x1A, 0xE1])

_REASON = (
    "このファイルはパスワード保護されています。"
    "パスワードを解除してから再度お試しください。"
)


def detect_encrypted(file_path: Path | str) -> str | None:
    """
    Best-effort: is this parse failure caused by the file being encrypted?

    Tries each encryption mechanism in turn (PDF → MS-OFFCRYPTO → ZIP),
    content-based so it ignores the extension. Returns the reason string on a
    hit, else None. Never raises.
    """
    path = Path(file_path)
    try:
        header = _read_header(path)
        if header is None:
            return None

        # 1. PDF — only thing that starts with "%PDF".
        if header.startswith(b"%PDF"):
            if _pdf_needs_password(path):
                return _REASON
            return None

        # 2. MS-OFFCRYPTO — encrypted Office (modern OR legacy) is an OLE
        #    container with an EncryptedPackage stream.
        if header == _OLE_MAGIC:
            if _ole_is_encrypted(path):
                return _REASON
            return None

        # 3. ZIP entry encryption (covers .zip; a normal OOXML is also a zip
        #    but is never flagged here — its entries aren't encrypted, the whole
        #    package is, which lands in the OLE branch above).
        if header.startswith(b"PK\x03\x04"):
            if _zip_is_encrypted(path):
                return _REASON
            return None
    except Exception as e:  # noqa: BLE001 — util contract is "never raise"
        logger.debug("Encryption detection failed for %s: %s", path.name, e)
    return None


def _read_header(path: Path, n: int = 8) -> bytes | None:
    """First n bytes, or None if unreadable."""
    try:
        with path.open("rb") as f:
            return f.read(n)
    except OSError:
        return None


def _pdf_needs_password(path: Path) -> bool:
    """Definite: pymupdf flags a PDF that needs a password and hasn't got one.
    Keyed on needs_pass (not is_encrypted) so an empty-owner-password PDF that
    opens fine is not a false positive."""
    try:
        import pymupdf
    except ImportError:
        return False
    doc = pymupdf.open(str(path))
    try:
        return bool(getattr(doc, "needs_pass", False))
    finally:
        doc.close()


def _ole_is_encrypted(path: Path) -> bool:
    """Definite: a password-encrypted MS Office file (any era) is an OLE
    compound document containing an `EncryptedPackage` stream (MS-OFFCRYPTO).
    A normal legacy .xls/.doc/.ppt is OLE but has NO such stream."""
    try:
        import olefile
    except ImportError:
        return False
    if not olefile.isOleFile(str(path)):
        return False
    ole = olefile.OleFileIO(str(path))
    try:
        return ole.exists("EncryptedPackage")
    finally:
        ole.close()


def _zip_is_encrypted(path: Path) -> bool:
    """Definite: any zip entry with the encryption bit (flag_bits & 0x1) set.
    Checks entries lazily and stops at the first encrypted one."""
    if not zipfile.is_zipfile(str(path)):
        return False
    with zipfile.ZipFile(str(path)) as zf:
        return any(info.flag_bits & 0x1 for info in zf.infolist())
