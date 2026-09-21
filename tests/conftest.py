"""Make the integration importable from the tests.

``calc.py`` is deliberately stdlib-only, so the pure tests import it
straight from the package directory without Home Assistant installed.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "custom_components" / "area_averages"))
