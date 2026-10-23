# 模块 B 附录 · 工具检索机制（Tool Retrieval / Tool RAG）

> 源码依据：`core/src/tools/handlers/tool_search.rs`、`tool_search_spec.rs`、`tools/src/tool_executor.rs`（ToolExposure）、`tools/src/tool_discovery.rs`。
> 定位：这是模块 B（工具调用与编排）的**专项深化**——当工具数量爆炸（100+）时如何管理。
> 学习目标：讲清「工具分流 + 工具检索」机制、Tool RAG 概念、以及 BM25 算法原理与选型理由。

---

## 1. 问题：工具太多会怎样

两个核心问题：

1. **选错**：工具越多，模型越容易调错（在 100+ 个工具里选 1 个，准确率断崖式下降）。
2. **占 token**：所有工具的完整 spec 都塞进顶层 `tools` 字段，消耗大量上下文预算。

> 这两个问题，正是「工具规模化（tool scaling）」要解决的。

---

## 2. Codex 的解法：工具分流 + 工具检索

**核心思想：不把所有工具塞给模型，而是「常用工具直接给，其余按需检索」。**

### 2.1 第一步：工具按「暴露级别」分流（ToolExposure）

每个工具有 `ToolExposure`（6 种级别，两个核心轴）：

| 级别 | 进初始列表？ | 可被检索？ | 用于 code mode？ |
|---|---|---|---|
| `Direct` | ✅ | ❌ | ✅ |
| `Deferred` | ❌ | ✅ | ✅ |
| `DeferredModelOnly` | ❌ | ✅ | ❌ |
| `DirectModelOnly` | ✅ | ❌ | ❌ |
| `CodeModeOnly` | ❌ | ❌ | ✅ |
| `Hidden` | ❌ | ❌ | ❌ |

两个核心轴：

- `is_direct()` = `Direct` / `DirectModelOnly` → **进**初始 `tools` 数组
- `is_deferred()` = `Deferred` / `DeferredModelOnly` → **可被检索**（不进初始列表）

### 2.2 第二步：初始请求里，`tools` 字段只有 Direct 工具 + `tool_search`

```
第 N 轮的请求 = {
    tools: [Direct 工具...] + [tool_search],    ← 顶层 tools 字段
    input: [ ...对话历史... ],
}
```

- **Direct 工具**：完整 spec 直接进 **`tools` 字段**。
- **Deferred 工具**：**`search_text`（从 spec 派生：工具名 + 描述 + 参数 schema 文本）被索引进 BM25 引擎**，但**完整 spec 不放进请求**。
- **`tool_search`**：一个轻量入口工具，告诉模型「有些工具没给你，用我去搜」。

`tool_search` 描述原文：

> "# Tool discovery\n\nSearches over deferred tool metadata with **BM25** and exposes matching tools for the **next model call**. Some of the tools may not have been provided to you upfront, and you should use this tool (`tool_search`) to search for the required tools."

### 2.3 第三步：检索结果进入 `input`，**不是** `tools` 字段（易错点！）

**⚠️ 关键：检索出的工具不是放进顶层 `tools` 字段，而是作为 `tool_search_output` 条目进入 `input` 数组。**

```
【第 N 轮】
  请求 = {
      tools: [Direct 工具...] + [tool_search],      ← 只有初始工具
      input: [ ...对话历史... ],
  }
  模型：direct 工具不够用 → 调 tool_search("查询用户", limit=8)
        ↓ BM25 检索 deferred 工具的元数据
        ↓ 返回 top-8 匹配工具（LoadableToolSpec）
        ↓ 作为一条 tool_search_output 条目【存进 input 数组】

【第 N+1 轮】
  请求 = {
      tools: [Direct 工具...] + [tool_search],      ← tools 字段【不变】
      input: [ ...对话历史...,
               {type: "tool_search_call", ...},      ← 模型的检索请求
               {type: "tool_search_output",           ← 检索出的工具在这里！
                tools: [工具A完整spec, 工具B完整spec]} ],
  }
  模型：Responses API 服务端读取 tool_search_output → 这些工具变得可调用
```

**两个位置放不同的东西**：

| 内容 | 放哪里 |
|---|---|
| **初始工具（Direct）** | 顶层 **`tools` 字段** |
| **检索出的工具（Deferred）** | **`input` 数组**里的 `tool_search_output` 条目 |

**源码证据**：

```rust
// 1. 检索结果序列化成【输入】条目（不是 tools 字段）
fn to_response_item(&self, call_id: &str, ...) -> ResponseInputItem {
    ResponseInputItem::ToolSearchOutput { tools, ... }   // ← ResponseInputItem
}

// 2. tools 字段的内容在 router 构建时就固定，检索后不变
pub(crate) fn model_visible_specs(&self) -> Arc<[ToolSpec]> {
    Arc::clone(&self.model_visible_specs)
}

// 3. build_tool_router 根本不接收 history / 检索结果
pub(crate) fn build_tool_router(session, turn_context, model_info, ...) -> ... {
    // ← 没有 history、没有 tool_search 结果
}
```

**所以 `tools` 字段每轮不变**（只有 Direct 工具 + `tool_search`），而检索出的工具在 `input` 里累积。

**关键时序**：检索结果**下一轮才生效**（"exposes matching tools for the next model call"）——是「先检索、后使用」的**两轮**过程。

> **补充：还有一种「增量模式」**。当模型是 `responses_lite` 且开启了 `IncrementalTools` feature 时（`uses_incremental_tools()` 为真），连**初始工具也不放 `tools` 字段**——`tools` 字段为空，所有工具通过 `input` 里的 `AdditionalTools` 条目提供。
>
> | 模式 | `tools` 字段 | 工具怎么给模型 |
> |---|---|---|
> | **传统模式** | 放初始工具 | `tools` 字段 + `input` 里的 `tool_search_output` |
> | **增量模式** | **空** | 全靠 `input` 里的条目（`AdditionalTools` + `tool_search_output`） |

### 2.4 四个必须精确的点

1. **deferred 工具是「元数据进索引，完整定义按需加载」**——省掉的不是元数据，而是 `parameters` schema 那块大头。
2. **检索结果下一轮生效**，不是本轮立即。
3. **实际 6 种暴露级别**，核心是两个轴：「进不进初始列表」和「能不能被检索」。
4. **检索出的工具进 `input` 数组（`tool_search_output` 条目），不进 `tools` 字段**——这是最容易讲错的一点。

---

## 3. 概念辨析：Tool RAG / Tool Retrieval / tool_search 是什么关系

**常见困惑**：这三个词看起来像三个并列的东西，其实是**两个层级**。

| 名称 | 层级 | 是什么 |
|---|---|---|
| **Tool Retrieval** | 概念层 | 通用**技术/范式**名 |
| **Tool RAG** | 概念层 | **同一个技术**的另一种叫法（类比 RAG） |
| **`tool_search`** | 实现层 | Codex 里的**具体工具**（模型调它触发检索） |

### 四层关系（名字 → 入口 → 算法 → 数据）

```
【技术名】Tool RAG / Tool Retrieval     ← 这套做法叫什么（不是数据！）
              │
              ▼
【入口工具】tool_search                 ← 模型调用它来触发检索
              │
              ▼
【算法】BM25                             ← 用来给工具打分的算法
              │
              ▼
【被检索的数据】deferred 工具的 search_text  ← 真正被搜索的东西（从 spec 派生）
```

**正确说法**：`tool_search` 这个工具，用 **BM25** 算法，检索 **deferred 工具的元数据**。而这整套做法，名字叫 **Tool RAG**。

**易错点**：不要说「BM25 检索 Tool RAG」。**Tool RAG 是技术的名字（相当于「搜索引擎」），不是被检索的数据。**

### 用「搜索引擎」类比

| Codex 里的东西 | 类比到搜索引擎 |
|---|---|
| Tool RAG / Tool Retrieval | 「搜索引擎」这个**技术名** |
| `tool_search` | Google 的**搜索框**（入口） |
| BM25 | 内部的**排序算法**（类似 PageRank） |
| deferred 工具元数据 | **网页索引**（被检索的数据） |

### 为什么有两个概念名

- **Tool Retrieval** —— 直白描述（「工具检索」）。
- **Tool RAG** —— 类比命名：**RAG 的思想用在工具上**。

```
普通 RAG：用户问 → 检索相关文档 → 塞进 prompt → 模型回答
Tool RAG：模型说缺工具 → 检索相关工具 → 塞进下一轮 tools → 模型用工具
```

结构完全一样，只是「检索对象」从「文档」变成「工具」。

---

## 4. BM25 详解（tool_search 的排序核心）

### 4.1 一句话定义

> **BM25 是一个「给文档打分」的排序函数**：给定一个查询词，计算每个文档与查询的相关度分数，然后按分数从高到低排序。

名字里的 **BM = Best Matching**，**25** 是第 25 个版本（历史遗留）。全名 **Okapi BM25**。

**在 tool_search 里**：

- 「文档」= 每个 deferred 工具的 `search_text`（从 spec 派生）
- 「查询」= 模型给出的 `query`
- **BM25 给每个工具打分 → 取 top-8 返回**

### 4.2 三个核心直觉（BM25 的全部精华）

#### 直觉 1：词频（TF）—— 出现越多越相关，但**有上限（饱和）**

```
"query" 出现 1 次 → 分数 +0.8
"query" 出现 2 次 → 分数 +1.2   （增量变小）
"query" 出现 5 次 → 分数 +1.5   （几乎不再增加）
"query" 出现 100 次 → 分数 +1.51 （基本没用）
```

**为什么要有上限？** 防止「关键词堆砌」刷分。一个文档把 "query" 重复 100 次，不代表它相关 100 倍。

> 这是 BM25 优于老前辈 **TF-IDF** 的关键之一——TF-IDF 是线性的，BM25 是饱和的。

#### 直觉 2：逆文档频率（IDF）—— 越稀有的词越重要

```
"query"    出现在 4 个文档中的 2 个 → IDF 中等（区分度一般）
"database" 只出现在 1 个文档中      → IDF 很高（区分度强！）
"the"      出现在所有文档中         → IDF ≈ 0（完全没用）
```

**为什么？** 到处都有的词没有区分能力。

**对应 tool_search**：如果所有工具描述都有 "tool" 这个词，它在排序里几乎不起作用；而 "database"、"cache" 才是区分工具的关键。

#### 直觉 3：文档长度归一化 —— 长文档要「打折」，但**不能打太狠**

```
文档 A（10 个词）里 "query" 出现 1 次
文档 B（1000 个词）里 "query" 出现 1 次
→ A 更相关（更短，"query" 占比更高）
```

但长文档确实可能更相关（内容多），所以 BM25 用**温和的归一化**，不是粗暴地「除以长度」。

### 4.3 公式拆解

```
score(D, Q) = Σ   IDF(qᵢ)  ·  ──────────────────────────────────
             qᵢ∈Q                f(qᵢ,D) + k1 · (1 - b + b · |D|/avgdl)
```

| 符号 | 含义 |
|---|---|
| `D` | 一个文档（一个工具的描述） |
| `Q` | 查询（模型给的 query） |
| `qᵢ` | 查询里的第 i 个词 |
| `f(qᵢ, D)` | 词 `qᵢ` 在文档 `D` 里出现的次数（**TF**） |
| `\|D\|` | 文档 `D` 的长度（词数） |
| `avgdl` | 所有文档的平均长度 |
| `IDF(qᵢ)` | 词 `qᵢ` 的逆文档频率（**稀有度**） |
| `k1`, `b` | 两个可调参数 |

**不用死记**，只要看它把三个直觉编码进去了：

- 分子有 `f` → 词频越高分越高
- 分母也有 `f` → 让增长**饱和**
- 分母有 `|D|/avgdl` → **长度归一化**
- 各项乘 `IDF` → **稀有词加权**

#### 两个关键参数

| 参数 | 作用 | 典型值 | 极端情况 |
|---|---|---|---|
| **`k1`** | 控制 TF **饱和速度** | 1.2 ~ 2.0 | `k1=0` → 退化为「有/无」二元匹配 |
| **`b`** | 控制**长度归一化强度** | 0.75 | `b=0` → 不归一化；`b=1` → 完全归一化 |

**面试点**：`k1` 和 `b` 是需要**调优**的超参数，不同语料调不同值。

#### IDF 的计算（Lucene 变体）

```
IDF(qᵢ) = ln( 1 + (N - n(qᵢ) + 0.5) / (n(qᵢ) + 0.5) )
```

- `N` = 文档总数
- `n(qᵢ)` = 包含词 `qᵢ` 的文档数
- 词出现在所有文档（`n = N`）→ IDF ≈ 0；只出现在极少数文档 → IDF 很大

### 4.4 具体算一遍

查询：`"query user database"`，语料里有 4 个工具：

```
D1: query_database - Query the primary database for authoritative records
D2: read_file      - Read the contents of a file
D3: send_email     - Send an email to a recipient
D4: query_cache    - Query the Redis cache for fast lookups
```

**第 1 步：算 IDF**

| 词 | 出现在几个工具 | IDF | 区分度 |
|---|---|---|---|
| `query` | D1, D4（2 个） | 中等 | 一般 |
| `user` | 0 个 | — | 不在语料，忽略 |
| `database` | D1（1 个） | **高** | 强 |

**第 2 步：逐个工具算分**

```
D1 命中 query(1次) + database(2次)
   → 中等·TF(query) + 高·TF(database) ≈ 高     ✅ 排第一

D4 只命中 query(1次)
   → 中等·TF(query) ≈ 中低                     排第二

D2、D3 命中 0 个查询词
   → 0                                        不返回
```

**结果**：返回 D1、D4（按分数排序，取 top-8）。

### 4.5 它是什么，不是什么

| 维度 | BM25 的特征 |
|---|---|
| **匹配方式** | **稀疏检索**（sparse）——基于**精确词匹配**（经分词/词干化） |
| **模型类型** | **词袋模型**（bag-of-words）——**忽略词序** |
| **成本** | 查询时**零 embedding 成本、零 GPU**，就是索引查表 |
| **可解释性** | **强**——能看到「哪个词贡献了多少分」 |
| **局限** | 不懂同义词（查 "car" 匹配不到 "automobile"） |

### 4.6 逻辑层面：怎么用 BM25（Python 伪代码）

BM25 的使用分三步：**建文档 → 建引擎 → 查询**。

```python
# ============ 第一步：把每个 deferred 工具的描述做成「文档」 ============
# 每个文档有一个 id（用来回查是哪个工具）和一段文本（工具的 search_text）
文档列表 = []
for idx, info in enumerate(deferred工具列表):
    文本 = info.entry.search_text      # 从工具 spec 派生的检索文本
    文档列表.append(文档(id=idx, text=文本))

# ============ 第二步：构建 BM25 检索引擎 ============
# 注意 language="english"：BM25 需要分词 + 词干化
引擎 = BM25引擎.构建(
    文档列表 = 文档列表,
    语言 = "english",       # 用于分词 + 词干化（把 querying/queries 归一成 query）
)

# ============ 第三步：查询 → 返回 top-N ============
结果 = 引擎.search(query="查询用户", limit=8)

# 结果是【文档 id】的排序列表（按 BM25 分数从高到低）
# 再用 id 回查，拿到对应的工具
命中工具 = [deferred工具列表[doc_id] for doc_id in 结果]
```

**关键点：**

1. **文档 id 用来回查**：BM25 只返回「文档 id 的排序」，需要再用 id 拿回对应的工具。
2. **语言参数很重要**：`language="english"` 决定**分词 + 词干化（stemming）**规则——比如把 "querying"、"queries"、"queried" 都归一成 "query"，这样查询词和文档词才能匹配上。
3. **limit 默认 8**：只取分数最高的 8 个，不是全部返回。

---

## 5. 索引的构建时机与缓存（纯逻辑）

> **先明确一件事**：本节说的「**构建索引**」= 4.6 里的**第 1 步（建文档）+ 第 2 步（建引擎）**，严格说**核心是第 2 步「建引擎」**（昂贵的倒排索引 + IDF 统计都在这里）。**第 3 步「查询」不是索引构建**，它属于「检索」。

### 5.0 与 4.6 的对应关系

| 4.6 的三步 | 属于「索引构建」吗？ | 成本 | 本节对应 |
|---|---|---|---|
| **第 1 步：建文档** | ✅ 是（准备原料） | 低 | 缓存里存「工具集」，正是为了判断它有没有变 |
| **第 2 步：建引擎** | ✅ **是（真正的索引构建）** | **高** | **本节说的「构建」就是这一步**，结果被缓存 |
| **第 3 步：查询** | ❌ 不是 | 低 | 本节说的「检索」，可以调很多次 |

```
┌─────────────────────────────────────────────────────┐
│ 4.6 的三步                                            │
│                                                      │
│  第1步 建文档 ──┐                                     │
│                 ├──► 【索引构建】= 昂贵 ──► 缓存起来    │
│  第2步 建引擎 ──┘         ↑                          │
│                   本节讲的就是"这一步什么时候做、       │
│                   做完怎么缓存、什么时候失效重做"       │
│                                                      │
│  第3步 查询 ─────► 【检索】= 便宜 ──► 可以调很多次     │
└─────────────────────────────────────────────────────┘
```

**为什么缓存里还要存「工具集」**：引擎本身不知道自己是用哪些工具建的，所以必须**另外记一份「当时用的工具集」**，每次访问时比对——一致就复用引擎，不一致就重建。

### 5.1 问题：BM25 索引什么时候建？

三种可能的做法：

| 做法 | 说明 | Codex 是否采用 |
|---|---|---|
| **A. 启动时预建** | 进程一启动就把所有 deferred 工具索引进 BM25 | ❌ |
| **B. 每次检索时重建** | 每次调 `tool_search` 都重新构建索引 | ❌ |
| **C. 按需构建一次 + 缓存复用** | 第一次需要时构建，之后一直复用 | ✅ |

### 5.2 为什么不是 A / B

- **A 的问题**：如果这个会话没有 deferred 工具，或模型从不调 `tool_search`，构建索引就是**白费**。
- **B 的问题**：`tool_search` 可能被调用很多次，而构建索引是**昂贵操作**（要读所有工具、分词、算统计量）——每次都重建太慢。
- **C 兼顾两边**：只在**真正需要时**构建一次，之后**复用**。

### 5.3 逻辑（Python 伪代码）

```python
cache = None   # 全局缓存，初始为空

def 获取工具搜索引擎(registry):
    global cache

    当前工具集 = [t for t in registry if t.is_deferred()]

    # 第一步：先查缓存
    if cache is not None and cache.工具集 == 当前工具集:
        return cache.引擎              # 有缓存且工具集没变 → 直接复用

    # 第二步：没有缓存（或工具集变了）→ 构建索引
    引擎 = 构建BM25引擎(当前工具集)      # ← 这里才真正"建索引"

    # 第三步：存进缓存
    cache = 缓存(引擎, 当前工具集)

    return 引擎
```

### 5.4 时间线

```
【会话开始】

【第一次要发请求给模型时】
   有 deferred 工具吗？
   ├─ 没有 → 不注册 tool_search，也不构建索引（直接跳过）
   └─ 有   → 查缓存 → 空 → 【构建索引】→ 存进缓存
                    ↓
【后续每一次请求】
   查缓存 → 命中且工具集未变 → 直接复用（不重建）
                    ↓
【deferred 工具集发生变化时】
   （如 MCP 动态接入新工具、插件加载/卸载）
   缓存失效 → 重新构建一次 → 再缓存
```

### 5.5 两个逻辑要点

**要点 1：构建贵、检索便宜 → 所以「构建一次、复用多次」**

| 操作 | 成本 | 频率 |
|---|---|---|
| **构建索引** | 高（读所有工具、分词、算统计） | 极少（需要时一次） |
| **检索** | 低（查表） | 可能很多次 |

**要点 2：没有 deferred 工具时，`tool_search` 根本不会出现**

```
有 deferred 工具   → 才注册 tool_search + 构建索引
没有 deferred 工具 → tool_search 不出现，索引也不构建
```

### 5.6 缓存失效的判断逻辑（Python 伪代码）

```python
def 缓存有效(cache, 当前工具集):
    if cache is None:
        return False
    if len(cache.工具集) != len(当前工具集):
        return False
    # 逐个比对：工具集完全一致才算有效
    return all(相同(旧工具, 新工具)
               for 旧工具, 新工具 in zip(cache.工具集, 当前工具集))
```

**失效触发条件**：deferred 工具集发生变化（数量或内容变了）→ 重建。

### 5.7 并发保护：双重检查（Python 伪代码）

```python
def 获取工具搜索引擎(registry):
    当前工具集 = [t for t in registry if t.is_deferred()]

    # 第一次检查
    if 缓存有效(cache, 当前工具集):
        return cache.引擎

    # 构建（耗时操作）
    引擎 = 构建BM25引擎(当前工具集)

    # 第二次检查：构建期间可能有别的任务已经建好了
    if 缓存有效(cache, 当前工具集):
        return cache.引擎

    cache = 缓存(引擎, 当前工具集)
    return 引擎
```

**为什么要二次检查**：构建索引有耗时，如果两个并发请求**同时发现缓存失效**，可能**同时构建两遍**。第二次检查能避免这种重复构建——谁先放进缓存就用谁的。

### 5.8 一句话总结

> BM25 索引是「**按需构建一次 + 缓存复用**」：
> 1. **何时构建**：第一次需要时（不是启动时，也不是每次检索时）；
> 2. **如何复用**：构建后放进缓存，后续请求直接复用；
> 3. **何时重建**：只有 deferred 工具集变化时才重建；
> 4. **并发保护**：双重检查缓存，防两个任务同时构建；
> 5. **额外前提**：如果没有 deferred 工具，`tool_search` 和索引都不会出现。
>
> 核心逻辑：**构建索引很贵、检索很便宜 → 所以「构建一次、复用多次」。**

---

## 6. 为什么 BM25 是 tool_search 的「绝配」

1. **工具描述关键词明确**：`query`、`database`、`file`、`read`、`email`——**字面匹配就能命中**，不需要语义理解。
2. **模型和工具描述「用词一致」**：模型生成的 query 和工具描述都是 LLM 领域英文技术文本，**精确匹配命中率高**。
3. **零成本零延迟**：工具检索是**每次请求都可能调**的高频操作。BM25 是纯本地计算，毫秒级、免费。
4. **可解释可调试**：工具选错时，能看清「哪个词没匹配上」，便于优化工具描述。

**关键数字**：`TOOL_SEARCH_DEFAULT_LIMIT = 8`（一次最多返回 8 个工具）。

---

## 7. 与其他检索方案对比

| 方案 | 优点 | 缺点 | 适合 |
|---|---|---|---|
| **BM25**（Codex 选它） | 零成本、快、可解释、关键词匹配好 | 不懂同义词 | **关键词明确的检索**（工具、API） |
| **TF-IDF**（BM25 前身） | 简单 | TF 线性、归一化粗暴 | 老式检索 |
| **向量检索**（embedding） | 懂语义、同义词 | 要 embedding 调用（成本+延迟）、不可解释 | **语义模糊、口语化查询** |
| **混合检索** | 兼顾 | 复杂度高 | 生产级 RAG 常用 |

> **面试金句**：「工具检索场景下，工具描述是**关键词密集的技术文本**，BM25 这种稀疏检索**性价比最高**——语义检索的能力用不上，却要付 embedding 的成本。**选型看场景，不是越先进越好。**」

---

## 8. 面试答题框架（分三层）

**第一层：核心机制**
→ 工具检索（Tool Retrieval / Tool RAG）：工具按 `ToolExposure` 分流——`Direct` 进初始 **`tools` 字段**，`Deferred` 只把元数据索引进 **BM25** 引擎。初始只给「Direct 工具 + `tool_search`」；模型调 `tool_search` 检索出 top-8 相关工具，其完整定义作为 **`tool_search_output` 条目进入 `input` 数组**（下一轮生效，两轮过程）。

**第二层：算法选型**
→ 稀疏检索（BM25）够用，因为工具描述关键词明确，且零 embedding 成本。

**第三层：工程兜底（除检索外的手段）**

| 手段 | 说明 |
|---|---|
| 命名空间分组 | 工具按 namespace 组织，降低选择负担 |
| 按模式过滤 | 不同模式暴露不同工具集 |
| 子 agent 委派 | 子 agent 只带自己需要的工具子集 |
| Code Mode | 让模型写代码调工具，一次组合多个 |
| 约束解码 | 保证输出只可能是合法工具名 |
| 工具精简治理 | 控制数量，定期合并/下线 |

---

## 9. 工具规模化的其他业界方案

### 9.1 先明确：这些方案解决的是「什么问题」

本节所有方案，都是解决**同一个问题**——**工具规模化（tool scaling）**，也就是「工具太多」。它有两个后果：

```
问题：工具太多
   ├─ 后果 A：模型【选错】（在 100+ 个工具里选 1 个）
   └─ 后果 B：【占 token】（所有工具的 spec 都塞进顶层 tools 字段）
```

**下面 5 个方案，就是这两条后果的不同解法。**

### 9.2 五个方案逐个拆解

| 方案 | 解决「选错」？ | 解决「占 token」？ | 核心思路 |
|---|---|---|---|
| **1. Tool RAG（工具检索）** | ✅ | ✅ | 不把全部工具给模型，先检索出相关的再给 |
| **2. 两级分类** | ✅ | 🔶 部分 | 把「100 选 1」拆成「先选类目，再选类目下的工具」 |
| **3. Sub-agent delegation** | ✅ | ✅ | 主 agent 只带少量元工具，具体工具在子 agent 里 |
| **4. Code-as-action** | 🔶 部分 | ✅ | 让模型写代码调工具，绕开 function call 的数量限制 |
| **5. Fine-tuned routing** | ✅ | ✅ | 训小模型专做工具路由，把「选工具」从主模型剥离 |

#### 方案 1：Tool RAG（工具检索）—— Codex 用的这套

- **核心**：工具先建索引，模型先检索再使用（详见本文档第 2~5 节）。
- **同时解决两个问题**：候选集从 100 缩到 8（解决选错）；工具定义按需加载（解决 token）。

#### 方案 2：两级分类（Hierarchical）

- **核心**：先把工具分成类目（如「文件操作」「数据库」「网络」），模型先选类目，再看类目下的具体工具。
- **主要解决「选错」**：把「100 选 1」变成「先 10 选 1（类目），再 10 选 1（具体工具）」——**每步候选集都小**。
- **对 token 帮助有限**：除非类目也做延迟加载，否则类目列表本身也要进 prompt。
- **Codex 的轻量版**：工具用 **namespace 分组**（如 `multi_agent_v1` 下挂 spawn/send/wait），算这个思路的简化。

#### 方案 3：Sub-agent delegation（子 agent 委派）

- **核心**：主 agent 只带几个「元工具」（如 `delegate_to_file_agent`、`delegate_to_db_agent`），具体工具在每个子 agent 内部。
- **同时解决两个问题**：每个 agent 的工具集都很小（解决选错）；每个 agent 的 prompt 只带自己的工具（解决 token）。
- **本质**：**用「架构隔离」来控制工具数量**。
- **Codex 也用了**：multi-agent 的 spawn agent（模块 D 会讲）。

#### 方案 4：Code-as-action（代码即行动）

- **核心**：让模型**写一段代码**来调工具，而不是「一个工具一个 function call」。
- **主要解决「token」**：不用给每个工具写一份 function call schema，只需提供「工具在代码里怎么调」的文档。
- **举例**：Codex 的 **Code Mode** 就是这套。

#### 方案 5：Fine-tuned routing（微调路由模型）

- **核心**：训一个**小模型**专门做「该用哪个工具」的判断。
- **解决两个问题**：小模型只输出「工具名」（输出小）；主模型不用在 prompt 里看全部工具（省 token）。
- **代价**：要训练、维护、上线，成本高——一般大规模场景才用。

### 9.3 关键认知：这些方案**不互斥，常组合使用**

**别以为它们是「五选一」。** 生产系统经常**同时用多个**。事实上 **Codex 自己就用了至少 3 个**：

| 方案 | Codex 是否用了 | 对应模块 |
|---|---|---|
| Tool RAG | ✅（`tool_search` + BM25） | 本文档 |
| Sub-agent delegation | ✅（multi-agent，spawn agent） | 模块 D |
| Code-as-action | ✅（Code Mode） | — |
| 两级分类 | 🔶（用了 namespace 分组，轻量版） | 模块 B |
| Fine-tuned routing | ❌（未用） | — |

**面试正确姿势**：

> 「工具规模化有多种解法（检索、分类、委派、代码化、微调路由），**它们不互斥**。生产系统通常**组合使用**——比如 Codex 同时用了工具检索 + 子 agent 委派 + code mode。」

### 9.4 一句话总结

> 本节 5 个方案**都是解决「工具规模化」这一个问题**——即「工具太多导致的**选错** + **占 token**」。不同方案侧重点不同，但**不互斥，生产系统常组合使用**（Codex 自己就用了 3 个）。

---

## 10. 面试追问推演

### 10.1 检索不到怎么办？

#### 先明确问题：「query 用词对不上」指什么

模型调 `tool_search` 时会填一个 `query` 参数：

```python
tool_search 的参数 = {
    "query": "给 deferred 工具用的搜索词",     # ← 模型自己填的
    "limit": "最多返回几个工具，默认 8"
}
```

**「query 用词和工具描述对不上」** 的意思是：模型在 `query` 里用的词，和工具描述里的词，**字面不重合**。

```
工具描述："Query the primary database for user records"
                              ↑ 用的是 query / database / user / records

模型填的 query："fetch client info"
                  ↑ 用的是 fetch / client / info   ← 完全对不上
```

**BM25 是精确词匹配**（稀疏检索），`fetch ≠ query`、`client ≠ user` → 打分全为 0 → **检索返回空**。

#### 三个常见原因

| 原因 | 例子 |
|---|---|
| **同义词** | 模型说 "fetch"，描述写 "query" |
| **中英文不一致** | 模型说「查询」，描述是英文 "query" |
| **专业术语 vs 口语** | 模型说「找一下客户」，描述写 "用户记录检索" |

#### Codex 的实际做法（两个机制）

**机制 1：在 `tool_search` 描述里预告「有哪些工具来源」（关键）**

看 `tool_search` 描述的真实内容：

```
# Tool discovery

Searches over deferred tool metadata with BM25 and exposes matching tools for the next model call.

You have access to tools from the following sources:
- Google Drive: Use Google Drive as the single entrypoint for Drive, Docs, Sheets, and Slides work.
- docs

Some of the tools may not have been provided to you upfront, and you should use this tool (`tool_search`) to search for the required tools.
```

注意 `You have access to tools from the following sources` —— 它把**工具来源清单**写进描述里。

**为什么能解决「检索不到」**：模型在检索**之前**就知道「环境里有 Google Drive 相关工具、有 docs 相关工具」，填 query 时**能用对词**（用 "drive" 而不是「云端文件」）。

> 补充：Codex 有 feature 开关 `DeferredToolWorldState`，开启后这份来源清单会从「工具描述」挪到「world state」注入，省工具描述的 token。

**机制 2：检索不到就返回空，让模型换词重试**

```rust
if self.search_infos.is_empty() {
    return Ok(ToolSearchOutput { tools: Vec::new() });   // 返回空列表
}
```

返回空而不是报错——模型收到空结果可以**换个 query 再试**（让模型自己纠错）。

> 另有基本校验：`query` 为空会返回 `"query must not be empty"`。

#### 业界通用解法（7 种，分预防 / 兜底）

**【预防类】让检索更可能成功**

| # | 手段 | 说明 |
|---|---|---|
| 1 | **优化工具描述，加同义词/别名**（最根本） | 把模型可能用的各种说法写进描述，让 BM25 能匹配上 |
| 2 | **在 `tool_search` 描述里预告工具来源** | Codex 的做法，给模型关键词提示 |
| 3 | **引导模型用工具描述里的关键词搜** | 在描述里明确写「用关键词搜，别用口语」 |
| 4 | **查询改写（Query Rewriting）** | 用一次 LLM 调用把口语 query 改写成标准术语，再检索 |
| 5 | **混合检索（BM25 + 向量）** | BM25 抓精确关键词，向量抓语义/同义词——生产级最常用 |

**手段 1 的例子：**

```
❌ "query_database - Query the primary database for user records"
✅ "query_database - Query the primary database for user records.
     Also known as: fetch customer info, look up user data, 查询用户资料。"
```

**手段 4 的例子：**

```
模型原始 query："找一下客户资料"
     ↓ LLM 改写成工具领域的标准术语
改写后 query："query user records"
     ↓ BM25 检索
命中 "Query the primary database for user records"
```

**【兜底类】检索失败后的退路**

| # | 手段 | 说明 |
|---|---|---|
| 6 | **核心工具保留为 Direct** | 最常用的工具**不做延迟加载**，直接进初始 `tools` 数组——即使检索失败，agent 也不会瘫痪 |
| 7 | **优雅降级：失败多次就扩大暴露范围** | 连续检索不到时，退回「默认暴露更多工具」——牺牲 token 换可用性 |

#### 面试答题框架

> **Q：工具检索不到怎么办？**
>
> 分两层：
>
> **预防层**：① 工具描述里加**同义词/别名**（最根本）；② 在 `tool_search` 描述里**预告工具来源**给关键词提示；③ **查询改写**（LLM 把口语 query 改写成标准术语）；④ **混合检索**（BM25 + 向量，向量能兜住同义词）。
>
> **兜底层**：⑤ 核心工具**保留为 Direct**，不依赖检索；⑥ 连续失败就**降级扩大暴露范围**（牺牲 token 换可用性）。
>
> **Codex 的做法**：工具描述质量 + 在 `tool_search` 描述里列出「有哪些工具来源」+ 返回空让模型换词重试。

#### 一句话总结

> 「检索不到」的根因是 **BM25 只认字面，同义词匹配不上**。
> **解法分预防和兜底**：预防靠「**优化描述（加同义词）+ 给关键词提示 + 查询改写 + 混合检索**」；兜底靠「**核心工具不延迟加载 + 失败降级**」。

### 10.2 检索出的工具是永久留在上下文，还是用完就撤？

**答案：留在上下文——不是「用完就撤」。** 它会作为一条历史记录持久存在，跨轮可用。

#### 机制：检索结果是一条正式的 history 条目

关键在于：`tool_search` 的返回**不是临时变量，而是一条正式的历史记录**。

```
tool_search 返回 → ToolSearchOutput { tools: [工具A完整spec, 工具B完整spec] }
                     ↓
                 序列化成一条 input 条目（type: "tool_search_output"）
                     ↓
                 存进对话历史（input 数组）
```

所以历史数组会长这样：

```python
input = [
    {"type": "message", "role": "user", "content": "..."},
    {"type": "tool_search_call", "call_id": "ts_1", "arguments": {"query": "..."}},
    {"type": "tool_search_output", "call_id": "ts_1",
     "tools": [工具A完整spec, 工具B完整spec]},        # ← 工具 spec 存在这里
    # ... 后续对话 ...
]
```

#### 证据：它被当作「正式历史条目」

源码里 `is_api_message()` 判断「哪些条目留在历史里、发给 API」：

```rust
ResponseItem::ToolSearchOutput { .. } => true,   // ← 留在历史
```

而且它**计入 token 统计**（源码统计了它的 `tools` 字段大小）：

```rust
ResponseItem::ToolSearchOutput { tools, .. } => json_content_bytes(tools),
```

**这两点说明**：检索出的工具 spec 是**真实占用上下文的持久内容**，不是一次性的。

#### 什么时候才会「撤」？—— 压缩（compaction）

**没有找到任何显式的「卸载/过期」机制**。唯一的「撤」是**隐式**的——**上下文压缩**（见模块 C）：

```
压缩后的历史 = 初始上下文 + 保留的用户消息 + 摘要
```

`tool_search_output` **不在这三者之列** → 压缩时会**被丢掉**。

完整生命周期：

```
检索到工具 → 存进 input → 持续可用（跨多轮）
                  ↓
          上下文满了触发压缩
                  ↓
          压缩只保留「初始上下文 + 用户消息 + 摘要」
                  ↓
          tool_search_output 被丢掉 → 工具「撤了」
                  ↓
          模型若还需要 → 重新调 tool_search（它自己还在）
```

**这个设计自洽**：`tool_search` 工具自己是 `Direct`（不会被撤），所以模型随时能再检索一次。

#### 为什么这样设计（而不是「用完就撤」）

| 方案 | 问题 / 收益 |
|---|---|
| 用完立刻撤 | 模型下一轮又要用同一个工具时，**得重新检索一遍**——浪费一次往返 |
| **留在上下文（Codex）** | 检索一次后续直接用——**省往返，但持续占 token** |

**权衡**：用「**持续占 token**」换「**减少重复检索往返**」。

#### 潜在问题：无限累积 + 三层缓解

反复检索会累积。缓解机制：

| 缓解机制 | 作用 |
|---|---|
| **`coalesce_loadable_tool_specs`** | **单次检索内**去重（同一工具不重复返回） |
| **`limit=8`** | 每次检索最多返回 8 个 |
| **压缩（compaction）** | 上下文满时丢掉这些条目，**兜底清空** |

#### 面试答题框架

> **Q：检索出的工具会一直留在上下文，还是用完就撤？**
>
> **会一直留在上下文。** 机制上，检索结果作为一条 `tool_search_output` **历史条目**存进 `input` 数组（源码里 `is_api_message = true`，且计入 token 统计），跨多轮持续可用。
>
> **没有显式「卸载」机制**，唯一的「撤」是**上下文压缩**——压缩只保留「初始上下文 + 用户消息 + 摘要」，`tool_search_output` 不在保留之列，会被丢掉。之后模型可用 `tool_search`（它自己不会被撤）重新检索。
>
> **设计权衡**：用「持续占 token」换「减少重复检索往返」。防无限累积靠：单次去重 + limit 上限 + 压缩兜底。

#### 一句话总结

> **检索出的工具留在上下文**——作为 `tool_search_output` 历史条目持久存在，跨轮可用。**唯一的「撤」是压缩**。这是「省往返」和「占 token」的权衡，靠「单次去重 + limit + 压缩兜底」防止无限膨胀。

### 10.3 deferred 工具的元数据（search_text）是怎么生成的？

**答案：默认从工具的 spec 自动派生，把 spec 里的多段文本拼接成 `search_text`。**

#### 默认实现：从 spec 派生

`ToolExecutor::search_info()` 的默认实现：

```rust
fn search_info(&self) -> Option<ToolSearchInfo> {
    let spec = self.spec();
    ToolSearchInfo::from_tool_spec(spec, /*source_info*/ None)   // ← 从 spec 派生
}
```

#### ⚠️ 纠正一个误解：不只 name + description

`search_text` **不只是** name + description，而是把 spec 里**多段文本**拼接起来。`default_tool_search_text()` 的拼接内容：

| # | 来源 | 内容 | 举例 |
|---|---|---|---|
| 1 | 工具名 | `tool.name` | `query_database` |
| 2 | **工具名（下划线换空格）** | `tool.name.replace('_', " ")` | `query database` |
| 3 | 工具描述 | `tool.description` | `Query the primary database...` |
| 4 | **参数 schema 文本（递归）** | 参数名 + 参数描述 + 数组项 + 联合类型 | `user_id`、`The user identifier` |

生成的 `search_text` 类似：

```
query_database query database Query the primary database for user records user_id The user identifier ...
```

#### 三个关键设计点

**① 工具名做「下划线换空格」处理**

```rust
push_search_part(parts, tool.name.clone());            // "query_database"
push_search_part(parts, tool.name.replace('_', " "));   // "query database"
```

**为什么？** 模型可能用 "query database"（带空格）搜，而工具名是 `query_database`（带下划线）。**同时索引两种形式**，两种写法都能匹配上。

**② 递归提取参数 schema 里的文本**

```rust
fn append_schema_search_text(schema, parts) {
    // 参数描述
    if let Some(description) = &schema.description { push(description) }
    // 每个参数的【名字】+ 递归进去
    for (name, sub_schema) in schema.properties {
        push(name);                                  // ← 参数名也进索引！
        append_schema_search_text(sub_schema, parts);
    }
    // 数组项、联合类型也递归
    if let Some(items) = &schema.items { ... }
    for variant in schema.any_of { ... }
}
```

**为什么？** 模型可能**按参数来找工具**。比如模型想「按用户 ID 查」，参数名 `user_id` 和它的描述就提供了额外的匹配机会。

**③ namespace 工具会递归展开**

如果工具是 namespace 形式（一个 namespace 下挂多个工具），`search_text` 会包含：
- namespace 的名字 + 描述
- **内部每个工具**的名字 + 描述 + 参数

#### 可以自定义覆盖（这就是「加同义词」的入口）

工具不必用默认派生——可以实现自己的 `search_info()` 提供**自定义检索元数据**：

```rust
fn search_info(&self) -> Option<ToolSearchInfo> {
    ToolSearchInfo::from_spec(
        "查询用户 查找客户 fetch client query user".to_string(),   // ← 手工加同义词/别名
        self.spec(),
        None,
    )
}
```

**这正是第 10.1 节「检索不到怎么办」的解法之一**——在 `search_text` 里主动加同义词。

#### 缓存优化

源码里还有 `from_shared_spec`：对**不可变的 spec**，可以**复用已算好的 `search_text` + 共享 spec**，避免重复计算和拷贝。

#### 面试答题框架

> **Q：deferred 工具的检索元数据怎么来？**
>
> **默认从工具 spec 自动派生**（`ToolSearchInfo::from_tool_spec`），把 spec 里的文本拼接成 `search_text`——包含 **① 工具名 ② 工具名（下划线换空格）③ 工具描述 ④ 参数 schema 里的文本（参数名/描述，递归提取）**。
>
> **关键是「不只 name + description」**：**参数名也在索引里**（模型可按参数找工具）；工具名会同时索引「带下划线」和「带空格」两种形式。
>
> **可以覆盖**：工具能自定义 `search_info()`，手工加同义词/别名——这正是「检索不到」的解法之一。

#### 一句话总结

> **deferred 工具的 `search_text` 默认从 spec 自动派生**，包含：**工具名（两种形式）+ 描述 + 参数 schema 文本（递归）**。**不是只有 name + description**——参数名也在索引里。工具也可自定义覆盖，用来加同义词。

### 10.4 其他待深入的问题

- **为什么不用向量检索？**（已有答案，见第 7 节）
