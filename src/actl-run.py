"""Windows entry shim: adds src to sys.path and calls actl.cli:main."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "actl"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from actl.cli import main

if __name__ == "__main__":
    main()
