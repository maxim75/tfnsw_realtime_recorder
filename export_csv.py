"""Combine recorded vehicle positions from S3 into one CSV for https://kepler.gl/demo.

    uv run export_csv.py --date 2026-10-16 --feed buses --route 333

Run with --help for all options.
"""
import sys

from recorder.export import main

if __name__ == "__main__":
    sys.exit(main())
