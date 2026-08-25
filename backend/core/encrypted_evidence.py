# core/encrypted_evidence.py
#
# ARCHITECTURE: Encrypts every Level 3-5 frame with AES-256-GCM.
# Saves a .enc binary (nonce prepended) and a .json metadata sidecar.
# SHA-256 hash of (nonce + ciphertext) enables tamper detection.
#
# KEY DERIVATION (v2 improvement):
#   If EVIDENCE_AES_KEY env var is set, derives a stable 32-byte AES key via
#   HKDF-SHA256. This ensures:
#     - The same env var always produces the same key (evidence survives restarts).
#     - The raw env bytes are never used directly as a key (defense-in-depth).
#   If no env var: auto-generates a random key (session-only — evidence unreadable
#   across restarts, but the system still works for demos).
#
# TAMPER DETECTION:
#   verify_evidence(enc_path, meta_path) checks SHA-256 of stored ciphertext
#   against the hash in the .json sidecar.

import cv2
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from dotenv import load_dotenv

load_dotenv()

from core.paths import EVIDENCE_DIR, EVIDENCE_DIR_STR

EVIDENCE_DIR_STR_LEGACY = EVIDENCE_DIR_STR  # backward-compat alias
KEY_VERSION  = "v2-hkdf"

# ── Key derivation ─────────────────────────────────────────────────────────────
_raw_key_env = os.getenv("EVIDENCE_AES_KEY", "").strip()

if _raw_key_env:
    try:
        # Decode hex → raw bytes (must be 32-64 hex chars, i.e. 16-32 bytes)
        raw_bytes = bytes.fromhex(_raw_key_env)
        if len(raw_bytes) < 16:
            raise ValueError("EVIDENCE_AES_KEY must be at least 32 hex chars (16 bytes)")

        # HKDF: derive a stable 32-byte AES key
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"sentrix-evidence-v2",
            info=b"aes-256-gcm-key",
        )
        AES_KEY = hkdf.derive(raw_bytes)
        print("[EncryptedEvidence] AES key derived via HKDF-SHA256 (stable across restarts).")
    except Exception as e:
        print(f"[EncryptedEvidence] Invalid EVIDENCE_AES_KEY ({e}). Auto-generating session key.")
        AES_KEY = os.urandom(32)
else:
    AES_KEY = os.urandom(32)
    print("[EncryptedEvidence] No EVIDENCE_AES_KEY set — using ephemeral session key.")


@dataclass
class EvidenceMetadata:
    file:             str
    timestamp:        str
    camera_id:        str
    tci:              float
    level:            int
    status:           str
    incident_type:    str
    reason:           str
    engine_scores:    dict
    detected_objects: list
    sha256:           str
    encryption:       str = "AES-256-GCM"
    key_version:      str = KEY_VERSION     # New: identifies which key version was used


class EncryptedEvidence:
    """Saves AES-256-GCM encrypted frame + JSON metadata sidecar."""

    def save_encrypted_frame(
        self,
        frame,
        result,
        scores: dict,
        detections: list,
        camera_id: str = "CAM_COMBINED",
    ) -> Optional[EvidenceMetadata]:
        if os.getenv("SENTRIX_EVAL_MODE") == "1":
            print(f"[EncryptedEvidence-EVAL] Mock Evidence Saved")
            return EvidenceMetadata(
                file="mock.enc", timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ"), camera_id=camera_id,
                tci=result.tci, level=result.level, status=result.status, incident_type=result.incident_type,
                reason=result.reason, engine_scores=scores, detected_objects=[], sha256="mock_hash"
            )
        try:
            # 1. Encode frame as JPEG bytes
            success, jpg_buffer = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90]
            )
            if not success:
                raise ValueError("Frame encoding failed")
            plaintext = jpg_buffer.tobytes()

            # 2. Generate 96-bit nonce (12 bytes) for AES-GCM
            nonce = os.urandom(12)

            # 3. Encrypt with AES-256-GCM
            aesgcm     = AESGCM(AES_KEY)
            ciphertext = aesgcm.encrypt(nonce, plaintext, associated_data=None)

            # 4. SHA-256 hash of (nonce + ciphertext) for tamper detection
            sha256_hash = hashlib.sha256(nonce + ciphertext).hexdigest()

            # 5. Write .enc file (nonce prepended — self-contained)
            timestamp_str = time.strftime("%Y%m%d_%H%M%S")
            base_name     = f"evidence_{timestamp_str}_{result.level}"
            os.makedirs(EVIDENCE_DIR_STR, exist_ok=True)
            enc_path = str(EVIDENCE_DIR / (base_name + ".enc"))

            with open(enc_path, "wb") as f:
                f.write(nonce + ciphertext)

            # 6. Build metadata
            detected_labels = []
            for d in detections:
                if isinstance(d, dict):
                    detected_labels.append(d.get("label", "unknown"))
                elif isinstance(d, (list, tuple)) and len(d) >= 5:
                    detected_labels.append(str(d[4]))

            scores_safe = {k: v for k, v in scores.items() if isinstance(v, (int, float))}

            meta = EvidenceMetadata(
                file             = base_name + ".enc",
                timestamp        = time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                camera_id        = camera_id,
                tci              = round(result.tci, 4),
                level            = result.level,
                status           = result.status,
                incident_type    = result.incident_type,
                reason           = result.reason,
                engine_scores    = scores_safe,
                detected_objects = detected_labels,
                sha256           = sha256_hash,
                key_version      = KEY_VERSION,
            )

            # 7. Write .json sidecar
            json_path = str(EVIDENCE_DIR / (base_name + ".json"))
            with open(json_path, "w") as f:
                json.dump(asdict(meta), f, indent=2)

            print(f"[EncryptedEvidence] Saved {base_name}.enc | SHA256: {sha256_hash[:16]}...")
            return meta

        except Exception as e:
            print(f"[EncryptedEvidence] save_encrypted_frame error: {e}")
            return None

    def list_evidence(self) -> list:
        """Return metadata list for all evidence JSON sidecars."""
        items = []
        try:
            if not EVIDENCE_DIR.is_dir():
                return items
            for fname in sorted(EVIDENCE_DIR.iterdir(), reverse=True):
                if not fname.suffix == ".json":
                    continue
                fpath = str(fname)
                try:
                    with open(fpath) as f:
                        items.append(json.load(f))
                except Exception:
                    continue
        except Exception as e:
            print(f"[EncryptedEvidence] list_evidence error: {e}")
        return items

    def verify_evidence(self, enc_path: str, meta_path: str) -> bool:
        """
        Tamper detection: read the .enc file, recompute SHA-256 of its raw bytes,
        compare against the hash stored in the .json sidecar.
        Returns True if the file is intact, False if tampered or unreadable.
        """
        try:
            with open(enc_path, "rb") as f:
                raw_data = f.read()
            with open(meta_path) as f:
                meta = json.load(f)

            stored_hash   = meta.get("sha256", "")
            computed_hash = hashlib.sha256(raw_data).hexdigest()
            return computed_hash == stored_hash
        except Exception as e:
            print(f"[EncryptedEvidence] verify_evidence error: {e}")
            return False


# Module-level singleton
encrypted_evidence = EncryptedEvidence()