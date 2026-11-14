from __future__ import annotations

import os
import time
from functools import lru_cache
from typing import Any, Callable, Sequence

from mem_ol.config import Settings, get_settings
from mem_ol.log import get_logger

logger = get_logger(__name__)

Scorer = Callable[[str, Sequence[str]], Sequence[float]]


class RerankService:
    """实体/上下文候选精排。

    默认权重：实体 0.6，上下文 0.4。两部分分别取 top-k，再分别按权重输出。
    精排模型不可用时降级为路由分数归一化，不阻塞 API。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        scorer: Scorer | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._scorer = scorer
        self._model = None

    def rerank(
        self,
        *,
        query: str,
        entities: Sequence[dict[str, Any]],
        contexts: Sequence[dict[str, Any]],
        entity_top_k: int = 10,
        context_top_k: int = 10,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        entity_started = time.perf_counter()
        entities_out = self._rerank_entities(query, entities, entity_top_k)
        entity_cost_ms = int((time.perf_counter() - entity_started) * 1000)
        context_started = time.perf_counter()
        contexts_out = self._rerank_contexts(query, contexts, context_top_k)
        context_cost_ms = int((time.perf_counter() - context_started) * 1000)
        logger.info(
            "精排完成",
            extra={
                "event": "rerank.done",
                "entity_count": len(entities_out),
                "context_count": len(contexts_out),
                "entity_cost_ms": entity_cost_ms,
                "context_cost_ms": context_cost_ms,
                "cost_ms": int((time.perf_counter() - started) * 1000),
            },
        )
        return {
            "entities": entities_out,
            "contexts": contexts_out,
            "scores": {
                "entity": {
                    str(item.get("entity_id")): round(
                        float(item.get("local_score", 0.0)), 6
                    )
                    for item in entities_out
                },
                "context": {
                    str(item.get("chunk_no")): round(
                        float(item.get("local_score", 0.0)), 6
                    )
                    for item in contexts_out
                },
            },
        }

    # ---------- entity ----------

    def _rerank_entities(
        self,
        query: str,
        candidates: Sequence[dict[str, Any]],
        top_k: int,
    ) -> list[dict[str, Any]]:
        if not candidates:
            return []
        texts = [_entity_text(item) for item in candidates]
        rerank_scores = self._score(query, texts)
        route_scores = [float(item.get("rrf_score", 0.0)) for item in candidates]
        return self._combine(
            candidates,
            rerank_scores,
            route_scores,
            top_k=top_k,
            kind_weight=self.settings.entity_weight,
        )

    # ---------- context ----------

    def _rerank_contexts(
        self,
        query: str,
        candidates: Sequence[dict[str, Any]],
        top_k: int,
    ) -> list[dict[str, Any]]:
        if not candidates:
            return []
        texts = [_context_text(item) for item in candidates]
        rerank_scores = self._score(query, texts)
        route_scores = [float(item.get("score", 0.0)) for item in candidates]
        return self._combine(
            candidates,
            rerank_scores,
            route_scores,
            top_k=top_k,
            kind_weight=self.settings.context_weight,
        )

    # ---------- core ----------

    def _combine(
        self,
        candidates: Sequence[dict[str, Any]],
        rerank_scores: Sequence[float] | None,
        route_scores: Sequence[float],
        *,
        top_k: int,
        kind_weight: float,
    ) -> list[dict[str, Any]]:
        if rerank_scores is None:
            rerank_values = _minmax(route_scores)
        else:
            # bge-reranker 输出的是 sigmoid 分数，直接使用，避免 min-max 放大差距。
            rerank_values = [
                max(0.0, min(1.0, float(score))) for score in rerank_scores
            ]
        route_norm = _minmax(route_scores)

        ranked: list[dict[str, Any]] = []
        for index, candidate in enumerate(candidates):
            local_score = 0.75 * rerank_values[index] + 0.25 * route_norm[index]
            item = dict(candidate)
            item["rerank_score"] = (
                float(rerank_scores[index])
                if rerank_scores is not None
                else None
            )
            item["local_score"] = local_score
            item["weighted_score"] = kind_weight * local_score
            item["kind_weight"] = kind_weight
            ranked.append(item)

        ranked.sort(key=lambda item: item["local_score"], reverse=True)
        return ranked[:top_k]

    def _score(
        self, query: str, texts: Sequence[str]
    ) -> list[float] | None:
        if not texts or not self.settings.rerank_enabled:
            return None
        try:
            if self._scorer is not None:
                scores = list(self._scorer(query, texts))
                if len(scores) != len(texts):
                    raise ValueError("rerank 分数数量与候选数量不一致")
                return [float(score) for score in scores]

            model = self._load_model()
            pairs = [(query, text[:600]) for text in texts]
            try:
                scores = model.predict(
                    pairs,
                    batch_size=self.settings.rerank_batch_size,
                    show_progress_bar=False,
                )
            except TypeError:
                scores = model.predict(
                    pairs, batch_size=self.settings.rerank_batch_size
                )
            return [float(score) for score in scores]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "精排失败，降级为路由分数",
                extra={"event": "rerank.failed", "error": str(exc)},
            )
            return None

    def warmup(self) -> None:
        """启动时预热模型，避免首个请求承担加载耗时。"""
        if self.settings.rerank_enabled:
            self._load_model()

    def _load_model(self):
        if self._model is None:
            path = self.settings.rerank_model_path
            if not os.path.isdir(path):
                raise FileNotFoundError(f"精排模型不存在: {path}")
            os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
            os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
            try:
                import transformers

                transformers.utils.logging.disable_progress_bar()
                transformers.logging.set_verbosity_error()
            except Exception:  # noqa: BLE001
                pass

            from sentence_transformers import CrossEncoder

            logger.info(
                "加载精排模型",
                extra={"event": "rerank.model.load", "path": path},
            )
            self._model = CrossEncoder(
                path,
                device=self.settings.rerank_device,
                max_length=self.settings.rerank_max_length,
            )
        return self._model


def _entity_text(candidate: dict[str, Any]) -> str:
    parts = [
        str(candidate.get("canonical_name") or ""),
        f"类型：{candidate.get('entity_type', 'other')}",
    ]
    aliases = candidate.get("aliases") or []
    if aliases:
        parts.append("别名：" + "、".join(str(alias) for alias in aliases))
    summary = candidate.get("summary")
    if summary:
        parts.append(str(summary))
    for attribute in candidate.get("attributes", [])[:4]:
        key = attribute.get("key")
        value = attribute.get("value")
        if key and value:
            parts.append(f"{key}={value}")
    for relation in candidate.get("relations", [])[:4]:
        source = relation.get("source_name")
        target = relation.get("target_name")
        rel_label = relation.get("relation_label")
        if source and target and rel_label:
            parts.append(f"{source}-{rel_label}->{target}")
    return "；".join(part for part in parts if part)[:320]


def _context_text(candidate: dict[str, Any]) -> str:
    # 精排只看命中的子块（约 80~120 字）；父块只用于召回后展开，不参与精排。
    text = candidate.get("content") or candidate.get("parent_content") or ""
    return str(text).strip()[:320]


def _minmax(values: Sequence[float]) -> list[float]:
    if not values:
        return []
    low = min(values)
    high = max(values)
    if high - low < 1e-9:
        return [0.5 for _ in values]
    return [(value - low) / (high - low) for value in values]


@lru_cache(maxsize=1)
def get_rerank_service() -> RerankService:
    return RerankService()
