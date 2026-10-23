# 模块 B · 工具调用与编排

> 源码依据：`core/src/tools/registry.rs`（注册）、`router.rs`（路由）、`orchestrator.rs`（编排）、`sandboxing.rs`（审批要求）、`approvals.rs`（审批）、`protocol/protocol.rs`（AskForApproval）。
> 学习目标：能画出一个工具调用的完整生命周期，讲清「审批 vs 沙箱」「升级不重新审批」「沙箱升级有界」三个核心设计。

---

## 1. 一句话本质

> 模型只能「**说出**要调哪个工具、传什么参数」，真正的**执行、管控、容错**全在工具编排层。

## 2. 工具系统的完整生命周期（分「事前」和「事后」两半）

```
═══════════ 事前：准备阶段（发请求之前）═══════════

  ①【注册层 registry】里存着每个工具的 spec + handler
        │
        ▼
  ②【描述层】把所有工具的 spec 序列化进请求的顶层 tools 字段
        │
        ▼
  发给模型 ──→ 模型看到「我有这些工具可以用」

═══════════ 模型生成（响应）═══════════

  模型决定调工具，输出 function_call("shell", '{"command":"ls"}')

═══════════ 事后：执行阶段（收到 function_call 之后）═══════════

  ③【路由层】按 name="shell" 找到 handler
        ┊
        ┊  ⚠️ 注意：这里只解析出【工具名】，arguments 仍是【原始字符串】
        ▼
  ④【handler 内部 · 第一件事】解析 arguments + 校验
        parse_arguments::<T>(arguments)      ← 解析与校验一步完成
        │
        ▼
  ⑤【handler 内部 · 第二件事】调编排层
        审批 → 选沙箱 → 执行 → 失败升级重试
        │
        ▼
  【runtime】真正执行 shell("ls")
```

**⚠️ 最容易搞错的一点：「解析」在两个不同地方，别混**

| 含义 | 发生位置 | 解析什么 |
|---|---|---|
| **解析 A**：解析 function_call 条目 | **路由层入口** | 把 API 的 `function_call` 条目解析成内部 `ToolCall`（**提取工具名 / call_id，arguments 保持字符串**） |
| **解析 B**：解析 arguments 参数 | **handler 内部** | 把 `arguments` 字符串解析成**类型化参数**并**校验** |

**顺序是「路由（解析 A）→ 解析 arguments（解析 B）→ 编排」**——不是「解析 → 路由 → 编排」。

**为什么必须是这个顺序**：因为**编排层的决策依赖解析后的参数**。例如模型在参数里写了 `sandbox_permissions: "require_escalated"`，编排层必须先解析出这个参数，才知道要不要升级沙箱。

**关键：registry 是「两半共享的中枢」**——它既存 `spec`（事前，生成 tools 字段），也存 `handler`（事后，执行）。

```
                    registry（中枢：spec + handler）
                    /                          \
        【事前】② 描述层                     【事后】③ 路由 → ④ 解析 → ⑤ 编排 → 执行
        （spec → tools 字段）                  （找 handler → 解析参数 → 审批/沙箱/执行）
```

**面试时能画出「事前/事后」两半 + registry 中枢，已经强过大多数人。**

---

## 3. 注册层 registry —— 工具名 → handler + spec 的映射（中枢）

**一句话**：registry 维护一张「工具名 → handler + spec」的映射表，是**事前和事后共享的中枢**。

```python
# registry 的本质：一张映射表
registry = {
    "shell":     Tool(name="shell",     spec=shell_spec,     handler=shell_handler),
    "read_file": Tool(name="read_file", spec=read_file_spec, handler=read_file_handler),
    "write_file": Tool(name="write_file", spec=write_file_spec, handler=write_file_handler),
}
```

每个工具 = **一个 handler（执行逻辑）+ 一份 spec（描述给模型）**。

- **事前**：遍历 registry，把所有工具的 `spec` 抽出来，序列化进 `tools` 字段发给模型。
- **事后**：模型要调 `shell`，就用 `"shell"` 这个 key 查 registry，拿到 `shell_handler` 执行。

> 这就是为什么说 registry 是「中枢」——它被**两半都依赖**。工具系统的新增/删除，本质就是在改这张表。

---

## 4. 描述层：ToolSpec —— 模型怎么知道有哪些工具（事前）

```python
tool_spec = {
    "name": "shell",
    "description": "执行 shell 命令",      # 模型靠它判断「什么时候该用」
    "parameters": {                        # JSON Schema，模型靠它知道「传什么参数」
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}
```

所有工具的 spec 序列化进请求的**顶层 `tools` 字段**。一个完整的模型请求长这样：

```json
{
  "model": "gpt-5",

  "instructions": "You are Codex...",          // ← 顶层字段 ①：系统指令

  "tools": [                                   // ← 顶层字段 ②：工具定义（spec 塞这里）
    {"type": "function", "name": "shell", "description": "执行 shell 命令", "parameters": {...}},
    {"type": "function", "name": "read_file", "description": "读文件", "parameters": {...}}
  ],

  "input": [                                   // ← 顶层字段 ③：对话历史
    {"type": "message", "role": "user", "content": "帮我列出文件"}
  ]
}
```

**「顶层」是什么意思**：`tools` 字段在 JSON 对象的**最外层**，和 `instructions`、`input` **平级**（三个并列的兄弟字段），而不是嵌在 `input` 数组里面。

| 字段 | 内容 | 性质 |
|---|---|---|
| `tools` | 工具清单 | 静态的「有什么工具可用」 |
| `input` | 对话历史 | 动态的「聊了什么」，每轮增长 |

**加分点（为什么 description 很重要）**：模型决定「调不调工具、调哪个工具」时，唯一能看到的就是 spec 里的文字——它**看不到 handler 代码**。所以 `description` 就是喂给模型的「工具路由规则」，质量直接决定工具选择的准确率。这是 prompt 工程常被忽略的一环（人们只打磨系统指令，忘了工具描述也是模型在看的文本）。

### 4.1 如何写好 description（从 Codex 真实范例提炼）

#### 先看 Codex 的真实范例

| 工具 | 真实描述 | 亮点 |
|---|---|---|
| `new_context` | "Start a new context window. **Does not clear, reset, or otherwise affect environment state.**" | 主动澄清「**不**做什么」 |
| `exec_command` | "Runs a command in a PTY, **returning output or a session ID** for ongoing interaction." | 讲清「做什么 + 环境 + 返回什么」 |
| `update_plan` | "Updates the task plan. Provide an optional explanation and a list of plan items... **At most one step can be in_progress at a time.**" | 写明硬约束 |
| 参数示例 | `"Wait before yielding output. Defaults to 10000 ms; effective range is 250-30000 ms."` | 参数写全默认值+范围 |
| 参数示例 | `"User-facing label (1-5 words)."` | 明确格式约束 |

#### 7 条写法原则

1. **写「做什么 + 返回什么」**：模型要知道调完拿到什么，才能规划下一步。
   ```
   ❌ "查询用户"
   ✅ "根据用户 ID 查询用户资料，返回姓名、邮箱、注册时间。用户不存在时返回 null。"
   ```

2. **主动消除误解——写清「不做什么」**（最高级的一条，`new_context` 示范）：
   ```
   ❌ "新建上下文窗口"                    ← 模型以为「一切重置」
   ✅ "开始新的上下文窗口。不会清空、重置或影响环境状态。"
   ```

3. **相近工具必须划清「边界」**（最容易出事故）：
   ```
   ❌ "查询数据库" / "查询缓存"
   ✅ "查询主数据库，返回权威数据。仅在缓存未命中或需要强一致性时使用，较慢。"
       "查询 Redis 缓存，返回可能略旧的数据。用于高频读取、对时效不敏感的场景，快。"
   ```

4. **参数描述写全「默认值 / 范围 / 格式 / 禁止」**：默认值尤其重要（不知默认值 → 乱填或不敢填）。

5. **写明「副作用 / 成本 / 风险」**：让模型知道是只读还是会改数据、快还是贵。

6. **硬约束一定要写**：如「同一时间最多一个步骤是 in_progress」。

7. **用「祈使句 / 指令」语气**，不是说明文档语气（这是**给模型的行为指令**）。

#### 可直接套用的模板

```json
{
  "name": "动词_名词",
  "description": "【做什么】…\n【何时用】…\n【何时别用】…\n【不做什么】…\n【返回值】…\n【约束】…",
  "parameters": {
    "参数名": {"type": "string", "description": "说明。默认值 X；范围 Y；格式 Z。"}
  }
}
```

#### 反面教材

| 错误 | 后果 |
|---|---|
| 不写 description 或只写「工具」 | 模型不知何时用，乱调或不调 |
| 只写「是什么」不写「何时用」 | 相似工具间无法选择 |
| 不写默认值 | 模型乱填参数 |
| 不写副作用 | 模型不知会改数据，可能误伤 |
| 参数名模糊（`data`/`input`） | 模型不知传什么 |

#### 大厂实战建议

1. **工具数 > 10 要警惕**：工具越多模型越易选错。Codex 为此做了 `tool_search`（按需查工具，而非全量塞给模型）。
2. **描述要迭代**：上线后观察误用情况，针对性改——**数据驱动 + A/B**。
3. **描述 = 产品文档，要有 owner**：像 API 文档一样被 review，不是随手写。
4. **考虑 token 成本**：所有描述都进 `tools` 字段占预算，要「够详细但不啰嗦」。

> **一句话**：站在「模型怎么选、怎么填」的角度写，而不是「人能看懂」的角度。**它不是文档，是给模型的决策指令。**

---

## 5. 工具规模化：tools 太多时如何管理（概览）

> **📄 完整深入版见 [02b-tool-retrieval.md](02b-tool-retrieval.md)**（含 BM25 详解、算例、与其他方案对比）。

工具数量爆炸（100+）时有两个问题：**选错** + **占 token**。

Codex 的解法（三步）：

1. **工具按 `ToolExposure` 分流**：`Direct` 进初始 `tools` 数组；`Deferred` 不进，但可被检索。
2. **初始只给 `Direct` 工具 + `tool_search`**：deferred 工具只把 `search_text`（从 spec 派生）索引进 **BM25** 引擎，完整 spec 不放进请求。
3. **模型调 `tool_search` 检索** → BM25 返回 top-8（`TOOL_SEARCH_DEFAULT_LIMIT = 8`）→ 其完整定义作为 **`tool_search_output` 条目进入 `input` 数组**（下一轮生效）。

**概念辨析**：Tool RAG / Tool Retrieval 是**技术名**（同一个概念），`tool_search` 是这个概念在 Codex 里的**具体工具**，BM25 是它内部用的**排序算法**，deferred 工具元数据是**被检索的数据**。

**为什么用 BM25 而非向量检索**：工具描述是关键词密集的技术文本，稀疏检索够用；且 BM25 零 embedding 成本、零延迟。

> 详细原理、公式拆解、具体算例、与其他方案对比，全部在 [02b-tool-retrieval.md](02b-tool-retrieval.md)。

---

## 6. 路由层 router —— 按 name 找 handler（事后第一步）

> **先说清一个术语（避免混用）**：「路由层」（router 模块）是一个**代码模块**，里面有两个函数：
>
> ```
> router 模块（一个文件）
> ├── build_tool_call()    ← 做【解析】：function_call → ToolCall
> └── dispatch_tool_call() ← 做【路由】：按 tool_name 找 handler
> ```
>
> 所以「解析」**发生在** router 模块里，但**「解析 ≠ 路由」**——「路由」特指「按 name 找 handler」这一步。

### 6.1 先明确两个定义

**① 什么是 function_call？**

> `function_call` 是模型自己判断「要调工具」时，输出的一个 **JSON 结构化条目**（不是文字）。它含 `name`（工具名）、`arguments`（参数）、`call_id`（调用 ID）等字段。

真实 JSON 长这样：

```json
{
  "type": "function_call",                  // 类型标记：这是「工具调用」，不是「文字」
  "name": "shell",                          // 要调哪个工具
  "arguments": "{\"command\": \"ls\"}",     // 参数（注意：是 JSON 字符串）
  "call_id": "call_xyz"                     // 调用 ID（结果回灌时配对用）
}
```

**关键认知**：`function_call` 是**模型生成的**——模型在训练时学会了「需要外部信息时就输出一个工具调用」。它的 `name`、`arguments` 都是模型自己决定的，**可能写错**（所以后面才有「解析 + 校验 + 回灌错误」兜底）。

**② 什么是路由？**

> **路由 = 根据 function_call 的 `name` 字段（工具名），去 registry 找到对应的 `handler`（工具的实现）。**

两个精确点：

- 路由**只看 `name` 字段**——`arguments`（参数）和 `call_id` 都还没轮到路由管。
- 路由找到的是 **handler（实现）**，不是 spec（描述）——因为接下来要执行它。

**③ 路由 ≠ 解析（别混）**

| 步骤 | 叫什么 | 做什么 |
|---|---|---|
| ① `build_tool_call` | **解析** | 把 API 的 `function_call` 条目 → 转成内部 `ToolCall` 结构 |
| ② `dispatch_tool_call` | **路由** | 用 `ToolCall.tool_name` 去 registry 查 handler |

**「路由」的核心是「分发」，不是「解析」**——它根据一个 key（工具名）把请求分发到正确的处理者（handler）。

### 6.2 具体流程（两个步骤）

**步骤 1：解析 —— `build_tool_call` 把 function_call 转成内部 `ToolCall` 结构**

```python
tool_call = {
    "tool_name": "shell",              # namespace + name
    "call_id": "call_abc",
    "payload": {"command": "ls"},      # ← 注意：arguments 【还没解析】，仍是原始字符串
}
```

**⚠️ 关键：`build_tool_call` 只提取出「工具名」和「call_id」，不碰 arguments 内容。**

源码（`router.rs` 的 `build_tool_call`）：

```rust
ResponseItem::FunctionCall { name, arguments, call_id, .. } => {
    Ok(Some(ToolCall {
        tool_name,                                    // ← 只提取工具名
        call_id,
        payload: ToolPayload::Function { arguments },  // ← arguments 原样保留为字符串
    }))
}
```

**步骤 2：路由 —— `dispatch_tool_call` 按 `tool_name` 查 registry 找到 handler**

```python
def dispatch(tool_call):
    tool = registry[tool_call["tool_name"]]      # 查 handler
    supports_parallel = tool.supports_parallel    # 查元数据
    return tool, supports_parallel
```

> **router 模块的两个函数分工**：`build_tool_call` 负责「把线上格式转成内部格式」，`dispatch_tool_call` 负责「找到实现」。两者都不做「执行」和「管控」，做完就把接力棒交给 handler（handler 会先解析参数，再调编排层）。

**为什么 `build_tool_call` 不解析参数？** 因为**每个工具的参数结构不同**（shell 要 `{command}`，plan 要 `{plan: [...]}`），router 模块不可能知道每个工具的格式。**只有工具自己知道**，所以参数解析必须留给 handler（下一节讲）。

---

## 7. 解析 —— arguments 字符串 → 类型化参数 + 校验（事后第二步）

### 7.1 解析发生在 handler 内部

**关键认知**：模型返回的 `arguments` 是 **JSON 字符串，不是 dict/对象**。源码注释（`models.rs`）原话：

> *"The Responses API returns the function call arguments as a **string** that contains JSON, not as an already-parsed object."*

```python
# 模型返回的是字符串
arguments_raw = '{"command": "ls"}'      # 一个 str
```

**解析发生在哪？** 在 **handler 内部**——路由找到 handler 后，handler 做的**第一件事**就是解析参数。源码（`exec_command.rs`）：

```rust
async fn handle_call(&self, invocation: ToolInvocation) -> ... {
    // ① 取出原始字符串
    let arguments = match payload {
        ToolPayload::Function { arguments } => arguments,   // 仍是字符串
        ...
    };
    // ② 在 handler 内部解析
    let mut args: ExecCommandArgs = parse_arguments(&arguments)?;
    ...
```

### 7.2 「解析」和「校验」在强类型语言里是**一步完成**的

共享的解析 helper（`handlers/mod.rs`）：

```rust
pub(crate) fn parse_arguments<T>(arguments: &str) -> Result<T, FunctionCallError>
where T: for<'de> Deserialize<'de>,
{
    serde_json::from_str(arguments).map_err(|err| {
        FunctionCallError::RespondToModel(format!("failed to parse function arguments: {err}"))
    })
}
```

关键是 `serde_json::from_str::<T>(arguments)`——**直接解析成类型化的 struct `T`**，不是通用 dict。所以：

| 如果... | 结果 |
|---|---|
| JSON 语法错误 | ❌ 报错 |
| **缺必填字段** | ❌ 报错（struct 字段是必须的） |
| **字段类型不对** | ❌ 报错 |
| 多传未知字段 | ❌ 报错（若声明了 deny_unknown_fields） |

**所以「解析」和「校验」在 Rust 里天然是一步的**——反序列化到强类型 struct 时，类型和必填性自动被检查。

**对比 Python**（解析和校验是分开的两步）：

```python
# Python：两步
args = json.loads(arguments_raw)          # 步骤1：只解析语法
if "command" not in args:                  # 步骤2：手工校验必填
    raise Error("missing required: command")
if not isinstance(args["command"], str):   # 步骤2：手工校验类型
    raise Error("command must be string")

# Rust：一步
let args: ShellArgs = parse_arguments(arguments)?;   # 解析 + 校验 一起做
```

### 7.3 错误如何回灌给模型：`RespondToModel`

注意错误类型是 **`FunctionCallError::RespondToModel(...)`**——名字很关键：

- **`RespondToModel`** = 「这个错误要**告诉模型**」→ 被包装成 `function_call_output`（`success=false`）**回灌**，模型看到后重新生成正确的调用。
- 对比 **`FunctionCallError::Fatal(...)`** = 「致命错误，直接中止」，不回灌。

**完整错误闭环**：

```
模型：调 shell，arguments = '{"cmd": "ls"}'      ← 参数名写错（应为 command）
   ↓
handler：parse_arguments::<ShellArgs>() 失败
   ↓
返回 RespondToModel("failed to parse function arguments: missing field `command`")
   ↓
包装成 function_call_output（success=false）→ 回灌进历史
   ↓
模型下一轮看到错误 → 重新调用 '{"command": "ls"}'   ✅
```

**这就是 agent「能自我纠正」的核心机制**（呼应模块 A 的「失败也回灌」）。

### 7.4 两种解析风格

**风格 1：严格型（多数内置工具）**

```rust
let args: UpdatePlanArgs = parse_arguments(arguments)?;   // 失败就报错回灌
```

**风格 2：宽松型（freeform 工具 / hook）**

```rust
fn function_hook_tool_input(arguments: &str) -> Value {
    if arguments.trim().is_empty() {
        return Value::Object(Map::new());
    }
    serde_json::from_str(arguments)
        .unwrap_or_else(|_| Value::String(arguments.to_string()))   // ← 解析失败就当字符串用
}
```

**宽松型的逻辑**：模型给的是非 JSON 纯文本（如 `apply_patch` 的补丁内容）时，解析失败**不报错**，直接把原字符串当参数用。

> 对应模块 B 讲过的 **freeform 工具**——`apply_patch` 的描述明确写了 "This is a FREEFORM tool, so do not wrap the patch in JSON"。

### 7.5 面试要点

| 问题 | 回答要点 |
|---|---|
| 参数在哪一层解析？ | **不是路由层，是每个工具的 handler**（只有工具知道自己要什么格式） |
| 解析和校验是一步还是两步？ | **强类型语言（Rust）里是一步**（反序列化到 struct 自动校验类型/必填）；动态语言要手工分两步 |
| 解析失败怎么办？ | **回灌给模型**（`RespondToModel`），不是崩溃——这是 agent 自我纠正的基础 |
| 为什么有两种解析风格？ | 严格型给结构化参数；宽松型给 freeform 工具（参数本身是纯文本） |

> **面试点**：这是「模型不可靠，客户端要兜底」的典型例子。永远不要假设模型的输出是合法的——参数解析和校验是客户端义不容辞的责任。

### 7.6 解析失败的四层解决手段谱系（重点）

#### Codex 到底怎么做的（先看总览）

Codex 的核心是「**二分法主框架 + 两个辅助**」：

```
工具执行参数处理：
  ① 缺可选字段？          → serde default 填默认值 → 继续（不报错）
  ② 参数是 freeform 文本？ → 宽松解析，当字符串用   → 继续（不报错）
  ③ 真的解析错了？        → 进入二分法：
        ├─ 大部分 → RespondToModel（回灌重试）
        └─ 致命   → Fatal（中止）
```

- **主框架**：`FunctionCallError` 二分法——要么 `RespondToModel`（回灌重试），要么 `Fatal`（中止）。
- **辅助 1**：serde default（缺**可选**字段 → 不报错，填默认值）。
- **辅助 2**：宽松解析（freeform 文本 → 不算错，当字符串用）。
- **辅助的作用**：让「不算错的情况」**根本不进入**回灌/中止的二分法。

所以 Codex 覆盖了四层中的三层（**第一层「防止」没用**）：

| 层 | Codex 的手段 | 用没用 |
|---|---|---|
| 第一层：防止 | strict / 约束解码 | ❌ 不用（`strict: false`） |
| 第二层：修复 | **serde default**（可选字段默认值） | ✅ |
| 第三层：回灌重试 | **RespondToModel**（回灌） | ✅ **核心** |
| 第四层：兜底降级 | **Fatal**（中止）+ **宽松解析**（freeform） | ✅ |

---

模型输出参数格式不对时，业界有**四层**手段，从「防止」到「补救」到「兜底」：

```
第一层：防止出错（事前，让模型根本不输出错格式）
第二层：自动修复（出错后，程序自己修，不用模型重来）
第三层：回灌重试（出错后，让模型重新生成）← Codex 在这里
第四层：兜底/降级（实在不行，优雅收场）
```

#### 第一层：防止出错

| 手段 | 原理 | 代表 |
|---|---|---|
| **Structured Outputs / strict mode** | API 层强制输出符合 JSON Schema（**约束解码**：解码时限制 token 只能生成合法 schema） | OpenAI `strict: true` |
| **Grammar 约束** | 定义文法（如 GBNF），强制输出符合文法 | llama.cpp、vLLM guided decoding |

**核心思想**：与其事后修，不如**在生成时就保证合法**。

> Codex 基本不用这层（工具 spec 都是 `strict: false`），因为约束解码会限制模型自由，且 freeform 工具本就不需要 JSON。

##### 约束解码（constrained decoding）详解

**先纠正一个误解**：约束解码**你控制得了**——它是你调 API 时传的一个参数（`strict: true`），只是「执行的机器」在服务端。

| 场景 | 谁执行约束解码 | 你怎么控制 |
|---|---|---|
| 用 OpenAI API | OpenAI 服务端 | 传 `strict: true` + `response_format: json_schema` |
| 自部署 vLLM | 你的服务端 | 配 guided decoding 参数 |
| 用 llama.cpp | 你的服务端 | 传 GBNF grammar |

**原理（通俗版）**：

正常生成时，模型每一步都从**整个词表**（5 万个 token）里按概率抽一个。约束解码改的是：**每一步先算「哪些 token 合法」，把非法 token 的概率压成 0**。

```
schema 要求 {"command": "..."}

第1步：JSON 对象必须以 { 开头
      → 除 { 外所有 token 概率压 0 → 只能生成 {
第2步：下一步必须是 "command"
      → 只能生成 "command"
第3步：必须是 :
第4步：必须是字符串的引号 "
第5步：字符串内容（这里才放开给模型自由发挥）
第6步：必须是 "
第7步：必须是 }
```

**所以模型不可能输出 `{"cmd": ...}`（字段名错）**——因为 `cmd` 这个 token 在 schema 约束下是非法 token，概率被压成 0 了。

**核心伪代码**：

```python
def constrained_sample(logits, allowed_tokens):
    for token in vocab:
        if token not in allowed_tokens:
            logits[token] = -inf      # 非法 token 概率压 0
    return sample(logits)             # 只能从合法 token 里抽
```

**它和「校验 / 修复」的本质区别是「时机」**：

| 做法 | 时机 | 本质 |
|---|---|---|
| **约束解码** | **生成时** | 边生成边「堵住」非法路径（**事前**） |
| **校验** | 生成后 | 生成完再「检查」（**事后**） |
| **JSON 修复** | 生成后 | 生成完再「修补」（**事后**） |

> 约束解码**控制的是生成过程本身**，不是生成结果——所以能从根源杜绝错误。

**为什么 Codex 反而没用它（trade-off）**：

| 缺点 | 说明 |
|---|---|
| 可能降低生成质量 | 复杂 schema 下模型可能被「卡住」 |
| 增加延迟 | 每步都要算「合法 token 集合」 |
| 限制自由 | freeform 工具（补丁文本）没法用 schema 约束 |
| 非所有模型支持 | 部分模型不支持 structured outputs |

**结论**：Codex 的工具参数结构简单、回灌重试够用，所以选「错了再改」而非「生成时约束」。

**什么时候该用约束解码**：当「**错一次的代价很高**」时——复杂 schema、下游系统直接消费、不能重试（成本敏感）。

#### 第二层：自动修复（程序自己修，不用模型重来）

| 手段 | 原理 | 代表 |
|---|---|---|
| **JSON Repair** | 规则修复**语法不合法**的 JSON（补括号、去尾逗号、补引号） | `json_repair` 库 |
| **Schema Repair** | 修复「JSON 合法但**不符合 schema**」（缺必填字段、类型错） | 各框架自实现 |
| **LLM 修复** | 再把非法输出发给模型，让它「修成合法格式」 | 多一次 LLM 调用 |

> ⚠️ **先纠正**：Codex **既没做 JSON Repair，也没做 Schema Repair**。它做的是「**serde `default`（可选字段有默认值）**」，这**不是「修复」**——详见下方两段说明。

##### JSON Repair 是什么，Codex 为什么不用

**JSON Repair 是什么**：当模型输出的 JSON **语法不合法**（缺括号、多逗号、没加引号）时，用规则把它「猜着补全」成合法 JSON。

```
模型输出：{"command": "ls",}          ← 多了尾逗号
         ↓ json_repair 修复
       {"command": "ls"}              ← 合法了
```

**Codex 为什么不用**：因为 **JSON repair 是「客户端替模型猜它想说什么」，而对有副作用的操作（执行命令），「猜错」是危险的**：

```
模型输出：{"command": "rm -rf /tmp/",      ← 缺右括号（残缺 JSON）

JSON repair 猜着补：{"command": "rm -rf /tmp/"}    ← 补上并执行了
但模型本来想写：   {"command": "rm -rf /tmp/*.log"}  ← 只想删日志文件

→ 猜着修 + 执行，结果删错了东西
```

**残缺的 JSON 往往意味着「模型没想清楚」或「生成被截断」**。Codex 的选择是：残缺/错误的 JSON → **回灌让模型重新完整生成**，保证「被执行的参数」是模型**明确表达**的意图，而不是客户端猜出来的。

> 工程原则：**对有副作用的操作（执行命令、写文件、删数据），宁可让模型「重新明确表达」，也不要客户端「猜着修复」。**

##### serde `default` 是什么，Codex 为什么用

**先纠正**：Codex 做的**不是 Schema Repair**，而是 **serde 的 `#[serde(default)]`**——「**可选字段**缺了有默认值」。

```rust
pub struct ExecCommandArgs {
    cmd: String,                              // ← 必填字段，【没有 default】
    #[serde(default)] shell: Option<String>,  // ← 可选字段，缺了 → None
    #[serde(default)] tty: bool,              // ← 可选字段，缺了 → false
    #[serde(default)] yield_time_ms: u64,     // ← 可选字段，缺了 → 10000
}
```

**serde `default` 是什么**：反序列化时，如果某个**可选字段**在 JSON 里没出现，就用**预先声明的默认值**填上，而不是报错。

**为什么 Codex 用它**：因为「缺可选字段」**不是错误**——模型合理地省略了非必填项（比如没传 `tty`，意思是「用默认的管道模式」）。用安全默认值兜住即可，没必要让模型重来。

**关键区别（两种「缺」性质不同）**：

| 情况 | 性质 | Codex 处理 |
|---|---|---|
| **缺可选字段**（没传 `tty`） | **不是错误**（合理省略非必填项） | serde `default` 兜底 |
| **缺必填字段 / JSON 语法错** | **是错误** | 回灌重试（`RespondToModel`） |

**为什么默认值是安全的**：因为 `default` 值是**开发者显式声明的安全值**（如 `tty=false`），不是「猜」出来的。所以「填默认值」安全，「猜着修」危险。

**三者对比**：

| 手段 | 本质 | Codex 用没用 |
|---|---|---|
| **JSON Repair** | 「猜」模型想说什么 | ❌ 不用（猜错危险） |
| **Schema Repair** | 修复「真错误」（缺必填/类型错） | ❌ 不做 |
| **serde `default`** | 可选字段有「显式声明的安全默认值」 | ✅ 用（不是修复，是「缺了用默认值」） |

#### 第三层：回灌重试（让模型重新生成）

| 手段 | 原理 | 代表 |
|---|---|---|
| **回灌错误信息** | 把「哪里错了」作为 function_call_output 喂回去，模型自己改 | **Codex 的核心**（`RespondToModel`） |

```
模型输出：{"cmd": "ls"}     ← 参数名错
   ↓
回灌："参数解析失败：缺少必填参数 command"
   ↓
模型重新输出：{"command": "ls"}   ✅
```

**这是 agent「自我纠正」的核心**，也是大多数 agent 框架的默认做法。

#### 第四层：兜底/降级

| 手段 | 原理 |
|---|---|
| **宽松解析** | 解析失败就把原字符串当参数用（freeform 工具） |
| **默认参数执行** | 用安全默认值跑 |
| **中止 + 报错给用户** | `Fatal`，让用户介入 |

#### Codex 的选择

`FunctionCallError` 只有两个变体：

```rust
pub enum FunctionCallError {
    RespondToModel(String),   // ① 回灌给模型，让它重试
    Fatal(String),            // ② 致命错误，直接中止
}
```

| 手段 | Codex 用了没 |
|---|---|
| 回灌错误让模型重试（`RespondToModel`） | ✅ **核心手段** |
| 致命错误中止（`Fatal`） | ✅ |
| 宽松解析（freeform 工具） | ✅ |
| 可选字段默认值（serde `#[serde(default)]`） | ✅（**不是 Schema Repair**） |
| 约束输出 / strict mode | ❌ 基本不用（`strict: false`） |
| JSON 自动修复（json_repair） | ❌ 没找到 |
| Schema Repair（修复缺必填字段） | ❌ 没做 |

**结论**：Codex 选择「**不预防，靠回灌重试**」路线——模型输出错就喂回去让它自己改，不做 JSON repair、不用约束解码。

#### 其他主流厂商

| 厂商/框架 | 主要手段 |
|---|---|
| **OpenAI** | **strict mode（约束输出）** + 客户端校验 |
| **Anthropic（Claude）** | tool use 带明确 `input_schema`，schema 遵循度较高 |
| **LangChain / LangGraph** | output parser + `RetryWithErrorOutputParser`（回灌重试的封装） |
| **LlamaIndex** | `FunctionTool` + structured output，混合约束 + 回灌 |
| **生产级通用** | **混合**：约束输出（防止）+ 回灌重试（补救）+ JSON repair（自动修） |

#### 一句话总结

> 「模型输出格式错」有四层解法：**防止（约束输出）→ 修复（JSON/Schema repair）→ 回灌重试 → 兜底降级**。Codex 以「回灌重试」为主，配合宽松解析和字段默认值，不用约束解码、不做 JSON repair。生产级通用做法是**混合**，并给「重试+修复」设**预算上限**防无限重试。

### 7.7 重试预算（防无限循环）

不管用哪种手段，**必须设「重试/修复」预算上限**，否则：

```
模型一直输出错 → 一直回灌 → 一直重试 → 无限烧 token/钱
```

生产系统通常：**「Retry + Repair 共用 N 次预算，超了就 Fatal 中止 + 报错给用户」**。

#### Codex 的做法：成本预算（RolloutBudget）

**关键**：Codex 对「回灌重试」**没有固定次数上限**（搜 `max_respond`、`max_tool_retries` 都无结果）。它用「**成本预算**」当隐式上限——**不数次数，数花了多少钱（token）**。

源码 `rollout_budget.rs` 揭示三个要素：

**要素 1：预算上限 + 权重**

```rust
struct RolloutBudgetConfig {
    limit_tokens: u64,              // ① 预算上限（token 数）
    sampling_token_weight: f64,     // ② 输出 token 的权重（更贵，权重更高）
    prefill_token_weight: f64,      // ③ 输入 token 的权重
    reminder_at_remaining_tokens: Vec<u64>,  // ④ 提醒阈值列表
}

// 记录用量：加权累加
weighted_tokens_used += 输出token × sampling权重 + 输入token × prefill权重
```

**要素 2：软提醒（预算快没了，先「劝」模型收敛）**

预算快耗尽时，**注入一条提醒给模型**（「你只剩 X token 了」），让模型**在撞上硬上限之前自己收敛**——而不是被动被掐断。

**要素 3：硬中止**

```rust
if self.runtime.rollout_budget.record_usage(usage)? {
    return Err(CodexErr::SessionBudgetExceeded);   // 预算耗尽 → 硬中止
}
```

**所以 Codex 的完整防失控是「两段式」**：

```
【软提醒】剩余 token 低于阈值 → 注入"你还有 X token" → 模型自己收敛
                    ↓ 如果还不收敛，继续烧
【硬中止】预算彻底耗尽 → SessionBudgetExceeded → 中止
```

**为什么用「成本预算」而非「固定次数」**（和模块 A「为什么不用步数上限」同一道理）：

| 方案 | 问题 |
|---|---|
| 固定次数（回灌最多 3 次） | 一刀切，复杂任务可能需要更多次 |
| **成本预算** | 按真实消耗计，长任务自然用更多；**token/钱才是真正稀缺资源** |

#### 业界通用：混合预算 + 逐步加码

| 预算类型 | 含义 | 例子 |
|---|---|---|
| 次数预算 | 回灌/修复最多 N 次 | 「最多回灌 3 次」 |
| 成本预算 | 最多花 X token/元 | Codex 的做法 |
| 时间预算 | 最多跑 X 秒 | 「单次 turn 最多 5 分钟」 |
| 混合 | 三者取最小 | 「次数≤3 且 token≤10万 且 时间≤5分钟」 |

**回灌次数经验值**：通常 **1~3 次**——超过 3 次还错，说明是 schema 设计问题或超出模型能力，继续烧也没用。

**逐步加码策略**（每次回灌给的提示升级）：

```
第 1 次：回灌「解析失败：缺少必填字段 command」
第 2 次：回灌「正确格式是 {"command": "..."}」（带示例）
第 3 次：放弃 → Fatal → 「多次尝试失败，请检查工具 schema」
```

### 7.8 选型决策树（通用框架）

> ⚠️ **重要声明：Codex 并没有采用这个决策树。** 本节是「**场景多样的系统**」的通用设计框架，用于回答「什么场景该用哪一层」。Codex 的实际做法是**一刀切**（见 7.6 的「Codex 到底怎么做的」）——所有工具统一「回灌重试 + serde default + 宽松解析」，不按场景切换。

#### 为什么 Codex 不需要决策树

因为 Codex 的场景**足够单一**：都是 coding 工具（shell、读文件、打补丁），参数都简单、都能重试、错误代价低。**一刀切最省事。**

而当你的系统**场景多样**（既有「错了能重试」的低代价场景，又有「下游直接消费、错一次就崩」的高代价场景）时，才需要「按场景选层」。

#### 决策树

```
开始：模型要输出某个工具的参数
│
├─ 参数是自由文本（freeform）吗？
│     ├─ 是 →【宽松解析】（第四层），不当 JSON 处理
│     └─ 否（结构化参数）→ 继续 ↓
│
├─ 错误代价高吗？
│   （下游系统直接消费 / 不能重试 / 安全敏感操作）
│     ├─ 高 →【约束输出】（第一层，strict mode），从根源防止
│     └─ 不高 → 继续 ↓
│
├─ 参数结构复杂吗？（字段多、嵌套深、约束多）
│     ├─ 复杂 →【约束输出 + 严格校验】
│     └─ 简单 → 继续 ↓
│
└─ 默认路径：
      【回灌重试】（第三层）+ serde default（可选字段默认值）
      写错了就回灌，缺可选字段就填默认值   ← Codex 的选择
```

#### 三个典型场景

| 场景 | 该用哪层 | 为什么 |
|---|---|---|
| **内部 agent 的 shell 命令** | 回灌重试（Codex 的做法） | 参数简单、能重试、错了没大后果 |
| **抽取 JSON 给下游 API**（如对账单系统） | **约束输出（strict）** | 下游直接消费，错一次就崩，不能靠重试 |
| **单次推理、成本敏感**（批量离线处理） | 约束输出 or 修复 | 不能重试（重试=多花一次钱） |
| **补丁/自由文本参数** | 宽松解析 | 参数根本不是 JSON |

#### 经验法则（一句话）

> **「错误代价」决定「防止 vs 回灌」**：
> - 错误代价高（下游直接消费、不能重试）→ **防止**（约束输出）；
> - 错误代价低（能重试、内部用）→ **回灌重试**（更简单灵活）。
>
> **「参数是不是 JSON」决定「要不要宽松解析」**：
> - 自由文本 → 宽松解析；
> - 结构化 → 才考虑上面那条。

#### 面试价值

面试官问「什么场景该用约束输出、什么场景该用回灌」时，套这个决策树：

> 「看**错误代价**和**能否重试**：错误代价高（下游直接消费、不能重试、安全敏感）就该用**约束输出**从根源防；错误代价低、能重试，用**回灌重试**更简单灵活。如果是 freeform 参数，直接用**宽松解析**。生产级通常是**分层混合**——关键路径用约束输出，普通路径用回灌兜底。」

> ⚠️ 注意区分两个问题：「**Codex 怎么做**」（答：一刀切回灌）vs「**你设计系统怎么权衡**」（答：上面决策树）。别混。

---

## 8. 编排层：orchestrator 核心序列（事后第三步，重点）

> ⚠️ **先澄清一个容易误解的点：不是所有工具都走编排层。**
>
> Codex 的工具分两类：

| 类型 | 特征 | 例子 | 走编排层吗？ |
|---|---|---|---|
| **有副作用**的工具 | 执行命令、写文件、联网 | `exec_command`（shell）、`apply_patch` | ✅ **走**（审批 → 沙箱 → 执行） |
| **纯逻辑**的工具 | 只做内存计算、查状态、返回结果 | `get_context_remaining`、`update_plan`、`tool_search`、`current_time` | ❌ **不走**（直接在 agent 进程内执行） |

**本质区别是「有没有外部副作用」**：

| | 纯逻辑工具 | 有副作用工具 |
|---|---|---|
| 会碰外部系统吗？ | 不会（只算内存/查状态） | 会（执行命令/写文件/联网） |
| 可能伤害外部世界吗？ | 不可能 | 可能（`rm -rf /`） |
| 需要沙箱/审批吗？ | ❌ 不需要 | ✅ 需要 |

**代码层面**：只有实现 `ToolRuntime` 的工具才进 `ToolOrchestrator::run`（审批+沙箱）；纯逻辑工具直接实现 `ToolExecutor::handle()`，不走编排层。

> **沙箱和审批，本质上是为「有外部副作用的操作」准备的**——只有这种操作才可能「伤害外部世界」。纯逻辑工具没这个风险，所以不需要。

---

**定位**：编排层负责「**安全 + 容错**」地执行工具——回答三个问题：

| 问题 | 对应步骤 |
|---|---|
| 执行**安全吗**？ | ① 审批 |
| 在**什么环境**执行？ | ② 选沙箱 |
| 执行**失败了怎么办**？ | ③④ 尝试 + 升级重试 |

源码注释原话：

> *"Central place for approvals + sandbox selection + retry semantics. Drives a simple sequence: **approval → select sandbox → attempt → retry with an escalated sandbox** on denial (no re-approval thanks to caching)."*

完整四步（含三个关键点）：

```
① 审批（approval）          —— 要不要用户/guardian 授权
② 选沙箱（select sandbox）   —— 在什么隔离环境执行
③ 第一次尝试（attempt）      —— 在选定沙箱里执行
④ 若沙箱拒绝【且满足升级条件】→ 升级沙箱重试（escalated retry）
      └─ 通常【不重复审批】（授权的是意图，不是环境）
```

**三个关键点（别漏）**：

1. **「安全 + 容错」两个职责**：审批 + 选沙箱是「安全」，尝试 + 升级重试是「容错」。
2. **升级是「有条件」的**：不是沙箱拒绝就升级，而是一串开关把关（`escalate_on_failure` / `wants_no_sandbox_approval` / `unsandboxed_allowed`，见 8.4）。
3. **升级通常「不重复审批」**：用户授权的是「执行这个命令」的**意图**，不是「在只读沙箱里执行」的**环境**——换环境不换意图，无需重问。

### 8.1 审批（Approval）—— 用户/guardian 是否授权

#### 审批层到底做什么

**一句话**：审批层负责「**把模型的意图翻译成可审查的动作，交给审批者（用户/guardian）裁决**」。

做四件事：

```
① 判断要不要审批     → Skip / Forbidden / NeedsApproval
② 生成审批请求       → ApprovalAction（把"要执行什么"结构化描述）
③ 发给审批者         → 用户（交互）或 guardian（自动审查）
④ 收集结果 + 缓存    → 批准/拒绝，并缓存避免重复审批
```

> **注意：不是「两层审批流程」**。`approval_policy` 是「背景配置」（预先设好的值），`exec_approval_requirement` 是「运行时算一次」。**只有一次最终动作**（Skip/Forbidden/NeedsApproval），没有「先全局审批再单个审批」。

#### exec_approval_requirement 怎么算出来的？（不是智能判断，是规则）

源码调用：

```rust
let requirement = tool.exec_approval_requirement(req)      // ① 工具自己声明
    .unwrap_or_else(|| default_exec_approval_requirement(approval_policy, 沙箱策略));  // ② 默认逻辑
```

**两个来源，二选一**：

| 来源 | 说明 |
|---|---|
| **① 工具自己声明** | 工具实现 `exec_approval_requirement` 方法，**开发工具时就写死了** |
| **② 默认逻辑** | 工具没声明时，用默认规则算 |

**默认逻辑的核心**（源码 `default_exec_approval_requirement`）：

```rust
let needs_approval = match policy {
    Never => false,                        // 完全访问 → 永不审批
    OnRequest | Granular => {
        沙箱是 Restricted（受限）→ true     // 要审批
        沙箱是 Unrestricted（无限制）→ false  // 不审批
    }
    UnlessTrusted => true,                 // 未信任项目 → 都要审批
};
```

**翻译成人话**：

| 审批策略 | 默认要不要审批 |
|---|---|
| `Never`（完全访问） | **从不**审批 |
| `OnRequest`（默认） | **看沙箱**：受限 → 审批；完全放开 → 不审批 |
| `UnlessTrusted` | **都要**审批 |

> **核心规则（OnRequest 模式）**：沙箱「受限」→ 审批；沙箱「完全放开」→ 不审批。这正好解释了「审批」和「沙箱」的关联——默认逻辑里，「要不要审批」由「沙箱是否受限」决定。

> **是提前规划好的，不是运行时智能判断**——就是「工具写死的规则」或「查表 + 规则匹配」。

#### 生成审批请求：ApprovalAction（关键）

当需要审批时，生成一个**结构化的审批请求**，把「要执行什么」描述清楚：

```rust
pub enum ApprovalAction {
    ExecCommand {          // 执行命令
        command: Vec<String>,          // 要执行的命令
        cwd: PathUri,                  // 工作目录
        sandbox_permissions: ...,       // 本次打算用的沙箱（保守初值，非最终）
        justification: Option<String>,  // 模型给的「理由」
    },
    ApplyPatch { files, patch },       // 打补丁（改哪些文件、补丁内容）
    NetworkAccess { host, port },      // 网络访问（访问哪个域名、端口）
    McpToolCall { ... },               // MCP 工具调用
    RequestPermissions { ... },        // 请求权限
}
```

**作用**：把「模型想做的事」翻译成「人能看懂、能审查的动作描述」。

#### 审批者是谁

| 审批者 | 场景 | 说明 |
|---|---|---|
| **用户** | 交互式 | 弹出「要执行 XX 吗？[允许][拒绝]」 |
| **guardian** | 自动审查 | Codex 内置的**安全审查器**，自动检查命令风险（如 `rm -rf /`） |

`strict_auto_review` 模式下，即使结果是 `Skip`，也要 guardian 自动 review 一遍。

#### Python 伪代码总览

```python
def 审批(tool, req, approval_policy, 沙箱策略):
    # ① 判断要不要审批（两个来源二选一）
    if tool.has_custom_approval_rule():
        requirement = tool.exec_approval_requirement(req)   # 工具写死的规则
    else:
        requirement = 默认规则(approval_policy, 沙箱策略)    # 默认逻辑

    if requirement == "Forbidden":
        return 拒绝()

    if requirement == "Skip":
        if strict_auto_review:
            action = tool.approval_action(req)     # ② 生成审批请求
            result = request_approval(action)      # ③ 发给 guardian
            if result == "拒绝": return 拒绝()
        return 批准(缓存)                           # ④ 缓存「已批准」

    if requirement == "NeedsApproval":
        action = tool.approval_action(req)         # ② 生成审批请求
        result = request_approval(action)          # ③ 发给用户
        if result == "拒绝": return 拒绝()
        return 批准(缓存)                           # ④ 缓存「已批准」


def 默认规则(policy, 沙箱策略):
    if policy == "Never":
        return "Skip"               # 从不审批
    if policy == "UnlessTrusted":
        return "NeedsApproval"      # 未信任都要审批
    if policy in ["OnRequest", "Granular"]:
        if 沙箱策略.kind == "Restricted":
            return "NeedsApproval"  # 沙箱受限 → 审批
        else:
            return "Skip"           # 沙箱放开 → 不审批
```

#### 一句话总结

> 审批层 = **判断要不要审批（工具声明 or 默认规则）+ 生成结构化审批请求（ApprovalAction）+ 发给审批者（用户/guardian）+ 收集裁决并缓存**。
>
> **「要不要审批」是规则算出来的，不是智能判断**：工具写死的规则，或默认规则「沙箱受限就审批」。审批请求里带的沙箱权限只是「本次尝试的保守初值」（所以后面有升级重试）。

### 8.2 选沙箱（Select Sandbox）—— 决定「要不要 + 选哪种」类型

#### 先分清「选」和「构建」——两个动作，两个时机

| | 选沙箱（select） | 构建沙箱（build） |
|---|---|---|
| **做什么** | 决定「用哪种类型」 | 实际「搭建」出来 |
| **产物** | 一个枚举值 `SandboxType` | 实际的 bwrap 命令、子进程、挂载、seccomp |
| **重量** | 轻量决策 | 重量操作（fork + 挂载 + 系统调用） |
| **时机** | 编排层 step ② | step ③（执行）**内部** |

**关键**：`select_initial()` 只返回一个 `SandboxType` **枚举值**，**不 fork 子进程、不生成 bwrap 命令、不挂载文件系统**。真正的「构建」发生在 step ③ 执行时（`tool.run()` 内部）。

```rust
pub fn select_initial(...) -> SandboxType {   // ← 只返回【类型枚举】
    if !self.should_sandbox(...) {
        return SandboxType::None;              // 就是"选个类型"
    }
    ...
}
```

#### 8.2 做的事：决定「要不要 + 选哪种」

```python
def select_initial(权限配置, 工具偏好):
    # 判断 1：要不要沙箱？
    if not should_sandbox(权限配置, 工具偏好):
        return SandboxType.None        # 不需要 → 无沙箱

    # 判断 2：需要的话，选哪种类型？
    if 平台 == "linux":
        return SandboxType.LinuxSeccomp
    elif 平台 == "macos":
        return SandboxType.MacosSeatbelt
    ...
```

#### 沙箱类型（`SandboxType` 枚举，5 种）

| 沙箱类型 | 平台 | 底层机制 |
|---|---|---|
| `None` | 全平台 | 无沙箱（完全访问） |
| `MacosSeatbelt` | macOS | Seatbelt（macOS 沙箱配置文件机制） |
| `LinuxSeccomp` | Linux | Seccomp（系统调用过滤）+ **bubblewrap**（命名空间/挂载） |
| `WindowsRestrictedToken` | Windows | 受限令牌（Restricted Token） |
| `WindowsMxc` | Windows | MXC（AppContainer 机制） |

> 注意：虽然枚举里 Linux 叫 `LinuxSeccomp`，实际 Linux 沙箱是 **bubblewrap + seccomp 组合**（seccomp 只是其中一部分）。

#### 一句话总结

> 8.2 节 = **「决定要不要沙箱 + 选哪种类型」**，产物是一个 `SandboxType` 枚举值——**只做决策，不做搭建**。真正的「构建」（bwrap 命令、fork、挂载、seccomp）发生在 step ③ 执行时（见 8.3）。

### 8.3 第一次尝试（First Attempt）—— 在这里「构建」并执行沙箱

#### 真正的「构建」发生在这里

8.2 只选了「类型」（返回 `SandboxType` 枚举），真正的「构建」在 `tool.run()` 内部——把类型 + 权限配置翻译成实际的 bwrap/seccomp 命令，fork 子进程，施加限制。

**① bubblewrap 做文件系统隔离（核心）**：

```bash
bwrap \
  --ro-bind / /              # ① 整个根目录挂载成【只读】
  --bind <工作区> <工作区>      # ② 工作区叠加成【可写】
  --ro-bind <工作区>/.git <工作区>/.git  # ③ 敏感路径（.git）重新设为【只读】
  <要执行的命令>
```

- **默认全只读**：命令默认啥都不能写。
- **只开工作区**：工作区可写。
- **再保护敏感路径**：.git、.codex 等即使在工作区内也保持只读。

**② seccomp 做系统调用过滤**：

```bash
PR_SET_NO_NEW_PRIVS      # 禁止进程提权
+ seccomp 网络过滤器        # 过滤网络相关系统调用
```

**关键：tool 永远在「新子进程」执行，不是「agent 进程内」**——沙箱施加在这个新子进程上（且限制会继承给子进程的子进程）。

#### 完整流程

```
8.2 选好的 SandboxType（如 LinuxSeccomp）
        ↓
tool.run() 内部：
  ① 把 SandboxType + 权限配置 → 翻译成 bwrap 命令参数
  ② fork 子进程（codex-linux-sandbox helper）
  ③ 施加 seccomp 过滤
  ④ 在受限子进程里 exec 命令
        ↓
  命令执行，若沙箱拦截越权操作 → SandboxErr::Denied → 进入 8.4 升级重试
```

### 8.4 沙箱拒绝 → 升级重试（Escalated Retry）

**关键场景**：沙箱拒绝命令（`SandboxErr::Denied`），如模型要写文件但初始沙箱只读。

Codex 的处理：

1. 检查 `tool.escalate_on_failure()` —— 工具是否允许升级重试。
2. 检查 `tool.wants_no_sandbox_approval()` —— 是否允许无沙箱重试。
3. **检查 `should_bypass_approval(approval_policy, already_approved)`** —— 若第一次已审批，跳过重复审批（"no re-approval thanks to caching"）。
4. 构建 `retry_attempt`（升级沙箱或无沙箱），再次执行。
5. 成功 → `"escalated"`；失败 → 报错。

---

## 9. 三个「大厂级」设计思想（面试高分点）

### 9.1 审批和沙箱是「两回事」

| | 审批（Approval） | 沙箱（Sandbox） |
|---|---|---|
| 回答的问题 | 用户**授权**执行吗？ | 用**多强隔离**执行？ |
| 本质 | 授权（authorization） | 执行环境（execution environment） |
| 谁决定 | 用户 / guardian / policy | 权限配置 + 工具声明 |

**它们正交**：命令可以「已授权但要在沙箱跑」，也可「未授权但技术上能无沙箱跑」。Codex 分开处理，而非混成一个布尔值。

### 9.2 授权与执行分离 —— 升级沙箱不重复审批

```
模型要执行 shell("git commit -am 'fix'")  → 需写 .git 目录
  ① 审批：用户点「允许」       ← 授权的是「执行这个命令」的意图
  ② 选沙箱：只读沙箱
  ③ 第一次尝试：沙箱拒绝（要写文件但只读）
  ④ 升级重试：可写沙箱 → 成功   ✅ 不再问用户（意图没变，只是环境变了）
```

- **为什么对**：用户审批的是「执行这个命令」这个**意图**，不是「在只读沙箱里执行」这个**具体环境**。
- **反例**：每次升级都重新弹审批 → 用户反复点允许 → **审批疲劳**，审批形同虚设。

### 9.3 沙箱升级是「有界」的

升级不是「失败就无脑去沙箱重跑」，而是一串开关限制范围：

- `escalate_on_failure()` —— 是否允许升级（只读工具失败就是失败）。
- `wants_no_sandbox_approval()` —— 是否允许「无沙箱」重试（最危险一级）。
- `unsandboxed_allowed` —— 全局是否允许无沙箱。
- `strict_auto_review` 模式：绕过沙箱需**重新 guardian review**（不能靠缓存跳过）。

> 升级是「只读沙箱 → 可写沙箱 →（极端）无沙箱」的**阶梯**，每级都有开关，不能一步跳到最危险的无沙箱。

---

## 10. 工具元数据全景 —— 一个工具远不止 spec

| 元数据 | 作用 | 对应点 |
|---|---|---|
| `spec`（name/desc/parameters） | 告诉模型怎么调 | 描述层 |
| `exec_approval_requirement` | 要不要审批 | 编排① |
| `sandbox_permissions` / `sandbox_preference` | 用什么沙箱 | 编排② |
| `supports_parallel` | 能不能并行 | 模块 A 并行工具 |
| `escalate_on_failure` | 失败是否升级重试 | 编排④ |
| `wants_no_sandbox_approval` | 是否允许无沙箱 | 编排④ |
| `should_bypass_approval` | 升级时能否跳过审批 | 编排④ |
| `network_approval_spec` | 网络访问审批 | 独立网络审批 |

**面试金句**：

> 「工具不只是 name + description + parameters 三件套。生产级工具还要声明**审批策略、沙箱要求、并发特性、失败升级策略、网络权限**等一整套运行时元数据。spec 告诉模型『怎么调』，元数据告诉编排层『怎么管』。」

---

## 11. 面试答题骨架

**Q1：工具调用从模型请求到执行完成，完整生命周期是什么？**
→ 分「事前/事后」两半：**事前** registry 的 spec 序列化进 `tools` 字段喂给模型；**事后**模型输出 function_call → ③**路由层**按 name 查 registry 找 handler（**arguments 仍是字符串**）→ ④**handler 内部解析** arguments + 校验 → ⑤**handler 调编排层**「审批 → 选沙箱 → 执行 → 失败升级重试」→ runtime 执行。registry 是两半共享的中枢。

**Q2：审批和沙箱有什么区别？**
→ 审批回答「用户授权吗」，沙箱回答「用多强隔离执行」。两者正交，Codex 分开处理。

**Q3：为什么沙箱被拒后升级重试，不重新审批？**
→ 用户授权的是「执行这个命令」的意图，不是「在某个沙箱里执行」的环境。意图没变，只换环境，无需重问。反之会导致审批疲劳。

**Q4：工具除了 name/description/parameters 还需要什么？**
→ 运行时元数据：审批要求、沙箱权限、并发特性（supports_parallel）、失败升级策略、网络权限等。spec 给模型看，元数据给编排层看。

**Q5：模型返回的参数为什么需要客户端解析和校验？**
→ LLM 只能输出文本，arguments 是「恰好符合 JSON 语法的字符串」，不是对象。解析、校验（必填、类型）是客户端责任；失败就回灌错误让模型重试。

**Q6：工具数量太多（100+）怎么办？**
→ 两个问题：**选错** + **占 token**。核心解法是 **Tool Retrieval（工具检索）**：工具按 `ToolExposure` 分流——`Direct` 进初始 **`tools` 字段**，`Deferred` 只把元数据索引进 **BM25** 引擎。初始只给「Direct 工具 + `tool_search`」；模型调 `tool_search` 检索出 top-8 相关工具，其完整定义作为 **`tool_search_output` 条目进入 `input` 数组**（下一轮生效，两轮过程）。用 BM25 而非向量检索，因为工具描述关键词明确、且零 embedding 成本。工程兜底还有命名空间分组、按模式过滤、子 agent 委派、code mode、约束解码。

---

## 12. 动手练习（Python）

写一个简化版工具编排器（~100 行）：

1. 建 registry：定义 3 个工具（shell、read_file、write_file），每个带 spec + handler + 审批要求 + 沙箱要求。
2. 实现「描述层」：遍历 registry，把 spec 序列化进 tools 字段。
3. 实现「解析 + 路由」：parse arguments 字符串 → dict，按 name 查 registry 找 handler。
4. 实现 `orchestrate(tool_call)`：审批 → 选沙箱 → 执行 → 若沙箱拒绝且 `escalate_on_failure` 为真 → 升级沙箱重试（不重复审批）。
5. 重点验证：写文件命令在只读沙箱被拒后，升级到可写沙箱成功，且**只审批一次**。
