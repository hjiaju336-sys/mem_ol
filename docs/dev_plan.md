# 开发计划（按优先级）

> 依据：15 万字导入测试、当前代码审计、以及 2026-11-13 的逐条决策。
> 状态：`[ ]` 待做，`[~]` 进行中，`[x]` 完成。

## 决策基线

- 增量世界导入要做，允许多批补充同一会话。
- 批次/窗口顺序作为**元数据**注入，不交给 LLM 判断。
- 抽取只负责产出结构化结果；合并与入库由整理 agent + 写工具负责。
- 实体溯源终点是图数据库里的实体；只有向量召回才需要追到子块/父块。
- 实体合并、别名冲突由整理 agent 负责；系统只上报“合并降级”，不自动打补丁。
- 后台任务持久化暂不做：已入库的数据不会丢；未入库的最近 5 轮原文仍在消息表。
- 召回轨迹跟着轮次上下文走，按 5 轮窗口保留。
- 状态管道（情绪/场景滚动状态）暂缓。

## P0：正确性

### 1. 增量世界导入

- [x] `import_world` 支持 `append=True`：已有实体的会话不再直接拒绝。
- [x] 新实体 → `create`；匹配到已有实体 → `update`（按规范名/别名匹配；向量候选后续可加）。
- [x] 批次记录补充 `doc_id`、`char_start`、`char_end`。
- [x] 校验：同一会话内 `batch_seq` 单调递增；重复导入同一区间要拒绝或跳过。
- [~] 测试：已有 `_build_decisions` 单测；真实两批导入待 LLM 额度恢复。

### 2. `source_order` 叙事顺序

- [x] 抽取结果携带 `source_order = batch_seq * 1_000_000 + window_index * 1_000 + item_index`。
- [x] 合并 agent 保留每个属性/关系的 `source_order`，同一 key 的不同 value 全部保留。
- [x] `:AttributeChange`、`:REL` 写入 `source_order`。
- [x] 当前值查询改为 `ORDER BY source_order DESC, occurred_at DESC`。
- [x] 对话轮次使用 `turn_no * 1_000 + item_index`，与世界导入共用同一套判断。
- [x] 测试：已做 Neo4j 集成验证，`source_order` 大的属性成为当前值。

### 3. 关系稳定 ID

- [x] `rel_id` 改为 `uuid5(thread_id + source_id + target_id + relation_type)`。
- [x] 同一对实体、同一关系类型跨轮次/跨批次只保留一条边。
- [x] 测试：连续两次提交同一关系，Neo4j 只有一条 REL。
- [x] 模型输出枚举外 `relation_type` 时降级为 `related_to` 并告警，避免整轮提交丢失。

### 4. 实体溯源收敛

- [x] 世界导入不再把所有父块编号挂到每个实体/属性/关系上。
- [x] 世界导入实体提交时 `source_chunk_nos=[]`；`entity_mentions.chunk_nos` 为空。
- [x] 向量召回继续保留 chunk/父块溯源，这是原文轨的终点。
- [x] 确认回退仍只依赖 `created_turn_no/created_batch_id/turn_no`，不依赖块号。

### 5. 合并降级可见

- [x] 导入 summary 增加 `consolidation_levels`、`consolidation_failures`、`degraded`。
- [x] 合并失败仍走 exact-canonical 兜底，但日志和返回值明确标记。
- [x] 不在代码里新增别名冲突自动合并；冲突由整理 agent 处理。

## P1：性能与流程

### 6. 长导入优化与续跑

- [ ] 记录每个窗口的抽取状态（pending/done/failed），支持从失败窗口继续。
- [x] 批次异常时置为 `failed` 并记录错误，不再永久停在 processing。
- [ ] 向量化按窗口分批写库，失败时保留已完成窗口。
- [ ] 评估 embedding batch size 与 CPU 线程占用，给出默认值。
- [ ] 15 万字回归：总耗时、峰值内存、失败恢复。

### 7. 召回轨迹随轮次保留

- [x] `retrieval_traces` 按 `turn_no` 与消息上下文同生命周期。
- [x] 每轮结束或 5 轮窗口推进时，删除窗口外的轨迹。
- [x] 回退时继续按 `turn_no` 删除。
- [x] 测试：6 轮后只剩最近 5 轮轨迹，turn 0 开场白轨迹保留。

### 8. LLM 结构化输出统一校验与重试

- [x] 新增 `LLMService.chat_json_model(model_cls, ...)`：
  - 调用 `chat_json`；
  - pydantic 校验失败时，把校验错误回传模型重试一次；
  - 仍失败则抛出，由调用方决定 fallback。
- [x] 接入：API2 `AP2AnalysisResult`、API3 `CommitResult`、世界抽取、整理 agent。
- [x] 文本类输出（GM 回复、开场白）增加空回复重试一次。
- [x] 测试：第一次返回非法结构、第二次合法，调用方拿到合法模型。

### 9. 并行/异步审计落地

- [x] API3 入库前实体 embedding 改为批量 `embed([...])`，不再逐个 `embed_one`。
- [x] `pre_turn` 的上下文召回与实体召回并行提交。
- [x] 多个假设实体的向量召回合并为一次批量 embedding。
- [ ] 对话 5 轮切分可并行（按轮提交），再统一批量向量化。
- [ ] `entity_index` 批量 upsert，减少逐条事务。
- [ ] 回退时 PostgreSQL 与 Neo4j 删除可并行（可选，需保证幂等）。
- [x] 输出一份“串行调用点清单”，标明哪些已并行、哪些保持串行及原因。

### 10. 三层主线记忆

- [x] L0：单轮摘要，每轮一次；active 阶段作为查询材料，离开 5 轮窗口后向量化归档。
- [x] L1：每 5 条 L0 合并为章节摘要，带 `turn_start/turn_end` 与向量。
- [x] L2：每个新章节递归合并为全局梗概，常驻 GM 提示词。
- [x] 主线召回：全局梗概 + `turn_end <= current_turn-5` 的章节摘要；无 L1/L2 时回退已归档 L0。
- [x] 回退删除 `plot_nodes` 中对应轮次/章节及全局梗概（下次维护重建）。
- [x] 7 + 5 轮真实链路验证：第 6 轮生成 L1/L2，第 11 轮更新 6–10 章节与全局。

## P2：暂缓 / 后续

- [ ] 状态管道：滚动场景/目标/情绪字段与乐观锁。
- [ ] 后台任务持久化与失败任务表（当前判断影响不大）。
- [ ] REINDEX 维护计数与后台重建。
- [ ] 前端重试原子化、流式输出、会话重命名/删除。
- [ ] 正式 schema 迁移机制。
- [ ] 数据库集成测试：rollback、5 轮入库、增量导入。

## 串行调用点清单

| 调用点 | 状态 | 说明 |
|---|---|---|
| 世界导入：多窗口抽取 | 已并行 | `world_extract_workers=6` |
| 世界导入：抽取 vs 向量化 | 已并行 | 两个 future 同时跑 |
| 世界导入：整理分组 | 已并行 | 每组一个 future，最多 3 路 |
| 实体入库前 embedding | 已批量化 | 一次 `embed(texts)` |
| `pre_turn`：上下文召回 vs 实体召回 | 已并行 | 上下文召回在独立 future |
| 假设实体向量 query | 已批量化 | 一次 `embed_queries` |
| 对话 5 轮切分 | 仍串行 | 共享 embedding 模型且 CPU 密集，先保持稳定 |
| `entity_index` 写入 | 仍逐条 | 单次最多 8 个实体，暂不引入批量 SQL |
| 回退：PostgreSQL vs Neo4j | 仍串行 | 优先保证补偿顺序和幂等 |
| API2 → GM → API3 | 必须串行 | 后者依赖前者输出 |
| 开场白：召回 → LLM | 必须串行 | LLM 依赖召回上下文 |

## 实施顺序

1. `source_order` + 稳定 `rel_id`（数据正确性的地基）
2. 增量导入 + 批次元数据
3. 实体溯源收敛 + 合并降级上报
4. LLM 统一校验/重试
5. 并行/异步优化
6. 轨迹保留 + 长导入续跑
