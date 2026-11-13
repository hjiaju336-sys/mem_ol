from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    openai_base_url: str = ""
    openai_api_key: str = ""
    openai_model: str = "deepseek-v4.1-flash"
    openai_timeout: float = 60.0
    openai_max_retries: int = 0
    openai_temperature: float = 0.8
    dialogue_timeout: float = 120.0
    dialogue_order_base: int = 1_000_000_000

    postgres_dsn: str = "postgresql://mem_ol:mem_ol@localhost:55432/mem_ol"
    neo4j_uri: str = "bolt://localhost:7688"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "mem_ol_dev"

    embedding_model_path: str = "./models/bge-large-zh-v1.5"
    embedding_device: str = "cpu"
    embedding_dim: int = 1024
    embedding_batch_size: int = 16
    embedding_query_instruction: str = "为这个句子生成表示以用于检索相关文章："

    memory_batch_turns: int = 5
    world_extract_window_chars: int = 20000
    world_extract_timeout: float = 240.0
    world_extract_workers: int = 6
    world_consolidate_max_chars: int = 30_000

    parent_chunk_size: int = 600
    parent_chunk_min: int = 250
    parent_chunk_max: int = 900

    child_chunk_size: int = 80
    child_chunk_min: int = 30
    child_chunk_max: int = 120
    child_overlap: int = 20
    chunk_unit_size: int = 80

    semantic_threshold: float = 0.45
    retrieval_top_k: int = 10
    context_recall_top_k: int = 30
    entity_recall_top_k: int = 10
    entity_candidate_top_k: int = 10
    context_candidate_top_k: int = 10
    plot_recall_top_k: int = 3
    plot_recall_min_score: float = 0.3
    plot_arc_size: int = 5
    entity_prompt_top_k: int = 6
    context_prompt_top_k: int = 4
    graph_depth: int = 2
    graph_neighbor_top_k: int = 4
    graph_path_limit: int = 20
    checkpoint_retention_turns: int = 5
    memory_context_char_limit: int = 700
    gm_max_tokens: int = 800
    dialogue_history_turns: int = 5

    rerank_enabled: bool = True
    rerank_model_path: str = (
        "D:/RAGPRO/rag-pro/rag_system/models/BAAI/bge-reranker-v2-m3"
    )
    rerank_device: str = "cpu"
    rerank_max_length: int = 192
    rerank_batch_size: int = 16
    entity_weight: float = 0.6
    context_weight: float = 0.4

    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8765

    log_level: str = "INFO"
    log_file: str = "logs/mem_ol.log"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
