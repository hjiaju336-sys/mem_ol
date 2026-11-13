from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import uvicorn  # noqa: E402


if __name__ == "__main__":
    uvicorn.run(
        "mem_ol.api.main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
    )
