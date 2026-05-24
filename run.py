"""Top-level launcher so the container start command works regardless of how Nixpacks installs the project.

Adds the src/ directory to sys.path so `import agentshive` resolves even if the package
was not installed into site-packages.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent
SRC = ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentshive.main import main

if __name__ == "__main__":
    main()
