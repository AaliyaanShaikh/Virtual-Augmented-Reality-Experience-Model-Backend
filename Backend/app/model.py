from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent

# TripoSR subprocess runner temporarily disabled — use static sample mesh for stable API.
# Re-enable by restoring the previous implementation that invoked TripoSR/run.py.


def run_triposr(image_path: str) -> str:
    """Return path to the model file (static ``sample.glb`` until TripoSR returns)."""
    _ = image_path  # reserved for future TripoSR input
    return str(BACKEND_ROOT / "sample.glb")
