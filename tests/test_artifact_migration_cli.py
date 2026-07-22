import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ArtifactMigrationCliTests(unittest.TestCase):
    def test_direct_script_invocation_from_repository_root(self):
        repository_root = Path(__file__).resolve().parents[1]
        script = repository_root / "scripts" / "migrate_legacy_artifacts.py"

        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--root",
                    str(Path(temporary) / "storage"),
                ],
                cwd=repository_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["discovered_versions"], 0)
        self.assertEqual(report["discovered_lineages"], 0)
        self.assertEqual(report["migrated_versions"], 0)
        self.assertEqual(report["errors"], [])


if __name__ == "__main__":
    unittest.main()
