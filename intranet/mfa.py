from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

from werkzeug.security import check_password_hash, generate_password_hash


def generate_totp_secret(length: int = 32) -> str:
    raw = secrets.token_bytes(length)
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def _normalize_secret(secret: str) -> str:
    allowed = {"A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O", "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z", "2", "3", "4", "5", "6", "7"}
    cleaned = "".join(ch for ch in (secret or "").upper() if ch in allowed)
    return cleaned


def _decode_secret(secret: str) -> bytes | None:
    normalized = _normalize_secret(secret)
    if not normalized:
        return None
    padding = "=" * ((8 - (len(normalized) % 8)) % 8)
    try:
        return base64.b32decode(normalized + padding, casefold=True)
    except (binascii.Error, ValueError):
        return None


def _totp(secret: str, counter: int, digits: int = 6) -> str:
    key = _decode_secret(secret)
    if key is None:
        return ""
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return str(code_int).zfill(digits)


def verify_totp(secret: str, code: str, *, period_seconds: int = 30, skew_windows: int = 2) -> bool:
    if not secret or not code:
        return False
    normalized = "".join(ch for ch in code if ch.isdigit())
    if len(normalized) != 6:
        return False
    normalized_secret = _normalize_secret(secret)
    if not normalized_secret:
        return False

    now_counter = int(time.time() // period_seconds)
    for window in range(-skew_windows, skew_windows + 1):
        generated = _totp(normalized_secret, now_counter + window)
        if generated and hmac.compare_digest(generated, normalized):
            return True
    return False


def build_otpauth_uri(secret: str, email: str, issuer: str = "LawFirmOS") -> str:
    label = quote(f"{issuer}:{email}")
    normalized_secret = _normalize_secret(secret)
    return f"otpauth://totp/{label}?secret={normalized_secret}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"


def generate_backup_codes(count: int = 10) -> list[str]:
    codes = []
    for _ in range(count):
        raw = secrets.token_hex(3).upper()
        codes.append(f"{raw[:3]}-{raw[3:]}")
    return codes


def hash_backup_code(code: str) -> str:
    return generate_password_hash(code.strip().upper())


def check_backup_code(code_hash: str, code: str) -> bool:
    return check_password_hash(code_hash, code.strip().upper())
