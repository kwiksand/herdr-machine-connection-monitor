import sys
from pathlib import Path

# Add plugin root to sys.path so tests can import `src`
plugin_root = Path(__file__).resolve().parent.parent
if str(plugin_root) not in sys.path:
    sys.path.insert(0, str(plugin_root))
