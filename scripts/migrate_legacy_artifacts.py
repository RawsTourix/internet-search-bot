"""CLI entry point for the optional legacy artifact-layout migration.

Use this only when ``storage/artifacts`` contains old ``art_*/file.bin``
payload directories created before the lineage/content-store artifact model.
The default invocation is a dry run; pass ``--apply`` only after reviewing the
report. New or empty installations do not require this migration.
"""

from pathlib import Path
import sys


# ``python scripts/migrate_legacy_artifacts.py`` places only ``scripts/`` on
# sys.path. Add the repository root so the local ``src`` package is importable
# without requiring an editable install or a platform-specific PYTHONPATH.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
project_root_text = str(PROJECT_ROOT)
if project_root_text not in sys.path:
    sys.path.insert(0, project_root_text)

from src.artifacts.migration import main  # noqa: E402


if __name__ == "__main__":
    main()
