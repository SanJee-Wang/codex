# 模块 A · Agent 主循环

> 源码依据：`codex-rs/core/src/session/turn.rs`（`run_turn`、`run_sampling_request`）、`client.rs`（模型调用）。
> 学习目标：能画出一张 Agent 主循环时序图 + 写出伪代码，并能讲清「回灌」「停止条件」「流式」三个关键点。

---

## 1. 一句话本质

**Agent 不是一个「被调用一次」的函数，而是一个「转起来直到做完」的循环。**

感知 → 决策 → 行动 → 观察 → 再决策 …… 直到模型说「做完了」。

Codex 源码里对主循环的官方定义（`turn.rs` 第 149–162 行注释，原意翻译）：

> 接收一轮输入，然后进入一个循环。每次「采样」（调模型），模型只会回复两种东西之一：
> - **请求函数调用**（要调工具）
> - **一条 assistant 文字消息**
>
> - 如果模型请求函数调用 → 执行它，把输出在**下一次采样**里发回给模型。
> - 如果模型只回了文字 → 把文字记进对话历史，**这一轮（turn）结束**。

---

## 2. 主循环伪代码（Python 视角）

```python
async def run_turn(user_input: str) -> str:
    history = load_history()          # 对话历史是「唯一真相源」
    last_message = ""

    while True:
        # ① 拼 prompt：每轮从历史重新构建，而不是增量拼接
        prompt = build_prompt(history, tools, system_instructions)

        needs_follow_up = False
        # ② 调模型（流式），边收边处理
        async for event in stream_model(prompt):
            if event.is_tool_call:                    # 模型要调工具
                output = await execute_tool(event)    # ③ 执行工具
                history.append(tool_output(output))   # ④ 结果回灌进历史
                needs_follow_up = True
            elif event.is_assistant_text:             # 模型给了文字回复
                last_message = event.text
                history.append(event.text)

        # ⑤ 决定是否再来一轮
        if needs_follow_up:
            continue        # 有工具结果要喂回去 → 再问一次模型
        else:
            break           # 只有文字 → 这一轮完成

    return last_message
```

**这就是所有 Agent 的共同骨架。** 无论 Codex、LangChain、还是你面试时手写，本质都是这 5 步。

---

## 3. 三个关键设计决策（Codex 的真实实现）

### 3.1 一次任务 = 多次「采样」（sampling）

- Codex 把「调一次模型」叫 **sampling request**。
- `run_turn` 的外层 `loop`：每次迭代 = 一次采样 + 一次工具执行。
- 模型返回「工具调用」→ 执行 → 回灌 → **再采样**；返回「纯文字」→ turn 结束。

这就是为什么一个看似简单的问题，Agent 可能调了 5 次模型、执行了 3 个工具。

### 3.2 回灌 ≠ 重发，而是「改历史」

这是最容易讲出深度的点。Codex 的实现：

- 每轮循环开始前，执行 `clone_history().for_prompt()` —— **从对话历史重新构建 prompt**。
- 工具的输出不是「临时塞给模型」，而是作为一条新消息 **append 进历史**，下一轮采样自动带上。

**核心思想：对话历史是唯一真相源（single source of truth），prompt 每轮从历史重建。** 这样做的好处：

- 模型看到的永远是「完整的、按时间排序的对话」。
- 历史天然支持持久化、恢复、压缩（后面模块会讲）。
- 不会出现「prompt 状态和真实历史不一致」的 bug。

### 3.3 停止条件（面试高频！）

Codex 里一轮 turn **真正终止**只有 5 种：

| # | 终止条件 | 机制 | 结果 |
|---|---|---|---|
| ① | **模型自己判定完成** | `needs_follow_up == false`（不再要工具 **且** 无待处理输入） | 正常结束（唯一「正常」出口） |
| ② | **成本/rollout 预算耗尽** | `SessionBudgetExceeded` | 报错中止 |
| ③ | **用户彻底取消** | `cancellation_token.cancel()` → TurnAborted | 中止 |
| ④ | **流中断** | 流在 `response.completed` 之前关闭 | 报错 |
| ⑤ | **重试耗尽** | 超过 `max_retries`（默认 5，降级 HTTP 后清零再来一轮） | 报错 |

面试官问「循环怎么停下来」，要一口气说出这 5 种，尤其强调：**「正常结束」是模型自己决定的，不是代码写死「最多循环 N 次」。**

**关键：Codex 没有「步数上限」。** 它用「资源预算」（token/成本）代替「步数预算」——任务复杂度差异巨大，一刀切的步数会误杀长任务；token/成本才是真正稀缺的资源。而且有 compaction「续命」，token 超限不立刻终止，所以真正兜底的是**成本预算**（会最终耗尽）而非步数。

**两个「看起来像终止、其实不是」的陷阱：**

| 陷阱 | 实际行为 |
|---|---|
| **上下文超限** | **不终止**，触发 compaction 压缩后继续（模块 C 讲） |
| **暂停（Esc）** | **不终止**，打断当前生成但 turn 存活待命（见 3.5 节） |

### 3.4 「继续循环」的真实条件（易错点）

很多人以为「循环 = 模型要调工具就继续、不调就结束」。**这只对了一半。** 源码：

```rust
let needs_follow_up = model_needs_follow_up || has_pending_input;
```

注意 **`|| has_pending_input`**：即使模型没调工具，只要**用户在模型运行期间插入了新消息**，循环仍会继续。

```
外层 turn 循环再迭代一次 = 模型要调工具  OR  有待处理用户输入
turn 结束                = 模型没要工具  AND  没有待处理输入
```

> ⚠️ **警惕「loop」一词的歧义**：这里说的「继续循环」指的是**外层 turn 循环**，不是「当前这次采样请求」。用户插话时，被终止的是**当前采样请求**，而 turn 本身继续。见下节。

### 3.5 用户中途插话：终止的是「采样请求」，不是「turn」

**问：如果 http-a 正在流式返回，此时用户插入了新输入，是不是应该直接中断 http-a、发新的 http-b？**
**答：对，Codex 就是这么做的，这个机制叫 preempt（抢占）。**

**但要分清两个层级**，否则会以为「继续循环」和「中断请求」互相矛盾：

```
run_turn（外层循环 = 一个 turn 的生命周期）
│
├─ 迭代 1:
│    └─ 采样请求 http-a ──流式返回中──┐
│                                    │
│                       用户插入新输入 │
│                                    ▼
│                    抢占 → 取消 http-a     ← 【终止的是这一层：采样请求】
│                    返回 needs_follow_up=true
│    ▲
│    └──── 外层 continue                    ← 【继续的是这一层：turn】
│
├─ 迭代 2:
│    └─ 采样请求 http-b（带新输入）
│
└─ … 直到 needs_follow_up == false → turn 结束
```

**关键**：http-b **不是新 turn**，而是**同一个 turn 内的新一次采样请求**。`turn_context` / `active_turn` 全程不变。

| 说法 | 精确表述 |
|---|---|
| 「继续循环」 | **外层 turn 循环**再迭代一次 |
| 「中断当前 loop」 | 终止**当前这次采样请求** |

两者不矛盾：**掐掉当前采样请求，正是让 turn 继续下去的实现方式。**

#### 正式术语：steering（转向）

源码里叫 `PersistContext::SteeredUserInput`——用户的新输入不是「开新会话」，而是「给正在进行的 turn 掰方向」。

#### 两个时间窗口，两套机制

| 新输入到达时机 | 机制 | 行为 |
|---|---|---|
| **流还在飞的时候** | **preempt 抢占** | 取消**采样请求** http-a，立刻发 http-b |
| **流已结束后**（轮次之间） | 外层 `has_pending_input` 检查 | 无请求可取消，直接下一轮带上 |

所以 `needs_follow_up = ... || has_pending_input` 是**第二个窗口的兜底**；抢占是**第一个窗口的处理**。二者互补。

#### 证据

`input_queue.rs` 的 `watch_user_input`：一个后台任务盯着输入队列，一旦发现用户输入就 `interrupt.cancel()` 取消在飞请求。

`turn.rs`：
```rust
.or_cancel(&preempt)          // 当前采样请求被抢占
...
if preempt.is_cancelled() {
    return Ok((SamplingRequestResult { needs_follow_up: true, ... }, ...));
}
```
被抢占 → `needs_follow_up = true` → 外层循环 continue → 发新请求。

#### 为什么值得抢占

抢占会丢掉已生成的部分（白花 token），但权衡是：**用户改主意时，继续把旧答案生成完，比丢掉半截更浪费。**

#### 精妙细节：只有「真正的用户消息」才抢占

`watch_user_input` 判断的是 `has_user_input()`——**系统注入**（世界状态、时间提醒、token 提醒）**不会打断模型**。否则后台每隔几秒追加一条提醒，模型就会疯狂被打断。

> 特例：其他 agent 的 mailbox 消息在某些情况下也会触发抢占（`mailbox_preemption`），因为「别的 agent 在跟你说话」优先级高于普通系统注入。

> 这也解释了为什么需要**连接预热（prewarm）**：抢占意味着随时可能有新请求，保持连接温热能省掉握手。

### 3.6 其实是「两层循环」（重点，容易绕）

**最容易混的点：这两个循环不在同一个函数里。** 内层循环在一个被调用的独立函数里：

```rust
async fn run_turn(...) {              // ← 函数 1
    loop {                            // ★ 外层循环
        ...
        run_sampling_request(...).await   // ← 调用函数 2 = 一次模型调用
        ...
    }
}

async fn run_sampling_request(...) {  // ← 函数 2（独立！）
    loop {                            // ★ 内层循环
        ...
        stream.next().await           // 一个事件一个事件地读
        ...
    }
}
```

**两个循环各自回答的问题：**

| | 谁 | 问的问题 |
|---|---|---|
| **外层循环** | `run_turn` | 「**还要不要再问模型一次？**」 |
| **内层循环** | `run_sampling_request` | 「**这一次模型调用，响应读完了吗？**」 |

#### 具体走查（「帮我修复 main.py 的 bug」）

```
run_turn 开始
│
├─【外层 第 1 次迭代】
│   调用 run_sampling_request ────► HTTP 请求 #1
│                                    └─【内层】读流事件：
│                                       事件a: "我先看看文件"
│                                       事件b: 要调 read_file → 执行
│                                       事件c: 响应结束 ✓
│                                   内层返回：要调工具 = 是
│   外层判断：调了工具 → 继续
│
├─【外层 第 2 次迭代】
│   调用 run_sampling_request ────► HTTP 请求 #2（历史已有文件内容）
│                                    └─【内层】读流事件：
│                                       事件a: 要调 edit_file → 执行（修好了）
│                                       事件b: 响应结束 ✓
│                                   内层返回：要调工具 = 是
│   外层判断：继续
│
├─【外层 第 3 次迭代】
│   调用 run_sampling_request ────► HTTP 请求 #3
│                                    └─【内层】读流事件：
│                                       事件a: "已修复，问题是……"
│                                       事件b: 响应结束 ✓
│                                   内层返回：要调工具 = 否
│   外层判断：没调工具 → 结束
│
└─ 返回最终回答
```

- **外层**转了 3 圈 = 调了 **3 次模型**。
- **内层**每次负责把**那一次**的响应读完。

#### 为什么必须两个循环

- **只有外层**（不读流）：一次调用会吐出几十上百个流事件，必须**逐个处理**——「逐个处理一堆东西」本身就要循环；且流出错要重试，也是循环。
- **只有内层**（只读流）：内层只管「读完这一次请求」，它不知道「工具跑完还要再问模型一次」这个决策。

> **一句话分工**：**内层**把这一次调用办完（读流 + 重试）；**外层**决定还要不要再来一次调用。

#### 回到「用户插话」

```
【外层 第 1 次迭代】
  调用 run_sampling_request ──► HTTP 请求 #1 读流中
                                └─【内层】读到一半 → 用户插话 → 抢占：内层中断返回
  外层判断：有待处理输入 → 继续
【外层 第 2 次迭代】
  调用 run_sampling_request ──► HTTP 请求 #2（带新输入） ← 这就是 http-b
```

- 「中断 http-a」= **内层循环被中止**。
- 「turn 继续」= **外层循环再转一圈**。
- **两者在不同层级，不矛盾。**

> 补充：`run_sampling_request` 内部实际上还有重试循环和读流循环，可粗略合并理解为「一次请求级的循环」。

### 3.7 修正版伪代码（贴近真实）

```python
while True:
    result = await run_sampling_request(history, tools)   # 一轮 = 采样 + 工具执行

    needs_follow_up = result.model_needs_follow_up or input_queue.has_pending_input()

    if needs_follow_up and token_limit_reached:
        await auto_compact()      # 压缩后继续，不退出
        continue

    if not needs_follow_up:
        run_stop_hooks()
        return result.last_agent_message    # ← 正常出口
```

---

## 4. 为什么是「流式 + 事件」，而不是一次性返回

- 模型输出是**边生成边到**的，工具调用也是边到边执行的（还可以并行）。
- Codex 用一个事件流处理：`ResponseEvent` 被一个接一个 `stream.next()` 拉出来处理。
- 三大好处：
  1. **首字延迟低**——用户不用等全部生成完。
  2. **支持并行工具调用**——一次采样里模型请求多个工具，可同时执行。
  3. **支持随时取消**——每个事件都带取消检查，用户随时打断。

---

## 5. Codex 比「教科书版」多出来的生产级细节

这些是你能拿来证明「我真看过源码」的加分点：

1. **连接预热（prewarm）**：turn 开始前先预热 WebSocket 连接，下一次采样复用同一连接，省一次握手。
2. **取消令牌贯穿全程**：`CancellationToken` 从外层循环传到最内层事件处理，任何一层都能随时中止。
3. **并行工具调用**：`tools/parallel.rs` 让一次采样里的多个工具调用并行跑。
4. **输入队列**：用户在模型运行中途还能插入新输入（steer），主循环会检查并处理。
5. **重试与降级**：WebSocket 失败降级到 HTTP，带重试预算（retry budget）。

### 5.1 传输层：WebSocket 优先，SSE 降级（会话级粘性）

**流式输出走哪条传输？** 优先 WebSocket，不可用则降级 HTTP SSE。

#### 主传输：Responses API over WebSocket

不是 Realtime API 的语音 WebSocket，而是把「Responses API 的请求/响应」跑在一条 **WebSocket 长连接**上，多轮采样复用同一连接。证据：`client.rs` 里的 `ResponsesWebsocketClient` / `stream_responses_websocket`。

#### 降级：HTTP SSE（Server-Sent Events）

降级路径 `stream_responses_api` 用的是 SSE（`text/event-stream`）。证据：

```rust
use eventsource_stream::EventStreamError;   // 事件流（SSE）
let (_, sse_telemetry) = Self::build_streaming_telemetry(...);  // "sse" 遥测
fn on_sse_poll(...)                          // SSE 轮询
session_telemetry.sse_event_completed(...)   // SSE 事件完成
```

#### 降级是「会话级粘性」的（易漏点）

源码注释（`client.rs` 第 257 行）：*"WebSocket fallback is session-scoped: once a turn activates the HTTP fallback, subsequent turns..."*

→ **一旦某个 turn 触发降级，整个会话后续所有 turn 都留在 HTTP，不再回头试 WebSocket。** 实现是 `force_http_fallback` 里 `disable_websockets.swap(true)` 的会话级永久开关。

#### 为什么「主 WS、备 SSE」

| | WebSocket（主） | SSE（备） |
|---|---|---|
| 方向 | 全双工 | 半双工（仅服务端→客户端） |
| 连接 | 一条长连接，多请求复用 | 每请求重建（或 chunked） |
| 每消息开销 | 低（无 HTTP 头） | 高（每次带 HTTP 头） |
| 延迟 | 低 | 较高 |
| 通用性 | 部分代理/网络不友好 | 几乎处处可用、穿透更稳 |

**设计逻辑**：优先 WebSocket（快、能复用连接、配合 prewarm 把多轮采样延迟压到最低）；一旦 WebSocket 在当前网络不可用（代理拦截、握手失败），**永久退回 SSE**（稳、通用），而非在两种传输间反复横跳。

> 这也解释了 prewarm 的「探测」作用：prewarm 那次 `generate=false` 空请求，就是在 WebSocket 上提前试探——通就复用，不通就切 SSE。

### 5.2 取消令牌贯穿全程（详细）

#### 取消的本质：`or_cancel` 是「赛跑，输家被丢弃」

这是整个机制的**原子操作**（`async-utils/src/lib.rs`），只有 6 行：

```rust
async fn or_cancel(self, token) -> Result<Output, CancelErr> {
    tokio::select! {
        _ = token.cancelled() => Err(CancelErr::Cancelled),  // 取消信号先到
        res = self => Ok(res),                                // future 先完成
    }
}
```

`tokio::select!` 同时等两件事，**谁先发生算谁的，另一个被丢弃（drop）**。所以「取消」的本质 = **把正在等的那个 future 丢掉**，级联回收它内部所有子 future。

> **关键认知**：Rust async 的「取消」不是设 `is_cancelled = true` 标志位让每层自查，而是**直接把 future drop 掉**。
> - 标志位法 = 喊「大家停一停」，指望每个工人自觉停——会漏。
> - drop 法 = 拉闸断电——不管在干什么全停，且每个工人的收尾逻辑（关 socket、放锁）自动跑。
>
> drop 是**结构性、深度自动、不会漏**的。

#### 树形取消：`child_token()` 为什么要有

每进一层派生一个子 token：

```rust
let child = parent_token.child_token();   // 父取消 → 子自动取消；子取消 → 不影响兄弟
```

作用域需要独立生命周期：一个 turn 被抢占不该杀整个会话；一个子 agent 预算耗尽不该影响父 agent；一次工具被抢占不该毙掉整个采样请求。**这是树形取消，不是全局开关。**

#### 三种中断信号（关键区分）

| 信号 | 类型 | 谁触发 | 效果 |
|---|---|---|---|
| **`cancellation_token`** | 根 CancellationToken | 用户彻底中止 / 预算耗尽 | 整个 turn 中止（TurnAborted） |
| **`preempt`** | 子 CancellationToken | 流还在飞时来了新输入 | 取消**当前采样请求**，turn 继续 |
| **`stream.interrupt`** | `oneshot::Sender<()>` | 用户「暂停/停止生成」（Esc） | 打断**当前生成**，turn 保持存活 |

#### 「暂停 → 输入 → 继续」的完整链路

```
① 暂停（Esc / 停止按钮）
   └─ stream.interrupt 触发 → 当前流被中断
   └─ turn.rs：needs_follow_up = true; continue;
        → turn 没死，回到「等待输入」状态（非 TurnAborted）

② 输入（用户打字）
   └─ 文字进入 input_queue；流已停，无需 preempt，输入安静待在队列里

③ 继续（回车）
   └─ 外层循环下一轮：get_pending_input() 取输入 → 记录进历史 → 发起新采样请求
```

> **核心认知**：没有「暂停再恢复」这回事。LLM 的流**不能在中间冻结**——只能「打断它，然后重新发起」。所以「继续」= 一次**全新的请求**，不是「接着上次没说完的继续」。这也解释了 prewarm 的必要性：继续即发新请求，连接要温热。

#### steer vs pause：本质相同，一个关键区别

两者底层都是「cancel + resample」，但**打断时新输入是否已就绪**不同：

| | steer（边跑边打字） | pause（先暂停再打字） |
|---|---|---|
| 打断触发源 | **输入本身**（`watch_user_input` 监听到输入） | **用户手动动作**（`Op::Interrupt`） |
| 打断时输入状态 | 已就绪 | 未就绪 |
| 中间态 | 无，一气呵成 | 有——「已停止、待命等你输入」 |
| 底层信号 | `preempt`（取消令牌） | `stream.interrupt`（oneshot） |

- **steer** = 副驾驶边抢方向盘边喊「往左」——方向已定。
- **pause** = 先喊「停一下」，停稳了才想「我要去哪」。

> 一句话：**steer 是「带着输入打断」，pause 是「不带输入打断（打断后等输入）」**。同一内核（cancel+resample）的两个入口。

### 5.3 并行工具调用（详细）

**一句话核心**：一次采样里模型请求 N 个工具 → 每个工具被 `tokio::spawn` 成**独立 task 真并行跑** → 结果**按模型发出调用的顺序**写回历史。

#### 完整流程

```
模型一次响应里吐出 5 个 function_call
   │
   │  （stream_events_utils.rs:350-353）每个 function_call → handle_tool_call(call, child_token)
   │      返回一个 tool_future
   │  （turn.rs:2811）future 被 push 进 in_flight 队列（类型 FuturesOrdered）
   │
   ├─ handle_tool_call 内部（parallel.rs:196）tokio::spawn(...)
   │     每个工具 spawn 成独立 task（5 个同时开跑，真并行）
   │
   ├─ 每个 task 执行前过「并行门」（parallel.rs:205-209）
   │     supports_parallel → lock.read()  读锁（可一起进）
   │     不支持并行        → lock.write() 写锁（独占，排队）
   │
   └─ 流结束后 drain_in_flight（turn.rs:2487）
         按顺序 await 结果 → 按顺序写回历史
```

#### 三个关键机制

**① `tokio::spawn` + `FuturesOrdered` = 真并行 + 有序收集**

- `tokio::spawn`：每个工具执行放到独立 tokio task，多核真并行（不是单线程轮切）。
- `FuturesOrdered`：并发轮询这些 future，但**按插入顺序**返回结果。

> 为什么「有序」重要：历史是唯一真相源，`input` 数组顺序 = 时间线。工具**并行跑（快）**，结果**按发起顺序落盘（稳）**，否则模型看到的对话会乱。

**② `parallel_execution` 读写锁 = 每个工具声明「我能不能并行」**

```rust
let guard = if supports_parallel {
    Either::Left(lock.read().await)      // 支持并行 → 读锁（共享）
} else {
    Either::Right(lock.write().await)    // 不支持并行 → 写锁（独占）
};
```

- 支持并行 → 读锁：多个并行工具同时持有 → 真并行。
- 不支持 → 写锁（独占）：与其它工具串行排队。

> 为什么不能无脑全并行：有的工具有副作用/资源冲突（两个 shell 命令同时改同一文件）。所以**并行是 opt-in 的**——属于「工具元数据」的一部分（schema、并发特性、审批策略、沙箱要求）。

**③ `parallel_tool_calls: true`（API 层声明）**

`Prompt` 结构（turn.rs:1613）里有 `parallel_tool_calls: true`，**告诉模型「你可以在一次响应里给我多个工具调用」**。并行是双向配合：模型侧要「敢」一次吐多个 function_call，客户端侧要「能」并行执行。少一边都不成立。

#### 并行例子

模型要读 3 个文件：一次响应里 3 个 `read_file` → 3 个 task 并行读 → 结果按 a→b→c 顺序写回。串行耗时 = 三者相加；并行 ≈ 最慢那个。

#### 面试加分点

1. **「并发」≠「并行」**：`FuturesOrdered` 只保证并发轮询，真并行靠 `tokio::spawn`。
2. **结果有序落盘**：并行执行但顺序回灌，因历史顺序 = 时间线。
3. **并行是 opt-in**：读写锁让「能并行的并行、不能并行的串行」。
4. **取消独立**：每个工具 future 带自己的 `child_token`，可单独取消某一个。

### 5.4 重试与降级（详细）

#### 默认重试次数：5，可配到 100

`model-provider-info/src/lib.rs`：

```rust
const DEFAULT_STREAM_MAX_RETRIES: u64 = 5;   // 默认 5 次
const MAX_STREAM_MAX_RETRIES: u64 = 100;     // 用户配置封顶 100

pub fn stream_max_retries(&self) -> u64 {
    self.stream_max_retries.unwrap_or(5).min(100)
}
```

#### 为什么「重试 5 次后还会接着重试」：降级重置机制

`responses_retry.rs` 里，重试**不是「5 次就放弃」，而是分层的**：

```rust
if retry_state.retries >= max_retries                     // 5 次耗尽
    && client_session.try_switch_fallback_transport(...)  // 且能降级（仅首次）
{
    retry_state.retries = 0;   // ← 重试计数【清零】
    return Ok(());             // ← 继续重试（现在走 HTTP）
}
```

```
WebSocket 上重试 5 次（默认）
   │ 第 5 次失败
   ▼
降级到 HTTP，重试计数【清零】
   │
   ▼
HTTP 上再重试 5 次（全新一轮）
   │ 又失败 5 次
   ▼
真正报错，放弃
```

> `try_switch_fallback_transport` 只在**首次**切换返回 true（源码注释："Returns true if this call activated fallback, or false if fallback was already active"）。所以「清零」只发生一次（WS→HTTP 那次）。HTTP 上再耗 5 次就真放弃。

#### 连接失败可「无限退避重试」（`UnboundedConnectionRetries`）

```rust
if feature.enabled(UnboundedConnectionRetries)
    && matches!(err.details(), CodexErrorDetails::ConnectionFailed(_))  // 只匹配「连接失败」
    && !session_source.is_internal() && !provider.is_amazon_bedrock()
{
    // 指数退避：5s → 10s → 20s → ... → 60s 封顶，无限重试等网络恢复
}
```

#### 两个独立计数

`ResponsesStreamRetryState` 里有两个计数：

| 计数 | 用途 | 上限 |
|---|---|---|
| `retries` | 流重试（业务/传输错误） | 默认 5，降级时清零 |
| `connection_retries` | 纯连接失败重试 | 可无限（若 feature 开启），指数退避 |

#### 错误类型区分：`ConnectionFailed` vs `Stream`

表象都是「请求失败」，但靠**错误类型**区分（`TransportError` → `CodexErrorDetails`）：

```rust
TransportError::Connection(source) => CodexErr::ConnectionFailed(...),  // 建连失败
TransportError::Network(msg) | Build(msg) => CodexErr::Stream(msg),     // 读流时网络出错
```

| 场景 | 底层错误 | 高层类型 | 重试策略 |
|---|---|---|---|
| **握手时**断（连都没连上） | `TransportError::Connection` | `ConnectionFailed` | 可无限退避重试 |
| **读流时**断（流式输出中断线） | `TransportError::Network` | `Stream` | 有限重试 + 降级 |

> **分界线 = 握手是否成功**。区分靠 `err.details()` 的类型标签，不靠「观察表现」。这就是「错误处理要类型化」的价值——**错误类型承载了「该怎么处理」的信息**。

---

## 6. 面试答题骨架

**Q1：用伪代码描述 Agent 主循环。**
→ 直接默写上面第 2 节的伪代码，重点讲「回灌」这一步。

**Q2：循环的终止条件有哪些？如何防死循环？**
→ 真正终止只有 5 种：**模型判定完成、成本预算耗尽、用户取消、流中断、重试耗尽**。
   防死循环**不靠步数上限**（Codex 没有这个），而靠：① 模型自己判定结束（主要出口）；② 成本/token 预算（硬上限 `SessionBudgetExceeded`）；③ 取消令牌（用户随时中止）。
   注意陷阱：上下文超限和暂停都不是终止——前者压缩后继续，后者打断生成但 turn 存活。

   **关于「重试耗尽」展开讲（见 5.4 节）**：
   - 默认单传输重试 5 次（`stream_max_retries`，可配到 100）。
   - 5 次耗尽 → **降级 HTTP 且计数清零** → 再重试一轮（所以看起来「5 次后还继续」）。
   - 纯连接失败（握手失败 `ConnectionFailed`）可**无限退避重试**（5s→60s）；读流中断（`Stream`）走有限重试。
   - 区分靠错误类型，不靠表现：握手成功与否是分界线。

**Q3：工具结果怎么回灌给模型？**
→ **核心一句话**：按调用顺序，把工具执行结果包装成「与 `function_call` 的 `call_id` **配对**」的 `function_call_output` 结构化条目，append 进历史（唯一真相源）。下一轮采样从历史重建 prompt，模型自然看到「调用+结果」的配对。

**Python 版完整流程：**

```python
history = []   # 唯一真相源：一个 list，每个元素是带 type 的 dict

async def run_turn(user_input: str) -> str:
    history.append({"type": "message", "role": "user", "content": user_input})

    while True:
        prompt_input = list(history)          # ① 从历史【重建】prompt（非增量拼接）
        needs_follow_up = False
        tool_calls = []
        last_message = None

        async for event in stream_model(prompt_input):   # ② 流式调模型
            if event["type"] == "function_call":
                tool_calls.append(event); needs_follow_up = True
            elif event["type"] == "message":
                last_message = event; history.append(event)

        outputs = await execute_tools_parallel(tool_calls)  # ③ 并行执行，结果有序

        for call, output in zip(tool_calls, outputs):       # ④ 回灌
            history.append(build_output_item(call, output)) #    包装后 append

        if not needs_follow_up: break                        # ⑤ 模型没调工具 → 结束
    return last_message
```

**回灌的核心函数：**

```python
def build_output_item(call, result):
    output = result.text if not result.is_multimodal else [
        {"type": "input_text", "text": result.text},
        {"type": "input_image", "image_url": result.image_url},
    ]
    output = truncate_tokens(output, max_tokens=20000)   # 截断保护

    if result.is_error:                                   # 失败也回灌
        return {"type": "function_call_output", "call_id": call["call_id"],
                "output": f"ERROR: {result.error_message}", "success": False}
    return {"type": "function_call_output", "call_id": call["call_id"],
            "output": output, "success": True}
```

**回灌后历史的样子（模型下一轮看到的）：**

```python
history = [
    {"type": "message", "role": "user", "content": "列出文件并读 main.py"},
    {"type": "function_call", "name": "shell", ..., "call_id": "call_1"},
    {"type": "function_call", "name": "read_file", ..., "call_id": "call_2"},
    {"type": "function_call_output", "call_id": "call_1", "output": "main.py\nREADME.md"},
    {"type": "function_call_output", "call_id": "call_2", "output": "def main()..."},
]
```

**两个「增强」（不是核心，但体现生产边界）：**

1. **失败也回灌（`success=False`）**：工具报错也回灌错误信息，模型才知道「这步失败」并自我纠正——不回灌它就会基于错误前提继续（如 `git push` 冲突后还宣称「已推送」）。
2. **超长输出截断**：`cat` 大日志可能几十万 token，无脑塞进历史会打爆上下文，故 `truncate_function_output_payload` 截断。

**为什么「append 历史」而非「临时注入」？** 避免两处状态（history + pending 缓冲）不一致——回灌 = 更新唯一真相源，下一轮 `list(history)` 自然带上，无并发状态 bug。

> 面试结构：先说核心一句（证明懂机制）→ 再补两个增强（证明考虑过生产边界）。

**Q4：为什么用流式？**
→ 首字延迟 + 并行工具调用 + 可随时取消，三点各展开一句。

---

## 7. 动手练习（Python）

用 `openai` SDK + `asyncio` 写一个最小主循环：

1. 一个 `while True` 循环。
2. 用 Responses API 流式调模型。
3. 检测到 function call 就执行（先写死一个 `get_weather` 假工具）。
4. 把结果回灌，继续循环；模型只回文字就 break。

目标：跑通「问天气 → 模型要调工具 → 执行 → 回灌 → 模型给出最终答案」的完整链路，然后对照本笔记第 2 节的伪代码，逐行理解。

---

## 8. 补充：范式辨析（ReAct vs Plan-and-Execute vs Multi-agent）

**问：本模块讲的循环是哪个框架？**
**答：就是 ReAct。** ReAct = 推理（Reason）+ 行动（Act）交替进行，即「决策→行动→观察→再决策」。任何基于工具调用的 agent，底层循环都是 ReAct。

**关键认知：这些范式不是互斥的，而是分层组合的。**

```
┌─────────────────────────────────────────────────┐
│  控制结构层：Plan-and-Execute / Reflection / Multi-agent / Search │
├─────────────────────────────────────────────────┤
│  原子层：ReAct 循环（决策 → 行动 → 观察 → 再决策）   │
└─────────────────────────────────────────────────┘
```

- Plan-and-Execute = 先规划 + 逐步执行；「执行每一步」内部仍是 ReAct 循环。
- Reflection = 外层再套一个「自我批判→重试」的循环。
- Multi-agent = 把某一步整个丢给子 agent，子 agent 内部仍是 ReAct 循环。

**Codex 的分层实现：**

| 范式 | Codex 实现 | 源码证据 |
|---|---|---|
| ReAct（地基） | 默认主循环 | `turn.rs` 的 `run_turn` |
| Plan-and-Execute | `update_plan` 计划工具，模型维护计划清单并勾选进度 | `plan_tool.rs`、`spec_plan.rs` 的 `PlanHandler` |
| Plan 模式 | 专门流式 plan mode | `turn.rs` 的 `handle_plan_segments` |
| Multi-agent | `SpawnAgent`/`ResumeAgent`/`WaitAgent` 工具 | `spec_plan.rs` 的 `multi_agents` handlers |

**面试金句：**
> 很多人把 ReAct、Plan-and-Execute、Multi-agent 当成互斥的框架去选，其实它们是分层组合的：ReAct 是原子循环，其他都是叠加在它上面的控制结构。Codex 就是默认跑 ReAct，再叠加 update_plan 计划工具和子 agent 派发来实现规划与多智能体。

### 8.1 深化：agent loop 与 ReAct 的关系

**它们就是同一个循环，只是名字不同、视角不同：**

- 「agent loop / 主循环」= **工程视角的通用名**，描述代码在做什么（while 循环：调模型 → 执行工具 → 回灌）。
- 「ReAct」= **研究视角的命名**，给「推理 + 行动交替」这个模式起的学术名字。

**唯一细微区别：ReAct 多一个显式的 Thought（推理痕迹）。**

原版 ReAct 流程：

```
Thought → Action → Observation → Thought → Action → ... → Final Answer
```

- ReAct 要求模型在每次行动**前**先输出一段「我现在想……所以我要做……」的文字（Thought）。
- 现代工具调用 agent（含 Codex 默认形态）通常**省略 Thought**：模型直接吐 function call，「决策」是隐式的（藏在工具调用里或模型内部 reasoning 字段）。

所以更精确的关系：

```
agent loop（通用） ⊇ ReAct（带显式 Thought 的版本）
```

- agent loop 更宽泛：只要「模型 → 工具 → 回灌」循环都算。
- ReAct 是其中「明确要求先输出推理文字再行动」的特定形态。

**一锤定音版回答：**
> agent loop 是通用机制，ReAct 是给它命名的学术范式，区别只在于 ReAct 显式要求 Thought 推理痕迹，而现代工具调用常把它隐式化。Plan-and-Execute、Reflection、Multi-agent 不是另一个循环，而是套在这个循环外面的控制结构。
