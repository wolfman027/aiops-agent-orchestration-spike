# AIOps Spike 计划 v4.0 · 对标 HolmesGPT 诊断核心

基于 Python + AgentScope 2.x · DeepSeek · 以只读诊断为主验证对象 · 审批修复降级为可选 Stage 2

> 本文档是 `deepseek_html_20260908_52b674.html`（v3.0）的修订版。v3.0 的重心是"审批 + 执行修复"（remediation 引擎）；v4.0 将重心移回 **HolmesGPT 的诊断核心**——工具可插拔 + 结论可被 golden 用例验证——并保留审批修复作为独立可选阶段（你们产品的增量，不是 Holmes 的 core）。

> **⚠️ 状态更新（2026-09-09）**：v4 基座（D0–M5）与 **trace_code 场景（Phase 0–5）已全部实现并离线验证通过**（46 passed）。本文件的 v4 规划已落地；关于 trace_code 的实现语义（含下文 Stage-2 措辞"approved→executing→completed"已废止为 **approve=终态签章零执行**），以 **`docs/TRACE_CODE_DESIGN.md`** + **`docs/TRACE_CODE_IMPLEMENTATION_PLAN.md`** 为唯一事实源，二者已按实现更新。

---

## 0. v3.0 → v4.0 关键调整（为什么改）

| # | v3.0 | v4.0 | 理由 |
|---|---|---|---|
| 1 | 证明对象：审批/执行/状态机能跑通 | 证明对象：**诊断质量可信 + 工具可插拔** | HolmesGPT 的差异化在诊断 loop 之外的"工具集 + 可信结论"，不在编排框架 |
| 2 | 主流程终点 = `execute_fix_plan` 执行完毕 | 主流程终点 = **只读诊断报告（RCA 结论）交付** | repo core 引擎只读、不自动修复；修复是 MCP/Operator 那种显式可选外围 |
| 3 | 3 个硬编码 mock 工具函数 | **工具注册表抽象**：`(name, desc, JSON schema, exec, read_only/needs_approval)` | 加集成 = 加 spec、agent 零改动，这是 Holmes 的护城河 |
| 4 | 无诊断质量评价 | **golden 故障用例跑分**（反幻觉，断言唯一可发现值） | 结论可信只能靠 eval 回归，不能靠流程验收 |
| 5 | 系统提示词不写死步骤（无知识载体） | **可注入调查策略/knowledge 层**，开/关对比 | runbook/skill 是独立知识、不是代码——Holmes 关键机制 |
| 6 | 入口 = 裸 `error_log` 文本 | 入口 **issue 化**（source/severity/affected_resource/time_range）+ 结论写回 payload | 覆盖 HolmesGPT Sources→Destinations 概念 |
| 7 | 完整审批状态机（approve/reject/closed_manual/TTL） | 状态机**大幅简化**：只读循环无审批；TTL 降为运维件 | 审批那套工程大多退回 Stage 2，别再占主路径 |
| 8 | denial 用 `is_error=False` 喂回 | denial+feedback 对标 repo：以 **error 结果回灌 + 明确文案**；Day 0 两种实测 | repo 生产做法是把"denied by user + feedback"作为 tool error 回灌，靠文案而非标志位 |

**一句话定位**：把 spike 从「证明 AgentScope 能做带审批的修复 agent」改成「证明 AgentScope 能复刻 HolmesGPT 的诊断质量」。即便最终不产品化，交付物（工具 spec、golden 用例、策略模板）也能直接平移到真实 K8s/Prometheus/工单。

---

## 1. 定位、目标与非目标

### 目标（v4.0 要证明的 5 件事）

1. AgentScope 能承载**多步只读诊断循环**：证据收集（调工具）→ 根因分析 → 输出结构化结论。
2. **工具可插拔**：新增一个 mock 数据源 = 只加一条 tool spec，不改 agent 逻辑与提示词。
3. **结论可信**：注入已知故障时，agent 按合理顺序调用正确工具、结论引用证据并命中 golden 唯一值；对无关资源不误报。
4. **知识是独立杠杆**：同一故障，开/关调查策略文档 → 结论质量可对比、可调。
5. （可选 Stage 2）审批修复流程 + 驳回反馈重跑在此框架下成立。

### 非目标（本次明确不做，留待后续）

- 不接入真实 K8s/Prometheus/Grafana 等外部系统（工具全部 mock，但遵循真实 API 形态）。
- 不做 `ask`（自由问答）模式——本次只做 issue 驱动的深挖式诊断。
- 不写任何真实变更到环境；核心 loop 内**无 needs_approval 工具**。
- 不做多 Agent、不做完整 skills 知识库、不做 UI（仅 REST + 简单前端轮询）。

---

## 2. 与 HolmesGPT 核心要素的映射

| HolmesGPT core 要素 | v4.0 对应载体 | 状态 |
|---|---|---|
| agentic 诊断 loop（工具收集证据→根因→结论） | `/diagnose` 只读循环 | ✅ 主验证对象 |
| 工具集插件抽象（YAML/声明 + 薄封装 + 健康检查 + server-side 过滤 + 详尽报错） | 工具注册表 + 3 个 mock spec | ✅ 新增重点 |
| 只读边界 / 只读工具不触发审批 | 工具分类 `read_only`，核心 loop 无审批点 | ✅ 由设计保证 |
| 敏感工具人工审批 + denial+feedback 回灌重规划 | Stage 2：/remediate + /approve；denial 以 error 回灌 | ⚠️ Stage 2 |
| 默认方法论 system prompt（五 whys / 必查日志 / 反幻觉护栏） | §7.1 方法论层（常驻，模板化可开关） | ✅ 新增 |
| 自规划（TodoWrite：agent 自己拆解调查任务并实时展示） | §7.2 `plan_investigation` 工具 + 清单进 `/status` | ✅ 新增 |
| runbook / skill 场景知识注入（jinja2 prompt，按需+优先级） | §7.3 场景策略文档（`strategy` 参数） | ✅ 新增 |
| 结构化结论 / strict schema 防乱格式 | Pydantic 结论 schema | ✅ |
| 结论中给出修复建议（只读推荐，不执行） | `DiagnosticConclusion.recommended_fix`；执行在 Stage 2 审批 | ✅ 随结论产出 |
| Sources（Jira/PD/AM 事件驱动 entry） | `/diagnose` 入参 issue 化 | 🟡 简化实现 |
| Destinations（写回 Slack/Jira） | 结论 → 写回 payload 纯函数（不真发） | 🟡 简化实现 |
| 反幻觉 / eval 回归（tests/llm 纪律） | golden 故障用例跑分脚本 | ✅ 新增重点 |
| 防空转 guardrail（防重复同参调用）+ max_rounds | 防重复调用检测 + 轮数上限 | ✅ |
| 多 provider / 上下文压缩 / TTL 等工程件 | AgentScope 内置 + SessionStore | 🟡 够用即可 |

---

## 3. 架构总览

```text
[Issue/日志]  ──POST /diagnose──▶  ┌───────────────────────────────────────┐
                                  │  只读诊断 Agent（AgentScope）           │
                                  │   入口：issue 元数据 + 策略文档(可选)     │
                                  │   循环：调工具 → 看结果 → 再调 / 收敛      │
                                  │   Guardrail：防重复同参调用、max_rounds   │
                                  │   输出：Pydantic 结构化诊断结论            │
                                  └───────────────┬───────────────────────┘
                                                 │ conclusion
                        ┌────────────────────────▼─────────────────────────┐
                        │  诊断报告交付（核心终点）                           │
                        │    = 根因 + 证据 + 建议修复方案（只读，不执行）       │
                        │  可选：写回工单/Slack payload（不真发）             │
                        └────────────────────────┬─────────────────────────┘
                                                 │ （可选 Stage 2，需人显式发起）
                                     ┌───────────▼───────────┐
                                     │  /remediate 修复审批    │
                                     │  approve→执行           │
                                     │  reject+feedback→重跑   │
                                     └───────────────────────┘
```

- **核心阶段（Stage 1）**：只读、无审批、状态机极简。可独立验收。
- **扩展阶段（Stage 2）**：你们的产品增量（self-healing + 人批），复用同一 Agent/工具层，加执行类工具与审批状态机。默认 Optional，视时间决定。

---

## 4. 核心只读诊断工作流（Stage 1）

> 诊断 = 根因 + 证据 + **建议修复方案**。建议方案是**只读产物**：给出"改什么、怎么改、影响面、风险、回滚"，但**不执行、不改任何状态**；真正执行要由 Stage 2 审批该方案。这样既保住 Holmes 的只读边界，又不丢你们要的修复价值。

```text
[POST /diagnose {issue}] → 立即返回 {session_id, status:"analyzing"}
    │  前端轮询 GET /status
    ▼
只读循环（无人工介入）：
    0. （常驻）默认方法论 system prompt：五 whys、必查日志、hedge 反幻觉等护栏（§7.1）
    1. （可选）加载匹配的调查策略文档（§7.3）
    2. 先 plan_investigation 出调查任务清单，再取证（§7.2）——清单实时进 /status 当进度
    3. 自主调用只读工具收集证据：diagnose_pod / query_logs / query_metrics / …
       - 命中 Guardrail（重复同参调用）→ 提示词纠正
       - 工具返回必须含：执行的查询、参数、时间范围、完整错误/空结果上下文
       - 随进度实时更新任务状态（in_progress/completed/failed）
    4. 根因分析，收敛判断
    5. 生成建议修复方案（只读：动作 + 目标 + 预期效果 + 风险 + 回滚）
    6. 输出结构化诊断结论（证据 + 根因 + 建议修复方案 + 置信度 + 待确认项）
    │
    ▼
status: "completed"，返回 /diagnose 结果（含建议修复方案）
    │
    ├─（可选）写回 payload：把结论（含建议修复方案）渲染为工单/Slack 消息结构（不真发）
    └─（可选 Stage 2）审批"建议修复方案"，用户显式点 /remediate 才进入执行

异常 → status: "failed"（记录失败阶段与工具调用，便于复现）
```

---

## 5. 工具抽象层（v4.0 新增，最优先）

> 参照 repo 的 `Tool`/`Toolset` 形态做最小实现。**目标是证明"加集成不改 agent"**，不是复刻整套插件系统。

### 5.1 最小 spec

```python
# tools/registry.py
@dataclass
class ToolSpec:
    name: str
    description: str                 # 让 LLM 理解何时用它（含何时别用）
    parameters: dict                 # JSON Schema（name/type/required/enum/…）
    read_only: bool = True           # False ⇒ 触发 needs_approval（Stage 2 用）
    execute: Callable[[dict], str]   # 返回对 LLM 友好的文本

REGISTRY: dict[str, ToolSpec] = {
    "diagnose_pod": ToolSpec(
        name="diagnose_pod",
        description="获取 Pod 状态、重启次数、最近事件（诊断 CrashLoop 等）。read-only。",
        parameters={
            "namespace": {"type": "string", "required": True},
            "pod":       {"type": "string", "required": True},
        },
        read_only=True,
        execute=mock_diagnose_pod,
    ),
    # query_logs / query_metrics 同理
}
```

### 5.2 三条硬规则（直接抄 repo 的教训，代价极低）

1. **详尽报错**：工具出错时返回 `执行的查询 + 参数 + 时间范围 + 完整 API 错误`，让 LLM 自纠错。空结果要说明"查了什么、在哪查的"。
2. **集合限量/过滤**：mock 数据也要带 `limit`/`time_range` 之类的参数并强制 server-side 过滤，模拟 repo "绝不返回 unbounded 数据"（防 token 溢出）。
3. **read_only 分类是安全边界**：`needs_approval` 的工具在 Stage 1 不注册，注册表只暴露只读工具给 LLM。

### 5.3 验收动作

新增第 4 个 mock 数据源（如 `query_events`）= **只加 spec**，证明 agent 逻辑/提示词零改动即可调用。

---

## 6. 结论与反幻觉设计（golden 用例跑分）

### 6.1 结构化结论

```python
# domain/conclusion.py
class EvidenceItem(BaseModel):
    source_tool: str          # 哪个工具
    query: str                # 实际执行的查询/参数
    finding: str              # 关键发现
class RemediationStep(BaseModel):
    action: str               # 建议动作：具体命令/操作（只读建议，未经审批不执行）
    target: str               # 作用对象（资源/工具/参数）
    expected_effect: str      # 预期效果
    risk: str                 # 影响面与风险（审批人据此决策）
    rollback: str             # 失败时的回滚方式

class DiagnosticConclusion(BaseModel):
    summary: str              # 一句话结论
    root_cause: str           # 根因
    evidence: list[EvidenceItem]   # 必须引用证据，禁止裸断言
    recommended_fix: list[RemediationStep] | None  # 建议修复方案（只读，不执行）
    confidence: float         # 0-1
    open_questions: list[str] # 仍需人工/更多数据确认的点
```

LLM 输出走 strict JSON / Pydantic 校验，格式错误自动纠一次再失败。

### 6.2 golden 用例（反幻觉）

- 在 mock `query_logs` 注入**唯一错误串**（如 `payment-checkout-db-conn-timeout-e9f2`），在 `query_metrics` 注入特定异常形态。
- 断言：结论命中那个唯一值、引用证据、根因正确；且对**无关资源不误报**。
- 断言 `recommended_fix`：建议修复引用的资源名/唯一值与证据一致，不凭空捏造动作。
- 反例（禁止）：断言 `Pod CrashLoopBackOff` 这类模型能猜出来的泛化文案——防幻觉靠"只能靠查询发现的具体值"，这是 repo 纪律。
- 跑分脚本：`python run_evals.py` → 逐用例 PASS/FAIL + 结论稳定度（同用例跑 3 次看一致性）。

---

## 7. 诊断引导三层：方法论 + 自规划 + 场景策略（对标 Holmes 提示词体系）

> 对齐 repo：Holmes **不在代码里写死步骤序列**，而是三层——**默认方法论**在 system prompt、调查计划由 agent 用 TodoWrite **自规划**、场景步骤按需注入 **skill/runbook**（见 §2 映射）。spike 同样用三层，避免把"不写死步骤"做成"提示词里啥都不给"，导致浅答/幻觉。

### 7.1 默认方法论层（system prompt，常驻）

即使没有场景策略，也注入一套**方法论护栏**（对标 `generic_ask.jinja2` 的各 enabled 块；是反浅答/反幻觉的硬规则，不是按故障的流程脚本）：

- 五 whys：挖到最深根因；A 牵连 B 就继续查 B；找到根因后继续排查其他可能原因并**编号列出**。
- 必查应用日志："running / healthy" 不代表无问题；k8s 场景沿 ownership chain（deployment→rs→pod），crash 必 `describe`+logs。
- 反幻觉纪律：confirmed vs hypothesis 用 hedging（"possible/likely/may"）；看不到的值（Secret）明说无法核实、不猜；error message 本身是证据（"auth failed" ⇒ 用户存在，与"user not found"互斥）。
- 精确资源名/命名空间/版本，给具体命令，不给泛化示例；terse 输出。
- 用 prompt 模板文件的开关（仿 repo `{% if %}` 组件化）整体启停某条规则，便于 A/B。

### 7.2 自规划层（TodoWrite 等价物）

新增只读规划工具 `plan_investigation`（写会话内存，非真实变更）：

- 指令：涉及 ≥3 步或多任务（≥2 个用户任务）的调查**必须先出任务清单再取证**；任务含 `id/content/status(pending|in_progress|completed|failed)/priority`，实时更新、不批量。
- 作用一：把"怎么查"显式化 + 与"查了什么"一并留痕 → **调查可审计**。
- 作用二：清单直接喂给 `/status` 当**实时进度**（见 §8）——解决"只有 analyzing、前端没内容可展示"的缺口。
- 对齐 repo `TodoWrite`（`investigator/core_investigation.py`），AgentScope 侧用普通只读 tool 实现即可。

### 7.3 场景策略注入层（skill/runbook 等价物）

- 策略文档独立成文件（如 `strategies/crashloop.md`），不进代码：写该场景的合理取证顺序、常见根因清单、何时停止。
- `/diagnose` 可带 `strategy` 按需注入。**7.1 永远在场**，7.3 只是叠加的场景知识，二者不冲突。
- 支持多份策略按优先级注入（对标 repo skill priority）。
- **对比实验**：同一条 golden 用例，分组 ① 仅方法论 ② 方法论+策略 各跑一次，记录结论质量差异 → 证明"知识"是独立可调杠杆。
- Prompt 放模板文件（.j2/.txt），不拼在代码字符串里。

---

## 8. API 设计与状态机（v4 简化版）

### Stage 1 接口

| 接口 | 语义 |
|---|---|
| `POST /diagnose` | 入参 issue（见下）；立即返回 `{session_id, status:"analyzing"}` |
| `GET /status/{session_id}` | 轮询：`{status, tasks?, conclusion?, error?}`——`tasks` = `plan_investigation` 实时任务清单，前端当进度条展示 |
| `POST /diagnose/{session_id}/stop` | 手动中止（可选） |

issue 入参（v4 新增，issue 化）：

```json
{
  "source": "alertmanager",          // source 概念占位
  "severity": "critical",
  "title": "payment-service CrashLoopBackOff",
  "affected_resource": {"kind": "pod", "namespace": "prod", "name": "payment-service-0"},
  "time_range": {"from": "2026-09-08T09:00:00Z", "to": "2026-09-08T10:00:00Z"},
  "error_log": "…原始日志片段…",
  "strategy": "crashloop"            // 可选，注入策略文档
}
```

### Stage 1 状态机

```text
analyzing ──► completed     （核心终点：诊断报告交付）
    │
    └──► failed            （API/工具异常，记 failure_stage 便于复现）
```

- **无 pending_approval / executing / rejected / closed_manual**——那些属于 Stage 2。
- analyzing 期间 `/status` 返回 `tasks`（`plan_investigation` 实时清单，前端当进度展示）；`conclusion` 仅在 completed 出现。
- completed/failed 会话进入 TTL（默认 1h）自动清理；`SessionStore` 沿用 v3.0 实现即可（运维件，不占验收主路径）。

### Stage 2 接口（可选，产品增量）

| 接口 | 语义 |
|---|---|
| `POST /remediate/{session_id}` | 用户显式发起修复：把 `conclusion.recommended_fix`（建议修复方案）作为待审批计划提交 |
| `GET /status/{session_id}` | 追加状态：`pending_approval / executing / analyzing(驳回重跑) / closed_manual` |
| `POST /approve/{session_id}` | `{approved, feedback?}` 批准/驳回执行类工具 |

Stage 2 状态机与 v3.0 一致（approved→executing→completed；reject+feedback→reanalyze，≥3 次→closed_manual + 清理挂起 agent）。**审批对象 = 诊断结论里的 `recommended_fix`**：批准则执行该方案，驳回则重新分析并更新建议方案（而不是从零再来）。**denial 喂回建议对标 repo**：以 error 结果回灌，文案 `Tool execution was denied by the user. User feedback: <feedback>`，而非 `is_error=False`——具体以 Day 0 实测为准（两种都测）。

---

## 9. 会话存储（沿用 v3.0，降为运维件）

```python
class SessionStore:
    # v3.0 实现不变：dict + expires_at + TTL；cleanup() 定时清理
    # completed/failed 会话进入 TTL 倒计时；命中 Stage 2 的 agent 实例随会话一并释放
```

> TTL/内存管理是必要但不性感的工程；不要在验收标准里把它当主角。

---

## 10. 实施计划（7 天重排）

| 天 | 主题 | 交付物 / 验收 gate |
|---|---|---|
| **Day 0** | 环境锁定 + 最小验证 | AgentScope tool-calling 官方示例跑通；DeepSeek base_url 可用；**工具注册表最小实现**；两种 denial 喂法（error 回灌 vs 普通结果）各验证一次并记录结论 |
| **Day 1** | 工具层完成 | 3 个只读 mock 工具（详尽报错 + limit/time_range 过滤）；结构化结论 schema（含 `recommended_fix`）；prompt 模板文件化 |
| **Day 2** | 只读诊断循环 | `/diagnose` 从 issue 到"结论 + 建议修复方案"端到端跑通；§7.1 方法论 system prompt + §7.2 `plan_investigation` 自规划（任务清单进 `/status`）；防重复调用 guardrail + max_rounds；异常 → failed |
| **Day 3** | golden 用例 + 跑分 | 2-3 条注入故障用例（唯一值断言 + 不误报断言 + `recommended_fix` 一致性）；`run_evals.py`；同用例 x3 稳定度 |
| **Day 4** | 知识杠杆对比 | `strategies/crashloop.md`；**三组对比**（仅方法论 / 方法论+策略 / 无引导空白组）记录差异；issue 化入口完善 |
| **Day 5** | 写回 payload + 收尾 | 结论 → 工单/Slack 消息 payload 纯函数；新增第 4 个数据源只加 spec 验证可插拔 |
| **Day 6** | （可选 Stage 2）| 若进度允许：/remediate + /approve + 驳回重跑 ≤3 + 强制关单（直接用 v3.0 方案，day0 已验 denial 语义） |
| **Day 7** | 端到端 + 复盘 | 全 golden PASS；写结论报告（哪些机制有效/无效、AgentScope 的坑、真实集成的迁移路径） |

> 若只保证 Holmes-core 对齐：Day 0-5 即可独立验收；Day 6 的 Stage 2 是加分项不是必需。

---

## 11. 验收标准（v4.0 重写）

**诊断质量（主）**

- 注入故障 A：agent 依合理顺序调用正确工具、结论引用证据、命中 golden 唯一值根因；对无关资源不误报。
- 结论包含**建议修复方案**（动作/目标/风险/回滚），且与证据引用的资源一致、不凭空捏造动作。
- 同一条故障跑 3 次，结论稳定（无随机漂移/无幻觉新事实）。
- 新增一个 mock 数据源 = 只加 tool spec，agent 逻辑与提示词零改动即被调用。

**架构性**

- 核心 loop 内所有工具 `read_only=True`，**无审批点**即可完成诊断并产出建议修复方案（证明只读模型成立）；执行需显式进入 Stage 2。
- 工具出错/空结果时返回含查询、参数、时间范围的上下文（可被 LLM 用于自纠错）。
- 引导三层生效：仅方法论（无策略）即可完成合理诊断，不浅答/不裸答（§7.1 护栏）；`analyzing` 期间 `/status` 返回实时 `tasks`（`plan_investigation` 自规划清单，§7.2）；开/关场景策略能观测到结论质量差异（知识杠杆成立，§7.3）。

**工程**

- `/diagnose` 立即返回 `analyzing`；轮询到 `completed` 拿到结构化结论；异常 → `failed`。
- 防重复同参调用生效；max_rounds 生效；completed/failed 会话 TTL 清理。

**（可选 Stage 2）**

- approve → executing → completed；reject+feedback → reanalyze、次数递增；≥3 → closed_manual + agent 清理；denial 语义按 Day 0 实测结果落地。

---

## 12. 前置条件

- DeepSeek API Key 可用，base_url 正确；AgentScope 2.x external tool 示例跑通。
- Python 3.11+ 虚拟环境。
- 确认 AgentScope 参数名（`requires_confirmation`/`max_rounds`/tool 注册），以官方文档为准，不照抄伪代码。
- 确认 AgentScope 支持 strict JSON/Pydantic 式输出约束的方式（或自行校验回退）。
- Day 0 两种 denial 喂法均验证完成。

---

## 13. 风险与应对

| 风险 | 应对 |
|---|---|
| Agent 空转/循环失控 | max_rounds + 防重复同参调用（同参再调直接警告纠正） |
| 结论是泛化猜测（幻觉）而非查证 | golden 用例全部用"只能靠查询发现的唯一值"做断言 |
| 模型输出格式乱 | strict JSON + Pydantic 校验 + 自动纠错一次 |
| 工具错误信息不足以自纠 | 硬规则：所有工具返回含查询/参数/时间范围的错误与空结果上下文 |
| 上下文超长 | mock 数据即强制 limit/time_range + AgentScope 上下文压缩 |
| denial 语义不符合预期 | Day 0 实测两种喂法，用结果定实现 |
| Stage 2 超预算 | Stage 2 明确标 Optional，Day 0-5 先保证 Holmes-core 验收 |

---

## 14. 后续演进路线

**Spike 阶段（本次）**

- mock 工具 + 工具注册表 + golden 用例
- 单 Agent、只读诊断
- （可选）审批修复 Stage 2

**生产准备**

- 真实集成按注册表接入：K8s/Prometheus/Grafana/工单——每条 = 一个 spec + 一个薄客户端
- runbook/skill 知识库化（复用第 7 章策略注入机制）
- 结论写回真实工单/Slack；审批超时与审计日志
- golden 用例扩充为 eval 回归集，接入 CI（对标 repo tests/llm）

**高级演进**

- `ask`（自由问答）模式
- 多 Agent 协作 / 主动监控触发（对标 repo Operator 模式）

---

## 附录：v3.0 四条修正的去向

| v3.0 修正 | v4.0 处置 |
|---|---|
| ① 审批反馈走 ToolResultBlock，废止 `ConfirmResult(feedback=...)` | 保留；语义改为对标 repo（denial 以 error 回灌 + 明确文案），Day 0 实测定案 |
| ② `/diagnose` 提交即返 `analyzing` | 保留（Stage 1 与 Stage 2 一致） |
| ③ 驳回 ≥3 次 `UserInterruptEvent` + 清理 agent | 移入 Stage 2（Stage 1 无审批故不适用） |
| ④ 状态机补 failed + 会话 TTL | Stage 1 保留精简版（analyzing/completed/failed + TTL）；完整审批状态机移入 Stage 2 |
