from __future__ import annotations

import base64
import json
import sqlite3
import tarfile
from types import SimpleNamespace

from intranet.routes.ops_plus import (
    _backup_key_fingerprint,
    _decrypt_backup_artifact,
    _encrypt_backup_artifact,
    _load_backup_encryption_key,
    _sha256_path,
    _verify_backup_integrity,
)


def _test_key() -> str:
    return base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("utf-8")


def test_backup_artifact_encryption_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", _test_key())
    key = _load_backup_encryption_key(required=True)

    source = tmp_path / "artifact.bin"
    payload = b"law-firm-os-backup-payload" * 1024
    source.write_bytes(payload)

    encrypted_path = _encrypt_backup_artifact(str(source), key)
    assert encrypted_path.endswith(".enc")
    assert not source.exists()

    restored = tmp_path / "artifact.restored.bin"
    _decrypt_backup_artifact(encrypted_path, key, str(restored))
    assert restored.read_bytes() == payload


def test_verify_backup_integrity_supports_encrypted_sqlite_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", _test_key())
    key = _load_backup_encryption_key(required=True)

    db_plain = tmp_path / "backup.db"
    con = sqlite3.connect(str(db_plain))
    try:
        con.execute("CREATE TABLE backup_records(id INTEGER PRIMARY KEY, name TEXT)")
        con.execute("INSERT INTO backup_records(name) VALUES ('record')")
        con.commit()
    finally:
        con.close()

    uploads_plain = tmp_path / "uploads.tar.gz"
    source_file = tmp_path / "note.txt"
    source_file.write_text("backup payload", encoding="utf-8")
    with tarfile.open(str(uploads_plain), "w:gz") as archive:
        archive.add(str(source_file), arcname="note.txt")

    db_plain_sha = _sha256_path(str(db_plain))
    uploads_plain_sha = _sha256_path(str(uploads_plain))

    db_encrypted = _encrypt_backup_artifact(str(db_plain), key)
    uploads_encrypted = _encrypt_backup_artifact(str(uploads_plain), key)

    run = SimpleNamespace(
        details_json=json.dumps(
            {
                "db_engine": "sqlite",
                "db_dump_path": db_encrypted,
                "db_dump_sha256": _sha256_path(db_encrypted),
                "db_dump_plain_sha256": db_plain_sha,
                "uploads_archive_path": uploads_encrypted,
                "uploads_archive_sha256": _sha256_path(uploads_encrypted),
                "uploads_archive_plain_sha256": uploads_plain_sha,
                "encryption": {
                    "enabled": True,
                    "algorithm": "aes-256-gcm",
                    "format": "lfos-aesgcm-v1",
                    "key_fingerprint": _backup_key_fingerprint(key),
                },
            }
        )
    )

    status, notes = _verify_backup_integrity(run)
    assert status == "passed"
    assert any("decryption verified" in note.lower() for note in notes)


def test_verify_backup_integrity_fails_for_encrypted_artifact_without_key(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY", raising=False)
    key = _load_backup_encryption_key(required=False)
    assert key is None

    db_plain = tmp_path / "backup.db"
    con = sqlite3.connect(str(db_plain))
    try:
        con.execute("CREATE TABLE t(id INTEGER PRIMARY KEY)")
        con.commit()
    finally:
        con.close()

    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", _test_key())
    enc_key = _load_backup_encryption_key(required=True)
    db_encrypted = _encrypt_backup_artifact(str(db_plain), enc_key)
    db_encrypted_sha = _sha256_path(db_encrypted)

    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY", raising=False)
    run = SimpleNamespace(
        details_json=json.dumps(
            {
                "db_engine": "sqlite",
                "db_dump_path": db_encrypted,
                "db_dump_sha256": db_encrypted_sha,
                "encryption": {"enabled": True, "key_fingerprint": _backup_key_fingerprint(enc_key)},
            }
        )
    )
    status, notes = _verify_backup_integrity(run)
    assert status == "failed"
    assert any("BACKUP_ENCRYPTION_KEY" in note for note in notes)
