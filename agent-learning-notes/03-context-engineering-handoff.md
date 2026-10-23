# 模块 C 学习交接文档（Handoff）

> 用途：在新的学习计划中继续学「模块 C · 上下文工程与压缩」。
> 已学内容见 [03-context-engineering.md](03-context-engineering.md)（本文件只做精简交接，不重复细节）。

---

## 一、模块 C 是什么

**核心思想**：模型的能力上限由「喂给它的上下文」决定；上下文又是最大成本，所以要「**拼装 + 压缩**」两手抓。

**两大主题**：

1. **拼装（context assembly）**：每轮 prompt 由哪些片段拼成（系统提示、用户目标、环境、记忆、多 agent 指令……）。
2. **压缩（compaction）**：上下文超限时，用模型把历史压缩成摘要，而非简单截断。

---

## 二、已学内容（承接之前的讨论，不重复展开）

以下已在 [03-context-engineering.md](03-context-engineering.md) 中掌握：

1. **上下文是「有界的历史」**——不是「从开始到现在的原始对话」，而是「压缩后的历史」。
2. **压缩分三类处理**（`build_compacted_history` 一句话暴露答案）：
   - **初始上下文**（系统/开发者指令、世界状态）→ **重新注入，完全不压**；
   - **用户消息** → **保留原文**（上限 2 万 token，超了截断）；
   - **助手回答 + 工具调用/输出** → **交给模型总结成摘要**。
3. **摘要被刻意放历史末尾**（位置即设计）。
4. **压缩本身是一次模型调用**（有耗时成本）。
5. **触发时机**：pre-turn（轮前）、mid-turn（轮中）、手动。

---

## 三、待学内容（新计划的重点）

这是当前**尚未讲透**的三点，是新计划的主攻方向：

### 待补 1：压缩的触发阈值怎么算

- token 预算、context window 剩余如何计算。
- 什么时候「触发」压缩（阈值、buffer、prefill token 等）。
- 关键源码：`context_window.rs`、`token_budget.rs`、`context_window_token_status`。

### 待补 2：多轮压缩的嵌套问题

- **摘要还能再被摘要吗？** 会不会信息层层丢失？
- 摘要的摘要如何处理（信息熵、保真度）。
- 关键源码：`compact.rs` 的多次压缩路径、`CompactionSummary`。

### 待补 3：world_state / user_goal 跨压缩保持连续

- 「世界状态」（环境、git 状态等）和「用户目标」如何跨压缩保持连续。
- 为什么它们**不参与压缩**（被单独保存、重新注入）。
- 关键源码：`context/world_state/`、`context/user_goal.rs`、`build_initial_context_with_world_state`。

---

## 四、源码地图

| 主题 | 关键文件 |
|---|---|
| 压缩核心 | `codex-rs/core/src/compact.rs` |
| 上下文拼装（片段） | `codex-rs/core/src/context/`（几十个 context fragment 文件） |
| 历史管理 | `codex-rs/core/src/context_manager/history.rs` |
| 历史归一化 | `codex-rs/core/src/context_manager/normalize.rs` |
| token 预算/窗口 | `codex-rs/core/src/session/context_window.rs`、`token_budget.rs` |
| 世界状态 | `codex-rs/core/src/context/world_state/` |
| 摘要 prompt | `codex-rs/prompts/templates/compact/prompt.md` |

---

## 五、面试考点（模块 C 的核心题）

1. **上下文窗口满了怎么办？**
   → 触发 compaction：三类处理（初始上下文重新注入、用户消息保留原文、其余摘要），摘要放末尾。不是简单截断。

2. **为什么用「模型摘要」而不是「直接截断」？**
   → 截断丢语义信息（任务进度、约束、偏好）；摘要保留意图和状态。代价是压缩本身一次模型调用。

3. **token 预算怎么算？**（待补 1）
   → 待学。

4. **哪些必须保留、哪些可丢？**
   → 用户诉求（原文）、系统指令（重新注入）必须保；工具输出细节、中间推理可摘要可丢。

---

## 六、前置知识依赖

模块 C 依赖以下已学内容：

- **模块 0（LLM API）**：input 数组、token、上下文窗口。
- **模块 A（主循环）**：回灌、停止条件（token 超限→压缩后继续，不是终止）。
- **模块 B（工具）**：工具输出回灌进历史（历史是唯一真相源）。

---

## 七、学习建议

- 按「待补 1 → 待补 2 → 待补 3」顺序推进（先搞清楚「何时触发」，再「多轮压缩」，再「世界状态」）。
- 每学完一点，更新 [03-context-engineering.md](03-context-engineering.md) 的「待补」节。
- 学习方式：Python 视角讲解 + 源码证据 + 面试题收口（沿用之前的风格）。
