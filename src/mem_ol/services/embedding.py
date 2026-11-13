from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Sequence

import numpy as np

from mem_ol.config import get_settings
from mem_ol.log import get_logger

logger = get_logger(__name__)


class EmbeddingService:
    """本地 sentence-transformers 模型，输出归一化稠密向量。"""

    def __init__(
        self,
        model_path: str | Path,
        device: str = "cpu",
        dim: int = 1024,
        batch_size: int = 16,
        query_instruction: str = "",
    ) -> None:
        path = Path(model_path)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[3] / path
        self.model_path = path
        self.device = device
        self.dim = dim
        self.batch_size = batch_size
        self.query_instruction = query_instruction
        self._model = None

    def _load_model(self):
        if self._model is None:
            import os

            os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
            os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

            from sentence_transformers import SentenceTransformer

            try:
                import transformers

                transformers.utils.logging.disable_progress_bar()
                transformers.logging.set_verbosity_error()
            except Exception:  # noqa: BLE001
                pass

            logger.info(
                "加载 embedding 模型",
                extra={"event": "embedding.model.load", "path": str(self.model_path)},
            )
            self._model = SentenceTransformer(
                str(self.model_path), device=self.device
            )
            get_dimension = getattr(
                self._model, "get_embedding_dimension", None
            ) or self._model.get_sentence_embedding_dimension
            actual_dim = get_dimension()
            if actual_dim != self.dim:
                raise ValueError(
                    f"embedding 维度不一致: 模型={actual_dim}, 配置={self.dim}"
                )
        return self._model

    def warmup(self) -> None:
        """启动时预热模型。"""
        self._load_model()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load_model()
        vectors = model.encode(
            list(texts),
            batch_size=self.batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        array = np.asarray(vectors, dtype=np.float32)
        if array.ndim == 1:
            array = array.reshape(1, -1)
        if array.shape[1] != self.dim:
            raise ValueError(
                f"embedding 维度不一致: 输出={array.shape[1]}, 配置={self.dim}"
            )
        return array.tolist()

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def embed_query(self, text: str) -> list[float]:
        return self.embed([f"{self.query_instruction}{text}"])[0]

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self.embed(
            [f"{self.query_instruction}{text}" for text in texts]
        )


@lru_cache(maxsize=1)
def get_embedding_service() -> EmbeddingService:
    settings = get_settings()
    return EmbeddingService(
        model_path=settings.embedding_model_path,
        device=settings.embedding_device,
        dim=settings.embedding_dim,
        batch_size=settings.embedding_batch_size,
        query_instruction=settings.embedding_query_instruction,
    )
