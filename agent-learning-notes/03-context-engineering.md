# 模块 C · 上下文工程与压缩（compaction）

> 源码依据：`codex-rs/core/src/compact.rs`、`context_manager/history.rs`、`context_manager/normalize.rs`、`prompts/templates/compact/prompt.md`。
> 学习目标：能讲清「上下文超限后压缩怎么工作」，以及「为什么不是简单截断」。

---

## 1. 先厘清一个常见误解

**误解**：每一轮 prompt 包含「从开始到现在的所有原始对话」。
**正解**：每一轮 prompt 包含的是**当前历史**，而当前历史**已经被压缩过**——早期的原始对话早已被摘要替换。

```
原始对话（无限增长，永不完整保留）
        │
        ▼  超出 token 预算 → compaction 压缩
当前历史（有界 = 初始上下文 + 保留的用户消息 + 摘要）
        │
        ▼  for_prompt + normalize（按模态过滤、补齐配对）
真正发给模型的 prompt（每轮从这里重建）
```

---

## 2. 核心结论：压缩不是「一起压」，而是分三类处理

Codex 的关键函数一句话给出答案：

```rust
build_compacted_history(initial_context, user_messages, summary_text)
// 压缩后的历史 = 初始上下文 + 保留的用户消息 + 摘要
```

| 历史内容 | 处理方式 | 设计原因 |
|---|---|---|
| **① 初始上下文**（系统/开发者指令、世界状态） | **重新注入，完全不压** | 每轮都必须有的固定指令，压缩无意义；压缩后应重新拼入 |
| **② 用户消息**（用户提问） | **保留原文**，上限 2 万 token；超出则截断，更早的丢弃 | 用户诉求最不能丢，宁可留原文也不让模型转述 |
| **③ 助手回答 + 工具调用/输出** | **交给模型总结成摘要** | 量最大、细节最可丢，最适合压缩 |

---

## 3. 压缩的执行步骤（源码级）

1. **收集用户消息**：`collect_annotated_user_messages(items)`
2. **保留最近 2 万 token 的用户消息**：
   ```rust
   const COMPACT_USER_MESSAGE_MAX_TOKENS: usize = 20_000;
   ```
   `build_compacted_history_with_limit` 从**最新往回**遍历（`.rev()`）保留——越新的越优先。
3. **超出预算的用户消息 → 截断**（注意：是截断，不是摘要）：
   ```rust
   truncate_text(&message.message, TruncationPolicy::Tokens(remaining))
   ```
4. **其余内容 → 调模型生成摘要**，prompt 为 `SUMMARIZATION_PROMPT`：
   > You are performing a CONTEXT CHECKPOINT COMPACTION.
   > Create a handoff summary for another LLM that will resume the task.
   > Include: 当前进度与关键决策 / 重要约束与用户偏好 / 待办事项 / 关键数据引用 ……
5. **摘要 push 到历史最后**：
   ```rust
   history.push(CompactionSummary::new(summary_text))
   ```
6. **初始上下文重新注入**：`insert_initial_context_before_last_real_user_or_summary(...)`

---

## 4. 两个反直觉但很加分的点

### 4.1 压缩本身是一次「模型调用」

不是程序规则裁剪，而是**让模型读旧历史、写一份「交接文档」（handoff summary）给后续的自己**。所以压缩有耗时和成本，不是免费的。

### 4.2 摘要被刻意放在历史末尾（位置即设计）

源码注释 `InitialContextInjection::BeforeLastUserMessage` 明确说：

> mid-turn 压缩后，模型被训练成把**压缩摘要看作历史中的最后一项**。

所以初始上下文被插到「最后一条真实用户消息**之前**」，就是为了保住「摘要永远在最末」这个位置契约。**prompt 里内容的位置本身就是设计的一部分**——高级细节。

---

## 5. 压缩触发时机

- **Pre-turn 压缩**：一轮开始前，若预计输入会超阈值，提前压缩（`run_pre_sampling_compact`）。
- **Mid-turn 压缩**：一轮进行中，token 达到上限，压缩后**继续**当前轮（不是结束）。
- **手动压缩**：用户主动触发。

> 注意：token 超限**不是** turn 的终止条件，而是触发压缩后继续（区别于模块 A 的停止条件表）。

---

## 6. 面试答题骨架

**Q：上下文窗口满了怎么办？**
→ 触发 compaction：把历史拆三类——初始上下文重新注入、用户消息保留原文（近 2 万 token）、其余交给模型总结成摘要；摘要放历史末尾。**不是简单截断。**

**Q：为什么用「模型摘要」而不是「直接截断」？**
→ 截断会丢掉任务进度、关键约束、用户偏好等「语义信息」；摘要能保留任务的「意图和状态」。代价是压缩本身要一次模型调用（耗时 + 成本）。

**Q：哪些必须保留、哪些可丢？**
→ 用户诉求（原文保留）、系统指令（重新注入）必须保；工具输出的细节、中间推理过程可摘要可丢。

---

## 7. 动手练习（Python）

1. 写一个 `compact(history)` 函数，模拟三类处理：
   - 系统指令原样保留
   - 取最近 N 条用户消息原文保留
   - 其余拼成字符串，调模型生成摘要
2. 验证压缩后 token 数显著下降，且用户最近的诉求仍在。

---

## 8. 待补（本模块尚未讲完）

- 压缩的**触发阈值**如何计算（token 预算、context window 剩余）。
- 多轮压缩的**嵌套**问题：摘要还能再被摘要吗？会不会信息层层丢失？
- `world_state` / `user_goal` 这些「世界状态」如何跨压缩保持连续。
