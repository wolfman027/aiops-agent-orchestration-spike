# 场景设计：trace_id → 代码缺陷 → 修复计划（人批复）

> **状态**：设计定稿（2026-09-09 多轮讨论收敛）→ **Phase 0–5 已按本文档实现并离线验证通过**（46 passed，2026-09-09；live golden 残留见 `IMPLEMENTATION_PLAN.md` §F）。本文档是这轮讨论的唯一事实源，实现以其为准。
> **范围**：为 spike 引擎新增 `case_type=trace_code` 场景的完整设计 —— 只读诊断 + 计划审批 + M3 runbook 机制。与既有的 k8s `crashloop` 场景共存。
> **配套**：`../spike_plan_v4.md`（v4 基座）、`IMPLEMENTATION_PLAN.md`（v4 实施，含 trace_code Phase 0–5 改动全表 + §E 已核对项）、`../aidiag/`（已按本文档实现，未 commit）。

---

## 1. 产品模型（一句话）

**agent 全程只读**：拿到 app 日志查询上下文 + `trace_id` → 用只读工具诊断一个简单的 app bug → 产出**给人批复的修复计划**（不可写、非执行工件）→ 人 approve/reject；approve = 终态签章（无下游动作），reject + feedback 回灌 → 重新只读分析。**agent 永不改代码、不 commit、不 push。**

### 1.1 流程与状态机

```
输入(issue) ─▶ analyzing ─▶ completed(含 plan)
                                │
                ┌───────────────┴────────────────┐
             approve                          reject(+feedback)
                ▼                               ▼
            approved                          feedbacks 并入下轮
            （终态，无下游动作）                 user 上下文
                                                │
                                                ▼
                                    重新只读分析 → 新 plan → 回到 completed
                                    reanalyze_count ≥ 3 → closed_manual
```

- **reject 是 REST 层动作**，feedback 以文本进下轮 `user` 上下文（现有 `compose_user_content` 已支持）；**不是** v4 里"工具被拒 → ERROR 回灌"那种 denial 语义（本模型无写工具 → 无工具级 denial）。
- **plan 对象复用单条**：reject 重析后同一对象回到 `pending_review`（换新 steps），不新建 plan 记录层；审计靠 `reanalyze_count` + `feedbacks` + `raw_reply` 历史还原。

### 1.2 全程只读边界

- **git 工具只读白名单（进 toolkit）**：`log / show / blame / diff / grep / status / ls-files / branch -l`。
- **排除（spec 层就不注册）**：`commit / push / reset / checkout / restore / rebase / merge / clean / apply`；`fetch` 默认也排（会改本地 refs），确需时单独加回。
- **纵深防御**：若 git MCP server 自带只读 profile，优先在 server 端启用；ToolSpec 白名单是第二道闸。
- **执行器不存在**：`apply_fix`、`ExecutionResult`、`status="executed"` 全部删除（v4 Stage2 的 mock 执行在本文档模型下无意义——审批作用于计划，不作用于执行）。

---

## 2. 分层选择模型（case_type vs strategy）

| 层 | 谁决定 | 机制 |
|---|---|---|
| `case_type`（粗，trace_code / k8s） | **外层平台/编排**按可达 MCP 分派时定 | 决定装配哪套工具集 + 哪些 runbook 候选。**诊断 agent 不自选**——它无法凭空决定自己能不能摸到 k8s 工具 |
| `strategy` runbook（细，trace_bug / ...） | **诊断 agent 证据驱动、跑动中自取（M3）** | 见 §7。显式覆盖路径（`issue.strategy`）仅用于 golden/确定场景 |

**关键取舍**：策略不在建 agent 前预烤成默认——因为调用方本来就不该预先知道场景（只有 trace_id + app）。选择推迟到"证据刚够支持它的时候"。

---

## 3. 合并 schema（L1 请求 / L2 计划输出 / L3 审批状态）

标记：`[现]` 原样保留 ｜ `[改]` 改语义 ｜ `[新]` 本轮新增 ｜ `[删]` 建议删。

```python
# ========== L1. 请求输入 —— 平台 POST 的 Issue ==========
# 【纪律】只给 "去哪查 + 基线"，绝不携带 bug 签名/根因方向（防作弊 / 保 discovery）
class Issue(BaseModel):                                # [改] 从"告警"泛化为"诊断请求"
    id: str = "issue-1"
    case_type: Literal["trace_code", "k8s"] = "trace_code"   # [新] 默认 trace_code（接受对 k8s 旧调用方的破坏，需显式传 "k8s"）

    title: str = ""
    description: str = ""           # 可只写 "诊断 trace T-3f2a... 的失败请求"

    app: str | None = None          # [新] 日志源身份（agent 第一跳去哪拉）
    repo: str | None = None         # [新] git 仓库；app→repo 一一对应时可省略
    trace_id: str | None = None     # [新] 日志关联键（天然强过滤，防 token 溢出）
    environment: str | None = None  # [新] 多环境切配置/凭据
    time_window: str | None = None  # [新] 可选提示，如 "2026-09-08T01:05Z/01:40Z"
    deployed: DeployedRef | None = None  # [新] 线上基线（优 = 平台直接给 commit）

    namespace: str = ""             # k8s legacy 字段；trace_code 下留空
    strategy: str | None = None     # 显式 runbook 名；trace_code 默认不设（走 M3 fetch）

    trigger: Literal["manual", "log", "metric"] = "manual"  # [新] 来源门（provenance）
    log_excerpt: str = ""           # [新] log 门：监控给的日志摘录（提示，须查证；非结论）
    metric_alert: MetricAlert | None = None  # [新] metric 门：告警内容


class MetricAlert(BaseModel):                          # [新] metric 门输入（只描述"什么指标、多少、什么条件"）
    metric: str = ""
    value: str = ""
    threshold: str = ""
    resource: str = ""               # pod / 服务 / deployment
    description: str = ""            # 原始告警文本（可选）


class DeployedRef(BaseModel):                          # [新]
    kind: Literal["commit", "image"]
    value: str                        # commit sha 或 image tag（如 checkout:1.4.2）
    source: Literal["platform", "log_meta", "git_tag"] = "platform"


# ========== L2. 计划输出 —— agent 只读诊断的终点 = 给人批复的 plan ==========
class EvidenceItem(BaseModel):                         # [现] 语义不变
    source: str = ""                 # app_log / git_log / git_blame / git_grep ...
    query: str = ""                  # trace_id / repo+pattern / commit 区间 ...
    finding: str = ""
    supporting_text: str = ""        # 原文引用，保留唯一错误标识
    commit: str | None = None        # [新] 可选：这条证据取自哪个 commit（deployed 还是 base）


class RemediationStep(BaseModel):                     # = FixOption.steps[] 的一项
    action: Literal["code_fix", "config_change", "restart", "scale", "db_action"] = "code_fix"
    target: str = ""                 # [改] infra=资源名；code=文件路径（须与 git 返回一致）
    change: str = ""                 # [新] 纯文本"怎么改"（code_fix 必填）
    expected_effect: str = ""
    risk: Literal["low", "medium", "high"] = "medium"
    rollback: str = ""               # code_fix 通常 "git revert <base>" 或撤该 commit
    suggested_diff: str | None = None  # [新] 示意 diff（给人看、无执行语义）；仅 action∈{code_fix,config_change}


class FixOption(BaseModel):                            # [新] = recommended_fix[] 的一项（一个方案）
    title: str = ""                  # 短标签，如 "空安全 trim（assignee 可空）"
    applies_when: str = ""           # 选择本方案的前提条件（消歧关键）；单方案时写主要适用场景
    recommended: bool = False        # 模型首选（恰一个为 true；系统确定性兜底）
    reason: str = ""                 # 一句话：为何推荐本方案（或该备选为何存在）
    steps: list[RemediationStep] = Field(default_factory=list)
# 方案有序，对外一律按 1-based 记作 方案1、方案2…。根因依赖未定前提时给互斥多方案
# （各写 applies_when），否则只给一个方案——避免把"二选一"读成"两个都做"。


class DiagnosticConclusion(BaseModel):                 # = plan 本体，给人批复
    root_cause: str = ""
    confidence: Literal["high", "medium", "low"] = "medium"
    summary: str = ""

    deployed_commit: str | None = None  # [新] 证据锚点：日志栈帧对应的线上 sha
    base_commit: str | None = None      # [新] diff 落点：默认 HEAD tip；必须显式给出
    base_note: str = ""                 # [新] deployed≠base 时写 case a/b/c 判定 + 迁移描述
    already_fixed_by: str | None = None # [新] 非空 = 无需新改动，已由 commit X 修复；recommended_fix 应为 0 个方案

    evidence: list[EvidenceItem] = Field(default_factory=list)
    recommended_fix: list[FixOption] = Field(default_factory=list)   # [改] 有序方案（原为扁平 list[RemediationStep]）


def preferred_option_index(options: list[FixOption]) -> int | None:
    """确定性选主（0-based）：第一个 recommended=True；零标记 → 0；空 → None。绝不抛错。"""


# ========== L3. 审批状态（aidiag 管）—— approve 是终态标签，零下游动作 ==========
class RemediationPlan(BaseModel):                      # [改] 语义从"执行记录"改为"计划审批"
    status: str = "pending_review"   # pending_review | approved | rejected | closed_manual   [改] 去掉 executed
    steps: list[RemediationStep] = Field(default_factory=list)  # = 选定 FixOption.steps 的快照
    option_index: int | None = None  # [新] 1-based，与「方案N」对齐；None=未知
    option_title: str = ""           # [新] 选定方案的 title（/status 回显）
    last_feedback: str = ""
    submitted_at: float = Field(default_factory=time.time)
    decided_at: float | None = None

# ExecutionResult / RemediationPlan.executions / status="executed"  [删] 无执行器，全部移除
# Session 本身基本不动：analyzing|completed|failed + reanalyze_count + feedbacks
```

**必须三件同步改**：`aidiag/domain.py` 模型、`conclusion_from_dict` 宽容解析、`aidiag/prompts/conclusion.j2` 契约文本（让 LLM 知道要输出 base_commit / already_fixed_by / change / suggested_diff）。

**改动输出形状时另需同步**（如 `recommended_fix` 扁平 → `FixOption` 多方案）：`aidiag/api.py` 的
`/remediate`（方案选择器 `{"option_index": N}`，1-based；只快照选中方案的 `steps`）、
`RemediationPlan.option_index/option_title`、`SessionStore.snapshot` 的 `remediation_option`，
以及 `scripts/run_golden.py` 的 target 断言（须穿透 options → steps）。

### 3.1 调研任务台账（`tasks`）的终态归一纪律

`Session.tasks`（`InvestigationTask`）是**模型自报账本**：`plan_investigation` 写计划、
`complete_task` 是**唯一**写 `status="done"` 的地方。而**最终结论走的是另一条通道**——runner 解析
模型最后那条 JSON。于是"收敛根因并给出修复方案"这类**收尾步骤**永远没人置 done，
`completed` 的会话却显示 `todo`。

**契约（由 `diag/session.py::finalize_terminal_tasks` 确定性保证，不靠提示词自觉）：**

> `session.status` 落到终态 ⇒ `tasks` 中不存在 `todo` / `in_progress`。

| session 终态 | 残留 todo/in_progress → | 语义 |
|---|---|---|
| `completed` / `approved` | `done` + `derived=True` | 有结论，收尾步骤**随结论一并完成**（derived 供 UI 灰显，不冒充模型显式完成） |
| `failed` / `closed_manual` | `cancelled` | **无结论**，步骤没跑完——绝不置 `done`，不伪造完成 |

调用点必须覆盖**每一条**落终态的路径：`diag/runner.py`（成功/异常两处）、`api.py` 的
`/stop` 与 `_schedule_run` 的 resolve 失败分支。`InvestigationTask.derived` 与
（新增的）`cancelled` 仅作**加法**扩进 `/status` 快照，不破既有字段。

前端（Problem Center「View diagnosis」）在同一契约上做**显示侧兜底**：终态却仍见
`todo`/`in_progress`（旧会话/异常路径）时不再印原始枚举，而是按上表派生成
「随结论完成」/「已中止」；非终态照常显示 待办/进行中/已完成。

---

## 4. 版本对齐与反幻觉（岔口 2）

### 4.1 原则
- **证据锚在部署 commit**：日志栈帧的 file:line、现场代码以 `C_deploy` 为底用 `git show` 核对——它是"读过去"的基准。
- **修复瞄准 HEAD**（未来改动要落的那条线）：plan 的 diff 以人下一步会编辑的代码为底——它是"写将来"的基准。
- 一句话：**部署 commit 决定信什么证据，HEAD 决定 diff 长在哪。**

### 4.2 三种情况（diff 动笔前必须判定）
| 情况 | 事实 | plan 动作 |
|---|---|---|
| a. 代码没动 | C_deploy 该文件与 HEAD 一致 | 直接以 HEAD 为底出 diff |
| b. 上游已修 | `C_deploy..HEAD` 间错误已被修掉 | **不写 diff**，填 `already_fixed_by=修复 commit`，recommended_fix 应为 **0 个方案**。判定靠 `git log -S "<错误签名>" C_deploy..HEAD -- <file>` |
| c. 改了但还在 | 重构挪位/改法，错误仍复现 | 以 HEAD 为底，但**禁用旧 file:line**——用错误签名在 HEAD 上重搜定位，再出 diff |

### 4.3 base_commit 纪律（反幻觉的硬约束）
- **agent 只做选择/引用，绝不拼 sha**：任何出现在 plan 里的 sha（`base_commit` / `deployed_commit` / `already_fixed_by` / `EvidenceItem.commit`）**必须是某次工具返回里出现过的 sha**。
- 因此 HEAD 由 **git MCP 的仓库状态工具**显式返回（真 server `get_repo_status` / mock `git_status`），LLM 从中引用；不再有本地 repo-state function 工具（避免"本地 checkout 必须与 server 同源"的隐性耦合）。
- `conclusion_from_dict` 加校验：plan 里的 sha 若从未在工具输出中出现 → 当作幻觉处理（降级 confidence / 拒收）。这复用并推广了既有的"`recommended_fix[].steps[].target` 只能引用工具返回中出现过的资源"铁律。
- diff 的 before 侧必须是 agent 在**目标 commit** 下 `git show` 拉到的真实内容；在 HEAD 定位不到与日志一致的地方 → **明说 + 降 confidence**，不硬凑。

---

## 5. 输入契约纪律（岔口 3）

- **C_deploy 三层给法（按确定性排）**：
  1. **优**：平台直接传 commit（平台管部署，本就知道线上 SHA）。**强倾向此层。**
  2. 中：传 image tag / app 版本 → agent 用 git 只读 `ls-remote --tags` 映射到 commit（多一跳，仍只读精确）。
  3. 差：啥都没有 → agent **必须明说"部署 SHA 未知、无法核对线上/HEAD"并降 confidence**；且 case-b（上游已修）检测失效。
- **只给"去哪查"，不给"答案"**：输入里的 app/trace/repo/deployed 全是 WHERE + base 元数据；错误签名、可疑代码、根因方向**一个字符都不能进输入**——否则亲手喂掉 discovery，golden 防幻觉断言全废。
- **log 请求形态**：app + trace_id 都给定（agent 第一跳 = 拉这条 trace）。不做服务发现/路由。
- **app-log MCP 查询原语（已确认）**：既支持 `trace_id` 拉单条 trace，也支持 **app + 时间窗（无 trace）** 拉日志——后者支撑 `trace_startup` 等无 trace 场景。
- **入口按"来源"分门（门 ≠ case_type）**：触发源是采集系统，不是人。**门（source）** 决定请求体形状与 provenance（`trigger`），**case_type** 只决定后端绑什么数据源。`POST /diagnose` = 通用/人工门（`manual`）；`POST /diagnose/logs` = 日志采集门；`POST /diagnose/metrics` = 指标采集门。三门汇入同一 `_start()`，后端诊断流程一致；两个专用门不暴露 `case_type`（都映射 `trace_code`）。`/status` 回显 `trigger`。
- **seed 是"提示"不是"结论"**：`log` 门随体带上日志摘录（`raw_logs`/`log_excerpt`），`metric` 门带上告警内容——**这是触发源给的证据**，渲染进 user 上下文时**必须显式标注"仅供参考 + 须用日志工具取完整日志/栈帧核对"**，且结论引用它时 `evidence.source` 记 **`trigger_log`**（区别于工具返回的 `app_log`）。否则模型会把异常复述成结论，跳过"定位 file:line → 读代码 → 定方案"这跳（golden 防幻觉断言只对**不带 seed** 的 `/diagnose` 门成立）。日志摘录**建议只给异常 message、不含 `at` 栈帧**，保留 agent 自查 file:line 的价值。

---

## 6. M3 runbook 机制（岔口 4 定案）

### 6.1 形态
- **system prompt 保持瘦**：只有 `methodology.j2` + `conclusion.j2` + `planning.j2`，**默认不再预烤任何 strategy**。
- 新增只读工具 **`fetch_strategy(name)`**：allowlist 校验 → 渲染对应 runbook `.j2` 作为**工具结果**返回；非法名 → 错误返回 + 可用清单（沿用工具错误纪律，模型编名字能自纠）。
- runbook 进 **registry**（单一事实源）：`name → .j2 文件 → 触发提示`，同时喂两处：① fetch 渲染用 ② `fetch_strategy` 的工具 description 当"目录"（模型靠 description 知道有哪些、何时取）。
- 目录按 `case_type` 过滤：trace_code 上下文不广告 k8s 的 crashloop，减少误取。
- `planning.j2` 加 fetch 指针一行："取到首条证据、判定错误类别后，命中某场景就 `fetch_strategy(name)`"。
- recorder/status 无需改——fetch 就是普通工具调用，`/status` 天然可见"agent 拉了哪个 runbook"。
- **M1 保留为覆盖路径**：`issue.strategy` 显式设置时仍预烤进 system（golden / 确定场景用）。两个路径并存，`build_system_prompt` 只在显式时烤。

### 6.2 纪律
- **fetch 必须证据驱动**：靠已拿到的证据签名触发，不是猜场景硬拉；没匹配还 fetch = 浪费一轮 + 误导方向。
- 多 runbook 可多次 fetch；顺序即优先级（v4"可多份带优先级"在 M3 下退化为模型行为，无系统排序）。

---

## 7. trace_code 首批 runbook 注册表

`case_type=trace_code` 默认候选全集；`trace_bug` 为兜底（不 fetch 也能靠 methodology + 契约出活，只是更泛）。

| name | 触发证据签名 | 主查询原语 | 链条要点 |
|---|---|---|---|
| **`trace_bug`**（兜底） | trace 有异常签名，不属特类 | trace_id | 失败请求 → 日志异常原文 file:line → git grep/blame → case a/b/c → code_fix + 示意 diff |
| **`trace_dependency`** | connect/socket/pool 超时、connection refused、handshake 到 host | trace_id | host:port 锚点 → 代码地址 vs 服务发现 → **依赖挂** vs **配置错** → target 可离开仓库（依赖方 / config_change，非 code_fix） |
| **`trace_startup`** | 应用起不来 / 反复重启 / 初始化即失败 | **app + 时间窗（无 trace）** | 启动期日志（init/配置/连接池）→ 错误原文 → git 定位启动代码 → code_fix/config_change |
| `trace_slow`（stretch） | 上游/客户端报超时但应用无异常 | trace spans + 时间窗 | 耗时热点 → 慢查询/N+1/锁 → perf 型 code_fix |

### 边界 / 排除
- **进程级内存/OOM 不建独立 runbook**：OOMKilled（SIGKILL）在纯日志里无痕迹。日志若现 `OutOfMemoryError` 栈，按异常类别归入 trace_startup / trace_bug 子分支；要证实 137/events 需回 k8s case_type。
- **`trace_startup` 证据弱于 k8s**（无 exit_code/events）：怀疑资源型崩溃时 runbook 须提示"需 k8s 佐证"并压低 confidence（反幻觉落点）。

---

## 8. golden / 测试策略（岔口 4 子决策 a）

- **既有 golden（如 crashloop）保留显式 `issue.strategy` 覆盖路径** = 确定性、零回归。
- **另加一条"发现型"golden**：不预烤 strategy，断言模型**真的调了 `fetch_strategy(<name>)`**（用 `include_tool_calls` 类断言），用来测 M3 本身。
- 不迁移全部成发现型（强但 flaky——模型忘 fetch 就挂）。
- 分层验证不变：① 离线机制单测（含 sha 幻觉校验）② REST 冒烟 ③ live 真 MCP。

---

## 9. 对现有 spike 的改动清单（实现时的 delta，尚未动）

| 文件 | 改动 |
|---|---|
| `aidiag/domain.py` | Issue 泛化 + DeployedRef + RemediationStep.change/suggested_diff + DiagnosticConclusion 四新字段 + EvidenceItem.commit；删 ExecutionResult |
| `aidiag/diag/session.py` | RemediationPlan 状态去 executed、去 executions |
| `aidiag/prompts/conclusion.j2` | 契约加 base_commit/deployed_commit/base_note/already_fixed_by + change/suggested_diff |
| `aidiag/prompts/planning.j2` | 加 fetch_strategy 指针行 |
| `aidiag/prompts/methodology.j2` | 基本不动（大方向适用；如需可加一句"file+commit 是精确命名对象"） |
| `aidiag/prompts/__init__.py` | build_system_prompt 默认不烤 strategy（仅显式时）；registry 单一来源 |
| `aidiag/tools/` | + fetch_strategy 工具；runbook registry 模块（HEAD 不再用本地 repo-state，改走 git MCP 仓库状态工具） |
| `aidiag/diag/runner.py` | 走 registry + fetch 工具集装配（trace_code 时） |
| `aidiag/prompts/strategy_*.j2` | 新增 trace_bug / trace_dependency / trace_startup（/ trace_slow） |
| `tests/` | 发现型 golden + sha 幻觉单测 |

---

## 10. 待办 / 悬而未决（实现前要补）

- [ ] app→repo 映射放哪层配置（toolset config 还是外层平台）。
- [ ] MCP 传输形态（stdio / HTTP）——影响 ToolSpec 包装（路径 A 包装 vs AgentScope 原生 MCPTool 路径 B；默认走 A：包成 ToolSpec.func，复用 recorder/权限白名单/read_only）。
- [ ] `trace_slow` 是否纳入首批（默认 stretch）。
- [ ] `case_type` 破坏性改动对现有 crashloop golden/冒烟 fixture 的更新（显式传 `"k8s"`）。

---

## 11. 参考

- `../spike_plan_v4.md` — v4 基座（只读诊断核心 + Stage2 审批）。
- `IMPLEMENTATION_PLAN.md` — v4 实施计划（D0 起）。
- `../aidiag/` — 现有代码（本次讨论未改动）。
- HolmesGPT repo（只读方法论权威）：`/Users/h.a.hu/accenture/analysis/HolmesGPT/holmesgpt` — `fetch_skill` 即 M3 的原型。
