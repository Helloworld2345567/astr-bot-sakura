import ast
import json
from pathlib import Path
import sys
import unittest

from support import ROOT, WORKSPACE


class ReleaseTests(unittest.TestCase):
    def test_manifest_is_explicit_complete_and_portable(self):
        manifest = json.loads((ROOT / "release-files.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest), len(set(manifest)))
        spec = __import__("importlib.util", fromlist=["spec_from_file_location"])
        module_spec = spec.spec_from_file_location("release_check", WORKSPACE / "scripts" / "check_release.py")
        scanner = spec.module_from_spec(module_spec)
        module_spec.loader.exec_module(scanner)
        for name in manifest:
            with self.subTest(name=name):
                path = ROOT / name
                self.assertTrue(path.is_file())
                self.assertTrue(path.resolve().is_relative_to(ROOT))
                self.assertFalse(scanner.findings(name, path.read_text(encoding="utf-8")))

    def test_core_has_no_framework_dependency(self):
        for path in (ROOT / "src").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
            imports += [name.name for node in ast.walk(tree) if isinstance(node, ast.Import) for name in node.names]
            self.assertFalse(any(name and name.startswith("astrbot") for name in imports), path)
        import video_understanding_core
        self.assertEqual(Path(video_understanding_core.__file__).resolve(), ROOT / "src" / "video_understanding_core" / "__init__.py")

    def test_plugin_archive_is_deterministic_and_excludes_runtime_data(self):
        import tempfile
        import zipfile
        from test_adapter import load_build_script
        from support import TEMP_ROOT
        with tempfile.TemporaryDirectory(prefix="video-archive-", dir=TEMP_ROOT) as directory:
            first = load_build_script().build(Path(directory) / "first.zip")
            second = load_build_script().build(Path(directory) / "second.zip")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                self.assertNotIn("sakura/runtime.py", archive.namelist())
                self.assertTrue(all(not name.endswith((".db", ".sqlite", ".log", ".mp4")) for name in archive.namelist()))
