"""Load the repository package in an isolated AstrBot data directory."""
import atexit
import importlib
import importlib.util
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TEST_ADMIN_ID = "test-owner"
_data = tempfile.TemporaryDirectory(prefix="sakura-test-data-")
atexit.register(_data.cleanup)
os.environ["ASTRBOT_ROOT"] = _data.name
if os.environ.get("ASTRBOT_SOURCE"):
    sys.path.insert(0, os.environ["ASTRBOT_SOURCE"])
package_name = "sakura_plugin_under_test"
spec = importlib.util.spec_from_file_location(
    package_name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
package = importlib.util.module_from_spec(spec)
sys.modules[package_name] = package
spec.loader.exec_module(package)
entry = importlib.import_module(f"{package_name}.main")
plugin = importlib.import_module(f"{package_name}.sakura.runtime")
permissions = importlib.import_module(f"{package_name}.sakura.permissions")
flow = importlib.import_module(f"{package_name}.sakura.message_flow")
web = importlib.import_module(f"{package_name}.sakura.web_reading")
