"""
Package entrypoint for `python3 -m src.hogp` (the CLI client).
"""
import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
