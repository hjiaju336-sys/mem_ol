# 可迁移记忆内核：业务逻辑流程图

> 当前实现的输入分流、召回、GM、状态/时间、后台维护、回退和世界导入。
> 参数与边界见 `docs/current_state.md`；验证结果见 `docs/eval_glm_2026-11-14.md`。

![业务逻辑流程图](business_logic_flow.png)

> 图片来源：`business_logic_flow.html`（Mermaid 渲染）。

## 1. 单轮对话 + 分流 + 召回 + 状态 + 后台维护

```mermaid
flowchart TD
    U["玩家输入 / ClientContext<br/>custom_prompt + pending + lorebook"] --> CLASSIFY["输入分流 API2-A<br/>narrative / meta / mixed"]

    CLASSIFY -- meta --> META["元交互<br/>OOC 回答 / 临时指令<br/>不落库、不计 turn"]
    CLASSIFY -- narrative / mixed --> PREP["LangGraph prepare<br/>计算 turn_no / 清空上一轮字段"]

    subgraph TURN["单轮主链路（同步）"]
        PREP --> A2A["API2-A：单轮总结 + 查询改写"]
        PREP --> A2B["API2-B：实体假设<br/>最近 5 轮"]
        A2A --> CTXQ["上下文 query<br/>rewritten_query + context_summary"]
        A2B --> ENTQ["实体检索词 entities"]
        ENTQ --> EK["Neo4j 关键词召回<br/>CJK 全文索引"]
        ENTQ --> EV["PG entity_index 向量召回"]
        EK --> FUSE["entity_id 去重 + RRF"]
        EV --> FUSE
        FUSE --> EXP["1~2 跳图扩展<br/>节点/边/环路去重"]
        CTXQ --> CR["子块向量召回<br/>父块展开 + 父块去重"]
        EXP --> RR["bge-reranker 精排<br/>实体 0.6 / 上下文 0.4"]
        CR --> RR
        RR --> STATE["state 节点<br/>读 game_states<br/>attack 检定 / 时间 / 状态"]
        STATE --> GM["GM 五段提示词<br/>核心规则 + 本轮契约 + 玩家自定义<br/>+ 世界书 + 记忆<br/>状态卡 + 本轮结算"]
        GM --> RECON["reconcile<br/>GM effects 校验<br/>HP / 标签 / flag / 时间"]
        RECON --> PERSIST["persist<br/>消息 + state_events + game_states"]
    end

    PERSIST --> REPLY["返回玩家"]
    PERSIST --> BG

    subgraph BG["后台任务（BackgroundTasks，ingest_lock 串行）"]
        API3["API3 实体/属性/关系固化"]
        ING["5 轮增量向量入库"]
        PLOTM["主线维护<br/>L0 归档 / L1 / L2"]
    end

    subgraph STORE["存储"]
        PG[("PostgreSQL + pgvector<br/>messages / chunks / entity_index<br/>retrieval_traces / plot_nodes<br/>game_states / state_events / checkpoints")]
        NEO[("Neo4j<br/>Entity / AttributeChange / REL<br/>relation_label 自由标签")]
        L0["L0 单轮摘要 + 时间标签"]
        L1["L1 章节摘要"]
        L2["L2 全局梗概"]
    end

    API3 --> NEO
    API3 --> PG
    ING --> PG
    PERSIST --> L0
    PLOTM --> L0
    PLOTM --> L1
    PLOTM --> L2
    L2 --> GM
    L1 --> GM

    subgraph RB["回退 / 重试"]
        UI["前端：最近 5 轮<br/>回退 / 重试"]
        RS["RollbackService<br/>mark rolled_back + 物理删除"]
    end

    UI -.-> RS
    RS --> PG
    RS --> NEO
    RS --> L0
    RS --> L1
    RS --> L2
```

## 2. 世界导入（初始 + 增量）

```mermaid
flowchart LR
    WT["世界文本<br/>doc_id / char_start / char_end"] --> SPLIT["按窗口切分<br/>默认 20000 字"]
    SPLIT --> PAR["N 路并行抽取<br/>默认 6；超时/失败二分"]
    WT --> VEC["父子段切分 + 向量化<br/>只嵌入子块"]
    PAR --> CONS["整理 agent<br/>树形合并 / 别名 / 自由关系标签"]
    CONS --> EVENT["重要约定/事件实体化<br/>婚约 / 三年之约 / 拜师 / 契约"]
    EVENT --> MATCH{"会话已有实体？"}
    MATCH -- 否 --> CREATE["entity_commit_plan<br/>create"]
    MATCH -- 是 / append --> UPDATE["规范名/别名匹配<br/>update + 新增"]
    CREATE --> NEO[("Neo4j<br/>Entity / REL.relation_label")]
    UPDATE --> NEO
    CREATE --> PG[("PG entity_index")]
    UPDATE --> PG
    VEC --> PG
```

## 3. 可迁移内容与应用场景

```mermaid
flowchart LR
    WT["世界文本 / 设定集"] --> K["记忆内核"]
    GP["GM 提示词<br/>角色 / 契约 / OOC / 输出格式"] --> K
    ER["实体与关系 schema<br/>类型 / 自由标签 / 事件实体"] --> K
    TS["工具与状态引擎<br/>检定 / 伤害 / 资源 / 时间"] --> K
    CP["玩家自定义<br/>提示词 / 世界书 / 临时要求"] --> K
    K --> N["小说生成"]
    K --> T["文字互动游玩 / AI TRPG"]
    K --> C["个人陪伴助手"]
    K --> D["数字人 / 角色代理"]
```

共同需求：**实时更新、长对话一致性、初期设定导入、后期自定义**。玩法与表现层可替换，记忆内核不变。

## 4. 关键规则

- **输入分流**：`narrative / meta / mixed`；纯 meta 不落消息、不计 turn；mixed 只把叙事部分送入 GM。
- **API2**：单轮总结 + 查询改写与实体假设两路并行。
- **三条召回**：实体路（关键词 + 向量 + 图扩展）、上下文路（子块 → 父块）、主线路（L2 常驻 + L1 章节）；自定义世界书和提示词由前端随请求携带，不落库。
- **GM**：五段提示词 + 隐藏 `plan`；只返回 `reply`。
- **状态与时间**：GM 根据剧情输出 `hp_delta/tag/flag/scale/time_advance`，引擎校验后写 `game_states`；时间按规则包历法推进，和 HP 一样由工具层修改。
- **关系**：`relation_label` 是自由短语，不做枚举；同一对实体的不同标签是不同边；重要约定/事件做成 event 实体。
- **主线生命周期**：L0 每轮一次，5 轮内 active，离开窗口后归档并向量化；L1 每 5 条 L0 合并；L2 每个新章节递归合并。
- **回退**：消息标记 `rolled_back`，物理删除 chunks/entity_index/mentions/traces/plot_nodes/state_events 和 Neo4j 对应实体/关系；checkpoint 只用于对话状态。
- **后台任务**：API3、5 轮入库、主线维护都走 `BackgroundTasks`，当前不可持久化。
