from __future__ import annotations

import base64
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


def _totp(secret: str, counter: int, digits: int = 6) -> str:
    padding = "=" * ((8 - (len(secret) % 8)) % 8)
    key = base64.b32decode((secret + padding).upper())
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return str(code_int).zfill(digits)


def verify_totp(secret: str, code: str, *, period_seconds: int = 30, skew_windows: int = 1) -> bool:
    if not secret or not code:
        return False
    normalized = "".join(ch for ch in code if ch.isdigit())
    if len(normalized) != 6:
        return False

    now_counter = int(time.time() // period_seconds)
    for window in range(-skew_windows, skew_windows + 1):
        if hmac.compare_digest(_totp(secret, now_counter + window), normalized):
            return True
    return False


def build_otpauth_uri(secret: str, email: str, issuer: str = "LawFirmOS") -> str:
    label = quote(f"{issuer}:{email}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"


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
