"""AES-256-GCM evidence vault with a chained, signed manifest.

Two defects are fixed.

**Filename collisions destroyed evidence.** Names were
``evidence_%Y%m%d_%H%M%S_{level}``, i.e. one-second resolution. The pipeline
wrote evidence several times per second, so same-second writes silently
overwrote each other. A system claiming chain of custody was losing evidence to
its own naming scheme. Names now carry millisecond resolution, a monotonic
counter and a random suffix.

**Per-file hashes in an unsigned sidecar prove nothing.** The old design stored
``sha256(nonce || ciphertext)`` in a plaintext ``.json`` next to the ``.enc``.
An attacker who can rewrite the evidence can rewrite the sidecar beside it, so
the hash detected disk corruption but not tampering. Here every bundle is also
appended to a **hash-chained manifest**: each entry includes the digest of the
previous entry, and the manifest is authenticated with HMAC-SHA256 under a key
derived from ``EVIDENCE_AES_KEY``. Altering or removing any bundle breaks the
chain at that point and every point after it.

On terminology: the correct claim is **tamper-evident**, not "court-admissible".
Admissibility is a legal determination about process and custody, not a property
a cipher confers. The mid-semester report should be softened accordingly.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sentrix.config import settings
from sentrix.paths import EVIDENCE_DIR
from sentrix.utils.logging import get_logger

log = get_logger("evidence")

try:
    import cv2
except ImportError:                                    # pragma: no cover
    cv2 = None                                         # type: ignore[assignment]

KEY_VERSION = "v3-hkdf"
MANIFEST_NAME = "manifest.jsonl"
_GENESIS = "0" * 64


def _derive_keys() -> tuple[bytes, bytes, bool]:
    """Derive (encryption key, manifest signing key, persistent?).

    Two independent keys from one secret via HKDF ``info`` separation, so the
    manifest signature is not forgeable by anyone who only has the AES key.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    raw = os.getenv("EVIDENCE_AES_KEY", "").strip()
    persistent = False
    if raw:
        try:
            material = bytes.fromhex(raw)
            if len(material) < 16:
                raise ValueError("EVIDENCE_AES_KEY needs at least 32 hex chars")
            persistent = True
        except ValueError as exc:
            log.error("invalid EVIDENCE_AES_KEY (%s) — using an ephemeral key; "
                      "evidence will NOT survive a restart", exc)
            material = os.urandom(32)
    else:
        log.warning("EVIDENCE_AES_KEY not set — using an ephemeral key. "
                    "Evidence written this session becomes permanently "
                    "unreadable when the process exits.")
        material = os.urandom(32)

    def expand(info: bytes) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=32,
                    salt=b"sentrix-evidence-v3", info=info).derive(material)

    return expand(b"aes-256-gcm-key"), expand(b"manifest-hmac-key"), persistent


@dataclass
class EvidenceRecord:
    """Metadata sidecar for one encrypted bundle."""

    file: str
    bundle_id: str
    timestamp: str
    incident_id: str | None
    camera_id: str
    tci: float
    level: int
    status: str
    incident_type: str
    reason: str
    features: dict[str, Any]
    detections: list[str]
    sha256: str
    prev_sha256: str
    encryption: str = "AES-256-GCM"
    key_version: str = KEY_VERSION
    extras: dict[str, Any] = field(default_factory=dict)


class EvidenceVault:
    """Encrypts frames and maintains the tamper-evident manifest chain."""

    def __init__(self, directory: Path | None = None) -> None:
        self.dir = directory or EVIDENCE_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._counter = itertools.count(1)
        self._aes_key, self._mac_key, self.persistent = _derive_keys()
        self._tip = self._load_tip()
        self.session_id = uuid.uuid4().hex[:12]

    # ── manifest ──────────────────────────────────────────────────────────────
    @property
    def manifest_path(self) -> Path:
        return self.dir / MANIFEST_NAME

    def _load_tip(self) -> str:
        """Last chain digest, so a restart continues the existing chain."""
        path = self.manifest_path
        if not path.exists():
            return _GENESIS
        tip = _GENESIS
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        tip = json.loads(line).get("chain", tip)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
        return tip

    def _append_manifest(self, record: EvidenceRecord) -> str:
        payload = json.dumps(asdict(record), sort_keys=True, default=str)
        chain = hashlib.sha256((record.prev_sha256 + payload).encode()).hexdigest()
        signature = hmac.new(self._mac_key, chain.encode(), hashlib.sha256).hexdigest()
        entry = {"bundle_id": record.bundle_id, "file": record.file,
                 "sha256": record.sha256, "prev": record.prev_sha256,
                 "chain": chain, "hmac": signature,
                 "ts": record.timestamp, "level": record.level}
        try:
            with open(self.manifest_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
        except OSError as exc:
            log.error("manifest append failed: %s", exc)
        return chain

    # ── writing ───────────────────────────────────────────────────────────────
    def _bundle_name(self, level: int) -> str:
        """Collision-free: millisecond stamp + monotonic counter + random tail."""
        stamp = time.strftime("%Y%m%d_%H%M%S")
        millis = int((time.time() % 1) * 1000)
        seq = next(self._counter)
        return f"ev_{stamp}_{millis:03d}_L{level}_{seq:05d}_{uuid.uuid4().hex[:6]}"

    def save(self, frame, result, level: int, status: str,
             incident_id: str | None = None, detections: list | None = None,
             camera_id: str = "CAM_0") -> EvidenceRecord | None:
        """Encrypt one frame and chain it into the manifest."""
        if cv2 is None or frame is None:
            return None
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            ok, buffer = cv2.imencode(
                ".jpg", frame,
                [cv2.IMWRITE_JPEG_QUALITY, settings.evidence.jpeg_quality])
            if not ok:
                raise ValueError("JPEG encode failed")

            nonce = os.urandom(12)
            ciphertext = AESGCM(self._aes_key).encrypt(nonce, buffer.tobytes(), None)
            blob = nonce + ciphertext
            digest = hashlib.sha256(blob).hexdigest()

            with self._lock:
                base = self._bundle_name(level)
                enc_path = self.dir / f"{base}.enc"
                enc_path.write_bytes(blob)

                labels = []
                for det in (detections or []):
                    labels.append(det.label if hasattr(det, "label") else str(det))

                record = EvidenceRecord(
                    file=enc_path.name,
                    bundle_id=base,
                    timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    incident_id=incident_id,
                    camera_id=camera_id,
                    tci=round(result.tci, 4),
                    level=level,
                    status=status,
                    incident_type=result.incident_type,
                    reason=result.reason,
                    features=result.features.model_features(),
                    detections=labels,
                    sha256=digest,
                    prev_sha256=self._tip,
                    extras={"session": self.session_id,
                            "fusion_source": result.source,
                            "persistent_key": self.persistent},
                )
                chain = self._append_manifest(record)
                self._tip = chain
                (self.dir / f"{base}.json").write_text(
                    json.dumps(asdict(record), indent=2, default=str),
                    encoding="utf-8")

            log.info("evidence %s sealed (L%d, sha256=%s...)", base, level,
                     digest[:12])
            return record
        except Exception as exc:
            log.error("evidence save failed: %s", exc)
            return None

    # ── reading ───────────────────────────────────────────────────────────────
    def list_records(self, limit: int = 200) -> list[dict[str, Any]]:
        records = []
        for path in sorted(self.dir.glob("*.json"), reverse=True)[:limit]:
            if path.name == MANIFEST_NAME:
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            # Legacy aliases for the existing evidence.html template.
            record.setdefault("detected_objects", record.get("detections", []))
            record.setdefault("engine_scores", record.get("features", {}))
            record.setdefault("sha", (record.get("sha256") or "")[:16])
            record["view_url"] = f"/media/evidence/{record.get('bundle_id', '')}"
            records.append(record)
        return records

    def decrypt(self, bundle_id: str) -> bytes | None:
        """Return the original JPEG bytes, or None if unreadable."""
        path = self.dir / f"{bundle_id}.enc"
        if not path.exists():
            return None
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            blob = path.read_bytes()
            return AESGCM(self._aes_key).decrypt(blob[:12], blob[12:], None)
        except Exception as exc:
            log.warning("decrypt failed for %s: %s", bundle_id, exc)
            return None

    # ── verification ──────────────────────────────────────────────────────────
    def verify_bundle(self, bundle_id: str,
                      expected_sha256: str | None = None) -> tuple[bool, str]:
        """Check one bundle against a known-good digest.

        ``expected_sha256`` should come from the **signed manifest**. The
        ``.json`` sidecar is unauthenticated, so an attacker who rewrites the
        ciphertext can rewrite the sidecar hash beside it; comparing the two
        proves only that the disk is not corrupt. Falling back to the sidecar is
        supported for standalone inspection, and reported as such.
        """
        enc = self.dir / f"{bundle_id}.enc"
        meta = self.dir / f"{bundle_id}.json"
        if not enc.exists():
            return False, "missing ciphertext"
        try:
            actual = hashlib.sha256(enc.read_bytes()).hexdigest()
            if expected_sha256 is not None:
                if actual != expected_sha256:
                    return False, "sha256 mismatch against signed manifest"
                return True, "intact"
            if not meta.exists():
                return False, "missing sidecar"
            record = json.loads(meta.read_text(encoding="utf-8"))
            if actual != record.get("sha256"):
                return False, "sha256 mismatch against sidecar"
            return True, "intact (sidecar only — unauthenticated)"
        except Exception as exc:
            return False, f"unreadable: {exc}"

    def verify_chain(self) -> dict[str, Any]:
        """Walk the whole manifest and report the first break.

        Detects modification *and* deletion: removing an entry breaks the link
        for every entry after it.
        """
        path = self.manifest_path
        if not path.exists():
            return {"ok": True, "entries": 0, "note": "no manifest yet"}

        entries = 0
        prev = _GENESIS
        problems: list[dict[str, Any]] = []
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for number, line in enumerate(handle, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    entries += 1
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        problems.append({"line": number, "error": "malformed"})
                        continue

                    expected = hmac.new(self._mac_key, entry["chain"].encode(),
                                        hashlib.sha256).hexdigest()
                    if not hmac.compare_digest(expected, entry.get("hmac", "")):
                        problems.append({"line": number,
                                         "bundle": entry.get("bundle_id"),
                                         "error": "bad signature"})
                    if entry.get("prev") != prev:
                        problems.append({"line": number,
                                         "bundle": entry.get("bundle_id"),
                                         "error": "chain break"})
                    # Compare the ciphertext against the digest recorded in the
                    # HMAC-signed manifest, never against the unsigned sidecar.
                    ok, note = self.verify_bundle(entry.get("bundle_id", ""),
                                                  entry.get("sha256"))
                    if not ok:
                        problems.append({"line": number,
                                         "bundle": entry.get("bundle_id"),
                                         "error": note})
                    prev = entry.get("chain", prev)
        except OSError as exc:
            return {"ok": False, "entries": entries, "error": str(exc)}

        return {"ok": not problems, "entries": entries, "problems": problems}


vault = EvidenceVault()

__all__ = ["EvidenceVault", "EvidenceRecord", "vault"]
