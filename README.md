# aiops-agent-orchestration-spike

用 **AgentScope 2.0.3** 实现 `spike_plan_v4.md`（v4.0 设计）+ `docs/TRACE_CODE_DESIGN.md`
（trace_code 场景 delta）落地验证：一个**只读诊断 agent**（对标
[HolmesGPT](https://github.com/HolmesGPT-dev/holmesgpt) 的诊断内核），两个并存场景：
- **`k8s`（crashloop，旧路径）**：mock K8s 数据源；
- **`trace_code`（真 MCP，新）**：按 `case_type` 分派，经 **MCP 控制面**挂 app-log（`get_trace`/
  `query_logs`）+ git（只读白名单）server，`trace_id` → 代码缺陷 → 修复计划。

全程**只读**：agent 永不改代码/不 commit；配 **Stage 2 计划级审批** —— `/approve` 审批
`conclusion.recommended_fix` 是**终态签章、零下游动作**（无 apply_fix/执行器）。

> 实施计划见 [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md)（v4 里程碑 D0 / M0–M5）
> 与 [`docs/TRACE_CODE_IMPLEMENTATION_PLAN.md`](docs/TRACE_CODE_IMPLEMENTATION_PLAN.md)
> （trace_code Phase 0–5 改动全表）。

## 这 spike 在验证什么

- **诊断质量可信**：方法论 / 自规划 / 场景策略全部走提示词（v4 §7 三层），
  工具只是数据源。反幻觉 golden 用例只把唯一错误码 token
  `payment-checkout-db-conn-timeout-e9f2` 放在 checkout 容器日志，LLM 必须**查询到**才写得进结论。
- **工具可插拔**：加一个新集成 = 在 registry 加一个 `ToolSpec`，提示词零改动
  （`FunctionTool` 从函数签名自动推断参数 JSON schema）。
- **场景策略证据驱动（M3）**：runbook 默认**不预烤**进 system prompt；agent 拿到首条证据后按触发
  签名调 `fetch_strategy(name)` 现取（目录按 `case_type` 收敛，trace_code 取不到 k8s 的 crashloop）。
- **MCP 控制面（A3，照 multi-agent-workflow 移植）**：store 配置 → manager 按 server 名建 client，
  `load()` 不预连接、首个 run 才拉起；git 只读白名单 = MCP 配置层 `enable_tools` + spec 层双闸；
  只读 MCP 工具（readOnlyHint）自动 ALLOW。
- **sha 幻觉纪律（B7）**：plan 里出现的每个 sha（`base_commit` / `deployed_commit` /
  `already_fixed_by` / `EvidenceItem.commit`）必须来自工具返回；`repo_state` 显式喂 HEAD，解析后清掉
  幻觉 sha 并降 confidence。
- **计划级审批**：根因 + 修复建议由 LLM 产出，但**执行**要人审批；`approve` = 终态签章（零执行）；
  驳回 + feedback 会并入上下文**重跑诊断**，达上限 → `closed_manual` 交人工。

## 技术栈

- `agentscope==2.0.3`（python ≥3.12，`uv` 管理依赖）
- LLM：真实 **DeepSeek** `deepseek-v4-flash`（OpenAI 兼容路径，`DEEPSEEK_API_KEY`）
- FastAPI / pydantic / jinja2 / pyyaml；dev：pytest、httpx、ruff

离线机制测试用注入的 `ScriptedJsonModel`（确定性输出）；**任何诊断质量结论只走真实 DeepSeek**。

## 快速开始

```bash
uv sync --extra dev
cp .env.example .env   # 填入 DEEPSEEK_API_KEY
# 可选：真实 MCP server（trace_code 的 git/app-log 角色；url 给了自动加载，留空回退本地 stdio mock）
#   AIDIAG_GIT_MCP_URL=http://127.0.0.1:8100/mcp   AIDIAG_GIT_MCP_TOKEN=…   AIDIAG_GIT_MCP_NAME=git-search-mcp-server
#   AIDIAG_APPLOG_MCP_URL=http://127.0.0.1:8101/mcp  AIDIAG_APPLOG_MCP_TOKEN=…  AIDIAG_APPLOG_MCP_NAME=app-log-search-mcp-server
# client 名=真实 server 名 → 工具前缀 mcp__git-search-mcp-server__search_code；详见 .env.example 说明
```

### M0 连通性（无工具）

```bash
uv run python scripts/smoke.py
```

### Stage1 只读诊断 + Stage2 审批（REST）

起服务（host/port 读 `.env` 的 `AIDIAG_API_HOST`/`AIDIAG_API_PORT`，默认 `127.0.0.1:8017`）：

```bash
make api                    # = uv run python scripts/run_api.py [--reload]
```

新终端 —— k8s（crashloop，旧 mock 路径）：

```bash
# approve 路径：diagnose → completed → remediate(pending_review) → approve（终态签章，零执行）
uv run python scripts/stage2_demo.py --decision approve

# reject 路径：驳回带 feedback → feedback 并入重跑 → 看新 root_cause
uv run python scripts/stage2_demo.py --decision reject --feedback "DB 运维确认 checkout-db 正常，请复核配置指向"
```

trace_code（真 MCP：POST `/diagnose` 带定位键，`repo_state` 绑默认工作区，日志走 app-log MCP）：

```bash
curl -s -X POST localhost:8017/diagnose -H 'content-type: application/json' -d '{
  "case_type": "trace_code",
  "title": "[trace] order-checkout 下单链路失败",
  "description": "order-api 的 T-88f1a2 请求失败，需诊断",
  "app": "order-api",
  "repo": "checkout",
  "trace_id": "T-88f1a2",
  "environment": "prod",
  "time_window": "2026-09-08T01:05Z/01:40Z",
  "deployed": {"kind": "commit", "value": "<线上 sha>"}
}'   # → {"session_id": …}，轮询 GET /status/{id}
```

### golden live 跑分 & 测试

```bash
uv run python scripts/run_golden.py            # case A crashloop（k8s），2 runs
uv run pytest -m live tests/ -v                # 同上，pytest 形式（需 DEEPSEEK_API_KEY）
uv run pytest tests/ -m "not live"             # 离线机制 + REST 冒烟（46 用例，无需 LLM/key）
```

## 目录

```
aidiag/
  config.py           pydantic-settings（AIDIAG_*，兼容 DEEPSEEK_API_KEY）
  llm.py              build_model：OpenAIChatModel / ScriptedJsonModel（离线回退）
  domain.py           Issue（case_type/app/repo/trace_id/deployed…）/ DiagnosticConclusion
                      + sha 幻觉校验（sanitize_hallucinated_shas，B7）
  agents.py           build_agent + run_agent（AgentScope ReAct，可 hybrid：func specs + MCP client）
  json_utils.py       extract_json（剥围栏 + 首个平衡 {}）
  tools/
    registry.py       ToolSpec + FunctionTool + 权限白名单（DONT_ASK + ALLOW）+ hybrid Toolkit +
                      _filter_connected_mcps / allow_extra（防静默 DENY）
    datasources.py    mock K8s/日志/指标数据源；crashloop_env()（case_type:"k8s" 用）
    repo_state.py     repo_state：只读读 HEAD，显式喂 sha（B7 锚点）
    strategies.py     fetch_strategy(name)：证据驱动取 runbook（M3，allowlist 按 case_type）
    todo.py           plan_investigation / complete_task（TodoWrite 等价物 → session.tasks）
    progress.py       工具调用进度 recorder 协议 + ToolCallEvent
  mcp/                A3 控制面：store（配置）/ profiles（case_type→server）/ manager（生命周期）
  prompts/            methodology.j2（§7.1）/ planning.j2（§7.2）/ conclusion.j2（JSON 契约）/
                      runbook_registry.py + strategy_{crashloop,trace_dependency,trace_bug,trace_startup}.j2
  diag/
    session.py        SessionStore：状态机 + TTL + 驳回计数
    recorder.py       SessionRecorder（session.tool_calls + known_shas）+ MCPRecorderMiddleware（B6）
    runner.py         run_diagnose（后台跑；挂 recorder/middleware；结论过 sha 消毒）
  api.py              Stage1 /diagnose /status /stop；Stage2 /remediate /approve /health；
                      _resolve_specs_for 按 case_type 装配
scripts/               smoke / diagnose_once / run_golden / api_demo / stage2_demo /
                       mock_git_mcp / mock_applog_mcp（本地 stdio MCP 桩）
evals/golden_crashloop.yaml   k8s golden 数据集 + 期望断言
tests/                离线机制测试 + -m live 真实 DeepSeek 跑分
docs/IMPLEMENTATION_PLAN.md  D0 实施计划（自包含）
```

## 状态机速览

- **Stage1**：`POST /diagnose` → `analyzing`（后台 ReAct）→ `completed`（结构化结论，含
  `recommended_fix` 只读建议）| `failed`。
- **Stage2**：`POST /remediate` 把 `recommended_fix` 提交为 `pending_review` 计划；
  `POST /approve`
  - `approve` → `approved` = **终态签章、零下游动作**（无 apply_fix / 执行器；审批作用在计划，
    人批复的是"修复建议"，不是把改动打进环境）；
  - `reject + feedback` → `rejected`，feedback 并入上下文**重跑诊断**（`reanalyze_count+=1`）；
    达 `max_reanalyze`(默认 3) → `closed_manual`（终态，交人工）。
