# 实体记忆与 MCP 工具设计（下一阶段）

> 设计稿，不是已实现代码。当前代码已有文本父子段向量化、基础向量召回、GM 对话、FastAPI/Streamlit。本文描述实体层、API 2/3、MCP 工具与精排接口。

## 1. 总体流程

```text
用户输入
  │
  ├─ API 2（轮次开始，同步）
  │   1. 总结本轮情境
  │   2. 生成假设性实体
  │   3. 假设实体 → 关键词召回（Neo4j）+ 向量召回（PostgreSQL）
  │   4. 拉取真实体详情、1~2 跳关系、上下文片段
  │   5. 实体候选 + 上下文候选 → 联合精排（后续）
  │   6. 返回 recall packet，写 retrieval_traces
  │
  ├─ API 1（主 GM，同步）
  │   输入 recall packet + 未入库对话
  │   LLM 生成 GM 回复
  │
  └─ API 3（轮次结束，异步）
      1. 拿到 API 2 的假设实体、真实体候选、上下文、本轮玩家输入、GM 回复
      2. LLM 判断哪些实体存在、哪些不存在，并抽取实体属性与关系变更
      3. 输出 create / update / skip 决策 JSON
      4. 校验后经 MCP entity_commit_plan 写库
      5. 写 retrieval_trace / tool_call_logs
```

核心边界：

- **Neo4j 是实体、属性、关系的唯一权威来源**。
- **PostgreSQL 不存权威实体**，只存两类数据：
  1. 实体名称/类型的派生向量索引，供向量召回；
  2. 召回轨迹、mention、工具调用审计等留痕数据。
- API 2/3 不直接访问数据库，只调用 MCP 工具。
- API 3 是实体与关系的唯一写入口；LLM 只输出决策 JSON，不生成 provenance，也不直接调用写工具。
- 属性变更 append-only；关系只新增，不覆盖。

## 2. 实体类型与身份

### 2.1 类型枚举

| 类型 | 含义 | 示例 |
|---|---|---|
| `character` | 人物/NPC | 石猴、老猿 |
| `location` | 地点/场景 | 花果山、水帘洞 |
| `item` | 物品/道具 | 石碣、石碗 |
| `faction` | 势力/组织 | 猴群、天庭 |
| `event` | 事件 | 石猴出世、猴王争夺 |
| `concept` | 设定/规则/概念 | 修仙、天地大数 |
| `other` | 其他 | 无法归类 |

> 当前测试沿用通用 7 类；后续可扩展游戏化类型，但需要同步提示词、校验器和索引。

### 2.2 身份

- `entity_id`：UUID，由 MCP 写工具生成，Neo4j 与 PostgreSQL 共用。
- `canonical_name`：规范名。
- `normalized_name`：NFKC、去首尾空白、拉丁字母小写、全角转半角；用于精确/包含匹配。
- `aliases`：别名列表；新增别名同时写一条 `AttributeChange(key='alias')` 留痕。
- 同名不同实体允许存在；由 API 3 根据候选证据与类型判断，不强制名称唯一。
- 合并暂不实现，预留 `status`、`merged_into`。

## 3. PostgreSQL：派生向量索引 + 留痕

现有 `threads/messages/ingestion_batches/chunks` 保留。实体权威数据不在 PG。

### 3.1 entity_index（派生索引，供向量召回）

```sql
CREATE TABLE IF NOT EXISTS entity_index (
    entity_id uuid PRIMARY KEY,
    thread_id uuid NOT NULL,
    canonical_name text NOT NULL,
    normalized_name text NOT NULL,
    entity_type text NOT NULL
        CHECK (entity_type IN ('character','location','item','faction','event','concept','other')),
    aliases jsonb NOT NULL DEFAULT '[]'::jsonb,
    embedding_text text NOT NULL,
    embedding vector(1024) NOT NULL,
    index_status text NOT NULL DEFAULT 'ready'
        CHECK (index_status IN ('ready','pending','failed')),
    last_indexed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_entity_index_thread
    ON entity_index (thread_id, entity_type);
CREATE INDEX IF NOT EXISTS idx_entity_index_hnsw
    ON entity_index USING hnsw (embedding vector_cosine_ops);
```

- 这是 Neo4j 实体的**派生投影**，可随时从 Neo4j 重建。
- `embedding_text` 建议：`名称：{canonical_name}；别名：{aliases}；类型：{entity_type}`。
- 属性不进入实体向量；属性变化不触发重新向量化。只有名称、别名、类型变化才更新索引。
- `index_status=pending/failed` 表示 Neo4j 已写入但 PG 索引尚未同步，由后台重试或重建。

### 3.2 entity_mentions（写入留痕）

```sql
CREATE TABLE IF NOT EXISTS entity_mentions (
    id uuid PRIMARY KEY,
    entity_id uuid NOT NULL,
    thread_id uuid NOT NULL,
    batch_id uuid REFERENCES ingestion_batches(id) ON DELETE SET NULL,
    trace_id uuid,
    turn_no integer,
    surface_form text NOT NULL,
    message_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    chunk_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    chunk_nos jsonb NOT NULL DEFAULT '[]'::jsonb,
    occurred_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_entity_mentions_entity
    ON entity_mentions (entity_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_entity_mentions_trace
    ON entity_mentions (trace_id);
```

### 3.3 retrieval_traces（统一召回轨迹）

```sql
CREATE TABLE IF NOT EXISTS retrieval_traces (
    id uuid PRIMARY KEY,
    thread_id uuid NOT NULL,
    trace_id uuid NOT NULL,
    turn_no integer,
    query_text text NOT NULL,
    context_summary text,
    hypothetical_entities jsonb NOT NULL DEFAULT '[]'::jsonb,
    entity_keyword_candidates jsonb NOT NULL DEFAULT '[]'::jsonb,
    entity_vector_candidates jsonb NOT NULL DEFAULT '[]'::jsonb,
    entity_fused_candidates jsonb NOT NULL DEFAULT '[]'::jsonb,
    context_candidates jsonb NOT NULL DEFAULT '[]'::jsonb,
    rerank_scores jsonb NOT NULL DEFAULT '{}'::jsonb,
    selected jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_retrieval_traces_thread_turn
    ON retrieval_traces (thread_id, turn_no);
CREATE INDEX IF NOT EXISTS idx_retrieval_traces_trace
    ON retrieval_traces (trace_id);
```

候选 JSON 统一结构：

```json
{
  "kind": "entity",
  "id": "uuid",
  "chunk_no": null,
  "name": "石猴",
  "route": "keyword",
  "route_rank": 1,
  "route_score": 0.82,
  "rrf_score": 0.031,
  "rerank_score": null,
  "matched_field": "alias",
  "batch_id": null,
  "turn_start": null,
  "turn_end": null,
  "message_ids": []
}
```

### 3.4 entity_index_jobs（可选：派生索引重试）

```sql
CREATE TABLE IF NOT EXISTS entity_index_jobs (
    job_id uuid PRIMARY KEY,
    entity_id uuid NOT NULL,
    thread_id uuid NOT NULL,
    reason text NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','done','failed')),
    retry_count integer NOT NULL DEFAULT 0,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
```

这不是跨库事务 outbox，只是“Neo4j 已写、PG 索引没跟上”的重试队列。demo 阶段可以先只记日志，后续再加。

### 3.5 tool_call_logs（MCP 审计）

```sql
CREATE TABLE IF NOT EXISTS tool_call_logs (
    id uuid PRIMARY KEY,
    trace_id uuid,
    thread_id uuid,
    tool_name text NOT NULL,
    arguments jsonb NOT NULL DEFAULT '{}'::jsonb,
    result_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    success boolean NOT NULL,
    error text,
    duration_ms integer,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tool_call_logs_trace
    ON tool_call_logs (trace_id, created_at);
```

## 4. Neo4j：实体权威图

### 4.1 节点

```cypher
(:Entity {
  entity_id: "uuid",
  thread_id: "uuid",
  user_id: null,
  canonical_name: "水帘洞",
  normalized_name: "水帘洞",
  entity_type: "location",
  aliases: ["洞府", "瀑布后的洞"],
  summary: "花果山瀑布后的石洞",
  status: "active",
  merged_into: null,
  first_seen_at: datetime,
  last_seen_at: datetime,
  last_batch_id: "uuid"
})
```

```cypher
(:AttributeChange {
  change_id: "uuid",
  key: "state",
  value: "洞中无人，石桌石碗尚存",
  occurred_at: datetime,
  batch_id: "uuid",
  trace_id: "uuid",
  turn_no: 6,
  source_chunk_nos: ["B0002-P003-C002"],
  source_message_ids: ["uuid"],
  evidence: "洞里有石桌石碗，我想在这里住下。"
})
```

关系：

```cypher
(:Entity)-[:HAS_ATTRIBUTE]->(:AttributeChange)

(:Entity)-[:REL {
  rel_id: "uuid",
  relation_label: "位于",
  thread_id: "uuid",
  batch_id: "uuid",
  occurred_at: datetime,
  source_chunk_nos: [],
  source_message_ids: [],
  evidence: "..."
}]->(:Entity)
```

- `relation_label` 是 2~40 字自由短语，不做枚举限制；同一对实体的不同标签是不同边。
- 允许多重边；不覆盖旧关系。
- 属性当前值 = 按 `occurred_at DESC` 取第一条；旧值全部保留。
- `summary` 是可更新的展示摘要，不承载事实变更；事实一律进 `AttributeChange`。
- 新增别名同时写 `AttributeChange(key='alias')`，并更新 `Entity.aliases`（Cypher 内去重）。

### 4.2 约束与索引

```cypher
CREATE CONSTRAINT entity_id_unique IF NOT EXISTS
FOR (e:Entity) REQUIRE e.entity_id IS UNIQUE;

CREATE INDEX entity_thread IF NOT EXISTS
FOR (e:Entity) ON (e.thread_id);

CREATE INDEX attribute_thread_time IF NOT EXISTS
FOR (a:AttributeChange) ON (a.thread_id, a.occurred_at);

CREATE INDEX rel_thread IF NOT EXISTS
FOR ()-[r:REL]-() ON (r.thread_id);

CREATE FULLTEXT INDEX entity_name_fulltext IF NOT EXISTS
FOR (e:Entity) ON EACH [e.canonical_name, e.aliases]
OPTIONS { indexConfig: { `fulltext.analyzer`: 'cjk' } };
```

> 关键词召回优先用 Neo4j 全文索引；若目标 Neo4j 版本/配置不支持 CJK analyzer，降级为 `CONTAINS` 查询 canonical_name/aliases。

### 4.3 当前属性与多跳

```cypher
MATCH (e:Entity {entity_id: $entity_id, thread_id: $thread_id})
      -[:HAS_ATTRIBUTE]->(a:AttributeChange)
WITH a.key AS key, a ORDER BY a.occurred_at DESC
WITH key, collect(a)[0] AS latest
RETURN key, latest.value, latest.occurred_at, latest.evidence
```

```cypher
MATCH (e:Entity {entity_id: $entity_id, thread_id: $thread_id})
      -[r:REL*1..2]-(n:Entity)
WHERE all(x IN nodes(r) WHERE x.thread_id = $thread_id)
RETURN e, r, n
LIMIT $limit
```

## 5. API 2：假设实体 + 双路召回

### 5.1 流程

1. 编排层直接调用 `store.get_recent_turns(thread_id, 5)` 取最近 5 轮原始消息（含已入库），拼入提示词；**确定性读取不走 MCP**。
2. API 2 两路并行分析（[`prompts/api2_context.md`](../prompts/api2_context.md)、[`prompts/api2_entities.md`](../prompts/api2_entities.md)）：
   - `api2_context`：只看上一轮，输出 `context_summary` + `rewritten_query`；
   - `api2_entities`：看最近 5 轮，输出扁平检索词列表（实体名、别名、关键词），最多 12 个。
   - 上下文检索用 `rewritten_query`，实体检索用抽取出的实体；LLM 失败时用 `QueryEnhancer` 原文兜底。
3. 对每个假设实体：
   - `entity_recall_keyword` 服务方法 → Neo4j 关键词候选。
   - `entity_recall_vector` 服务方法 → PG 向量候选。
4. 按 `entity_id` 去重，保留两路名次与分数；RRF 融合。
5. `entity_get` 取融合后 top10 实体的属性与 1 跳关系。
6. `entity_neighborhood(seed_ids, depth=2)` 做 1~2 跳扩展：
   - 节点按 `entity_id` 去重，边按 `rel_id` 去重；
   - 跳过节点重复出现的环路路径；
   - 新邻居补 `entity_get` 详情后与种子实体合并；最多补 4 个邻居。
7. `context_recall_vector(thread_id, rewritten_query + context_summary, k=30)` 取上下文子块。
8. 上下文按 `parent_no` 去重：同一父块只保留向量得分最高的子块作为代表，其余 siblings 记入 `child_chunk_nos`；取 top10 父块。
9. 精排：实体/上下文各取 top10，用 `bge-reranker-v2-m3` 打分；实体权重 0.6、上下文 0.4。
10. `retrieval_trace_write` 写完整轨迹；`selected` 中记录 `context_parents`、`child_chunk_nos`、`graph_paths`、`query_analysis`。
11. 返回 recall packet 给 API 1。

### 5.2 输出

```json
{
  "trace_id": "uuid",
  "entities": ["水帘洞", "洞府", "瀑布"],
  "entity_candidates": [
    {
      "entity_id": "uuid",
      "canonical_name": "水帘洞",
      "entity_type": "location",
      "aliases": ["洞府"],
      "score": 0.88,
      "routes": ["keyword", "vector"],
      "rrf_score": 0.0328,
      "attributes": {"state": "无人"},
      "relations": []
    }
  ],
  "context_candidates": [
    {
      "chunk_no": "B0001-P002-C002",
      "parent_no": "B0001-P002",
      "content": "...",
      "score": 0.49,
      "turn_start": null,
      "turn_end": null,
      "message_ids": []
    }
  ]
}
```

## 6. API 3：实体与关系固化

API 3 是实体与关系的**唯一写入口**。它拿到：本轮玩家输入、GM 回复、API 2 假设实体、真实体候选、上下文片段、`trace_id`、`turn_no`。它判断实体存在性，并抽取属性变更与关系变更。

### 6.1 LLM 输出

```json
{
  "turn_summary": "玩家进入水帘洞，确认洞内无人但石桌石碗尚存。",
  "decisions": [
    {
      "action": "update",
      "matched_entity_id": "uuid",
      "match_confidence": 0.93,
      "canonical_name": "水帘洞",
      "entity_type": "location",
      "aliases": ["洞府"],
      "summary": "花果山瀑布后的石洞",
      "surface_form": "洞府",
      "attribute_changes": [
        {
          "key": "state",
          "value": "洞中无人，石桌石碗尚存",
          "evidence": "洞里空着，石桌石碗尚存"
        }
      ],
      "relations": [
        {
          "target_ref": "uuid-of-花果山",
          "relation_label": "位于",
          "evidence": "水帘洞位于花果山瀑布之后"
        }
      ]
    },
    {
      "action": "create",
      "matched_entity_id": null,
      "match_confidence": 0.0,
      "canonical_name": "石桌石碗",
      "entity_type": "item",
      "aliases": [],
      "summary": "水帘洞内的石制家具",
      "surface_form": "石桌石碗",
      "attribute_changes": [
        {"key": "location", "value": "水帘洞", "evidence": "洞里有石桌石碗"}
      ],
      "relations": [
        {
          "target_ref": "水帘洞",
          "relation_label": "位于",
          "evidence": "石桌石碗在水帘洞内"
        }
      ]
    }
  ]
}
```

### 6.2 执行流程

1. LLM 调用 [`prompts/entity_commit.md`](../prompts/entity_commit.md)。
2. 校验 JSON：字段、枚举、长度；候选 `entity_id` 必须属于当前线程且状态 active。
3. 为决策补 provenance：`thread_id`、`trace_id`、`turn_no`、`batch_id`、`source_chunk_nos`、`source_message_ids`、`occurred_at`。
4. MCP `entity_commit_plan`：
   - **先写 Neo4j**（权威）：创建/匹配 `:Entity`，追加 `:AttributeChange`，创建 `:REL`，记录 aliases。
   - **再同步 PG 索引**：为新建/改名实体 upsert `entity_index` 与向量；失败则记录 `entity_index_jobs`，由后台重建。
   - 写 `entity_mentions`（PG 留痕）。
   - 事务边界：Neo4j 事务成功后即使 PG 索引失败也算实体写入成功；索引最终一致。
5. 返回每个 decision 的 `entity_id/action/applied_counts`。
6. 写 `tool_call_logs`。

### 6.3 幂等

- `idempotency_key = f"{thread_id}:{turn_no}:{trace_id}"`。
- 同一 trace 重放时，`entity_commit_plan` 返回已有结果，不重复创建实体/变更/关系。
- 属性变更按 `(entity_id, key, value, occurred_at, batch_id)` 去重；关系按 `rel_id` 幂等。

## 7. MCP 与确定性读取

MCP server 使用 Python MCP SDK 的 `MCPServer`，通过 **streamable HTTP** 暴露在 `127.0.0.1:8765`（`scripts/run_mcp_server.py`）。

边界原则：

- **MCP 只暴露需要模型决策后的写工具**：当前只有 `entity_commit_plan`。
- **确定性读取/召回不做 MCP 工具**：最近消息、关键词/向量召回、实体详情、多跳、chunk 查询、召回轨迹写入，全部由编排层直接调用 `EntityToolService` / `PostgresStore` 服务方法。
- LLM 不直接调用写工具；LLM 只输出 JSON，编排层校验后调用 `entity_commit_plan`。

### 7.1 确定性读取/召回（服务方法）

| 方法 | 输入 | 输出 | 数据源 |
|---|---|---|---|
| `store.get_un_ingested_messages` | `thread_id, limit` | 未入库消息列表 | PG |
| `context_recall_vector` | `thread_id, query, k=30` | chunk 候选（chunk_no/parent_no/content/score/turn/message_ids） | PG |
| `entity_recall_keyword` | `thread_id, keywords[], limit=10` | 实体候选（entity_id/name/type/aliases/score/matched_field） | Neo4j |
| `entity_recall_vector` | `thread_id, query, limit=10` | 实体候选（entity_id/name/type/score） | PG entity_index |
| `entity_get` | `thread_id, entity_id, include_attributes=true, include_relations=true` | 实体详情、最新属性、1 跳关系 | Neo4j |
| `entity_neighborhood` | `thread_id, entity_ids[], depth=2, limit=20` | 节点与边（均带 thread_id） | Neo4j |
| `chunk_get` | `thread_id, chunk_nos[]` | 片段详情与溯源字段 | PG |
| `retrieval_trace_write` | `thread_id, trace_id, turn_no, ...` | `trace_id` | PG |

### 7.2 MCP 写工具

| 工具 | 输入 | 输出 | 说明 |
|---|---|---|---|
| `entity_commit_plan` | `thread_id, trace_id, turn_no, batch_id, occurred_at, decisions[], source_chunk_nos[], source_message_ids[]` | 每个决策的 `entity_id/action/applied_counts` | 唯一实体/关系写入口；Neo4j 主写，PG 索引同步 |

### 7.3 规则

- 所有服务方法与写工具强制 `thread_id`；MCP server 先校验线程存在。
- 不接受 SQL/Cypher 字符串，只接受结构化参数。
- 写工具使用 `idempotency_key`。
- 工具调用写 `tool_call_logs`，带 `trace_id`、耗时、成功/失败。
- 不返回 Neo4j internal id 等内部字段。
- LLM 永远不直接调用写工具；写工具只由 API 3 编排层在 JSON 校验后调用。

## 8. 精排（已实现）

```python
@dataclass
class RecallCandidate:
    kind: Literal["entity", "context"]
    candidate_id: str
    text: str                 # 送精排的文本
    route: str                # keyword/vector/context
    route_rank: int
    route_score: float
    metadata: dict            # entity_id/chunk_no/turn/message_ids...

class Reranker(Protocol):
    def score(self, query: str, candidates: list[RecallCandidate]) -> list[float]: ...
```

- 模型：本地 `D:/RAGPRO/rag-pro/rag_system/models/BAAI/bge-reranker-v2-m3`。
- 实体候选与上下文候选各取 top10；上下文在精排前已按父块去重，实体在精排前已按 `entity_id` 去重并完成多跳扩展。
- 实体权重 0.6、上下文权重 0.4（`ENTITY_WEIGHT` / `CONTEXT_WEIGHT`）。
- 计算：`local_score = 0.75 * rerank_score + 0.25 * route_score_norm`；`weighted_score = kind_weight * local_score`。
- 精排分数与最终选择写入 `retrieval_traces.rerank_scores/selected`；模型不可用时降级为路由分数。
- 主 prompt 使用实体 top6、上下文 top4，避免 token 膨胀；完整 top10 保留在 recall packet 和轨迹里。
- 性能：精排只送命中的 80~120 字子块和压缩到 320 字内的实体文本，`max_length=192`；实测 CPU 约 2.6 秒/10 个实体、1.5 秒/10 个上下文；模型在 API 启动时预热。

## 9. 留痕矩阵

| 标识 | 产生方 | 落点 |
|---|---|---|
| `trace_id` | API 2 | retrieval_traces、entity_mentions、tool_call_logs、AttributeChange/REL |
| `entity_id` | entity_commit_plan | Neo4j Entity、PG entity_index/mentions |
| `chunk_id/chunk_no` | 记忆管道 | chunks、entity_mentions、AttributeChange/REL 的 source 字段、retrieval_traces |
| `message_id/turn_no` | 对话引擎 | messages、entity_mentions、AttributeChange/REL、retrieval_traces |
| `batch_id/batch_seq` | 记忆管道 | ingestion_batches、chunks、entity_mentions、AttributeChange/REL |
| `change_id/rel_id` | entity_commit_plan | Neo4j AttributeChange/REL；返回给 API 3 |
| `index_job_id` | entity_index 同步失败 | entity_index_jobs、tool_call_logs |

## 10. 已定与待确认

已定：

1. **API 3 负责实体与关系**：从输入中判断存在性，抽取属性变更与关系变更，经 MCP 写入。
2. **匹配阈值**：暂定 `match_confidence >= 0.75`，测过再调。
3. **MCP 传输**：streamable HTTP（`127.0.0.1:8765`）；API 2/3 用 MCP client。LLM 不直接绑定写工具。
4. **实体类型**：先用通用 7 类，测试后再扩展。
5. **存储主从**：Neo4j 权威；PostgreSQL 只做实体名向量索引 + 留痕。写入先 Neo4j，后 PG 索引；PG 失败不阻塞实体写入，由索引重试/重建补偿。
6. **精排**：`bge-reranker-v2-m3`；实体/上下文各 top10；实体权重 0.6、上下文 0.4；主 prompt 取实体 top6、上下文 top4。

关于 outbox：

- outbox（事务性发件箱）是“把待执行的外部副作用和业务写入放进同一事务，再由后台任务执行”的模式，用来避免两个数据库之间丢更新。
- 本设计以 Neo4j 为权威，PG 是派生索引，所以**不需要跨库 outbox**；只需要一个轻量的索引重试队列（可选 `entity_index_jobs`）或后台重建任务。
- 如果后续要求 PG 与 Neo4j 强一致，再引入 outbox。

测试文本提醒：当前《西游记》属于 LLM 常识，只能验证链路；验收记忆效果需换私有/合成世界观文本。

## 11. LangGraph 编排与 checkpoint（已实现）

- 图：`src/mem_ol/graph/conversation.py`，节点 `prepare → pre_turn → generate → persist → END`。
- state：`thread_id/turn_no/user_message/pre_turn/reply/message_ids/trace_id/pending_turns/commit_required/ingest_required/turn_checkpoint_id`。
- checkpoint：`PostgresSaver` 使用同一 PostgreSQL，按 `thread_id` 保存；每轮结束取最终 `checkpoint_id` 写回 `messages.checkpoint_id`。
- 关键点：LangGraph 会恢复上一轮 state；`prepare` 必须从 DB 重算 `turn_no` 并清空上一轮临时字段，否则第二轮会复用旧 turn_no。
- API 3 与 5 轮入库仍作为后台任务，由图最终 state 提供 `pre_turn/reply/message_ids`。
- checkpoint 保留最近 5 轮（`checkpoint_retention_turns`）：按 checkpoint 的 `channel_values.turn_no` 保留窗口，清理旧 checkpoints/writes/未被引用 blobs，并置空旧消息的 `checkpoint_id`。
- LLM JSON：输出截断时提高 max_tokens 重试；解析失败用 `json_repair` 修复；提示词约束单行字符串、禁用未转义英文双引号。
