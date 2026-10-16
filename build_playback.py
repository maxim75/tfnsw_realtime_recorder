"""Build an optimized local playback file for one day from S3 (see recorder/playback.py).

    uv run build_playback.py --date 2026-10-16
"""
import sys

from recorder.playback import main

if __name__ == "__main__":
    sys.exit(main())
