# 可迁移叙事记忆内核：当前状态与交接（2026-11-14）

> 用于在此之上继续修改。详细规格见 `需求`，工程约定见 `CLAUDE.md`，流程图见 `docs/business_flow.md`。

## 定位

**经过验证的可迁移叙事记忆内核**，不是绑定单一作品的 Demo。内核负责记忆、召回、主线、回退与状态；Streamlit、游戏 GM、数值/时间系统是参考前端与扩展层。

三条核心能力：

1. 5 轮增量记忆 + 实体关系图 + 双路召回；
2. turn 级回退，消息、向量、图、主线摘要一起回滚；
3. L0/L1/L2 三层主线摘要，防止长对话碎片化。

## 验证摘要

- 60k 外部语料：806 chunks / 57 实体 / 68 关系，无降级；
- 20 轮对话：20/20 完成，L0 29 / L1 5 / L2 1；
- 记忆探针：8/10 完整、2/10 部分；
- 路由：16 题任一路 @5 = 1.0；20 轮中上下文 9/10、实体 10/10、主线 9/10；
- 回退、长程召回、时间推进均通过；
- 跨题材迁移：玄幻（斗破 10k 字，14 轮）、现实（儒林外史 49k 字，5 轮）、西幻（原创，5 轮）均跑通，角色登场即绑定实体；
- 后台任务：PG `background_tasks` + worker，崩溃恢复、失败退避、正常一轮均验证；
- 94 个测试通过。

细节见 `docs/eval_glm_2026-11-14.md`、`docs/eval_v2_2026-11-13.md`。

## 可迁移内容

| 层 | 可替换/可扩展 | 入口 |
|---|---|---|
| 世界观文本 | 任意小说/设定集/增量段落 | `WorldImportService` / `seed_worldview.py` |
| GM 提示词 | 角色、契约、OOC、输出格式 | `prompts/gm_*.md` |
| 实体与关系 | 类型、自由关系标签、别名、事件实体化 | `domain/` + 导入/固化提示词 |
| 玩家自定义 | 自定义提示词、世界书、临时要求 | 前端 `ClientContext`（不落库） |
| 工具与状态 | 检定、伤害、资源、时间/历法 | `numeric_engine.py` + `rules/default.json` |
| 检索参数 | 窗口、切块、top-k、精排权重 | `config.py` / `.env` |

主流程不改；换设定、换提示词、换 schema、换状态工具即可迁移到另一套玩法。

## 应用场景

- 长对话小说生成：设定导入 + 持续增量记忆 + 主线摘要；
- 文字互动游玩 / AI TRPG：行动检定、状态变化、回退重试；
- 个人陪伴助手：人物/关系设定 + 长期一致性 + 自定义文风；
- 数字人 / 角色代理：记忆内核与表现层解耦。

都需要“实时更新 + 长对话一致性 + 初期设定导入 + 后期自定义”，这是内核的定位。

## 每轮流程

```text
用户输入
  ├─ API2-A 输入分流（narrative/meta/mixed）+ 单轮总结 + 查询改写
  ├─ API2-B 实体假设（最近 5 轮）
  ↓ 两路并行
  ├─ meta：写/读 pending instructions，不召回、不落库、不计 turn
  ├─ 实体召回：关键词一次 + 向量一次 → RRF → 1~2 跳
  ├─ 上下文召回：rewritten_query + context_summary
  └─ 主线召回：L2 常驻 + L1 章节（turn_end <= current-5）
  ↓
  rerank（实体 0.6 / 上下文 0.4）
  ↓
  state：读 game_states；attack 时引擎掷骰/伤害，产出 Resolution
  ↓
  GM 五段拼装：核心规则 + 响应契约 + 玩家自定义 + 世界书 + 记忆
     + 当前状态卡（含历法时间）+ 本轮结算（数值权威）；只输出正文
  ↓
  judge：独立状态裁决 LLM 读玩家输入 + 正文 + 状态卡 + 结算，输出 effects
  ↓
  reconcile：effects（标签/数值/flag/时间）经引擎校验后写回状态
     （重复结算 HP 会被拒绝）
  ↓
  persist：消息落库、L0 摘要入 plot_nodes
  ↓
  后台：API3 实体固化、5 轮入库、plot 维护（归档/章节/全局）
```

## API 2

- `prompts/api2_context.md`：先判断 `channel`（narrative/meta/mixed）和 `intent`，再看上一轮输出 `context_summary` + `rewritten_query`；meta 时输出 `directives`/`meta_question`；
- `prompts/api2_entities.md`：看最近 5 轮，输出扁平 `entities`；
- 两路独立失败降级；
- 上下文 query 使用 `rewritten_query + context_summary`。

## 输入分流、元交互与玩家自定义

- **narrative**：正常召回 + GM + 落库。
- **meta**：纯元指令不生成正文、不落消息、不计 turn；前端保存为 `pending_instructions`，下次叙事请求静默生效。OOC 问题走 `gm_meta.md`，回答只进系统回答区。
- **mixed**：拆出 `narrative_query` 走故事流程，`directives` 作为本轮生成参数。
- **GM 提示词五段**：`gm_core.md` → `gm_contracts.md`（按 intent）→ 玩家自定义 → 世界书命中条目 → 记忆；最后一条 user 消息附本轮生成要求。
- **状态裁决**：GM 只写正文；`services/state_judge.py` 在正文之后独立调用一次 LLM，按 `prompts/state_judge.md` 输出 `effects` 和本轮登场的 `appearances`，引擎校验后写回；登场角色会建立状态 actor 并绑定 Neo4j 实体。裁决失败只记日志，不影响正文与落库。
- **世界书**：兼容 SillyTavern 原生和 Character Card V2；`lorebook.py` 做解析/触发/渲染；`ClientContext` 随请求携带，不进数据库、不进 checkpoint。
- **持久化约定**：自定义提示词、世界书、pending instructions 都是前端内容，服务端只在本次请求内使用，不写入数据库。

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

CUSTOM_PROMPT_MAX_CHARS=4000
LOREBOOK_SCAN_DEPTH=3
LOREBOOK_MAX_CHARS=1500
LOREBOOK_MAX_ENTRIES=2000
LOREBOOK_MAX_BYTES=2000000
LOREBOOK_RECURSION_STEPS=0

# 可选：自定义规则包
# MEM_OL_RULES_PATH=./src/mem_ol/rules/default.json
```

## 结构约定

- `domain/`：共享枚举、Pydantic 模型、规则包；业务代码不再从 `mcp_server.schemas` 导入。
- `domain/models.py`：API2/GM/实体写入模型。
- `domain/enums.py`：channel/intent/action/effect 等闭集。
- `domain/rules.py` + `rules/default.json`：默认角色/武器、检定基础值、效果上限；按 mtime 热加载，支持 `MEM_OL_RULES_PATH`。
- `api/schemas.py`：HTTP 请求与客户端上下文模型。
- `prompts/state_judge.md`：状态裁决的 effects 约束，改提示词不用改代码。
- `scripts/validate_assets.py`：校验规则包与所有提示词文件，改配置后先跑一遍。
- 关系：Neo4j `:REL` 存 `relation_label` 自由短语（不做粗分类枚举）；`rel_id` 含标签，同一对实体不同关系可并存；`relation_label_fulltext` 支持按关系模糊查询。
- `mcp_server/` 只保留工具实现与 server，不再存放共享 schema。

## 存储职责

- PostgreSQL：消息、批次、chunks、entity_index、mentions、traces、plot_nodes、tool logs、checkpoint；
- Neo4j：`:Entity`、`:AttributeChange`（append-only）、`:REL`；
- 实体权威在 Neo4j；PG entity_index 是派生索引；
- 角色状态 `game_states.actors` 通过 `entity_id` 绑定 Neo4j 实体（精确规范名/别名匹配）；数值仍只存 game_states，不写 Neo4j；
- 属性当前值按 `source_order`：对话 `turn_no*1000`，世界导入 `batch_seq*1e6+窗口*1000`；
- `rel_id` 由线程 + 两端实体 + 类型决定，重复提交不新增边；
- 回退：mark messages rolled_back + 物理删除 chunks/entity_index/mentions/traces/plot_nodes + Neo4j 删除；
- 后台任务已迁到 PostgreSQL `background_tasks` 表 + worker（`FOR UPDATE SKIP LOCKED`、失败退避重试、启动回收 `running`），服务重启不丢；
- 多租户在查询层注入 `user_id` 即可接入；评测复用 `scripts/eval_*.py`。

## 清理记录

- 删除空占位模块：`pipelines/{state,rollback,coreference}.py`、`services/extraction.py`、`maintenance/`、`scripts/demo_scenario.py`；
- 删除旧提示词 `api2_analysis.md`（已拆成 `api2_context.md` / `api2_entities.md`）；
- 删除生成的测试文本：`doupo_{2000,5000,10000,append_2k,smoke}.txt`、`rulin_{20000,150000}.txt`、`xiyouji_001_raw.txt`；保留 `doupo_excerpt.txt`、`rulin_raw.txt`、`rulin_001_002.txt`、`xiyouji_001.txt`；
- 清空 PostgreSQL / Neo4j 中所有测试会话与数据；
- 删除 `.playwright-mcp`、`.pytest_cache`、`__pycache__`、历史日志；
- 当前测试：`94 passed`。
