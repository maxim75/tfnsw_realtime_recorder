"""Run the recorder service locally (web page on http://127.0.0.1:8000).

    uv run main.py

Reads configuration from the environment or a .env file; see README.md.
Data goes to ./data unless DATA_DIR is set.
"""
import os

import uvicorn

from recorder.config import load_dotenv


def main() -> None:
    load_dotenv()
    os.environ.setdefault("DATA_DIR", "data")
    uvicorn.run(
        "recorder.web:create_app",
        factory=True,
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "8000")),
        timeout_graceful_shutdown=25,
    )


if __name__ == "__main__":
    main()
