from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile

from sqlalchemy.engine import make_url

from flask import abort, flash, redirect, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..helpers import audit
from ..models import BackupRun, DRTarget, RestoreVerification
from ..policies import enforce_data_residency
from ..templates import page

BACKUP_ENCRYPTION_MAGIC = b"LFBK1"
BACKUP_ENCRYPTION_NONCE_SIZE = 12
BACKUP_ENCRYPTION_TAG_SIZE = 16


def _ops_admin_required() -> None:
    if current_user.role != "admin":
        abort(403)


def _sha256_path(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _cleanup_backup_artifacts(backup_dir: str, run_id: int, timestamp_suffix: str) -> None:
    prefix = f"backup_run_{run_id}_{timestamp_suffix}"
    try:
        names = os.listdir(backup_dir)
    except OSError:
        return
    for name in names:
        if not name.startswith(prefix):
            continue
        path = os.path.join(backup_dir, name)
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except OSError:
            continue


def _load_backup_encryption_key(*, required: bool = False) -> bytes | None:
    raw = (os.environ.get("BACKUP_ENCRYPTION_KEY") or "").strip()
    if not raw:
        if required:
            raise RuntimeError("BACKUP_ENCRYPTION_KEY is required to verify encrypted backups.")
        return None

    if re.fullmatch(r"[0-9a-fA-F]{64}", raw):
        return bytes.fromhex(raw)

    padded = raw + ("=" * (-len(raw) % 4))
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("utf-8"))
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError(
            "BACKUP_ENCRYPTION_KEY is invalid. Use 32-byte URL-safe base64 or 64-char hex."
        ) from exc
    if len(decoded) != 32:
        raise RuntimeError(
            "BACKUP_ENCRYPTION_KEY must decode to exactly 32 bytes for AES-256."
        )
    return decoded


def _backup_key_fingerprint(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


def _build_encryptor(key: bytes, nonce: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise RuntimeError("cryptography is required for backup encryption.") from exc
    return Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()


def _build_decryptor(key: bytes, nonce: bytes, tag: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise RuntimeError("cryptography is required for backup encryption.") from exc
    return Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()


def _encrypt_backup_artifact(path: str, key: bytes) -> str:
    out_path = f"{path}.enc"
    tmp_path = f"{out_path}.tmp"
    nonce = os.urandom(BACKUP_ENCRYPTION_NONCE_SIZE)
    encryptor = _build_encryptor(key, nonce)
    try:
        with open(path, "rb") as src, open(tmp_path, "wb") as dst:
            dst.write(BACKUP_ENCRYPTION_MAGIC)
            dst.write(nonce)
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                dst.write(encryptor.update(chunk))
            tail = encryptor.finalize()
            if tail:
                dst.write(tail)
            dst.write(encryptor.tag)
        os.remove(path)
        os.replace(tmp_path, out_path)
        return out_path
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


def _decrypt_backup_artifact(path: str, key: bytes, out_path: str) -> None:
    min_size = len(BACKUP_ENCRYPTION_MAGIC) + BACKUP_ENCRYPTION_NONCE_SIZE + BACKUP_ENCRYPTION_TAG_SIZE
    size = os.path.getsize(path)
    if size < min_size:
        raise RuntimeError("Encrypted artifact is truncated.")

    with open(path, "rb") as src:
        magic = src.read(len(BACKUP_ENCRYPTION_MAGIC))
        if magic != BACKUP_ENCRYPTION_MAGIC:
            raise RuntimeError("Encrypted artifact format is invalid.")
        nonce = src.read(BACKUP_ENCRYPTION_NONCE_SIZE)
        if len(nonce) != BACKUP_ENCRYPTION_NONCE_SIZE:
            raise RuntimeError("Encrypted artifact nonce is invalid.")

        src.seek(size - BACKUP_ENCRYPTION_TAG_SIZE)
        tag = src.read(BACKUP_ENCRYPTION_TAG_SIZE)
        if len(tag) != BACKUP_ENCRYPTION_TAG_SIZE:
            raise RuntimeError("Encrypted artifact authentication tag missing.")

        src.seek(len(BACKUP_ENCRYPTION_MAGIC) + BACKUP_ENCRYPTION_NONCE_SIZE)
        ciphertext_remaining = size - min_size
        decryptor = _build_decryptor(key, nonce, tag)
        try:
            with open(out_path, "wb") as dst:
                while ciphertext_remaining > 0:
                    chunk = src.read(min(1024 * 1024, ciphertext_remaining))
                    if not chunk:
                        raise RuntimeError("Encrypted artifact ciphertext truncated.")
                    ciphertext_remaining -= len(chunk)
                    dst.write(decryptor.update(chunk))
                tail = decryptor.finalize()
                if tail:
                    dst.write(tail)
        except Exception:
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)
            except OSError:
                pass
            raise


def _decrypt_backup_artifact_to_temp(path: str, key: bytes, suffix: str) -> str:
    fd, temp_path = tempfile.mkstemp(prefix="lfos_backup_", suffix=suffix)
    os.close(fd)
    _decrypt_backup_artifact(path, key, temp_path)
    return temp_path


def _dump_database(database_uri: str, out_path: str) -> dict:
    url = make_url(database_uri)
    backend = url.get_backend_name()
    if backend == "sqlite":
        if not url.database or url.database == ":memory:":
            raise RuntimeError("sqlite in-memory databases cannot be backed up to disk")
        source_path = url.database
        if not os.path.isabs(source_path):
            source_path = os.path.abspath(source_path)
        if not os.path.isfile(source_path):
            raise RuntimeError(f"sqlite database file missing: {source_path}")
        src = sqlite3.connect(source_path)
        try:
            dest = sqlite3.connect(out_path)
            try:
                src.backup(dest)
            finally:
                dest.close()
        finally:
            src.close()
        return {"engine": "sqlite", "source": source_path}
    if backend == "postgresql":
        try:
            proc = subprocess.run(
                ["pg_dump", "--format=custom", "--no-owner", "--file", out_path, database_uri],
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("pg_dump not installed on server") from exc
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "pg_dump failed").strip()[:800])
        return {"engine": "postgresql", "source": "pg_dump"}
    raise RuntimeError(f"Unsupported database backend for backup: {backend}")


def _archive_uploads(upload_dir: str, out_path: str) -> int:
    total = 0
    with tarfile.open(out_path, "w:gz") as archive:
        for name in sorted(os.listdir(upload_dir)):
            if name == "backups":
                continue
            src = os.path.join(upload_dir, name)
            if os.path.isdir(src):
                archive.add(src, arcname=name)
                for root, _dirs, files in os.walk(src):
                    for fn in files:
                        try:
                            total += os.path.getsize(os.path.join(root, fn))
                        except OSError:
                            continue
            else:
                archive.add(src, arcname=name)
                try:
                    total += os.path.getsize(src)
                except OSError:
                    continue
    return total


def _verify_backup_integrity(run: BackupRun) -> tuple[str, list[str]]:
    notes: list[str] = []
    status = "passed"
    details = {}
    try:
        details = json.loads(run.details_json or "{}")
    except json.JSONDecodeError:
        return "failed", ["Backup details JSON is invalid."]

    db_dump = details.get("db_dump_path")
    uploads_archive = details.get("uploads_archive_path")
    db_hash = details.get("db_dump_sha256")
    uploads_hash = details.get("uploads_archive_sha256")
    db_plain_hash = details.get("db_dump_plain_sha256")
    uploads_plain_hash = details.get("uploads_archive_plain_sha256")
    db_engine = details.get("db_engine")
    encryption = details.get("encryption") or {}
    encrypted = bool(encryption.get("enabled"))
    temp_paths: list[str] = []
    key: bytes | None = None

    if not db_dump or not os.path.isfile(db_dump):
        return "failed", ["Database backup artifact missing."]
    if db_hash and _sha256_path(db_dump) != db_hash:
        return "failed", ["Database backup checksum mismatch."]
    db_verify_path = db_dump
    uploads_verify_path = uploads_archive

    try:
        if encrypted:
            try:
                key = _load_backup_encryption_key(required=True)
            except RuntimeError as exc:
                return "failed", [str(exc)]

            expected_key_fingerprint = (encryption.get("key_fingerprint") or "").strip()
            if expected_key_fingerprint and _backup_key_fingerprint(key) != expected_key_fingerprint:
                return "failed", ["Backup encryption key fingerprint mismatch."]

            try:
                db_plain_path = _decrypt_backup_artifact_to_temp(db_dump, key, ".dbdump")
            except Exception as exc:
                return "failed", [f"Database backup decryption failed: {str(exc)[:400]}"]
            temp_paths.append(db_plain_path)
            db_verify_path = db_plain_path
            if db_plain_hash and _sha256_path(db_plain_path) != db_plain_hash:
                return "failed", ["Database backup decrypted checksum mismatch."]
            notes.append("Database backup checksum and decryption verified.")
        else:
            notes.append("Database backup artifact and checksum verified.")

        if uploads_archive:
            if not os.path.isfile(uploads_archive):
                return "failed", ["Uploads archive missing."]
            if uploads_hash and _sha256_path(uploads_archive) != uploads_hash:
                return "failed", ["Uploads archive checksum mismatch."]
            if encrypted:
                if key is None:
                    return "failed", ["Backup encryption key unavailable for uploads archive verification."]
                try:
                    uploads_plain_path = _decrypt_backup_artifact_to_temp(uploads_archive, key, ".tar.gz")
                except Exception as exc:
                    return "failed", [f"Uploads archive decryption failed: {str(exc)[:400]}"]
                temp_paths.append(uploads_plain_path)
                uploads_verify_path = uploads_plain_path
                if uploads_plain_hash and _sha256_path(uploads_plain_path) != uploads_plain_hash:
                    return "failed", ["Uploads archive decrypted checksum mismatch."]
                notes.append("Uploads archive checksum and decryption verified.")
            else:
                notes.append("Uploads archive and checksum verified.")

        if db_engine == "sqlite":
            con = sqlite3.connect(db_verify_path)
            try:
                row = con.execute("PRAGMA integrity_check;").fetchone()
                if not row or str(row[0]).lower() != "ok":
                    return "failed", ["SQLite integrity check failed."]
            finally:
                con.close()
            notes.append("SQLite integrity check passed.")
        elif db_engine == "postgresql":
            try:
                proc = subprocess.run(
                    ["pg_restore", "--list", db_verify_path],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=60,
                )
            except FileNotFoundError:
                status = "failed"
                notes.append("pg_restore not installed; PostgreSQL dump structure not validated.")
            else:
                if proc.returncode != 0:
                    status = "failed"
                    notes.append((proc.stderr or "pg_restore --list failed").strip()[:500])
                else:
                    notes.append("PostgreSQL dump structure validated with pg_restore --list.")
    finally:
        for path in temp_paths:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except OSError:
                continue

    return status, notes


def register_ops_plus_routes(app):
    @app.get("/ops/backup/status")
    @login_required
    def ops_backup_status():
        _ops_admin_required()
        runs = BackupRun.query.order_by(BackupRun.started_at.desc()).limit(100).all()
        return page("Backup Status", "ops_plus/backup_status.html", runs=runs)

    @app.post("/ops/backup/run")
    @login_required
    def ops_backup_run():
        _ops_admin_required()
        enforce_data_residency("backups")

        run = BackupRun(started_at=dt.datetime.utcnow(), status="running", triggered_by=current_user.id)
        db.session.add(run)
        db.session.flush()

        backup_dir = os.path.join(app.config["UPLOAD_DIR"], "backups")
        os.makedirs(backup_dir, exist_ok=True)
        ts = dt.datetime.utcnow().strftime("%Y%m%d%H%M%S")
        db_dump_path = os.path.join(backup_dir, f"backup_run_{run.id}_{ts}.dbdump")
        uploads_archive_path = os.path.join(backup_dir, f"backup_run_{run.id}_{ts}_uploads.tar.gz")
        manifest_path = os.path.join(backup_dir, f"backup_run_{run.id}_{ts}.json")

        try:
            db_info = _dump_database(app.config["SQLALCHEMY_DATABASE_URI"], db_dump_path)
            upload_bytes = _archive_uploads(app.config["UPLOAD_DIR"], uploads_archive_path)
            db_plain_sha = _sha256_path(db_dump_path)
            uploads_plain_sha = _sha256_path(uploads_archive_path)
            key = _load_backup_encryption_key(required=False)
            encryption_meta = {"enabled": False}
            if key is not None:
                db_dump_path = _encrypt_backup_artifact(db_dump_path, key)
                uploads_archive_path = _encrypt_backup_artifact(uploads_archive_path, key)
                encryption_meta = {
                    "enabled": True,
                    "algorithm": "aes-256-gcm",
                    "format": "lfos-aesgcm-v1",
                    "key_fingerprint": _backup_key_fingerprint(key),
                }

            payload = {
                "backup_run_id": run.id,
                "timestamp": dt.datetime.utcnow().isoformat(),
                "db_engine": db_info.get("engine"),
                "db_source": db_info.get("source"),
                "db_dump_path": db_dump_path,
                "db_dump_plain_sha256": db_plain_sha,
                "db_dump_sha256": _sha256_path(db_dump_path),
                "db_dump_size_bytes": os.path.getsize(db_dump_path),
                "uploads_archive_path": uploads_archive_path,
                "uploads_archive_plain_sha256": uploads_plain_sha,
                "uploads_archive_sha256": _sha256_path(uploads_archive_path),
                "uploads_archive_size_bytes": os.path.getsize(uploads_archive_path),
                "uploads_payload_size_bytes": upload_bytes,
                "encryption": encryption_meta,
            }
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            run.location = manifest_path
            run.status = "succeeded"
            run.details_json = json.dumps(payload)
            flash("Backup completed and artifacts stored.", "info")
        except Exception as exc:
            run.status = "failed"
            run.details_json = json.dumps({"error": str(exc)[:800]})
            _cleanup_backup_artifacts(backup_dir, run.id, ts)
            flash(f"Backup failed: {exc}", "warning")

        run.finished_at = dt.datetime.utcnow()
        db.session.commit()
        audit("backup_run", "BackupRun", run.id, {"status": run.status, "location": run.location})
        return redirect(url_for("ops_backup_status"))

    @app.route("/ops/restore/verify", methods=["GET", "POST"])
    @login_required
    def ops_restore_verify():
        _ops_admin_required()

        if request.method == "POST":
            backup_run_id = request.form.get("backup_run_id", type=int)
            notes = (request.form.get("notes") or "").strip()
            backup_run = db.session.get(BackupRun, backup_run_id) if backup_run_id else None
            if backup_run is None:
                flash("Valid backup run is required.", "warning")
                return redirect(url_for("ops_restore_verify"))

            computed_status, verification_notes = _verify_backup_integrity(backup_run)
            merged_notes = "\n".join(x for x in [notes, *verification_notes] if x)
            row = RestoreVerification(
                backup_run_id=backup_run_id,
                status=computed_status,
                notes=merged_notes or None,
                verified_by=current_user.id,
                verified_at=dt.datetime.utcnow(),
            )
            db.session.add(row)
            db.session.commit()
            audit("restore_verify", "RestoreVerification", row.id, {"status": computed_status})
            if computed_status == "passed":
                flash("Restore verification passed.", "info")
            else:
                flash("Restore verification failed. Review notes for details.", "warning")
            return redirect(url_for("ops_restore_verify"))

        runs = BackupRun.query.order_by(BackupRun.started_at.desc()).limit(100).all()
        verifications = RestoreVerification.query.order_by(RestoreVerification.verified_at.desc()).limit(200).all()
        return page("Restore Verification", "ops_plus/restore_verify.html", runs=runs, verifications=verifications)

    @app.route("/ops/dr/targets", methods=["GET", "POST"])
    @login_required
    def ops_dr_targets():
        _ops_admin_required()

        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            if not name:
                flash("DR target name required.", "warning")
                return redirect(url_for("ops_dr_targets"))

            row = DRTarget.query.filter_by(name=name).first()
            if row is None:
                row = DRTarget(
                    name=name,
                    rpo_minutes_target=request.form.get("rpo_minutes_target", type=int) or 60,
                    rto_minutes_target=request.form.get("rto_minutes_target", type=int) or 240,
                )
                db.session.add(row)
            row.last_actual_rpo_minutes = request.form.get("last_actual_rpo_minutes", type=int)
            row.last_actual_rto_minutes = request.form.get("last_actual_rto_minutes", type=int)
            row.updated_at = dt.datetime.utcnow()
            db.session.commit()
            audit("dr_target_upsert", "DRTarget", row.id)
            flash("DR target saved.", "info")
            return redirect(url_for("ops_dr_targets"))

        rows = DRTarget.query.order_by(DRTarget.updated_at.desc()).all()
        return page("DR Targets", "ops_plus/dr_targets.html", targets=rows)
