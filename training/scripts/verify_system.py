# training/scripts/verify_system.py
#
# Verification script for SENTRIX Backend:
# Tests DB init, AI engine instantiation, TCI calculation, and state updates.

import sys
from pathlib import Path

# Add backend directory to sys.path
BASE_DIR = Path(__file__).parent.parent.parent / "backend"
sys.path.insert(0, str(BASE_DIR))

print("=" * 60)
print("SENTRIX: Running System Engine Verification")
print("=" * 60)

import db.database as database
import core.engine_instance as engine_instance
from core.state import state
from ai.fusion_engine import FusionEngine, TCIResult

# 1. Initialize Database
print("[1/4] Initializing Database...")
database.init_db()
print("  --> SQLite tables initialized.")

# 2. Initialize Engines
print("[2/4] Initializing AI & Core Engines...")
engine_instance.initialize_all()
sys_engine = engine_instance.get_system_engine()
print("  --> SystemEngine instance created successfully.")

# 3. Test Fusion Engine with simulated signals
print("[3/4] Testing Multi-Modal Fusion Engine...")
fusion = sys_engine.fusion
test_scores = {
    "vision": 0.85,
    "audio": 0.60,
    "motion": 0.50,
    "identity": 0.0,
    "weapon": 0.80,
    "fire": 0.0,
    "unauthorized": True,
    "behaviour_label": "running"
}
result = fusion.compute(test_scores)
print(f"  --> TCI: {result.tci:.3f} | Level: {result.level} ({result.status}) | Incident: {result.incident_type}")
print(f"  --> Reason: {result.reason}")
print(f"  --> Top Factors: {result.top_factors}")
print(f"  --> Uncertainty: {result.uncertainty}")

# 4. Check loaded models in backend/models
print("[4/4] Checking Trained Model Weights in backend/models/...")
models_dir = BASE_DIR / "models"
for m in sorted(models_dir.iterdir()):
    if m.is_file():
        size_mb = m.stat().st_size / (1024 * 1024)
        print(f"  • {m.name} ({size_mb:.2f} MB)")

print("\n" + "=" * 60)
print("SUCCESS: ALL SYSTEM ENGINE CHECKS PASSED SUCCESSFULLY!")
print("=" * 60)
