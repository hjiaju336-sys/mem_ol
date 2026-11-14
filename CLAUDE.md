# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目状态

当前已实现「世界导入（上下文向量化 + 实体初始化）+ LangGraph 编排 + 输入分流（叙事/元交互/混合）+ GM 对话 + 5 轮增量向量化 + 语义父子段切分 + 实体假设/双路召回 + bge-reranker 精排 + 实体与关系固化 + 三层主线摘要 + 5 轮回退 + MCP 工具 + 玩家自定义/世界书 + FastAPI/Streamlit」。`需求`（中文）是完整规格；当前开发计划见 `docs/dev_plan.md`。

```bash
conda activate mem_ol          # Python 3.12.16，已设置 PYTHONNOUSERSITE=1
docker compose up -d           # PostgreSQL(pgvector) + Neo4j
python scripts/init_db.py
python scripts/seed_worldview.py data/worldview/xiyouji_001.txt --thread-name "西游·世界导入演示"
python scripts/chat_demo.py --turns 5
python main.py                 # FastAPI :8000
streamlit run app/streamlit_app.py
python scripts/run_mcp_server.py   # MCP :8765
pytest
```

数据库端口：PostgreSQL `55432`、Neo4j Bolt `7688` / HTTP `7475`；连接配置在 `.env`，模板见 `.env.example`。当前尚无 lint 配置。

## 系统定位

**经过验证的可迁移叙事记忆内核**：基于 Graph RAG 变体的对话即时增量记忆层，对话过程中实时写入/变更实体与关系，并支持随对话回退的可变式记忆。内核不绑定具体作品；Streamlit、游戏 GM、数值/时间状态是参考前端与扩展层。60k 外部语料 + 20 轮对话验证见 `docs/eval_glm_2026-11-14.md`。

## 技术栈与明确排除项

- PostgreSQL：消息、线程/用户、会话状态、指代映射、失败任务表；所有写入走事务。
- pgvector（PostgreSQL 扩展）：原文片段向量表、实体向量表，物理分表、逻辑按线程隔离。
- Neo4j Community Edition：`:Entity` 节点与关系边。**无数据库级隔离**，隔离完全靠应用层注入过滤条件。
- Embedding：本地 bge-large-zh-v1.5（`models/bge-large-zh-v1.5`，1024 维，默认），兼容 BGE-M3；稀疏权重当前不接入，仅留接口。
- 明确不引入：FAISS、Milvus、Redis、独立 BM25 引擎、消息队列中间件。异步管道用 PostgreSQL 表 + 定时轮询实现。

## 架构总览（规格目标）

**存储组件 + 两条异步管道 + 一条同步查询链路**：

- **状态管道（每轮触发）**：消息落库 → 读取会话状态 → 应用层计算滚动状态 → 乐观锁写回（version，冲突最多重试 3 次）。不触发任何向量化或图写入。
- **记忆管道（每 N 轮触发，推荐定时轮询）**：按 `source_batch_id` 取批 → 过滤 `rolled_back` 与水位线 → 切片 → 片段向量化 → 实体抽取 → 三级实体链接（别名精确 → 编辑距离模糊 → 实体名 embedding top-1）→ Neo4j 节点/关系 + 实体向量双写 → 指代映射写入 → 标记批次已处理。
- **查询链路**：原文轨（pgvector 向量检索）与实体轨（指代处理 → 实体链接 → Neo4j 1~2 跳遍历）并行，RRF 融合后按粒度分组组装 prompt。
- **回退流程**：标记消息 `rolled_back` → 推进水位线（单调递增）→ 判断批次是否已落地 → 物理删除 pgvector 行、Neo4j 节点/关系、清理指代映射。
- **后台维护**：累计物理删除量占比超阈值（如 15%）时 `REINDEX CONCURRENTLY`；失败任务重试（上限如 5 次后转人工）。

## 当前参考实现

- 对话引擎：`services/dialogue.py`，GM 五段提示词（`gm_core.md` 核心规则 + `gm_contracts.md` 本轮契约 + 玩家自定义 + 世界书 + 记忆）+ 最近 5 轮原始消息上下文（含已入库）；消息按 user/assistant 同一 `turn_no` 落库。GM 返回隐藏 `plan` + `reply`，只使用 `reply`。
- 输入分流：`services/pre_turn.py` 先判断 `narrative/meta/mixed`。纯 meta 指令不生成正文、不落库、不计 turn，前端保存为 `pending_instructions` 下次附带；OOC 问题由 `prompts/gm_meta.md` 单独回答，不进故事正文。
- 玩家自定义：`ClientContext` 随 `/messages` 携带 `custom_prompt/pending_instructions/lorebook`，服务端不持久化、不写入 checkpoint；世界书由 `services/lorebook.py` 解析 SillyTavern/Character Card V2 格式并按 constant/关键词/概率/预算/位置注入。
- 数值状态：`services/numeric_engine.py` 最小实现 attack 检定（1d20+攻击 vs 10+防御、伤害、状态标签）、`apply_effects`（hp_delta/tag/flag/scale/time_advance，含重复扣血拦截与范围裁剪）和规则包历法（默认 12 月 × 30 天，时间由 GM 提 `time_advance`、引擎推进）；默认数值/上限/历法在 `src/mem_ol/rules/default.json`，由 `domain/rules.py` 校验并按 mtime 热加载。`services/game_state.py` 读写 PG `game_states`/`state_events`。Graph 增加 `state` 节点在 GM 前结算并注入状态卡/结算，`reconcile` 节点把 GM 提出的剧情后果校验后写回状态；`persist` 落消息后提交状态。`entity_index` 只做召回，不存 HP/攻击力。回退按 `turn_no` 删 `state_events` 并恢复快照。
- 结构约定：共享枚举/模型在 `domain/`，HTTP 请求模型在 `api/schemas.py`，GM 输出格式在 `prompts/gm_output.md`；`mcp_server/` 只保留工具与 server，业务代码不再从 `mcp_server.schemas` 导入。改规则或提示词后运行 `python scripts/validate_assets.py`。
- 原始窗口：最近 5 轮常驻；召回结果过滤掉完全落在这 5 轮内的 chunk，避免重复。
- 增量入库：`pipelines/memory.py`，每 5 个完整轮次一批；不足 5 轮的消息不删，继续作为 LLM 上下文。
- 切分：`pipelines/chunking.py`，父块 250~900（目标 600），子块 30~120（目标 80，重叠 20）；文本单元先按句切到 ≤80 字；相邻单元 embedding 相似度低于 0.45 时断父块；尾部碎片并入前块。
- 向量化：只对子块算 embedding；父块只存文本，用于子块命中后展开，检索 SQL 也只查 `level='child'`。
- 编号：对话 chunk 按轮次切分，父块 `B{batch_seq:04d}-T{turn_no:04d}-P{parent:03d}`，子块追加 `-C{child:03d}`；世界观 chunk 无 T 段。同时保存 `turn_no`、`char_start/char_end`、`turn_start/turn_end`、`message_ids` 供召回与回退溯源。
- 召回：`pipelines/query.py`，子块向量检索（带 bge 检索指令）→ 父块展开 → `retrieval_traces` 按轮次留痕，随最近 5 轮上下文一起清理（turn 0 开场白保留）。
- 精排：`services/rerank.py`，`bge-reranker-v2-m3`；实体/上下文各取 top10，实体权重 0.6、上下文 0.4；只对 80~120 字子块与压缩实体文本打分（max_length 192），实测 CPU 约 2.6 秒/10 实体、1.5 秒/10 上下文；启动时预热，失败降级为路由分数。
- 世界观：`kind='worldview'` 批次，测试文本为公有领域《西游记》第一回（`data/worldview/xiyouji_001.txt`），不使用盗版文本。
- 编排：`graph/conversation.py`，LangGraph 节点 `prepare → pre_turn → generate → persist`；`PostgresSaver` 按 `thread_id` 保存 checkpoint，最终 checkpoint_id 回写 `messages.checkpoint_id`；默认保留最近 5 轮。
- 后台触发：`persist` 节点计算 `pending_turns/ingest_required`；API 路由在 graph 返回后用 FastAPI `BackgroundTasks` 调度 API 3（每轮）和 5 轮入库（pending>=5）。不是图内 hook/定时轮询，`BackgroundTasks` 非持久化。
- 接口：FastAPI `api/routes.py`；界面：`app/streamlit_app.py`（左侧会话列表，无下拉框；最近 5 轮每轮下有「回退」「重试」，`.streamlit/config.toml` 开启保存自动重跑）。达到 5 轮时由 FastAPI 后台任务触发入库。
- 开场白：新线程调用 `OpeningService`/`POST /opening`，有世界记忆时召回后生成，否则用通用开场；以 assistant、`turn_no=0` 落库，不参与 5 轮批次计数。
- 实体记忆：`services/pre_turn.py`（API 2 两路并行：单轮总结+查询改写 / 5 轮实体假设，再做 Neo4j 关键词/PG 向量双路召回 + 上下文召回 + 主线召回）、`services/query_enhancer.py`（LLM 分析失败时的查询改写 fallback）、`services/entity_commit.py`（API 3 判断存在性并写实体/属性/关系）、`services/plot_memory.py`（L0/L1/L2 主线摘要维护）、`services/world_import.py`（世界导入：上下文向量化 + 实体初始化 + 检查修复 agent）、`mcp_server/tools.py`（确定性读取服务 + 实体写工具实现）。
- 世界导入：检索父子段独立向量化；实体抽取短文本直接抽、长文本按 20000 字切窗（`world_extract_workers` 默认 6 路并行，单次超时 240s，失败窗口二分重试；单窗跳过整理 agent）；所有抽取结果按 `world_consolidate_max_chars=30000` 分组交给 `prompts/world_consolidate.md` 的检查修复 agent 做树形合并，不在代码里写繁简/别名特例。抽取只输出 `canonical_name/aliases/type/description/attributes{key,value}` 与 `{source_ref,target_ref,relation_label}`，不再输出 `mention/confidence/evidence`；关系标签是自由短语，不做枚举限制。
- 世界导入增量：`import_world(append=True)` 允许同一会话补充导入，按规范名/别名匹配已有实体走 `update`，批次记录 `doc_id/char_start/char_end` 并拒绝区间重叠；`source_order = batch_seq*1_000_000 + window*1_000 + item` 由代码注入，不交给 LLM。实体溯源终点是图里的实体，世界导入不再把父块编号挂到实体上。
- 存储权威：Neo4j 存 `:Entity/:AttributeChange/:REL`；PostgreSQL 的 `entity_index` 是派生名称向量索引，另有 `entity_mentions/retrieval_traces/tool_call_logs` 留痕。
- 召回去重：实体按 `entity_id` 去重 + RRF，再按 `entity_id/rel_id` 去重做 1~2 跳扩展并跳过环路；上下文按 `parent_no` 去重，同一父块只保留最高分子块（siblings 记在 `child_chunk_nos`），再 rerank top10。
- MCP：`mcp_server/server.py` 只暴露需要模型决策后的写工具 `entity_commit_plan`；最近消息、召回、实体详情等确定性读取由编排层直接调用服务方法，不作为 MCP 工具。`python scripts/run_mcp_server.py` 提供流式 HTTP MCP。
- 日志：所有模块经 `mem_ol.log` 汇总，格式约定见下。
- checkpoint 保留：`checkpoint_retention_turns=5`；每轮结束后清理旧轮 checkpoints/writes/blobs，并置空旧消息的 checkpoint_id。
- LLM JSON：`finish_reason=length` 时提高 max_tokens 重试；解析失败用 `json_repair` 修复；提示词约束单行字符串/禁用未转义双引号。API2/API3/GM/开场白统一 `dialogue_timeout=120s`；429/5xx 在 `LLMService._create` 内退避重试 3 次（5s/10s）；`chat_json_model(model_cls, ...)` 做 pydantic 结构校验，失败时把错误回传模型重试一次。
- 回退：`services/rollback.py`，按 turn 级 provenance 物理删除 PostgreSQL chunks/entity_index/mentions/traces 与 Neo4j 实体/关系，checkpoint 只用于对话状态，不能替代数据库回退。
- 尚未实现：实体合并、失败任务重试/REINDEX 后台维护。

## 实体记忆（已实现）

- 设计文档：`docs/entity_memory_design.md`；业务流程/流程图：`docs/business_flow.md`；提示词：`prompts/api2_context.md`（单轮总结 + 查询改写）、`prompts/api2_entities.md`（实体假设）、`prompts/entity_commit.md`（API 3）、`prompts/plot_arc.md`/`plot_global.md`（主线摘要）。
- 流程：API 2 拆成两路并行 LLM：①输入分流（`channel`）+ 单轮总结/查询改写（只看上一轮，输出 `context_summary/rewritten_query`；meta 时输出 `directives/meta_question`）；②实体假设（看最近 5 轮，输出扁平 `entities`）→ 纯 meta 直接返回空正文；narrative/mixed 用 `rewritten_query + context_summary` 召回，实体用检索词做一次关键词召回 + 一次向量召回 → `entity_id` 去重/RRF → 1~2 跳图扩展 → 上下文父块去重 → 精排 → 主 GM（五段提示词 + 隐藏 plan）→ API 3 判断存在性并写实体/属性/关系。
- 主线记忆：`plot_nodes` 三层结构。L0 单轮摘要（每轮一次，active，离开 5 轮窗口后归档向量化）；L1 章节摘要（每 5 条 L0 合并，`plot_arc_size=5`）；L2 全局梗概（每个新章节递归合并）。GM 提示词常驻 L2，按 `turn_end <= current_turn-5` 召回相关 L1，L0 仅在没有 L2/L1 时兜底；回退按 turn 删除对应节点。
- 存储：**Neo4j 是实体权威**（`Entity/AttributeChange/REL`，属性 append-only）；PostgreSQL 只存派生 `entity_index`（名称/别名/类型 + 1024 维向量）和 `entity_mentions/retrieval_traces/tool_call_logs` 留痕。PG 索引失败不阻塞实体写入，可重建。
- MCP：工具实现在 `mcp_server/tools.py`；MCP server 只暴露写工具 `entity_commit_plan`；确定性读取/召回由编排层直接调用服务方法。API 2/3 默认 in-process 调用，`scripts/run_mcp_server.py` 提供 streamable HTTP MCP（127.0.0.1:8765）。
- 查询增强：`prompts/api2_context.md` 只总结上一轮并改写当前 query；`prompts/api2_entities.md` 输出扁平检索词；两路并行。`services/query_enhancer.py` 仅在 LLM 失败时返回原文兜底。
- 接口：`POST /api/threads/{id}/opening`（开场白）、`POST /messages` 自动 API 2 + 后台 API 3；`POST /pre-turn` 调试 API 2；`GET /entities`、`/entity-mentions`、`/traces`。
- 精排：`bge-reranker-v2-m3`（`D:/RAGPRO/rag-pro/rag_system/models/BAAI/bge-reranker-v2-m3`）；实体/上下文各取 top10，权重实体 0.6、上下文 0.4，可通过 `ENTITY_WEIGHT`/`CONTEXT_WEIGHT` 调整。
- 测试文本提醒：《西游记》属于 LLM 常识，只能临时验证链路；验收记忆效果必须换私有/合成世界观文本。

## 必须遵守的硬约束

- **归属过滤强制注入**：所有对 pgvector 和 Neo4j 的查询必须包含 `thread_id`（多用户时加 `user_id`），由统一的查询构造层保证。绕过该层的裸查询视为违规。
- **向量维度固定 1024**：与 BGE-M3 严格一致；更换 embedding 模型必须全量重建，禁止维度混写。
- **事件时间优先**：所有新旧判断、水位线推进、覆盖守卫以消息 `occurred_at` 为准，不以写入时间或自增 ID 为准。
- **异步管道幂等**：状态管道以消息 ID 为幂等键，记忆管道以 `source_batch_id` 为幂等键；失败重试先检查已写入部分，跳过已完成步骤。`source_batch_id` 必须是全局唯一 UUID，禁止自增或线程内序号。
- **线程内串行、线程间并行**：同一线程的状态管道、记忆管道、回退操作通过批次行锁/线程级锁串行化；LLM 长调用期间不持锁，写入前二次校验批次状态与 `rolled_back`。
- **管道失败不阻塞对话**：失败写失败任务表，后台扫描重试。
- **两轨互不阻塞**：任一轨失败或为空时降级到另一轨，均空则返回空上下文。
- **回退删除分批**：Neo4j `DETACH DELETE` 每批约 10000 个节点；删除量累加到维护计数表，HNSW 墓碑由后台重建清理，不在回退时同步做。全部物理删除，没有 `is_deleted` 软删。
- **记忆管道一致性守卫必须同时满足**：批次查询条件含 `occurred_at > 水位线`；步骤 6 前二次校验 `rolled_back`；图写入永不覆盖，实体属性变更一律追加变更记录（append-only，带 `occurred_at` + `source_batch_id`）。
- **实体作用域**：线程即实体库，不做跨批次共享实体判定，不建实体-批次引用表；回退按 `source_batch_id` 删除，召回来源写入查询轨迹表留痕。
- **实体权威在 Neo4j**：PostgreSQL `entity_index` 只是派生的名称向量索引；写入先 Neo4j，再同步 PG；PG 失败不阻塞实体写入，可重建。
- **实体 provenance**：`:Entity` 与 `entity_index` 记录 `created_turn_no/created_batch_id/last_turn_no`；`:AttributeChange`/`:REL` 记录 `turn_no` 与 `source_order`（对话 `turn_no*1000+序号`，世界导入 `batch_seq*1_000_000+窗口*1000+序号`），当前值按 `source_order` 取最新；`rel_id` 由线程、两端实体和 `relation_label` 决定；同一对实体的不同关系标签是不同边。`relation_label` 是自由短语，不做枚举；Neo4j `relation_label_fulltext` 支持按关系模糊查。
- **实体写入只经写工具**：API 2/3 与 LLM 不直接执行 SQL/Cypher；LLM 只输出 JSON 决策，编排层经 `entity_commit_plan` 写入；MCP server 对外暴露同一写工具。确定性读取/召回可走服务方法，但不操作写入。
- **玩家自定义内容不落库**：`custom_prompt`、`pending_instructions`、`lorebook` 只随请求进入内存，禁止写入 PostgreSQL、LangGraph checkpoint 或 `retrieval_traces`；日志只记录 hash/条目数/命中 uid，不记录正文。纯 meta 指令不生成消息、不计 turn。
- **数值状态边界**：HP/攻击/防御/装备/物品实例存 `game_states`，由 `numeric_engine` 计算；`entity_index` 只存名称向量，Neo4j 只存语义身份/关系，禁止把每轮变化的数值写进这两处。
- **实体属性 append-only**：`:Entity` 的语义属性只追加 `:AttributeChange`，关系只新增；当前值按 `occurred_at` 取最新。

## 日志约定

- 初始化：应用入口调用一次 `mem_ol.log.setup_logging()`；模块内只使用 `get_logger(__name__)`，禁止各自 `basicConfig`/`addHandler`。
- 输出：控制台文本（UTF-8）+ `logs/mem_ol.log` JSONL（10MB 轮转，保留 5 份）；所有模块日志经 root 汇总，用 `logger` 字段区分来源。
- 固定字段：`ts`、`level`、`logger`、`message`、`thread_id`、`user_id`、`batch_id`、`trace_id`、`event`；其余字段进入 `extra`。
- `event` 统一用 `模块.动作`：如 `state.message.persist`、`memory.batch.done`、`query.track.done`、`rollback.batch.deleted`。
- 跨模块上下文用 `with log_context(thread_id=..., batch_id=...):`，不要每条日志重复传参。
- `tests/test_log.py` 固定 JSON 字段契约；改格式必须同步更新测试和本文档。

## 最小实现范围

必须实现：§3 状态管道、§4 记忆管道、§5 回退流程、§6 查询流程、§7 指代处理、§8 后台维护（文档 `需求` 的章节号）。

仅预留接口、不实现：多租户与 JWT（查询构造层留 `user_id` 注入点）、完整双时态版本化（Neo4j 预留 `valid_from`/`valid_to`）、情绪聚合（会话状态滚动字段写占位值）、精排模型、意图识别与路由。

## 当前实现选型

- 编排：LangGraph 已接入（`graph/conversation.py`）；`PostgresSaver` 按 thread_id 保存 checkpoint，最终 checkpoint_id 写入 `messages.checkpoint_id`。每轮 `prepare` 必须重算 turn_no 并清空上一轮字段。
- 接口与界面：FastAPI + Streamlit。
- LLM：OpenAI 兼容接口（环境变量配置 base_url/api_key/model），单元测试用 Mock 抽取器。
- Embedding：本地 bge-large-zh-v1.5（默认，已就位）或 BGE-M3，均为 1024 维，通过配置切换，禁止混写。
- 精排：本地 `bge-reranker-v2-m3`，实体权重 0.6、上下文 0.4；模型不可用时降级为路由分数。
- 数据库：PostgreSQL + pgvector、Neo4j Community，Docker Compose 启动。
