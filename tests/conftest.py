"""Make the plugin importable as a package, the way AstrBot loads it.

Plugin modules use relative imports (``from ..core.models import ...``), so tests
import them through the plugin package name, e.g.
``from astrbot_plugin_tieba_moderator.core.models import ContentItem``.
"""

import sys
from pathlib import Path

PLUGINS_DIR = Path(__file__).resolve().parents[2]
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))
