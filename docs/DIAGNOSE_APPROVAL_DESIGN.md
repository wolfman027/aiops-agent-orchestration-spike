# 设计：诊断结论的审批与关单 —— 拒绝重跑 / 忽略 / 误报 / 历史计划

> **状态**：设计定稿（2026-09-10）→ 待实现。本文档是「诊断完成后人对结论下判断」这一段流程的唯一事实源。
> **范围**：spike 加一个终态门（`POST /dismiss`）；APM 加两个端点（决策 + 历史读取）与 `RecordStore.close()`；
> UI 在 `View diagnosis` 弹窗里加审批条与历史计划。
> **配套**：`TRACE_CODE_DESIGN.md`（诊断内核 + Stage2 审批基座）、`TRACE_CODE_IMPLEMENTATION_PLAN.md`（改动全表）。
> **Approve 本轮不做**（理由见 §5.3）。

---

## 1. 需求背景

现状：诊断链路已经跑通到「拿到结论 + 多方案」——spike 产出
`conclusion.recommended_fix`（互斥多方案，带 `steps`/`risk`/`rollback`/`suggested_diff`），
UI 在 `View diagnosis` 弹窗里按「根因 → 总结 → 修复方案（分页签）→ 分析链路」渲染。

**缺的是"人对结论下判断"这一段**：看完方案之后，弹窗里没有任何动作入口——
不能选方案、不能拒绝、也不能把这条问题单关掉。spike 侧的 `/remediate` + `/approve` 早已实现，
只是没有调用方。

本轮补齐三个动作 + 一份历史：

| 人的判断 | 语义 | 结果 |
|---|---|---|
| **拒绝** | 方案不对／不够，我告诉你该往哪查 | 诊断**重新跑**，产出新一版修复方案 |
| **忽略** | 这条不需要做 | 问题单**关闭**（`closed`） |
| **误报** | 这条根本不是问题 | 问题单关闭并**记一次误报**（`resolved` + FPR 回写） |

多方案时"选中的那个页签"就是**待处理方案**，它决定拒绝时快照哪一版 plan（`option_index`）。

---

## 2. 决策矩阵与状态对照

| 动作 | UI 按钮 | spike 调用 | APM 落库 | 问题单 `state` | 诊断会话 `status` |
|---|---|---|---|---|---|
| **拒绝** | `拒绝 · 重新分析`（反馈必填） | `/remediate{session_id, option_index}` → `/approve{decision:"reject", feedback}` | evidence 追加 `diagnose_decision` | **不变**（`pending`/`in_progress`） | `analyzing` → `completed`；达上限 → `closed_manual` |
| **忽略** | `忽略`（二次确认） | `/dismiss{session_id}`（404 容忍） | 同上 + `records.close(reason="ignored")` | **`closed`** | `dismissed`（会话已过期则记 `expired`） |
| **误报** | `误报`（二次确认） | `/dismiss{session_id}`（404 容忍） | 同上 + FPR 回写 + `records.resolve(reason="false_positive")` | **`resolved`** | `dismissed` |

**术语澄清（最容易混的一处）**：

- **spike 只认一个"被忽略"终态 `dismissed`**——它不区分"忽略"和"误报"。误报是**业务口径**，
  是"记一次误判给误报率"，属于 APM 的事。spike 是诊断内核，不该知道误报率。
- **APM 才区分**：`closed`（忽略，不需要做）vs `resolved` + FPR（误报，不是真问题）。
  两者都是终态、都退出 `_OPEN_STATES`，所以复发都会开新单。

诊断会话状态机（新增 `dismissed` 与 `/dismiss` 路径用 **粗体** 标出）：

```
POST /diagnose ─▶ analyzing ─┬─▶ completed(含 conclusion)
                             ├─▶ failed
                             └─▶ **dismissed**（仅经 POST /dismiss，且前置态须为 completed/failed/closed_manual）
completed ─┬─ /remediate ─▶ plan=pending_review
           │                  └─ /approve reject ─┬─▶ 重跑（analyzing，循环）
           │                                       └─▶ reanalyze_count ≥ 3 ─▶ closed_manual
           │                                              └─ **/dismiss ─▶ dismissed**
           └─ **/dismiss ─▶ dismissed**
```

已存在、**本轮不动**的分支：`approve`（终态签章，零下游动作）、`/stop`（→ `failed`）、
阶段/迭代上限（→ `failed`）。

---

## 3. 三仓职责与时序

**职责边界**：spike 管"诊断内核 + 会话状态"，APM 管"问题单状态 + 业务口径（误报率）+ 持久化"，
UI 只管渲染与提交。

### 3.1 拒绝（重跑）

```
UI                     APM                                   spike
│ pcDiagRec.state ∈ open
│ 选中方案 = dgxOptSel+1
│ 填 feedback（必填）
├─POST /v1/problems/{id}/diagnose/decision─────────────────▶│
│  {decision:"reject", option_index:N, feedback:"..."}      │
│                        │ 1. 守卫：记录终态? 未绑诊断?     │
│                        │ 2. feedback 空 → 422              │
│                        ├─POST /remediate/{sid}────────────▶│ 快照选中方案为 plan
│                        │◀── {steps, option_title, ...} ────┤ (plan.status=pending_review)
│                        │ 3. 暂存 steps（历史计划的唯一留存处）
│                        ├─POST /approve/{sid}──────────────▶│ plan.status=rejected
│                        │  {decision:"reject", feedback}    │ reanalyze_count+1
│                        │◀── {remediation_status,           │ 未达上限 → _schedule_run(重跑)
│                        │     reanalyze_count, max_reanalyze}│ 达上限   → closed_manual
│                        │ 4. append_evidence(diagnose_decision)
│                        │ 5. record.state 不变（重跑中）
│◀── {record_state:"pending", session_status:"completed", ...}│
│ 5'. 重启 2s 轮询 + 抑制旧快照（§4.2 的 dgxRerunPending）
│ 6'. 刷新历史（GET .../diagnose/decisions）
```

### 3.2 忽略 / 误报（关单）

```
UI                     APM                                   spike
├─POST /decision──────────────────────────────────────────▶│
│  {decision:"ignore" | "false_positive"}                   │
│                        │ 1. 守卫同 §3.1                    │
│                        ├─POST /dismiss/{sid}──────────────▶│ status=dismissed
│                        │◀── {status:"dismissed"} 或 404 ───┤ (404 = 会话已过期)
│                        │   404 → 容忍，记 session_status="expired"
│                        │ 2a. ignore → records.close()      │
│                        │ 2b. false_positive → _record_fpr() │
│                        │     + records.resolve()           │
│                        │ 3. append_evidence(diagnose_decision)
│◀── {record_state:"closed"|"resolved", ...}                 │
│ 4'. 关弹窗 + 刷新列表
```

**关键点：忽略/误报不能让"会话过期"挡住关单。** spike 的 session 是内存态 + TTL
（`SessionStore` 懒清理，默认 `session_ttl_seconds=3600`），进程重启或超时即 404。
问题单的关闭是 APM 自己的业务动作，不该依赖内核会话还活着——否则就会出现
"想关单却被 404 挡住"的死胡同（这条路径此前已经在别的场景暴露过一次）。
**拒绝是例外**：没有活着的会话就没有东西可重跑，只能 409 并如实告知。

---

## 4. 契约

### 4.1 spike `POST /dismiss/{session_id}`

```jsonc
// request（body 可缺省）
{"reason": "operator ignored"}      // 记进 session.feedbacks；空串不记
// response 200
{"session_id": "…", "status": "dismissed"}
// 404 session not found or expired
// 409 会话处于 {status}，不可忽略（仅 completed/failed/closed_manual 可忽略）
```

- **前置态**：仅 `completed` / `failed` / `closed_manual`。`analyzing` → 409（先 `/stop`）；
  已 `dismissed` / `approved` → 409（幂等语义明确，不静默成功）。
- **不重跑**：不改 `conclusion`、不改 `remediation`、不 `_schedule_run`。
- **不碰 `tasks`**：`completed` 会话的 tasks 已在落终态时被 `finalize_terminal_tasks` 归一
  （`done` + `derived`），`failed` 会话的残留已是 `cancelled`；dismiss 只是归档，
  再调一次归一反而会把 `cancelled` 弄脏。**故显式不调用** `finalize_terminal_tasks`。
- **不新增字段**：`status` 本就是自由 `str`，`feedbacks` 是现成的 list；`snapshot()` 已回传二者。
- `/remediate`、`/approve`、`_resolve_specs_for`、`/status` 全部不动。

### 4.2 APM `POST /v1/problems/{record_id}/diagnose/decision`

```jsonc
// request
{
  "decision": "reject" | "ignore" | "false_positive",
  "feedback": "…",        // reject 必填非空（422）；忽略/误报忽略之；max_length=2000
  "option_index": 2       // 1-based；仅 reject 用；省略 → spike 侧推荐方案
}
// response 200
{
  "record_id": "PR-…",
  "decision": "reject",
  "session_id": "…",
  "session_status": "completed" | "dismissed" | "expired",
  "remediation_status": "rejected" | "closed_manual" | null,
  "reanalyze_count": 1, "max_reanalyze": 3,     // 仅 reject 有值
  "record_state": "pending" | "closed" | "resolved"
}
```

守卫（顺序）：记录不存在 → 404；`state ∈ {resolved, closed, archived}` → 409（镜像
`POST /diagnose` 的终态守卫）；未绑定诊断会话 → 409；`reject` 且 feedback 空 → 422。

⚠️ **`session_status` 在 reject 路径是旧值。** `_schedule_run` 只投后台任务，
`session.status = "analyzing"` 是后台 `run_diagnose` 才置的（`aidiag/diag/runner.py:115`）。
所以 reject 的响应里 `session_status` 仍是 `completed`——**调用方不能靠它判断"已重跑"**，
必须无条件重启轮询，并用本地 flag 抑制旧快照（UI §4.2）。

出站纪律沿用现有写法：`OutboundGateway.validate_url(url)`；`httpx.HTTPError` →
`AppException(ErrorCode.UPSTREAM)`（502）；spike 非 2xx → 502；spike 404（会话过期）在
reject 路径 → 409，在忽略/误报路径 → 容忍并继续。

### 4.3 APM `GET /v1/problems/{record_id}/diagnose/decisions`

```jsonc
{"items": [/* evidence 里 type=="diagnose_decision" 的条目，按写入顺序 */]}
```

- **不加状态守卫**（只读，供 UI 渲染历史）。
- **不复用 `GET /{id}/diagnose`**：那条路径必须保持"**逐字回传 spike 快照**"
  （UI `renderDiagnosis` 直接读顶层字段：`status`/`conclusion`/`tasks`/`tool_calls`…），
  塞入历史会破坏形状契约。历史走独立端点。

### 4.4 evidence 条目形状

```jsonc
{
  "type": "diagnose_decision",
  "decision": "reject" | "ignore" | "false_positive",
  "session_id": "…",
  "option_index": 2, "option_title": "加连接池超时",   // 仅 reject
  "steps": [ /* 该版计划的 steps 快照，仅 reject */ ],
  "feedback": "…",                                    // 仅 reject
  "session_status": "dismissed" | "expired",
  "remediation_status": "rejected" | "closed_manual" | null,
  "reanalyze_count": 1, "max_reanalyze": 3,           // 仅 reject
  "decided_at": "2026-09-10T…Z"
}
```

无关字段一律 `null`，形状统一，UI 不必分支解析。

---

## 5. 三个设计取舍的理由

### 5.1 历史计划为什么必须落 APM evidence

**spike 的 `session.remediation` 是单值的**（一个 `RemediationPlan` 字段），而且每次重跑
`run_diagnose` 开头就会 `session.remediation = None` + `session.conclusion = None`
（`aidiag/diag/runner.py:120-121`，注释写明"重跑后面向新 conclusion 的方案，旧 plan 不再适用"）
——**历史计划在 spike 侧必丢**，而且 spike 本身是内存态，进程重启就什么也没有。

`problem_record.evidence` 是 JSON 列、跟着问题单持久化，是唯一能活下来的地方。
所以 APM 在调 `/remediate` 拿到 `steps` 后**立刻快照进 evidence**；UI 的历史计划卡片
就是渲染这些条目。

代价：每次拒绝会往 evidence 里存一份 steps（含 `suggested_diff` 代码块）。规模是可控的
——上限 `max_reanalyze=3`，每次一份。若将来发现某条 `suggested_diff` 过大，再考虑截断。

### 5.2 为什么 忽略=`closed`、误报=`resolved`

- **误报** 复用 APM 既有的 M7/UC-7.6 语义：`POST /{id}/resolve` 带 `{"false_positive": true}`
  会写 `fpr_table`（误报计数 + FPR 重算）并刷新 `aiops_false_positive_rate` Gauge，
  然后 `resolve()` → `state='resolved'`。所以误报就该是 `resolved` + FPR 回写，与既有链路一致。
- **忽略** 是"不需要做"，与"判定为误报"是两件事（后者要进误报率统计，前者不进）。
  APM 的 `state` 枚举里本就有 `closed`（`V1__init_tables.sql:17` 的注释与生成列语义都含它），
  **无需 DDL 迁移**：`open_group_key` 生成列只认 `pending/in_progress`
  （同文件 39-41），所以 `closed` 会自动退出 UNIQUE、允许复发开新单——与 `resolved` 行为一致。
  实现上 `RecordStore.close()` 镜像 `resolve()`，复用 `resolved_at` / `resolve_reason`
  两个通用审计列（"关闭时间/原因"，`closed` 同样适用）。

已知且接受的后果：列表页 KPI 里 `closed` 既不计入 OPEN 也不计入 RESOLVED
（`js/app.js:3705-3721` 现有口径），问题单会从两个计数里"消失"；它仍可在 `state=Closed`
过滤下看到（`filteredProblems` 已含 `closed`/`archived`）。

### 5.3 为什么本轮不做 Approve

spike 的 `/approve{decision:"approve"}` 是**终态签章**：置 `plan.status="approved"`、
`session.status="approved"`，**零下游动作**（返回里明写 `note: "approve 为终态签章，不执行任何改动"`）。
`TRACE_CODE_DESIGN.md` §1.2 也写死了"**执行器不存在**"——`apply_fix` / `ExecutionResult` /
`status="executed"` 全被删除，因为本模型下审批作用于**计划**，不作用于执行。

在没有执行层的前提下，一个"批准"按钮点下去只会把会话标成 `approved` 然后什么也不发生
——对操作者是误导。**先不做**；等真正要落地执行（改代码/开 PR/跑 remediation）时，
把"批准"和那一层的执行器一起设计。

---

## 6. 与 HolmesGPT 的对照

> 结论先说：**两套东西不在同一层**。HolmesGPT 的审批是**逐次工具调用的执行门**（"这一次动作放不放行"）；
> 我们这套是**结论/方案层的业务审批**（"这份诊断结果要不要认"）。不是"移植 vs 自建"的关系，
> 而是正交的两层，可以叠加。
>
> 对照基线：`/Users/h.a.hu/accenture/analysis/HolmesGPT/holmesgpt`（上游 HolmesGPT）。

### 6.1 HolmesGPT 实际有的是什么

| 维度 | HolmesGPT | 证据 |
|---|---|---|
| 审批对象 | **单个 tool call**，不是计划/结论 | `StructuredToolResultStatus.APPROVAL_REQUIRED`（`holmes/core/tools.py:64-69`）；`Tool.invoke()` 在 `context.user_approved` 为假时直接拒跑（`:353-377`） |
| 触发条件 | 工具名命中配置的 `approval_required_tools`（fnmatch 名单） | `holmes/core/tools.py:408-421` |
| bash 侧判定 | 三段式：硬阻断(`sudo`/`su`) → deny 列表 → allow 列表 → **都不中 ⇒ APPROVAL_REQUIRED** | `holmes/plugins/toolsets/bash/validation.py:423-433`；`bash/common/config.py:9-12` |
| CLI 三选项 | Yes / Yes+把这个前缀永久加白 / **No+告诉它该怎么改** | `holmes/interactive.py:1836-1900` |
| 拒绝之后 | `feedback` 作为 tool result 注回，**同一轮调查继续往下走** | `holmes/core/tool_calling_llm.py:372-384` |
| Server 模式 | SSE `approval_required` 暂停 + 客户端 `tool_decisions:[{tool_call_id, approved}]` 恢复 | `docs/reference/http-api.md:902-955` |
| server 默认 | `enable_tool_approval=false` 时审批类工具**直接转成 error 喂回模型** | `holmes/core/tool_calling_llm.py:1420-1424` |
| 防伪 | HS256 签名 token 绑定 `tool_call_id` + args hash | `holmes/utils/approval_tokens.py` |
| 持久化 | 只有 **bash 前缀白名单** `~/.holmes/bash_approved_prefixes.yaml` | `bash/common/cli_prefixes.py:42,64` |
| 只读定位 | 默认只读；唯一 mutating 是 opt-in 的 Kubernetes Remediation MCP（`run_kubectl_command`：Mutating=Yes / Approval=Human，Helm 默认关） | `README.md:129`、`docs/why-holmesgpt.md:74`、`specs/kubernetes-remediation-mcp.md:36` |

### 6.2 HolmesGPT 明确**没有**的

- **多候选修复方案**：结论是自由文本 `ChatResponse.analysis: str`
  （`holmes/core/models.py:351-357`）；prompt 只要求"如果有多个可能**原因**就列出来"
  （`holmes/plugins/prompts/generic_ask.jinja2:46`），从没有"互斥多**方案**、人选一个"。
- **计划级批准**：没有 approve/reject 一份分析结论的机制。
- **拒绝 → 重新出计划**：没有 `reanalyze` / 重新生成计划的概念；最接近的只是
  "这次工具不批 + 附带建议 + 继续聊"。
- **忽略 / 误报关单**：无。也没有误报率——全仓无 `false_positive` / `fp_rate` 概念，
  只有 `IssueStatus.OPEN/CLOSED`（`holmes/core/issue.py:7-9`）。
- **计划历史**：无 approvals/plans 表（`holmes/core/supabase_dal.py:72-89` 的表清单里没有）；
  只有平台侧的会话/结果历史与上面那份 bash 前缀白名单。

### 6.3 对我们的三条落地结论

1. **我们的拒绝 → 重跑、忽略/误报 → 关单，上游没有对应实现，属自建。** 可以直接说清楚：
   这套 HITL 是我们按产品需要设计的，不是从 HolmesGPT 抄来的。
2. **本轮不做 Approve 是对的，上游也印证了这一点**：HolmesGPT 有一个真实的执行门，
   是因为它有 opt-in 的 mutating 工具要放行；我们的 approve 后面没有执行器，
   所以先不出现"批准"按钮，避免做成一句空话。
3. **将来真要做执行层，抄 HolmesGPT 那一套**：`approval_required_tools` 名单 +
   签名 token 防伪造 + pause/resume（`tool_decisions`）——那是"执行层审批"的成熟参考实现。
   届时我们的模型会变成**两层审批**：结论层（本文档，人认不认这份诊断）→ 执行层
   （HolmesGPT 式，每次动作放不放行）。

### 6.4 一处刻意的不同（成本量级）

上游"拒绝"拒绝的是**一条命令**，feedback 注回后同一轮调查继续，代价很小；
我们的"拒绝"是**重跑整个诊断**，代价高一个量级。这直接决定了三条设计：

- `max_reanalyze=3` 上限必须存在（达上限落 `closed_manual`，交人工）；
- 拒绝**必须带修改建议**（422 硬校验）——不能让操作者空手重跑三遍烧 token；
- 达上限后 UI 只保留"忽略/误报"，不再给"拒绝"。

---

## 7. 边界与非目标

- **不做 Approve 按钮**（§5.3）。
- **不给 spike 加持久化**：历史计划由 APM evidence 承载，不改 spike 的存储模型。
- **不改 `GET /{id}/diagnose` 的响应形状**（保持逐字回传）。
- **不改 `_resolve_specs_for` / `case_type` 分派 / 三个 `/diagnose*` 门**。
- **不改列表页 KPI 口径**（§5.2 的已知后果）。
- **不动 `ProblemRecord` 模型与 DDL**（`closed` 已在既有语义内）。
- **已知命名坑（本轮不动）**：列表页行上的 `Ignore` 按钮（`js/app.js:3795` + `confirmIgnore`）
  **语义上就是"误报"**（它发 `{false_positive: true}`）。本轮不碰它；
  若要消除歧义，建议后续单独把该按钮文案改成 `False positive`。

---

## 8. 验证

**离线（权威）**

1. spike：`uv run pytest tests/ -m "not live" -q` —— 新增 `tests/test_dismiss.py`
   （终态 gate、409/404 分支、不碰 tasks）。
2. APM：`uv run pytest tests -q` —— 新增 `tests/test_problem_diagnose_decision_api.py`
   （reject 双调用顺序、422/409 守卫、忽略/误报关单、**会话过期仍能关单**、历史按序）；
   `tests/test_records.py` 补 `close()`。

**静态**

3. UI：`node --check js/app.js`。

**端到端**（无 LLM 也能验链路：spike `:8017` + APM `:7070`（`APM_ALLOW_LOOPBACK=true`）+ UI `:8123`）

4. 真实问题单「分析new」→ `completed` → 弹窗出现审批条；
   多方案切页签 → 「拒绝」填建议 → 轮询重启、状态回 `analyzing`、重跑完成后方案更新、
   **最下边历史计划出现「已拒绝」条目（带旧步骤与拒因）**；
   连拒 3 次 → `closed_manual` + 审批条只剩忽略/误报。
5. 「忽略」→ 该行 `state=closed`；「误报」→ 该行 `resolved` 且
   `aiops_false_positive_rate` 变化。
6. **会话过期（重启 spike）后点「忽略」→ 仍能关单**（不出现 404 死胡同）。
7. 无真会话时：拦截 `window.fetch` 注入合成 `/status` 快照，覆盖 `dismissed` 状态胶囊、
   审批条隐藏、历史条目渲染三类场景。
