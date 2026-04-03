"""Herald CLI — Python wrapper that delegates to the bash CLI.

This allows `herald speak/pause/resume/...` to work after `pip install heyvox`.
"""

import os
import subprocess
import sys

from heyvox.herald import HERALD_BIN, get_herald_home


def main() -> None:
    """Run the Herald CLI with HERALD_HOME set to the package directory."""
    env = os.environ.copy()
    env["HERALD_HOME"] = get_herald_home()

    result = subprocess.run(
        ["bash", str(HERALD_BIN), *sys.argv[1:]],
        env=env,
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
