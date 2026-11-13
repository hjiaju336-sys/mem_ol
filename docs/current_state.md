# 当前状态与交接（2026-11-13 清理后）

> 用于在此之上继续修改。详细规格见 `需求`，工程约定见 `CLAUDE.md`，流程图见 `docs/business_flow.md`。

## 定位

AI 文字冒险 / AI TRPG 的叙事记忆层：让 GM 记得住、回得去、主线不崩。

三条能力：

1. 5 轮增量记忆 + 实体关系图 + 双路召回；
2. turn 级回退，消息、向量、图、主线摘要一起回滚；
3. L0/L1/L2 三层主线摘要，防止长对话碎片化。

## 每轮流程

```text
用户输入
  ├─ API2-A 单轮总结 + 查询改写（上一轮单轮）
  ├─ API2-B 实体假设（最近 5 轮）
  ↓ 两路并行
  ├─ 实体召回：关键词一次 + 向量一次 → RRF → 1~2 跳
  ├─ 上下文召回：rewritten_query + context_summary
  └─ 主线召回：L2 常驻 + L1 章节（turn_end <= current-5）
  ↓
  rerank（实体 0.6 / 上下文 0.4）
  ↓
  GM 生成（最近 5 轮原文 + 主线 + 召回）
  ↓
  persist：消息落库、L0 摘要入 plot_nodes
  ↓
  后台：API3 实体固化、5 轮入库、plot 维护（归档/章节/全局）
```

## API 2

- `prompts/api2_context.md`：只看上一轮，输出 `context_summary` + `rewritten_query`；
- `prompts/api2_entities.md`：看最近 5 轮，输出扁平 `entities`；
- 两路独立失败降级；
- 上下文 query 使用 `rewritten_query + context_summary`。

## 主线记忆

`plot_nodes` 三层：

| 层级 | 内容 | 生命周期 |
|---|---|---|
| L0 `turn` | 单轮摘要 | 每轮一次；5 轮内 active，离开窗口后归档并向量化 |
| L1 `arc` | 每 5 条 L0 合并 | 永久保留，带 turn_start/turn_end + 向量 |
| L2 `global` | 全局梗概 | 每个新章节递归合并，常驻 GM |

召回：L2 常驻；L1 只查 `turn_end <= current_turn - 5`，避免和原文窗口重复；没有 L1/L2 时才回退已归档 L0。回退删除对应 L0/L1 与 L2，后续维护重建。

## 世界导入

- 抽取：≤20000 字单窗；长文 20000 字切窗，6 路并行，单次超时 240s，失败窗口二分；
- 整理：按 30000 字符分组树形合并；
- 增量：`append=True`，批次记录 `doc_id/char_start/char_end`，区间重叠拒绝；已有实体按规范名/别名走 update；
- 实体溯源：世界导入实体不挂父块编号；块号只服务向量召回。

## 关键参数

```env
OPENAI_MODEL=qwen-plus
DIALOGUE_TIMEOUT=120

WORLD_EXTRACT_WINDOW_CHARS=20000
WORLD_EXTRACT_TIMEOUT=240
WORLD_EXTRACT_WORKERS=6
WORLD_CONSOLIDATE_MAX_CHARS=30000

MEMORY_BATCH_TURNS=5
CHECKPOINT_RETENTION_TURNS=5
DIALOGUE_HISTORY_TURNS=5

PLOT_ARC_SIZE=5
PLOT_RECALL_TOP_K=3
PLOT_RECALL_MIN_SCORE=0.3
```

## 存储与边界

- PostgreSQL：消息、批次、chunks、entity_index、mentions、traces、plot_nodes、tool logs、checkpoint；
- Neo4j：`:Entity`、`:AttributeChange`（append-only）、`:REL`；
- 实体权威在 Neo4j；PG entity_index 是派生索引；
- 属性当前值按 `source_order`：对话 `turn_no*1000`，世界导入 `batch_seq*1e6+窗口*1000`；
- `rel_id` 由线程 + 两端实体 + 类型决定，重复提交不新增边；
- 回退：mark messages rolled_back + 物理删除 chunks/entity_index/mentions/traces/plot_nodes + Neo4j 删除；
- 后台任务仍是 FastAPI `BackgroundTasks` + `ingest_lock`，服务重启会丢；
- 无多租户/鉴权；无正式评测集。

## 清理记录

- 删除空占位模块：`pipelines/{state,rollback,coreference}.py`、`services/extraction.py`、`maintenance/`、`scripts/demo_scenario.py`；
- 删除旧提示词 `api2_analysis.md`（已拆成 `api2_context.md` / `api2_entities.md`）；
- 删除生成的测试文本：`doupo_{2000,5000,10000,append_2k,smoke}.txt`、`rulin_{20000,150000}.txt`、`xiyouji_001_raw.txt`；保留 `doupo_excerpt.txt`、`rulin_raw.txt`、`rulin_001_002.txt`、`xiyouji_001.txt`；
- 清空 PostgreSQL / Neo4j 中所有测试会话与数据；
- 删除 `.playwright-mcp`、`.pytest_cache`、`__pycache__`、历史日志；
- 当前测试：`41 passed`。
