# 当前业务流程与信息处理说明

> 本文描述的是**当前已实现代码**的真实流程，不是最终规格。用于核对理解，发现偏差请直接指出。

## 1. 系统全景

```mermaid
flowchart TD
    U[用户 / Streamlit] --> API[FastAPI /api/threads/.../messages]
    API --> G[LangGraph ConversationGraph]

    G --> P[prepare<br/>取新 turn_no / 清空上一轮字段]
    P --> A2[pre_turn<br/>API 2]
    A2 --> QE[API2 合并 LLM：查询增强 + 实体抽取]
    QE --> KR[Neo4j 关键词召回]
    QE --> VR[PostgreSQL entity_index 向量召回]
    KR --> F[entity_id 去重 + RRF]
    VR --> F
    F --> GE[entity_get + 1~2 跳扩展]
    GE --> CR[上下文子块召回<br/>k=30]
    CR --> CD[parent_no 父块去重]
    GE --> RR[bge-reranker-v2-m3 精排<br/>实体 0.6 / 上下文 0.4]
    CD --> RR
    RR --> GM[generate<br/>主 GM 回复]
    GM --> PS[persist<br/>消息落库]
    PS --> PG[(PostgreSQL)]
    PS --> BG1[后台 API 3<br/>实体/关系固化]
    PS --> BG2[后台 5 轮入库<br/>文本向量化]

    BG1 --> MCP[MCP 工具层]
    MCP --> N4[(Neo4j)]
    MCP --> PG
    BG2 --> CH[语义父子段切分]
    CH --> EMB[embedding]
    EMB --> PG
```

### 阶段 0：世界导入（一次性）

```mermaid
flowchart LR
    T[世界文本] --> C[父子段切分 + embedding<br/>与检索链路一致]
    T --> E[实体抽取<br/>短文本直接抽 / 长文本 20000 字切窗并行]
    E --> A[检查修复 agent<br/>合并实体/别名/关系]
    A --> W[entity_commit_plan<br/>初始化 Neo4j + PG 索引]
```

- 检索切片与实体抽取窗口**解耦**：抽取不复用父子段，避免同一实体反复抽取。
- 短文本（≤20000 字）一次抽取；长文本按 20000 字窗口切分，窗口抽取并行（默认 6 路，单次超时 240s），失败窗口再二分重试；单窗不调整理 agent。
- 所有抽取结果送 `world_consolidate.md` 检查修复 agent，由 LLM 合并重复实体、别名、繁简/异体写法和关系。
- 初始导入要求新线程；已有实体的线程拒绝导入，避免重复。
- 实体/关系经 `entity_commit_plan` 写入 Neo4j，并同步 PostgreSQL 实体向量索引。

存储职责：

- **PostgreSQL**：线程、消息、批次、文本父子段、实体派生向量索引、mention、召回轨迹、工具调用日志、LangGraph checkpoint。
- **pgvector**：文本片段向量、实体名称向量。
- **Neo4j**：实体、属性变更、关系，是实体与关系的权威来源。
- **MCP 工具层**：API 2/3 与数据库之间的唯一入口；当前 API 2/3 进程内调用同一实现，MCP server 也可独立通过 streamable HTTP 暴露。

## 2. 一轮对话时序

```mermaid
sequenceDiagram
    participant U as 用户
    participant API as FastAPI
    participant G as LangGraph
    participant T as EntityToolService
    participant L as LLM
    participant P as PostgreSQL
    participant N as Neo4j

    U->>API: POST /messages
    API->>G: run(thread_id, user_message)
    G->>P: next_turn_no(thread_id)
    G->>P: 直接读取最近 5 轮消息（含已入库）
    G->>L: API2 两路并行（总结+改写 / 实体假设）
    L-->>G: context_summary + rewritten_query + entities
    G->>T: entity_recall_keyword / entity_recall_vector
    T->>N: 关键词全文检索
    T->>P: entity_index 向量检索
    G->>T: entity_get + entity_neighborhood
    T->>N: 属性 / 1~2 跳关系
    G->>T: context_recall_vector(k=30)
    T->>P: 子块向量检索
    G->>G: 父块去重 + 实体/上下文精排
    G->>L: 主 GM 提示词
    L-->>G: reply
    G->>P: 落库 user + assistant
    G->>P: 回写 checkpoint_id
    API-->>U: reply + trace_id + checkpoint_id
    API-)T: 后台 API 3 实体固化
    API-)P: 后台 5 轮入库（若 pending>=5）
```

## 3. 一轮对话的详细步骤

### 3.0 开场白

- 新线程（尚无 messages）首次进入前端时调用 `POST /api/threads/{id}/opening`。
- 有世界记忆时：召回世界观片段 → LLM 生成 150~300 字开场白。
- 无世界记忆时：使用通用开场白。
- 以 `assistant` 消息、`turn_no=0` 落库；`count_un_ingested_turns` 只统计完整 user+assistant 轮次，因此开场白不会提前触发 5 轮入库。
- 最近 5 轮原始上下文额外包含开场白，不占用 5 轮对话名额。

### 3.1 prepare

- 从 `messages` 表计算新的 `turn_no`。
- 清空上一轮 state 字段。
- 原因：LangGraph 会用 `thread_id` 恢复上一轮 checkpoint state，不能直接复用旧 `turn_no`。

### 3.2 pre_turn（API 2）

输入：`thread_id`、`user_message`、`turn_no`、最近 5 轮消息（含已入库）。

1. 编排层直接从 `messages` 读取最近 5 轮消息（含已入库），拼入提示词；**确定性读取不走 MCP**。
2. API 2 两路并行 LLM：
   - `api2_context.md`：只看上一轮，输出 `context_summary`（单轮总结）和 `rewritten_query`；
   - `api2_entities.md`：看最近 5 轮，输出扁平 `entities` 检索词（实体名、别名、关键词，最多 12 个）。
   - **上下文检索用 `rewritten_query + context_summary`，实体检索用 `entities`**；单路失败时各自降级。
3. 用 `entities` 做一次实体召回：
   - **关键词路**：Neo4j CJK 全文索引；失败降级 `CONTAINS`。
   - **向量路**：PostgreSQL `entity_index`，查询 bge-large-zh-v1.5 向量。
4. 实体候选按 `entity_id` 去重，保留两路名次/分数，RRF 融合。
5. 对融合后 top10 调用 `entity_get`，取最新属性和 1 跳关系。
6. 调用 `entity_neighborhood` 做 1~2 跳扩展：
   - 节点按 `entity_id` 去重；
   - 边按 `rel_id` 去重；
   - 跳过节点重复的环路；
   - 新邻居最多补 4 个，合并进实体候选。
7. 用增强后的 query 召回上下文子块 `k=30`，按 `parent_no` 父块去重：
   - 同一父块只保留最高分子块作为代表；
   - 其他 sibling 记入 `child_chunk_nos`；
   - 取 top10 父块。
8. 精排：
   - 实体 top10、上下文 top10 分别送 `bge-reranker-v2-m3`；
   - `local_score = 0.75 * rerank_score + 0.25 * route_norm`；
   - 实体权重 0.6，上下文权重 0.4。
9. 写 `retrieval_traces`：假设实体、关键词候选、向量候选、融合候选、上下文候选、精排分数、最终选择的父块/实体/多跳路径，以及 `query_analysis`。

### 3.3 generate（主 GM）

- 读取最近 5 轮原始消息作为完整上下文，无论是否已入库；召回结果过滤掉完全落在这 5 轮内的 chunk，避免重复。
- 当前这一轮用户输入在生成时还未落库，会单独追加在历史之后；所以实际是“最近 5 轮历史 + 当前输入”。
- 系统提示词包含：
  - GM 角色约束；
  - 精排后的实体 top6；
  - 精排后的上下文父块 top4；
  - 实体多跳关系路径。
- 调用 LLM 生成 GM 回复；不在本节点落库。

### 3.4 persist

- 写入 user + assistant 两条消息，同一 `turn_no`。
- 计算 `pending_turns = 未入库完整轮次数`。
- `ingest_required = pending_turns >= 5`。
- 图结束后取最终 checkpoint_id，回写这一轮消息的 `checkpoint_id`。

### 3.5 后台触发

当前**不是图内 hook，也不是定时轮询**：

```text
graph.run() 返回后，FastAPI 路由检查最终 state：
  commit_required  → BackgroundTasks(_run_commit)   # API 3，每轮
  ingest_required  → BackgroundTasks(_run_ingest)   # 5 轮入库
```

- `BackgroundTasks` 在响应发送后执行，同进程；进程重启会丢失。
- `POST /api/threads/{id}/ingest` 可手动补偿入库。
- API 3 当前没有自动重试；失败只记日志。

## 4. API 3 实体/关系固化

```mermaid
flowchart TD
    A[API 3] --> B[输入：假设实体 + 真实体候选 + 上下文 + GM 回复]
    B --> C[LLM 输出 create/update/skip 决策]
    C --> D[校验 matched_entity_id / 枚举 / 长度]
    D --> E[MCP entity_commit_plan]
    E --> F[先写 Neo4j 权威图]
    F --> G[Entity / AttributeChange / REL]
    F --> H[再同步 PostgreSQL entity_index]
    H --> I[entity_mentions + tool_call_logs]
    H --> J[索引失败写 entity_index_jobs 供重试]
```

规则：

- 已存在实体必须引用候选中的 `entity_id`。
- 新实体由 MCP 生成 `entity_id`。
- 属性 append-only，不覆盖旧值；关系允许多重边。
- 关系类型受控枚举：`located_in/owns/member_of/knows/ally/enemy/causes/part_of/related_to`。
- Neo4j 写成功即视为实体写入成功；PG 索引失败不阻塞，可重建。
- 每个实体 mention、属性变更、关系都带 `trace_id`、`batch_id`、`chunk_nos`、`message_ids`、`evidence`。

## 5. 5 轮增量入库

```mermaid
flowchart TD
    A[未入库完整轮次 >= 5] --> B[取 5 个完整 user+assistant 轮]
    B --> C[拼接带角色标记的批次文本]
    C --> D[按句切分为 <=80 字文本单元]
    D --> E[父块：目标 600 字，250~900]
    D --> F[子块：目标 80 字，30~120，重叠 20]
    E --> G[父块 embedding]
    F --> H[子块 embedding]
    G --> I[(chunks)]
    H --> I
    I --> J[chunk_no + turn/message 映射]
```

- 一个“轮次” = 一条用户消息 + 一条 GM 回复。
- **原始上下文窗口**：最近 5 轮原始消息始终保留在 LLM 上下文里，无论是否已入库；入库不会删除 messages。只有轮次被新消息挤出最近 5 轮后，才只通过召回提供。
- 父块用于展开上下文；子块用于向量召回。
- 对话 chunk 按轮次边界切分，`chunk_no` 形如 `B0002-T0003-P001-C002`；世界观 chunk 无 T 段。
- `chunks.turn_no` 与 `turn_start/turn_end` 同时落库，供召回与回退使用。
- 每个 chunk 保存 `char_start/char_end`、`turn_start/turn_end`、`message_ids`，用于召回留痕。
- 世界观文本通过 `scripts/seed_worldview.py` 以 `kind='worldview'` 批次进入同一线程，参与召回。

## 6. 存储与留痕

### PostgreSQL

| 表 | 作用 |
|---|---|
| `threads` | 线程 |
| `messages` | 消息、turn_no、checkpoint_id |
| `ingestion_batches` | 5 轮批次/世界观批次 |
| `chunks` | 文本父子段与向量 |
| `retrieval_traces` | 每次 API 2 的完整召回轨迹 |
| `plot_nodes` | 三层主线：L0 单轮摘要 / L1 章节摘要 / L2 全局梗概 |
| `entity_index` | Neo4j 实体的派生名称向量索引 |
| `entity_mentions` | 实体 → 消息/片段/批次/trace |
| `entity_index_jobs` | PG 索引失败重试队列 |
| `tool_call_logs` | MCP 工具调用审计 |
| LangGraph checkpoint 表 | 每轮 state 快照，默认保留 5 轮 |

### Neo4j

- `:Entity`：`entity_id/thread_id/canonical_name/entity_type/aliases/summary/...`
- `:AttributeChange`：append-only 属性变更
- `:REL`：实体关系，允许多重边
- 所有查询强制 `thread_id`。

### 关键标识

- `thread_id`：隔离单位
- `turn_no`：轮次
- `trace_id`：一次召回/固化链路
- `chunk_no`：批次内父子段编号
- `message_ids`：消息溯源
- `entity_id`：Neo4j 与 PG 共用
- `created_turn_no/created_batch_id/last_turn_no`：实体创建/最近更新轮次，用于回退
- `rel_id/change_id`：关系/属性变更溯源
- `checkpoint_id`：LangGraph 轮次锚点

## 7. MCP 与确定性读取

MCP server 只暴露需要模型决策后的写工具：

| 工具 | 作用 |
|---|---|
| `entity_commit_plan` | 唯一实体/关系写入工具（create/update/skip + 属性/关系） |

以下都是**确定性读取/召回**，由编排层直接调用 `EntityToolService` / `PostgresStore` 服务方法，不作为 MCP 工具：

- 最近 5 轮消息：直接 `store.get_recent_turns` 后拼 prompt
- `context_recall_vector`、`entity_recall_keyword`、`entity_recall_vector`
- `entity_get`、`entity_neighborhood`、`chunk_get`
- `retrieval_trace_write`（审计写入，由编排层直接调用）

MCP server 通过 `scripts/run_mcp_server.py` 独立暴露，默认 `127.0.0.1:8765/mcp`。

## 8. Checkpoint 策略

- `PostgresSaver`，与业务表共用 PostgreSQL。
- `thread_id` 作为 checkpoint 线程键。
- 每轮节点都会产生 checkpoint；图结束后取最终 `checkpoint_id` 回写 `messages.checkpoint_id`。
- 默认保留最近 5 轮（`CHECKPOINT_RETENTION_TURNS=5`）：
  - 按 checkpoint 的 `channel_values.turn_no` 保留；
  - 删除旧 checkpoints/writes/未被引用 blobs；
  - 旧消息 `checkpoint_id` 置空；
  - 保留 checkpoint 的 parent 若指向被删 checkpoint，置空避免悬挂。
- `get_state_history` 可查看保留窗口内的节点级快照。

## 9. 当前已完成 vs 未完成

**已闭环：**

- 世界导入 + 增量导入：上下文向量化、实体/关系初始化、检查修复 agent
- 新线程开场白（turn_no=0，不计入 5 轮批次）
- Streamlit 会话列表 + 最近 5 轮回退/重试
- LangGraph 编排、5 轮增量入库、父子段切分/编号/溯源
- 实体关键词 + 向量双路召回、1~2 跳扩展、父块去重、bge-reranker 精排
- API 2 两路并行（单轮总结+改写 / 实体假设）
- 三层主线摘要（L0 单轮 / L1 章节 / L2 全局）
- API 3 实体/属性/关系固化，MCP `entity_commit_plan`
- checkpoint 保留 5 轮
- 41 个测试

**当前边界：**

- 后台任务仍是 FastAPI BackgroundTasks，服务重启会丢任务
- 无多租户/JWT
- 没有正式评测集（Recall/MRR/主线忠实度）
- 跨轮实体合并依赖整理 agent / API3 模型判断
- 精排仍在 CPU
- 状态管道（情绪/场景滚动状态）未实现
