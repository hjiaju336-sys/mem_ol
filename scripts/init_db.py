from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mem_ol.config import get_settings  # noqa: E402
from mem_ol.log import get_logger, setup_logging  # noqa: E402
from mem_ol.storage.postgres import init_schema  # noqa: E402


def main() -> None:
    setup_logging()
    logger = get_logger(__name__)
    settings = get_settings()
    init_schema(settings.postgres_dsn, ROOT / "src/mem_ol/storage/schema.sql")
    logger.info("数据库 schema 初始化完成", extra={"event": "db.schema.init"})


if __name__ == "__main__":
    main()
