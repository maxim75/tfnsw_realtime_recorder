"""Export one recorded day as static files for the GitHub Pages playback site.

    uv run export_static.py --date 2026-10-16 --out ../tfnsw-playback-map/data
"""
import sys

from recorder.static_export import main

if __name__ == "__main__":
    sys.exit(main())
