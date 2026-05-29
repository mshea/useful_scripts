#!/usr/bin/env python3
"""
Backward-compatible wrapper — delegates to the new package-based entry point.

The RSS reader has been refactored into a proper Python package.
See run.py for the main CLI, or import from the rss_reader package directly.
"""

import sys
import os

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from run import main
    main()
