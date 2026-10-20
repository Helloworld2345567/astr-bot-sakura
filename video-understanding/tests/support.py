"""Always test this checkout, with isolated host data and no personal runtime."""
import atexit
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
sys.path.insert(0, str(ROOT / "src"))
TEMP_ROOT = WORKSPACE / ".codex" / "copies"
TEMP_ROOT.mkdir(parents=True, exist_ok=True)
DATA = tempfile.TemporaryDirectory(prefix="video-test-host-", dir=TEMP_ROOT)
atexit.register(DATA.cleanup)
os.environ["ASTRBOT_ROOT"] = DATA.name


def result_payload(**overrides):
    return {"received_media": True, "summary": "合成视频中的色块变化。",
            "visual_observations": [{"at_seconds": .5, "text": "蓝色色块"}],
            "audio_observations": [{"at_seconds": .5, "text": "测试音"}],
            "spoken_quotes": [], "onscreen_text": [], "inferences": [], "uncertainty": [],
            "warnings": [], **overrides}
