# 模块 0（前置） · OpenAI API 调用与消息结构

> 源码依据：`codex-rs/protocol/src/models.rs`（`ResponseItem` 枚举）、`core/src/client.rs`（`build_responses_request`）、`core/src/client_common.rs`（`Prompt` 结构）。
> 学习目标：搞清「用户提问、模型回答、工具调用、工具输出」分别放在 API 请求的哪个位置。
> 定位：这是模块 A（Agent 主循环）的**前置知识**，也是模块 B（工具编排）的基础。

---

## 1. 两种 API 风格

| | Chat Completions（老） | **Responses API（新，Codex 用）** |
|---|---|---|
| 对话历史 | `messages: [...]` | `input: [...]` |
| 系统指令 | 一条 `role: "system"` 的消息 | **顶层 `instructions` 字段** |
| 工具定义 | 顶层 `tools` | 顶层 `tools` |
| 工具结果 | `role: "tool"` 的消息 | `type: "function_call_output"` 的 item |
| 思考过程 | 不单独存（或隐藏字段） | `type: "reasoning"` 的 item |

**Codex 用的是 Responses API。**

---

## 2. 请求的真实结构（三个顶层字段）

Codex 的 `build_responses_request` 就是把内容塞进这三个字段：

```json
{
  "model": "gpt-5",

  "instructions": "You are Codex, a coding agent...",   // ← 系统指令

  "tools": [                                            // ← 工具定义
    {"type": "function", "name": "shell", "description": "...", "parameters": {...}}
  ],

  "input": [                                            // ← 所有对话内容
    {"type": "message", "role": "user",
     "content": [{"type": "input_text", "text": "帮我修复这个 bug"}]},

    {"type": "reasoning", "summary": [{"type": "summary_text", "text": "我需要先看代码..."}]},

    {"type": "function_call", "name": "shell",
     "arguments": "{\"command\":\"ls\"}", "call_id": "call_abc"},

    {"type": "function_call_output", "call_id": "call_abc",
     "output": "main.py\nREADME.md"},

    {"type": "message", "role": "assistant",
     "content": [{"type": "output_text", "text": "我看到项目里有这些文件..."}]}
  ]
}
```

---

## 3. 「谁放在哪」完整对照表

| 内容 | 放哪个字段 | 具体的 item 类型（`ResponseItem`） |
|---|---|---|
| **系统指令**（角色设定、规则） | 顶层 `instructions` | —（单独字段） |
| **工具定义**（有哪些工具可用） | 顶层 `tools` | —（单独字段） |
| **用户提问** | `input` 数组 | `Message { role: "user" }` |
| **模型回答** | `input` 数组 | `Message { role: "assistant" }` |
| **模型思考过程** | `input` 数组 | `Reasoning { summary, content }` |
| **模型请求调工具** | `input` 数组 | `FunctionCall { name, arguments, call_id }` |
| **工具执行结果** | `input` 数组 | `FunctionCallOutput { call_id, output }` |
| **子 agent 间消息** | `input` 数组 | `AgentMessage { author, recipient }` |

> 消息内容（`content`）本身也是结构化的：`input_text` / `input_image` / `input_audio` / `output_text`。

---

## 3.5 `input` 数组的顺序规则（重要）

**是的，`input` 是按时间顺序排列的对话记录（transcript），模型从上往下读。** 但有 5 个细节：

### ① 严格说是「事件顺序」，不是「发言顺序」

一轮里 item 远不止「你一句我一句」，而是每个事件按发生顺序平铺：

```
user: 帮我修复这个 bug
reasoning: 我需要先看代码...
function_call: shell("ls")            ← 模型要调工具
function_call_output: "main.py..."    ← 工具结果
reasoning: 我先读 main.py
function_call: read_file("main.py")
function_call_output: "def foo()..."
message(assistant): 我发现了问题...
```

Codex 每来一个事件就往数组**末尾 push**（`history.rs` 的 `record_items`），所以数组**只增不减、单调增长**（直到压缩）。

### ② 第一条**不**一定是用户提问

用户提问**前面**还垫着系统注入的「初始上下文」item（`build_initial_context_with_world_state`）：

```
[developer 指令]                    ← 注入
[环境上下文：OS / 工作目录 / git 状态]  ← 注入
[用户指令：AGENTS.md 项目规范]        ← 注入
[用户提问：帮我修复这个 bug]          ← 用户第一条
[模型回答 ...]
```

### ③ 最后一条**也**不一定是用户提问

正确的心智模型是：**最后一个 item = 最近发生的那件事**，用户提问只是其中一种。

- **一轮的第一次请求**：用户消息排得很靠后，但 Codex 通常紧接着**再追加几条上下文提醒**（当前时间、世界状态变化、token 预算，都在 `clone_history()` 之前记录）：

  ```
  [用户提问：帮我修复这个 bug]
  [当前时间提醒]
  [世界状态：分支 dev、有未提交改动]
  ```

  所以最后一个可能不是用户原话。这是故意设计——让模型感知「时间过了、环境变了」。

- **一轮中途**：最后一个是**最新的工具输出**，用户消息早被挤到中间。
- **压缩之后**：最后一个是**压缩摘要**。

### ④ 压缩会打破纯时间顺序

压缩后历史被重建为：`[初始上下文] + [保留的用户消息] + [摘要（刻意放最后）]`。摘要**不按时间顺序**，而是被挪到末尾。

> **为什么位置这么讲究**：模型对上下文**开头和结尾**注意力最强，中间易「迷失（lost in the middle）」。所以压缩摘要被刻意放最后（最重要的交接信息享受最强注意力），用户最新诉求也靠后。**「重要信息放头尾」是 prompt 工程的一条实战原则。**

### ⑤ 最该记住的认知

**模型没有记忆，它每一轮只看到这一个数组。数组的顺序 = 它全部的时间感。**

- 数组里有的，它才知道；没的，它完全不知道发生过 → 这就是「回灌」必须真的 append 的原因。
- 也是为什么压缩不能乱丢信息——丢掉的模型就永远忘了。

---

## 4. 核心机关：`call_id` 配对

- **`FunctionCall`** 带 `call_id`（如 `"call_abc"`）—— 模型说「我要调工具」。
- **`FunctionCallOutput`** 带**同一个** `call_id` —— 工具跑完，结果回填。

模型靠 `call_id` 知道「结果对应哪次调用」。一次请求里并行请求多个工具时，就靠多个 `call_id` 区分——**这是并行工具调用成立的基础**。

### `call_id` 是谁生成的？

**正常情况：模型（服务端）生成。** 证据：`FunctionCall.call_id` 是**必填的 `String`**（不是 `Option`），它随 API 响应一起返回。

- 发起调用的是模型 → 由模型分配 ID。
- 客户端职责：**原样保存、原样回传**。换 ID 模型就认不出对应关系。
- 这也是「必须存整个 response item」的又一个原因。

**为什么不由客户端自增序号？**
1. 由发起方分配 ID，回复方照抄，协议最不易错。
2. 一次响应可能并行发起多个调用，需互不相同的 ID。
3. 跨往返必须保持稳定（中间隔着工具执行、可能重试）。
4. 模型内部也靠这个 ID 配对。

**例外：非模型发起的动作，Codex 自己造 ID：**

| 场景 | 生成的 ID | 源码 |
|---|---|---|
| 用户直接敲 shell 命令（`!command`） | `Uuid::new_v4()` | `tasks/user_shell.rs:182` |
| 内部安装 MCP 依赖 | `"mcp-deps-{sub_id}"` | `mcp_skill_dependencies.rs:303` |
| 网络审批流程 | `"{guardian_approval_id}#{uuid}"` | `network_approval.rs:774` |

> **洞察**：transcript 格式是**统一**的——不管动作来自模型还是用户，都用同一套「调用 + 结果」（call_id 配对）结构。**统一的数据结构换来处理逻辑的简单。**

---

## 4.5 为什么 `reasoning`（推理过程）也要放进 `input`？

**关键认知：reasoning 不是「给模型读的文字」，而是「模型的状态」。**

### `reasoning` item 的三个字段

```rust
Reasoning {
    summary: Vec<...>,                 // ① 人类可读摘要（给 UI 看的）
    content: Option<Vec<...>>,         // ② 明文推理文本（会被剥掉！）
    encrypted_content: Option<String>, // ③ 加密推理状态（真正传回去的）
}
```

### 证据一：Codex 剥掉明文推理

`models.rs` 的 `should_serialize_reasoning_content`：只要推理里有明文 `ReasoningText`，该字段就**不序列化**。

→ **传回 API 的不是模型读得懂的推理文字。**

### 证据二：Codex 索取加密推理

`client.rs:963`：
```rust
let include = vec!["reasoning.encrypted_content".to_string()];
```

→ **明文扔掉，加密保留并传回。**

### 为什么必须传回：推理模型是「无状态」的

- 推理模型（o 系列、GPT-5）在服务端做长链式思考。
- 为支持「无状态调用」，它把思考状态**加密成 blob 交给客户端保管**。
- **下一轮必须原样传回**，模型才能接着上次的思路继续想。
- 不传 → 模型从零重推 → 忘了「当初为什么调这个工具」→ 多轮工具调用明显变笨。

### 类比：草稿纸

`encrypted_content` = 模型的**草稿纸**。草稿内容不给看（隐私），只给摘要；但**草稿纸必须留着**，否则下一轮要把整道题重做。

### 内容 vs 状态

| 类别 | 例子 | 作用 |
|---|---|---|
| **语义内容** | 用户消息、工具输出、assistant 回答 | 给模型「读」 |
| **续算状态** | encrypted reasoning | 让模型「接着想」 |

### 实用推论

**「把历史拍平成字符串再拼进 prompt」会掉性能**——丢掉了 encrypted reasoning，推理模型退化成普通模型。**现代 agent 必须原样保存 response items，而不能只提取 text。** 这就是 Codex 存整个 `ResponseItem` 数组的深层原因。

---

## 5. 与 Agent 主循环的关系

**「回灌」= 往 `input` 数组 append 一个 `function_call_output`，再把整个 `input` 重新发一次。**

```python
input_items.append({                  # ← 「回灌」就这一行
    "type": "function_call_output",
    "call_id": tc.call_id,
    "output": result,
})
resp = client.responses.create(input=input_items, ...)   # 整个数组再发一次
```

这也解释了模块 A 说的「每轮从历史重建 prompt」：**历史就是这个 `input` 数组**，发请求就是把它整个发出去。

---

## 6. Python 可运行示例

```python
from openai import OpenAI
client = OpenAI()

input_items = [{"role": "user", "content": "现在几点了？用工具查一下"}]

tools = [{
    "type": "function",
    "name": "get_time",
    "description": "获取当前时间",
    "parameters": {"type": "object", "properties": {}, "required": []},
}]

while True:                                    # ← Agent 主循环
    resp = client.responses.create(
        model="gpt-5",
        instructions="你是一个助手",            # ← 系统指令
        tools=tools,                           # ← 工具定义
        input=input_items,                     # ← 历史
    )

    input_items += [i.model_dump() for i in resp.output]   # 模型输出进历史

    calls = [i for i in resp.output if i.type == "function_call"]
    if not calls:                              # 无工具调用 → 结束
        print(resp.output_text)
        break

    for tc in calls:                           # 有工具调用 → 执行 + 回灌
        result = execute(tc.name, tc.arguments)
        input_items.append({
            "type": "function_call_output",
            "call_id": tc.call_id,             # ← call_id 配对
            "output": result,
        })
```

**这 20 行就是模块 A 伪代码的真实可运行版本。** Agent loop 剥开看，就是「不断往一个数组追加 item，再整个发出去」。

---

## 6.5 核心原则总结：谁发起、谁保管

### 底层原则：模型是无状态的，客户端是「忠实搬运工」

> **模型没有记忆。凡是模型「自己产出过」的东西，都必须由客户端保管好、下一轮送回来。** 服务端不留档——你不还给它，它就当没发生过。

### 完整的「谁发起」清单

`input` 数组 = **两边产出物的并集**，按时间顺序排：

| item | 谁发起 | 客户端做什么 |
|---|---|---|
| `message` (user) | 用户 | 追加 |
| `message` (assistant) | **模型** | 保管 + 回传 |
| `reasoning` | **模型** | 保管 + 回传（**剥掉明文**，只留 summary + encrypted） |
| `function_call` | **模型** | 保管 + 回传（原样） |
| `function_call_output` | **客户端**（工具结果） | 追加，用 `call_id` 对齐 |
| 注入的上下文（环境/时间/世界状态） | **客户端** | 追加 |

**规律**：模型只「产出」，客户端负责「记账 + 搬运」。整个 agent 的历史管理，本质是客户端在替无状态的模型维护记忆。

### 注意：「原样回传」有例外（归一化过滤）

回传前客户端会做过滤，并非字面意义的「原样」：

- **剥明文推理**（只留 summary + encrypted）
- **剥不支持的模态**（模型不支持图片就剥掉）
- **清内部元数据**（`clear_internal_chat_message_metadata_passthrough`）
- **截断过长的工具输出**

---

## 7. 面试答题骨架

**Q：Agent 的对话上下文是怎么组织的？**
→ 用 Responses API：系统指令放顶层 `instructions`，工具定义放顶层 `tools`，所有对话内容（用户消息、模型回答、思考、工具调用与输出）按时间顺序放在 `input` 数组里，每一轮整个数组重新发送。

**Q：工具调用和结果怎么对应？**
→ 靠 `call_id`。`function_call` 带 call_id，`function_call_output` 带同一 call_id；并行多工具时靠多个 call_id 区分。
