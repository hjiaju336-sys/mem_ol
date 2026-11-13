# 业务逻辑流程图

> 当前实现的单轮对话、召回、后台维护、回退和世界导入。参数与边界见 `docs/current_state.md`。

![业务逻辑流程图](business_logic_flow.png)

> 图片来源：`business_logic_flow.html`（Mermaid 渲染）。

## 1. 单轮对话 + 召回 + 后台维护

```mermaid
flowchart TD
    U["玩家输入"] --> PREP["LangGraph prepare<br/>计算 turn_no / 读取最近 5 轮"]

    subgraph TURN["单轮主链路（同步）"]
        PREP --> A2A["API2-A：单轮总结 + 查询改写<br/>只看上一轮"]
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
        RR --> GM["GM 生成回复<br/>最近 5 轮原文 + 主线 + 召回"]
    end

    GM --> PERSIST["persist<br/>消息落库 + 本轮 L0 摘要暂存"]
    PERSIST --> REPLY["返回玩家"]

    subgraph BG["后台任务（BackgroundTasks，顺序受 ingest_lock 约束）"]
        API3["API3 实体/属性/关系固化"]
        ING["5 轮增量向量入库"]
        PLOTM["主线维护<br/>L0 归档 / 生成 L1 / 更新 L2"]
    end

    PERSIST --> API3
    PERSIST --> ING
    PERSIST --> PLOTM

    subgraph STORE["存储"]
        PG[("PostgreSQL + pgvector<br/>messages / chunks / entity_index<br/>retrieval_traces / plot_nodes / checkpoints")]
        NEO[("Neo4j<br/>Entity / AttributeChange / REL")]
        L0["plot_nodes L0<br/>单轮摘要"]
        L1["plot_nodes L1<br/>章节摘要"]
        L2["plot_nodes L2<br/>全局梗概"]
    end

    API3 --> NEO
    API3 --> PG
    ING --> PG
    PERSIST --> L0
    PLOTM --> L0
    PLOTM --> L1
    PLOTM --> L2
    L2 --> PLOTCTX["主线上下文<br/>L2 常驻"]
    L1 --> PLOTCTX
    PLOTCTX --> GM

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
    WT["世界文本<br/>doc_id / char_start / char_end"] --> SPLIT["按 20000 字切窗"]
    SPLIT --> PAR["6 路并行抽取<br/>单窗超时 240s；失败二分"]
    WT --> VEC["父子段切分 + 向量化<br/>只嵌入子块"]
    PAR --> CONS["整理 agent<br/>按 30000 字符树形合并"]
    CONS --> MATCH{"会话已有实体？"}
    MATCH -- 否 --> CREATE["entity_commit_plan<br/>create"]
    MATCH -- 是/append --> UPDATE["规范名/别名匹配<br/>update + 新增"]
    CREATE --> NEO[("Neo4j")]
    UPDATE --> NEO
    CREATE --> PG[("PG entity_index")]
    UPDATE --> PG
    VEC --> PG
```

## 3. 关键规则

- **API2 两路并行**：A 路只看上一轮，产出 `context_summary + rewritten_query`；B 路看最近 5 轮，产出扁平 `entities`。
- **三条召回**：实体路（关键词 + 向量 + 图扩展）、上下文路（子块 → 父块）、主线路（L2 常驻 + L1 章节）。
- **主线生命周期**：L0 每轮一次，5 轮内 active，离开窗口后归档并向量化；L1 每 5 条 L0 合并；L2 每个新章节递归合并。
- **回退**：消息标记 `rolled_back`，物理删除 chunks/entity_index/mentions/traces/plot_nodes 和 Neo4j 对应实体/关系；checkpoint 只用于对话状态。
- **后台任务**：API3、5 轮入库、主线维护都走 `BackgroundTasks`，当前不可持久化。
