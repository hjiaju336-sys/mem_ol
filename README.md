# mem_ol — 游戏化叙事记忆 Demo

在游戏 GM 对话中演示“传统 RAG + 增量记忆 + 主线摘要”：每 5 轮（用户 + GM）把上下文向量化入库，按语义切父子段并保留可溯源编号；最近 5 轮原始消息始终作为 LLM 上下文，更早内容按向量召回；L0/L1/L2 三层主线摘要维持长线叙事。

完整设计规格见 [需求](需求)，工程约定见 [CLAUDE.md](CLAUDE.md)，当前真实业务流程与流程图见 [docs/business_flow.md](docs/business_flow.md)。

> 当前状态：已跑通“LangGraph 编排 → GM 对话 → 5 轮增量入库 → 父子段向量化 → 双路召回 + 精排 → 实体与关系固化 → 三层主线摘要 → turn 级回退”。后台任务持久化尚未接入。

## 快速开始

```bash
conda activate mem_ol
docker compose up -d
python scripts/init_db.py

# 首次：导入世界（上下文向量化 + 实体/关系初始化）
python scripts/seed_worldview.py data/worldview/xiyouji_001.txt --thread-name "西游·世界导入演示"

# 终端 1：FastAPI
python main.py

# 终端 2：Streamlit
streamlit run app/streamlit_app.py

# 可选：对外暴露 MCP 实体工具（127.0.0.1:8765）
python scripts/run_mcp_server.py
```

也可以只用命令行跑 5 轮：

```bash
python scripts/chat_demo.py --turns 5
```

API 在 `http://127.0.0.1:8000`，Streamlit 默认在 `http://localhost:8501`，MCP server 默认在 `http://127.0.0.1:8765/mcp`。

### 简单前端

- `app/streamlit_app.py`：左侧会话列表（新建/切换）+ 主区对话；最近 5 轮每轮下方有「回退」「重试」两个按钮，回退删除该轮及之后的对话与记忆，重试回退后重新发送该轮玩家输入。
- `.streamlit/config.toml` 已开启 `runOnSave=true` 和 `fileWatcherType=auto`，保存代码后 Streamlit 自动重跑。
- 新线程首次进入会调用 `POST /api/threads/{id}/opening` 生成开场白：
  - 有世界记忆时，用召回上下文生成；
  - 没有世界记忆时使用通用开场；
  - 开场白以 assistant 消息、`turn_no=0` 落库，不参与 5 轮批次计数。

## 环境

- conda 环境：`mem_ol`（Python 3.12.16，已设置 `PYTHONNOUSERSITE=1`）
- PostgreSQL + pgvector：容器 `mem-ol-postgres`，宿主机端口 `55432`，库/用户/密码均为 `mem_ol`
- Neo4j Community：容器 `mem-ol-neo4j`，Bolt `7688`、HTTP `7475`，账号 `neo4j` / `mem_ol_dev`
- 本地 embedding：`models/bge-large-zh-v1.5`（1024 维，CPU）
- LLM：OpenAI 兼容接口，当前默认 `qwen-plus`（可切换 `qwen-flash` / `qwen-max`）

### 依赖安装

```bash
# 仅首次需要，先装 CPU 版 torch
pip install torch==2.15.0+cpu --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
```

## 配置与模型

- `.env` 已根据业务空间 API Key 生成，包含 `OPENAI_BASE_URL`、`OPENAI_API_KEY`；该文件已被 `.gitignore` 忽略，**不要提交或外传**。
- `.env` 中的 PostgreSQL/Neo4j 连接串已对应上面的宿主机端口和 compose 凭据。
- `OPENAI_MODEL` 当前为 `qwen-plus`；可用 `qwen-flash` 降低成本/延迟，或用 `qwen-max` 提升质量。
- `models/bge-large-zh-v1.5` 已复制到项目内，约 2.5GB，包含 `model.safetensors` 与 `pytorch_model.bin` 两套权重。
- 精排模型：`D:/RAGPRO/rag-pro/rag_system/models/BAAI/bge-reranker-v2-m3`（约 2.27GB，CPU 加载）。
- 精排权重：实体 `0.6`、上下文 `0.4`，可在 `.env` 通过 `ENTITY_WEIGHT`/`CONTEXT_WEIGHT` 调整。
- 世界观测试文本：公有领域《西游记》第一回，经 `scripts/prepare_worldview.py` 清理 Wikisource 标记后放在 `data/worldview/xiyouji_001.txt`。

## 目录结构

```text
mem_ol/
├── main.py                       # FastAPI 启动入口
├── requirements.txt
├── docker-compose.yml
├── pytest.ini
├── .env / .env.example
├── src/mem_ol/
│   ├── config.py                 # 环境变量与切分参数
│   ├── log.py                    # 统一日志（控制台文本 + JSONL）
│   ├── storage/
│   │   ├── postgres.py           # PostgreSQL + pgvector；实体派生索引与留痕
│   │   ├── neo4j.py              # Neo4j 实体图、属性变更、关系
│   │   └── schema.sql            # threads/messages/batches/chunks/entities/plot_nodes/traces
│   ├── pipelines/
│   │   ├── chunking.py           # 语义父子段切分
│   │   ├── memory.py             # 5 轮批量入库 + 世界观入库
│   │   └── query.py              # 基础向量召回 + 轨迹
│   ├── services/
│   │   ├── embedding.py          # bge-large-zh-v1.5
│   │   ├── llm.py                # Chat Completions + JSON 输出
│   │   ├── dialogue.py           # 主 GM 提示词 + 实体/上下文组装
│   │   ├── pre_turn.py           # API 2：两路并行（总结/改写 + 实体假设）+ 召回
│   │   ├── plot_memory.py        # L0 单轮摘要 → L1 章节摘要 → L2 全局梗概
│   │   ├── entity_commit.py      # API 3：实体/关系固化
│   │   ├── opening.py            # 新线程开场白生成
│   │   ├── world_import.py       # 世界导入：向量化 + 实体初始化
│   │   ├── query_enhancer.py     # LLM 分析失败时的查询改写 fallback
│   │   └── prompts.py            # 提示词加载与渲染
│   ├── mcp_server/
│   │   ├── schemas.py            # 假设实体/写入决策校验
│   │   ├── tools.py              # 唯一 DB 工具实现（in-process + MCP）
│   │   └── server.py             # MCP streamable HTTP server
│   ├── graph/conversation.py     # LangGraph：prepare→pre_turn→generate→persist
│   └── api/
│       ├── main.py               # FastAPI app + lifespan
│       └── routes.py             # 线程/对话/PreTurn/实体/批次/片段/轨迹
├── app/streamlit_app.py          # 简单对话前端
├── .streamlit/config.toml        # Streamlit 热加载配置
├── docs/entity_memory_design.md  # 实体记忆设计
├── docs/business_flow.md         # 当前业务流程/流程图
├── docs/business_logic_flow.md   # 业务逻辑流程图（Mermaid + PNG）
├── docs/current_state.md         # 清理后的交接说明
├── prompts/
│   ├── api2_context.md           # API 2A：单轮总结 + 查询改写
│   ├── api2_entities.md          # API 2B：实体假设
│   ├── plot_arc.md               # L1 章节摘要合并
│   ├── plot_global.md            # L2 全局主线梗概
│   ├── entity_commit.md          # API 3 提示词
│   ├── world_import.md           # 世界导入：分章节实体抽取
│   └── world_consolidate.md      # 世界导入：跨章节检查修复 agent
├── scripts/
│   ├── init_db.py                # 建表
│   ├── prepare_worldview.py      # 清理 Wiki 文本
│   ├── seed_worldview.py         # 导入世界：向量化 + 实体初始化
│   ├── chat_demo.py              # CLI 5 轮对话 + 自动入库
│   ├── run_mcp_server.py         # 启动 MCP server
│   └── extract_gutenberg_chapters.py  # 从公版原文截取章节
├── tests/
│   ├── conftest.py
│   ├── test_log.py
│   ├── test_chunking.py
│   ├── test_prompts.py
│   ├── test_entity_schemas.py
│   ├── test_rerank.py
│   ├── test_graph.py
│   ├── test_query_enhancer.py
│   ├── test_world_import.py
│   ├── test_opening.py
│   ├── test_rollback.py
│   └── test_llm.py
└── data/worldview/
    ├── xiyouji_001.txt            # 公版《西游记》第一回
    ├── rulin_raw.txt              # 公版《儒林外史》原文
    ├── rulin_001_002.txt          # 前两回
    └── doupo_excerpt.txt          # 本地测试材料，未随仓库上传
```

## 世界导入（阶段 0）

- **上下文向量化**：导入文本先按父子段切分并 embedding 入库，和检索链路完全一致；这一步与实体抽取解耦。
- **实体抽取**：
  - 短文本（≤20000 字）直接一次抽取；
  - 长文本按 20000 字切窗，默认 6 路并行，单次超时 240 秒，失败窗口二分重试；
  - **不复用检索父子块做抽取**，避免同一实体在多个小块里被反复抽取。
- **检查修复 agent**：抽取结果按 30000 字符分组送 `world_consolidate.md` 做树形合并，由 LLM 合并重复实体、别名、繁简写法和关系；不需要在代码里写繁简/别名特例。
- **写入**：合并后的实体/关系经 `entity_commit_plan` 初始化 Neo4j，并同步 PostgreSQL 实体向量索引。
- **增量导入**：初始导入后可追加同一文档的后续片段；按规范名/别名匹配已有实体走更新，批次记录 `doc_id/char_start/char_end`，区间重叠会被拒绝。
- **命令**：`python scripts/seed_worldview.py <text> --thread-name <新会话名>`；补充片段加 `--append --doc-id <文档ID> --char-start <起始>`。

## 当前记忆机制

- **批次触发**：5 个完整轮次（user + assistant 为同一轮）凑满后，调用记忆管道。
- **原始上下文窗口**：始终保留最近 5 轮 user+assistant 原始消息，不论是否已入库；滑出窗口的旧轮次只通过召回提供，召回结果会过滤最近窗口内的重复片段。
- **批次文本**：按 `【第N轮 · 用户】…【第N轮 · GM】…` 拼接，记录每条消息的字符区间。
- **父块**：目标 600 字，最小 250、最大 900；相邻文本单元向量相似度低于 `SEMANTIC_THRESHOLD`（默认 0.45）且达到最小长度时断开；尾部过短的父块并入前一个。
- **子块**：目标 80 字，最小 30、最大 120，向后重叠 20 字；文本单元先按句切到 ≤80 字，保证子块平均约 80 字；尾部过短的子块并入前一个。
- **编号与溯源**：chunk 按轮次边界切分，父块 `B0002-T0003-P001`，子块 `B0002-T0003-P001-C002`；同时保存 `turn_no`、`char_start/char_end`、`turn_start/turn_end`、`message_ids`，可回溯到原始消息。世界观的 chunk 不带 T 段（无对话轮次）。
- **向量化**：父子块都写入 `chunks.embedding`；查询用 bge 的检索指令编码后检索子块，再展开父块内容。
- **精排**：实体候选与上下文候选各取 top10，用本地 `bge-reranker-v2-m3` 交叉编码器打分；实体权重 0.6、上下文权重 0.4；精排分数写入 `retrieval_traces.rerank_scores`。
- **召回留痕**：每次召回写入 `retrieval_traces`，记录命中的 chunk ID、编号与分数。
- **世界观**：`kind='worldview'` 的独立批次，与对话块存在同一线程，参与召回。
- **入库状态**：`ingestion_batches` 的 `pending/processing/done/failed` 状态；FastAPI 在对话后达到 5 轮时用后台任务触发。

## LangGraph 编排与 checkpoint

- 图定义：`graph/conversation.py`，节点为 `prepare → pre_turn → generate → persist → END`。
- state 字段：`thread_id`、`turn_no`、`user_message`、`pre_turn` recall packet、`reply`、`message_ids`、`trace_id`、`pending_turns`、`commit_required`、`ingest_required`、`turn_checkpoint_id`。
- checkpoint：`PostgresSaver` 使用同一个 PostgreSQL，按 `thread_id` 保存；每轮结束取最终 `checkpoint_id` 回写到 `messages.checkpoint_id`。
- `prepare` 每轮强制从 DB 取新的 `turn_no` 并清空上一轮字段；LangGraph 会恢复上一轮 state，不能直接复用。
- API 3 与 5 轮入库仍由 API 后台任务触发；图 state 提供 recall packet、reply 和 message_ids。
- `get_state_history` 可以查看每轮节点级快照；默认只保留最近 5 轮（`CHECKPOINT_RETENTION_TURNS=5`），更早的 checkpoints/writes/blobs 会被清理，旧消息的 `checkpoint_id` 置空。

### 后台触发流程

- 图里的 `persist` 节点落库后计算 `pending_turns` 和 `ingest_required`。
- 路由在 `graph.run()` 返回后通过 FastAPI `BackgroundTasks` 调度：
  - `commit_required` → API 3 实体固化（每轮）
  - `ingest_required` → 5 轮增量入库（仅 `pending_turns >= 5`）
- 这不是 LangGraph 图内 hook，也不是定时轮询；`BackgroundTasks` 在响应后执行，进程重启会丢失。
- `POST /api/threads/{id}/ingest` 可手动补偿入库；API 3 暂无自动重试。

## 日志

- 应用启动时调用一次 `setup_logging()`；各模块只通过 `get_logger(__name__)` 获取 logger，统一向 root 汇总。
- 控制台输出人类可读文本；文件输出 JSON Lines 到 `logs/mem_ol.log`，10MB 轮转、保留 5 份。
- 固定字段：`ts`、`level`、`logger`、`message`、`thread_id`、`user_id`、`batch_id`、`trace_id`、`event`；其余字段进入 `extra`。
- `event` 统一用 `模块.动作` 命名，如 `memory.worldview.start`、`memory.batch.done`、`query.retrieve.done`、`llm.chat.done`。

## 测试

```bash
pytest
```

当前覆盖：日志 JSON 字段契约、语义父子段切分、消息/轮次溯源、提示词加载/渲染、实体决策 schema、精排权重、父块去重、LangGraph checkpoint、API 2 两路结构化输出、世界导入窗口切分、开场白落库、turn 级 chunk 编号、关系类型降级、source_order（41 passed）。端到端另外手工验证了 API 2/3、实体/关系写入、多跳路径、精排、世界导入、增量导入、7 轮主线摘要与回退。

## 主线记忆（三层摘要）

- **L0 单轮摘要**：API2 的 `context_summary` 只总结上一轮；每轮一次，先 `active` 作为查询材料，离开 5 轮窗口后向量化归档。
- **L1 章节摘要**：每 5 条 L0 合并（`PLOT_ARC_SIZE=5`），带 `turn_start/turn_end` 和向量。
- **L2 全局梗概**：每个新章节递归合并，常驻 GM 提示词。
- **召回**：L2 常驻；L1 按 `turn_end <= current_turn-5` 检索；无 L1/L2 时用已归档 L0 兜底。
- **回退**：删除目标轮次之后的 L0/L1 及 L2，后续维护重建。

## 实体记忆（已实现）

- 设计文档：[docs/entity_memory_design.md](docs/entity_memory_design.md)
- API 2 提示词：[prompts/api2_context.md](prompts/api2_context.md)、[prompts/api2_entities.md](prompts/api2_entities.md)
- API 3 提示词：[prompts/entity_commit.md](prompts/entity_commit.md)

每轮流程：

1. 编排层直接从 `messages` 读取最近 5 轮消息（无论是否已入库）。
2. **API 2 两路并行 LLM**：
   - `api2_context.md`：只看上一轮，输出 `context_summary`（单轮总结）和 `rewritten_query`；
   - `api2_entities.md`：看最近 5 轮，输出扁平 `entities` 检索词。
   - 上下文检索用 `rewritten_query + context_summary`，实体检索用 `entities`；单路失败时各自降级。
3. **实体召回**：`entities` 做一次 Neo4j 关键词召回（CJK 全文索引，失败降级 `CONTAINS`）和一次 PostgreSQL 向量召回（`entity_index`）；候选按 `entity_id` 去重并 RRF 融合。
4. 对融合后的 top10 实体做 **1~2 跳图扩展**：节点按 `entity_id` 去重、边按 `rel_id` 去重、跳过环路；新邻居补详情后一起参与精排。
5. 上下文用增强后的 query 召回 30 个子块，按 `parent_no` **父块去重**：同一父块只保留得分最高的子块作为代表，其余 sibling 记在 `child_chunk_nos`，再取 top10 父块。
6. 实体/上下文分别用 `bge-reranker-v2-m3` 精排；实体权重 0.6、上下文 0.4。
7. **主线召回**：`context_summary` 离开 5 轮窗口后归档并向量化；L2 全局梗概常驻 GM 提示词，L1 章节摘要按 `turn_end <= current_turn-5` 检索，L0 仅在无 L1/L2 时兜底。
8. **主 GM** 把主线梗概/章节、精排后的实体/上下文/多跳关系路径注入提示词生成回复；最近 5 轮原始消息始终作为完整上下文。
9. **API 3** 在后台判断实体存在性，输出 create/update/skip + 属性变更 + 关系，经 `entity_commit_plan` 写入。

存储与留痕：

- **Neo4j 权威**：`:Entity`、`:AttributeChange`、`:REL`。属性 append-only，当前值按 `source_order`（对话 `turn_no*1000`，世界导入 `batch_seq*1e6+窗口`）取最新；关系按稳定 `rel_id` 去重。`Entity` 记录 `created_turn_no/created_batch_id/last_turn_no`，属性与关系记录 `turn_no`，为 5 轮窗口回退准备。
- **PG 实体索引 provenance**：`entity_index` 记录 `created_turn_no/created_batch_id/last_turn_no`，回退时可判断实体是否创建于目标轮次之后。
- **PostgreSQL 派生**：`entity_index`（名称/别名/类型 + 1024 维向量）、`entity_mentions`、`retrieval_traces`、`tool_call_logs`。PG 索引失败不阻塞实体写入，可重建。
- `entity_id` 两边共用；`trace_id` 贯穿 API 2/3、mention、属性/关系、工具日志。

MCP：

- **MCP 只暴露写工具**：`entity_commit_plan`（需要模型决策后的实体/关系写入）。
- **确定性读取/召回**（最近 5 轮消息、关键词/向量召回、`entity_get`、`entity_neighborhood`、`chunk_get`、`retrieval_trace_write`）由编排层直接调用服务方法，不作为 MCP 工具。
- 工具实现只有一份 `mcp_server/tools.py`；API 2/3 在进程内直接调用服务方法，MCP server 通过 `python scripts/run_mcp_server.py` 只对外暴露写工具。
- 所有写工具强制 `thread_id`；不接受 SQL/Cypher；按 `trace_id` 幂等。

主要接口：

- `POST /api/threads/{id}/opening`：新线程开场白（turn_no=0）。
- `POST /api/threads/{id}/messages`：主对话，自动调用 API 2，后台调度 API 3。
- `POST /api/threads/{id}/pre-turn`：单独调试 API 2。
- `GET /api/threads/{id}/entities`、`/entity-mentions`、`/traces`：检查实体、留痕与召回轨迹。

## 已知限制与下一步

- 精排已接入 `bge-reranker-v2-m3`；只对 80~120 字子块和压缩后的实体文本打分，实测 CPU 约 **2.6 秒/10 个实体**、**1.5 秒/10 个上下文**；模型在 API 启动时预热，加载耗时取决于磁盘缓存。
- checkpoint 默认只保留最近 5 轮（`CHECKPOINT_RETENTION_TURNS=5`）：旧轮 checkpoint、writes、未被引用的 blobs 会被清理，旧消息的 `checkpoint_id` 置空。
- LLM JSON 已增加截断检测（`finish_reason=length` 提高 token 重试）、`json-repair` 修复和更严格的提示词；仍可能失败，但日志会记录 `finish_reason` 与响应长度。
- API 2/3 的确定性读取与召回通过 in-process 服务方法调用；MCP server 目前只暴露 `entity_commit_plan` 写工具，HTTP MCP client 适配层尚未接入。
- 实体匹配阈值暂定 0.75；世界导入的重复实体由整理 agent 合并，API3 跨轮合并仍依赖候选与模型判断。
- 回退与指代解析已接入；后台任务持久化、状态管道、REINDEX 维护尚未实现。
- CPU embedding 较慢（约 40~60 秒 / 万字符），入库已放到后台任务。
- 《西游记》属于 LLM 常识，只能验证链路；验收记忆效果需换私有/合成世界观文本。
- 无认证与多租户，当前按线程隔离。
