# 实施计划：AgentScope 只读诊断核心（对标 HolmesGPT）

> 本文件把 `../spike_plan_v4.md`（v4.0 设计）落成可执行的 Python 实施计划。目标：在绿地 spike 仓库内实现一个 **AgentScope 只读诊断 agent**，证明「诊断质量可信 + 工具可插拔」，尽量按 v4 全量（含可选 Stage 2 修复审批）。
> 语言：中文。所有文件均建在本仓库根目录下。**不改动 / 不依赖 agentflow 与 HolmesGPT 两个仓库的代码。**

## 1. 范围与硬性决定

- **技术栈**：`agentscope==2.0.3`（pin）、python ≥3.12（用 `uv` 建 venv）、DeepSeek（`deepseek-v4-flash`，OpenAI 兼容路径）、FastAPI、pydantic、pydantic-settings、jinja2、pyyaml、python-dotenv。dev：pytest、pytest-asyncio、httpx、ruff。
- **LLM 验证只用真实 DeepSeek**（env `DEEPSEEK_API_KEY`）。诊断质量结论一律 live；仅纯机制单测可注入假 responder（ScriptedJsonModel）。
- **Stage 1（主）**：只读诊断 → 结构化 `DiagnosticConclusion`（含 `recommended_fix` 建议修复，只读、不执行）。状态机仅 `analyzing → completed | failed` + TTL。
- **Stage 2（产品增量）**：计划级审批。`/approve` 审批 `conclusion.recommended_fix`；批准后单独执行确定性 `apply_fix`（写回执行结果，**不真改环境**）；驳回 + feedback → 用反馈重跑诊断更新方案，≤3 次 → `closed_manual`。**不做运行时工具审批**。

## 2. 已验证的 AgentScope 2.0.3 惯用法（照此实现，勿猜）

来源：`multi-agent-workflow` repo（agentflow）`agents/scopes.py` / `agents/mcp.py` / `agents/tools.py` + venv 校验。

- **不调用 `agentscope.init`**。直接构造：
  ```python
  OpenAIChatModel(credential=OpenAICredential(
      api_key=..., base_url="https://api.deepseek.com/v1"),
      model="deepseek-v4-flash", stream=True)
  ```
- 工具：`async def` 返回扁平 dict；`FunctionTool(func=fn, name=..., description=..., is_read_only=True)`（参数 JSON schema 从函数签名自动推断）；`Toolkit(tools=[...])`。
- Agent：`Agent(name=..., system_prompt=str, model=..., toolkit=..., state=AgentState(permission_context=ctx), react_config=ReActConfig(max_iters≈10))`。整段 ReAct 循环在 `await agent.reply(UserMsg(name="user", content=str))` 内部跑完。
- 权限白名单（关键坑）：`PermissionContext(mode=PermissionMode.DONT_ASK)`，并对每个已注册工具加
  ```python
  allow_rules[tool] = [PermissionRule(tool_name=tool, rule_content=None,
      behavior=PermissionBehavior.ALLOW, source="tenant/local")]
  ```
  **allow 规则必须与 toolkit 完全一致，否则工具被静默 DENY**。`is_read_only=True` 的工具可免规则自动 ALLOW。
- JSON 输出：系统提示词要求「仅输出严格 JSON」，用宽容 `extract_json`（剥 ``` 围栏 + 找首个平衡 `{}` + `json.loads`）解析；不用 AgentScope 结构化输出 API。

## 3. 目标文件布局

```
docs/IMPLEMENTATION_PLAN.md   # 本文档
pyproject.toml, .env.example, .gitignore(追加)
aidiag/
  __init__.py
  config.py          # pydantic-settings；deepseek key/base_url/model
  llm.py             # build_model(OpenAIChatModel)；key 空时回退 ScriptedJsonModel（仅机制单测）
  json_utils.py      # extract_json（宽容）
  domain.py          # Issue / EvidenceItem / RemediationStep / DiagnosticConclusion / RemediationPlan / Pydantic
  tools/
    __init__.py
    registry.py      # ToolSpec + build_toolkit（FunctionTool + allow 白名单 + 进度包装）
    datasources.py   # mock 数据源 + 故障数据集注入（含 golden 唯一 token）
    todo.py          # plan_investigation（TodoWrite 等价物）
    progress.py      # 工具调用事件 -> session sink（供 /status）
  prompts/
    methodology.j2   # §7.1 默认方法论（移植 Holmes generic_ask 硬规则）
    conclusion.j2    # 输出 JSON 契约 + recommended_fix schema hint
    strategy_crashloop.j2  # §7.3 场景策略（可选注入）
  agents.py          # build_agent / run_agent -> dict
  diag/
    __init__.py
    session.py       # SessionStore：状态机 + TTL + tasks + tool_calls 事件
    runner.py        # diagnose(issue, strategy?) -> conclusion（后台任务 + 进度写入）
  api.py             # FastAPI：Stage1 /diagnose /status(/stop)；Stage2 /remediate /approve
scripts/
  smoke.py           # M0：DeepSeek 连通性（无工具）
  run_golden.py      # golden 用例 live 跑分
tests/
  conftest.py
  test_session.py test_registry.py test_api_mechanics.py   # 无 LLM（假 responder）
  test_golden_live.py   # -m live：真实 DeepSeek
evals/golden_crashloop.yaml  # 数据集 + 期望断言（case A）
README.md            # 实现说明（覆盖原 66B 占位）
```

## 4. 诊断引导三层（提示词体系）

| 层 | 文件 | 等价物 | 内容要点 |
|---|---|---|---|
| 7.1 默认方法论（常驻） | `prompts/methodology.j2` | Holmes `generic_ask.jinja2` | 五 whys；必查应用日志；k8s ownership chain；hedge 反幻觉；error message 即证据（引原文）；精确资源名；terse |
| 7.2 自规划 | `tools/todo.py` `plan_investigation` | Holmes `TodoWrite` | ≥3 步必先出任务清单；任务状态实时更新；喂给 /status 当进度 |
| 7.3 场景策略（按需叠加） | `prompts/strategy_crashloop.j2` | skill / runbook | `strategy` 参数按需注入，可多份带优先级 |

7.1 永远在场；7.3 只在指明场景时叠加。

## 5. 里程碑与验收（顺序执行，各自可验）

| # | 内容 | 验收 |
|---|---|---|
| **D0** | 生成本文档（已完成） | 仓库 `docs/` 内有实施计划 |
| **M0** | `uv` venv(py3.12) + pin 依赖；`config.py` + `llm.py` + `json_utils.py`；`scripts/smoke.py` DeepSeek 无工具回复 | 网络/key/模型通 |
| **M1** | registry + mock datasource（crashloop 数据集，logs 含唯一 token `payment-checkout-db-conn-timeout-e9f2`）+ methodology/conclusion 提示词 + agents.py → `DiagnosticConclusion` | live 一次：JSON 合法、含证据 |
| **M2** | `plan_investigation` + progress + session/runner + `evals/golden_crashloop.yaml` + `run_golden.py` | 断言唯一 token 命中 / `recommended_fix` 与证据资源一致 / 不误报 / 同用例 2 次稳定 → PASS |
| **M3** | FastAPI `/diagnose`（立即返 analyzing）→ 后台 runner；`/status` 返 `{status,tasks,tool_calls,conclusion?,error?}`；`/stop`（可选） | curl 走通 analyzing→completed |
| **M4** | Stage2：`/remediate` + `/approve`（approved→执行确定性 apply_fix；rejected+feedback→重跑诊断并入 feedback ≤3→closed_manual）；TTL sweeper | approve 成功 / reject 重跑 / ≥3 closed_manual |
| **M5** | tests/（无 LLM 机制测试 + `-m live` golden）、README、`ruff`、`.env.example` | 单测绿 + live golden PASS |

## 6. 关键实现说明

- **进度 / tasks 实时性**：`Agent.reply` 一次调用内部跑完整循环，无法中途流式取中间状态。方案：每个工具经 registry 的**日志包装函数**执行（先记 session sink 再调真函数）；`plan_investigation` 同步更新任务状态 → `/status` 从 session sink 读。诊断在后台 asyncio 任务跑，REST 轮询。
- **结论稳定 / JSON**：结论契约写死在 prompt（含 schema hint）；`extract_json` 失败自动纠一次、再失败置 `failed` 并保留原始文本便于排查。
- **反幻觉 golden**：数据集只把唯一 token 放在该故障的 logs；断言必须「只能靠查询发现的值」，禁止断言可猜文案；注入数据不进提示词。
- **Stage2 驳回重跑**：`reanalyze_count` 计入 session；feedback 文本并入下一轮 user 上下文；达上限 → `closed_manual`（终态）。

## 7. 验证方式（真实 DeepSeek）

```bash
# 依赖环境
export DEEPSEEK_API_KEY=...   # 或写 .env

# M0 连通性
uv run python scripts/smoke.py

# golden live
uv run python scripts/run_golden.py
uv run pytest -m live tests/test_golden_live.py

# API
uv run uvicorn aidiag.api:app
# POST /diagnose -> {session_id, status: analyzing}
# GET  /status/{id} 轮询 -> completed 出 conclusion
# Stage2: /remediate/{id} -> /approve -> approved/executed ; rejected+feedback -> 重跑

# 机制单测（无 LLM、离线）
uv run pytest tests/ -m "not live"
```

## 8. 风险与对策

- AgentScope 2.0.3 个别 API 细节与记录不符 → M0 以 venv 内实测为准（已按安装版核过签名，风险低）。
- live DeepSeek 慢 / 耗 token → `max_iters` 收敛（≈10）、数据集小、golden 少而准；失败保留原始输出可复盘。
- 后台任务 + 轮询一致性 → SessionStore 单线程事件循环内访问 + 状态机收口。
