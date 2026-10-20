"""Build a clean AstrBot ZIP using an explicit source manifest, never runtime data."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def build(destination):
    manifest = json.loads((ROOT / "release-files.json").read_text(encoding="utf-8"))
    files = {}
    for relative in manifest:
        path = ROOT / relative
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise ValueError("invalid release manifest")
        if relative.startswith("adapters/astrbot/"):
            files[relative.removeprefix("adapters/astrbot/")] = path
        elif relative.startswith("src/"):
            files[relative.removeprefix("src/")] = path
        elif relative in ("README.md", "LICENSE") or relative.startswith("docs/"):
            files[relative] = path
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, path in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    print(json.dumps({"archive": str(destination), "sha256": digest, "files": len(files)}))
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    build(parser.parse_args().output)
