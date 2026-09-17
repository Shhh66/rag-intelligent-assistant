# LangFuse 可观测改造 —— UI 验证清单

> 用途：改造完成后，对照 LangFuse UI 逐条核对。带 `[ ]` 的可直接打勾。
> 每项都写了「在哪看 / 期望看到什么 / 对不上说明什么」。

---

## 前置条件

- [ ] Docker 已启动，LangFuse 可访问：http://localhost:3000
- [ ] 业务服务已重启（**代码改动必须重启才生效**，容器重启用 `docker compose up -d`）
- [ ] `.env` 里 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` 已配
- [ ] `config.py` 中 `LANGFUSE_ENABLED = True`、`LANGFUSE_CAPTURE_IO = True`
- [ ] 问一个**会触发工具调用**的问题（如「北京天气怎么样」），否则 trace 里没有工具 span

启动后台线程确认（app.py 会自动起，此处仅备查）：

```bash
docker compose exec main python -c "import config; print(config.LANGFUSE_CAPTURE_IO, config.LANGFUSE_TRACE_IO_MAXLEN)"
# 期望输出：True 2000
```

---

## 阶段 P0：generation 的入参 / 出参

**访问路径**：LangFuse → Traces → 点开任一 trace → 点开 `decision_engine.decide`

### P0-1 能看到「模型收到了什么」

- [ ] GENERATION 的 **Input** 区块不再为空
- [ ] 显示为逐条 message 列表，每条带 `role`（system / user / assistant）
- [ ] 每条内容**被截断到 500 字**，超长的末尾有 `...(+N)` 标注
- [ ] system 那条只露出开头的角色定义（历史摘要、长期记忆注入**没有**被全量带出）

> **对不上说明**：Input 仍为空 → `LANGFUSE_CAPTURE_IO` 没生效（改的是 config 文件但服务没重启）。

### P0-2 能看到「模型答了什么」

- [ ] GENERATION 的 **Output** 区块显示 LLM 返回的正文
- [ ] 超长同样有 `...(+N)` 标注

### P0-3 思考过程进了 metadata

- [ ] GENERATION 的 **Metadata** 里有 `reasoning_content` 字段（截断 500 字）
- [ ] 同时仍有原有的 `call_site` 和 `cost_rmb`
- [ ] **没有**出现空的 `reasoning_content=""`（无思考过程时不写该键）

> 推理模型的思维链单独放 metadata，**不占** Output 字段 —— 所以 Output 里应该是干净的正文，不是思考过程。

### P0-4 总开关确实能关（可选，验完记得改回）

- [ ] 设 `LANGFUSE_CAPTURE_IO = False` → 重启 → 问一个问题
- [ ] 新 trace 的 Input / Output **全空**，但 **Token / 成本照常显示**
- [ ] 改回 `True` 并重启

---

## 阶段 P1：trace 顶层 IO + 工具 span 输出

### P1-1 trace 列表页一眼可见（最直观的一项）

**访问路径**：LangFuse → Traces（列表页，不用点进去）

- [ ] **Input 列**显示用户问题原文
- [ ] **Output 列**显示最终答案（超 2000 字截断，末尾 `...(+N)`）
- [ ] Name 列仍是 `chat`（未改动）
- [ ] User / Session / Latency / Cost 列照常

> **这是 P1 的核心收益**：改前这两列全空，必须逐条点进去看 span 才能猜这次问了什么。

### P1-2 trace 详情页顶部

- [ ] 点开 trace，顶部 Input / Output 区块与列表页一致

### P1-3 工具 span 带上了返回结果

**访问路径**：trace 内 → `工具:query_weather`（SPAN）

- [ ] **Input** 显示调用参数（如 `{"city": "北京"}`）
- [ ] **Output** 显示工具返回结果（截断 500 字）—— **改前这里是空的**
- [ ] Metadata 里仍有 `success` / `latency_ms` / `error`

### P1-4 工具失败仍标红

- [ ] 问一个会失败的工具调用（如课表查询，**前提是容器还没重建、`Crypto` 仍缺失**）
- [ ] `工具:edu_query_schedule` 显示为 **ERROR** 级别（红色）
- [ ] Metadata 的 `error` 字段有错误摘要
- [ ] 该 span 的 Output 显示错误文本

---

## 阶段 P2：ReAct 轮次分组 + memory 耗时

### P2-1 每一轮成为可折叠的父节点

**访问路径**：点开 trace，看树形结构

- [ ] 出现 `ReAct 第 1 轮`、`ReAct 第 2 轮`…… 的父节点
- [ ] 该轮内的 `decision_engine.decide` / `工具:xxx` / `judge_evaluate` **嵌在对应轮次之下**，不再平铺在 trace 根
- [ ] 每个轮次节点的 Metadata 里有 `turn` 字段
- [ ] `memory.extract` 等收尾节点嵌在**触发它的那一轮**之下（它在该轮 return 之前执行）

**期望的树形**：

```
chat
├─ ReAct 第 1 轮
│   ├─ decision_engine.decide
│   ├─ 工具:query_weather
│   ├─ judge_evaluate
│   │   └─ judge.evaluate
│   └─ memory.extract          ← 该轮收尾时执行
├─ ReAct 第 2 轮
│   └─ decision_engine.decide
```

> ⚠️ **必须问一个需要多轮的问题**才能看到分组效果，例如「帮我查一下北京和上海的天气，
> 然后对比哪个更适合旅游」。单轮就结束的问题（如「北京天气怎么样」）只会显示
> `ReAct 第 1 轮` 一个父节点 —— 这是正常的，不是 bug。

### P2-2 memory 耗时不再是 0.00s

- [ ] `memory.extract` 显示真实耗时（通常几百 ms ~ 数秒），不再是 `0.00s`
- [ ] 若本次触发了 `memory.summarize` / `memory.extract_summary`，它们同样有真实耗时

---

## 边界场景（建议一并核对）

### B-1 RAG 检索链路的 span（跨进程验证）

**问法**：问一个**知识库**问题（如「文档里讲了什么」），不要问天气/课表

- [ ] trace 里出现 `查询改写` / `混合检索` / `重排` 三个 SPAN
- [ ] 它们挂在**同一条 trace** 下，**不是**各自变成独立 trace

> 这一项同时验证了**跨进程 trace_id 透传**（MCP 子进程的 span 并入主 trace）。
> 只问天气/课表时这三个 span 不会出现 —— 那是正常的，不是 bug。

### B-2 User / Session 归因

- [ ] trace 列表的 User 列显示登录用户
- [ ] 点 Session 值能过滤出同一会话的多条 trace
- [ ] 在 Streamlit 点「清空记忆」后再提问 → 新 trace 属于**新的 session**

---

## 已知未做（不是 bug）

| 现象 | 原因 |
|---|---|
| Evaluation / Prompt 管理两个大功能不存在 | 项目未接 LangFuse 的 score 与 prompt 体系（RAGAS 是独立体系） |
| trace 的 Name 固定为 `chat` | 未做动态命名 |
| 只看到 `ReAct 第 1 轮`，没有第 2 轮 | 该问题一轮就结束了，正常 |

---

## 对不上时的排查顺序

1. **先确认服务重启过** —— 改 config 不重启是最常见原因
2. **确认 trace 是不是新的** —— 旧的 trace 不会有新字段
3. `docker compose exec main python -c "import config; print(config.LANGFUSE_CAPTURE_IO)"`
4. 查子进程是否拿到环境变量：`docker compose exec main env | findstr LANGFUSE`
5. **本地审计兜底**（数据不出本机，永远可用）：
   ```bash
   python trace_view.py --last        # 最近一次请求的完整链路
   python trace_view.py -n 10         # 列出最近 10 条
   ```

---

## 附：本次改造涉及的配置项

| 配置 | 默认 | 作用 |
|---|---|---|
| `LANGFUSE_CAPTURE_IO` | `True` | 总开关。`False` = 完全不上报入参/出参（**不是**「取消截断」） |
| `LANGFUSE_IO_MAXLEN` | `500` | 单条 message / generation 输出的截断长度 |
| `LANGFUSE_TRACE_IO_MAXLEN` | `2000` | trace 顶层入参/出参的截断长度 |

三者均在 `rag_assistant/config.py`，改动后需重启服务。
