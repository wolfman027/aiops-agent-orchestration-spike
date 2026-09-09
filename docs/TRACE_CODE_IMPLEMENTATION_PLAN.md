# 实现计划：trace_code（只读诊断 → 修复计划给人批复）· 对接真实 MCP server

> **状态**：实现计划定稿（2026-09-09），**尚未实现、未 commit**。
> **事实源**：`./TRACE_CODE_DESIGN.md`（设计定稿）+ 本文档（运行期接线 + 改动计划 + A3 真 MCP 对接）。
> **参考**：multi-agent-workflow（`acc-aiops-platform-zjb/multi-agent-workflow`）的 MCP 控制面——`agentflow/api/mcp_store.py`、`agentflow/agents/mcp_manager.py`、`agentflow/agents/mcp.py`、`agentflow/agents/agent_config.py`、`agentflow/agents/scopes.py`、`agentflow/agents/runner.py`、`agentflow/api/app.py`、`scripts/mock_mcp_server.py`。
> **配套**：`../spike_plan_v4.md`（v4 基座）、`IMPLEMENTATION_PLAN.md`（v4 实施）、`../aidiag/`（现有代码）。

---

## A. 运行期序列设计

### A1. 现状（k8s crashloop，mock 数据源）

- 工具集是**启动期单例**：`app.state.specs = _default_specs()`（`api.py:47`），由 `crashloop_env()` + `build_datasources()` + `bind_tools()` 组装成 `list[ToolSpec]`。**不按请求分派。**
- `POST /diagnose`（`DiagnoseRequest{title, description, namespace, strategy}`）→ `STORE.create(Issue)` → `_schedule_run(session)` 投后台 asyncio task。
- `run_diagnose(session, model, app.state.specs, strategy=session.issue.strategy)`（`runner.py:76`）：
  1. 置 analyzing，清空旧 tasks/tool_calls/conclusion；
  2. `specs + build_todo_specs(session)`（planning 开）；
  3. `build_system_prompt(strategy=..., planning=True)` = methodology.j2 + conclusion.j2 + planning.j2 + (strategy_*.j2)；
  4. `build_agent(...)`：把每个 ToolSpec.func 包一层 SessionRecorder（`registry.py _hooked`），FunctionTool 注入 toolkit；
  5. `run_agent` 喂 `compose_user_content(issue, feedbacks)` → `Agent.reply` 完整 ReAct 循环 → 宽容 `extract_json`；
  6. 未解析 JSON → 追问一次；成功 → `conclusion_from_dict` → completed。
- `GET /status/{id}` 轮询读 `session.tool_calls` / `session.tasks` / `session.conclusion`（recorder 在工具调用前后写 session）。
- Stage2：`POST /remediate` 把 `conclusion.recommended_fix` 快照为 `RemediationPlan(pending_review)`；`POST /approve` approve→`apply_fix`（`remediate.py`）→ `status="executed"`；reject+feedback → feedback 并入重跑，≥ `max_reanalyze` → closed_manual。

### A2. 目标序列（trace_code，真实 MCP）

```
调用方 ─ POST /diagnose (Issue 全字段) ─▶ STORE.create(Issue{case_type:"trace_code", app, repo,
        trace_id, environment, time_window?, deployed?})
                                          │ 立即返回 {session_id, status: analyzing}
                                          ▼
                                    _schedule_run(session)
                                          │ asyncio.create_task
                                          ▼
                                 resolve_specs(issue)  ← 新增：按 case_type/app/repo 分派
                                          │  = function tools（todo + fetch_strategy + repo-state）
                                          │    + 绑定该 case_type 的 MCP client（app-log、git）
                                          ▼
                                run_diagnose(session, model, resolve_specs(issue))
                                          │ build_system_prompt(planning=True)   ← strategy 默认不预烤(M3)
                                          │ build_agent: hybrid Toolkit = FunctionTool + MCP tools
                                          ▼
                                agent.reply —— ReAct 循环
                                    plan_investigation → todo 清单（/status 进度）
                                    查 app-log MCP  (mcp__applog__get_trace …)
                                    判错误类别 → fetch_strategy(name)   ← M3 证据驱动取 runbook
                                    查 git MCP (只读白名单) → repo-state 取 HEAD → case a/b/c
                                    → conclusion(含 base_commit/deployed_commit/change/suggested_diff)
                                          │ conclusion_from_dict（含 sha 幻觉校验）
                                          ▼
                                     completed（含给人批复的 plan）
                                          │
                        ┌─────────────────┴─────────────────┐
            POST /approve approve                 POST /approve reject(+feedback)
                        ▼                                 ▼
                 approved（终态，无下游动作）         feedbacks 并入下轮 user 上下文
                 删除 apply_fix / executions               │ 重跑（reanalyze_count+=1）
                                                   ≥ max_reanalyze → closed_manual
```

**要点**
- **每个报错日志请求 = 一个 Session + 一个后台 asyncio task**：与现状一致，扩展的是 Issue 承载更多"去哪查"字段 + 按请求解析工具集。
- **工具集从"启动期单例"改成"每请求解析"**：`resolve_specs(issue)` 决定 agent 看到什么——function tools（todo / fetch_strategy / repo-state）恒在；MCP 端按 `issue.case_type` 绑定 app-log + git。
- **Approve = 终态签章，零执行**：去掉 `apply_fix`、`ExecutionResult`、`status="executed"`、Session `executed` 态。审批作用在 plan（`RemediationPlan.status: approved`），不作用在执行。
- **Reject = REST 层动作 + 文本 feedback 回灌**（`compose_user_content` 已支持），非工具级 denial；同一条 plan 对象回到 pending_review（换新 steps）。

### A3. 每请求绑定与 MCP client 生命周期的关系

- **MCP client（进程）是 app 级共享的**：stateful stdio git MCP 不可能每请求 spawn 子进程。manager 在启动/CRUD 后持有连接好的 client。
- **"每请求分派" = 选择该 session 的 agent 可见哪几个已连接 client**，不是每请求重建 client。即 agentflow 的"两态绑定"（无/精确子集）在 spike 侧由 `issue.case_type` profile 决定。
- 真实世界：每 repo 一个 stdio git client（表里多行）；spike 范围内只接一个 repo → 一行。app-log MCP 用 HTTP（stateless）即可承载任意 app。

---

## B. A3 真 MCP 对接（参考 multi-agent-workflow）

### B1. 参考项目的配置模型（spike 照抄的表结构）

`agentflow/api/mcp_store.py` 的 `mcp_servers` 表：记录只描述 **server 本身**，不存 agent 绑定（绑定以 agent 主表 `mcp_server_ids` 建模）：

| 列 | 含义 |
|---|---|
| `name` | 也即 `MCPClient.name`，须 `^[a-zA-Z0-9_-]+$`（LLM 侧工具名前缀） |
| `transport` | `stdio` \| `http` |
| `config` | JSON：stdio=`{command,args,env,cwd}`；http=`{url,headers,timeout}` |
| `is_stateful` | stdio 强制 1；http 默认 0 |
| `enable_tools` / `disable_tools` | JSON list[str]\|null——**只读白名单闸门**（见 B5） |
| `enabled` | 运行时 `list_enabled()` 只加载 enabled=1 |

行 → client 的解析（`mcp_manager._build_client`）：

```python
if transport == "stdio":
    mcp_config = StdioMCPConfig(command=cfg["command"], args=cfg.get("args"),
                                env=cfg.get("env"), cwd=cfg.get("cwd"))
    stateful = True
elif transport == "http":
    mcp_config = HttpMCPConfig(url=cfg["url"], headers=cfg.get("headers"),
                               timeout=cfg.get("timeout"))
    stateful = bool(row.get("is_stateful"))
return MCPClient(name=row["name"], is_stateful=stateful, mcp_config=mcp_config,
                 enable_tools=row.get("enable_tools"),
                 disable_tools=row.get("disable_tools"))
```

**绑定在 agent 主表**（`agent_config.py`）：`ResolvedAgent.mcp_server_ids: set[str]`，语义 v1.12.1 起"**没配置 = 没有 server**"（两态：空 set → 无；非空 → 精确子集）。`AgentConfigResolver.server_ids_for(name)` 供运行时过滤。**spike 侧对应物 = case_type profile**（见 B4）。

### B2. spike 落地形态

spike 不做前端 CRUD；控制面收敛为一个**配置存储 + seed**，两层都对齐参考的表结构：

- `aidiag/mcp/store.py`：轻量 `MCPStore`（aiosqlite 或 memory；字段/方法面照 B1，够用即可：`list_enabled/get/update_tools`）。`settings.state_db_path` 复用 v4 的 sqlite 路径约定。
- seed（启动时幂等 upsert）：往 store 落两行真实 server 配置——
  - **app-log**（HTTP stateless）：`{url: <app-log-mcp-url>, headers: {...auth...}, timeout: 60}`；工具如 `get_trace(app, trace_id)` / `query_logs(app, time_start, time_end, ...)`。
  - **git**（stdio stateful）：`{command: "npx"|"uv", args: [...git-mcp-server...], cwd: <repo 路径>, env: {...}}`；`enable_tools=[只读白名单]`（见 B5）。
- 具体传输字段（command/args/cwd/url/headers）从 `.env` 读（`AIDIAG_APPLOG_MCP_URL`、`AIDIAG_GIT_MCP_*`），不硬编码——延续"启动配置收进 .env"的约定。

### B3. 运行期 client 生命周期（port `MCPClientManager`）

`aidiag/mcp/manager.py` 移植参考的 `MCPClientManager` 关键行为：

- **load()**：启动时 `store.list_enabled()` → `_build_client` → stateful best-effort connect；**单条配置坏/连不上只 log，不拖垮启动**。
- **refresh_server(mid) / close_all()**：CRUD 热刷新（evict → 重建→ 连接）与 shutdown 清 stdio 子进程。
- **clients_for(case_type_profile)**：按 profile 的 server id 集合过滤 → 返回 enabled 且（stateful）已连接的 client；stateful 失联**重连一次**，失败跳过不阻塞 run。
- **allow_names_for(profile)**：预计算该 profile 可见 MCP 工具的 AgentScope **精确名**（`client.list_tools()` 已应用 enable/disable 过滤；名 = `mcp__{server}__{tool}`），供 `build_permission_context` 注入 allow 规则。
- `Toolkit.__init__` 对 "stateful 但未 connect" 的 client 抛 ValueError → hybrid 组装前必须做一层 `_filter_connected_mcps` 防御剔除（参考 `mcp.py:_filter_connected_mcps`）。

### B4. agent 如何调用（hybrid Toolkit + 每请求绑定）

参考 `agentflow/agents/mcp.py:build_toolkit`——**同一 agent 两类工具共存**：

```python
def build_toolkit(specs, *, recorder=None, mcp_clients=None) -> Toolkit:
    func_tools = [FunctionTool(func=_hooked(s.func, recorder, s.name), name=s.name,
                               description=s.description, is_read_only=s.read_only)
                  for s in specs]
    mcps = _filter_connected_mcps(mcp_clients or [])
    if mcps:
        return Toolkit(tools=func_tools, mcps=mcps)   # hybrid
    return Toolkit(tools=func_tools)
```

- **MCP 工具对 LLM 的名字** = `mcp__{server}__{tool}`（AgentScope 侧经 `MCPTool.name` 定，sanitize 规则内置）。agent 靠 tool description 目录发现；spike 的方法论/planning.j2 只给"去查 app-log + git"的纪律，不写死具体工具名——名字由 AgentScope 暴露，天然与 mock/真 MCP 两态兼容。
- **只读自动放行**：git/app-log 全部工具在 server 端标 `readOnlyHint=True`（参考 `mock_mcp_server.py` 的 `ToolAnnotations(readOnlyHint=True)`）→ AgentScope 只读路径自动 ALLOW。非只读残留由 allow 预计算兜底（DONT_ASK + 精确 allow 名）。
- **每请求绑定**：`resolve_specs(issue)` 产出「function tool spec 列表 + profile 命中的 MCP client 列表」→ 交给 `run_diagnose`。两态：case_type 没配 profile = 无 MCP（纯 function 兜底，如离线测试/`case_type:"k8s"` 旧路径）；配了 = 精确子集。

### B5. 只读边界在配置层的落地（对应 DESIGN §1.2 的纵深防御）

- **第一道闸（server 进程侧）**：git MCP server 若自带只读 profile / 部署用最小权限——spike 真 server 部署时开启。
- **第二道闸（配置层，spike 落地处）**：git 行 `enable_tools=[git_log, git_show, git_blame, git_diff, git_grep, git_status, git_ls_files, git_branch, git_rev_parse, git_ls_remote, ...]`，写类工具（commit/push/reset/checkout/restore/...）**根本不进 enable 名单**；即便 server 暴露它们，agent 也看不到。
- **第三道闸（权限层）**：DONT_ASK + 只对 enable 名单生成 allow；未授权 = DENY 不执行。
- 这样 DESIGN "spec 层就不注册"的白名单语义**上移到 enable_tools**（真 MCP 下没有 ToolSpec 可删）。

### B6. recorder / 证据纪律在真 MCP 下的适配（关键差异）

现状 spike 的 recorder（`registry.py:_hooked`）包在每个 **FunctionTool.func** 上 → **原生 MCP 工具不走这条路**，其调用不会出现在 `session.tool_calls`（`/status` 进度/trace 会空）。

参考项目 agentflow 的做法是 **AgentScope middleware**（`runner.py`：`Agent(..., middlewares=[recorder])`），对 FunctionTool 与 MCP 工具**统一**采集（llm_call/tool_call 明细），DENY 调用靠跑后补扫 `scan_denied_blocks`。

**落地选择**：把 spike 的 `SessionRecorder` 从"包 func"迁移为 AgentScope middleware（`build_agent` 传 `middlewares=[SessionRecorderMiddleware(session)]`，保留现有 `on_tool_start/on_tool_end` → `session.tool_calls` 语义）。function tools 的 `_hooked` 包装可退役；若 AgentScope 2.0.3 middleware 无工具级回调，退路 = 真 MCP 层 recorder.tool_calls 为空但 `/status` 仍显示 tasks/conclusion（把结论 + 关键工具调用写回 session 的最后一跳），live 层以结论断言为主。

### B7. sha 幻觉纪律在真 git 下（对应 DESIGN §4.3）

- **repo-state 仍是 FunctionTool**（registry），不是 MCP：薄封装对同一 repo 路径跑 `git rev-parse HEAD` + 可解析的部署 sha，**显式返回 HEAD** → LLM 只引用不拼 sha。
- `conclusion_from_dict` 校验升级：plan 里出现的每个 sha（`base_commit` / `deployed_commit` / `already_fixed_by` / `EvidenceItem.commit`）必须命中 recorder 采集的工具返回中出现过的 sha 集合；否则降 confidence / 拒收。repo-state 的返回值进 recorder → 校验有据可依。
- `fetch_strategy` 同理为 FunctionTool：allowlist 校验 name → 渲染 runbook .j2 作为工具结果返回；非法名错误返回 + 可用清单。

---

## C. 分阶段改动清单（delta，全部未动）

### Phase 0 — domain（对齐 DESIGN §3）
| 文件 | 改动 |
|---|---|
| `aidiag/domain.py` | Issue 泛化：+`case_type`(默认 `trace_code`)、`app/repo/trace_id/environment/time_window`、`deployed: DeployedRef`；新增 `DeployedRef`；EvidenceItem +`commit`；RemediationStep +`change`/`suggested_diff`；DiagnosticConclusion +`deployed_commit/base_commit/base_note/already_fixed_by`；**删 ExecutionResult**。`conclusion_from_dict` 同步宽容解析 + **sha 幻觉校验**（见 B7）。 |
| `aidiag/diag/session.py` | RemediationPlan 去 `executed` 态、去 `executions` 字段；Session 去 `executed` 态（保留 analyzing/completed/failed/reanalyze_count/feedbacks）。 |
| `aidiag/diag/remediate.py` | **删除** `apply_fix`（审批无执行）。 |

### Phase 1 — M3 prompts + runbook registry
| 文件 | 改动 |
|---|---|
| `aidiag/prompts/__init__.py` | `build_system_prompt` **默认不再预烤 strategy**（仅 `issue.strategy` 显式时）；registry（name→.j2→触发签名）单一来源，目录按 case_type 过滤。 |
| `aidiag/prompts/conclusion.j2` | 契约补 `base_commit/deployed_commit/base_note/already_fixed_by` + `change/suggested_diff`。 |
| `aidiag/prompts/planning.j2` | 加一行 fetch 指针："取到首条证据、判定错误类别后，命中某场景就 `fetch_strategy(name)`"。 |
| `aidiag/prompts/strategy_trace_bug.j2` / `strategy_trace_dependency.j2` / `strategy_trace_startup.j2` | 新增三个 runbook（trace_slow = stretch 不首批）。 |

### Phase 2 — 工具（function 侧）
| 文件 | 改动 |
|---|---|
| `aidiag/tools/repo_state.py`（新） | FunctionTool：对 repo 路径 `git rev-parse HEAD`（只读），返回 HEAD sha（进 recorder 供 B7 校验）。 |
| `aidiag/tools/strategies.py`（新） | `fetch_strategy(name)` FunctionTool：allowlist + 渲染 runbook 作工具结果；非法名错误返回 + 可用清单。 |
| `aidiag/tools/datasources.py` | trace_code 时**不再挂 mock 数据源**；`crashloop_env()` 保留给 `case_type:"k8s"`（旧路径）。 |
| `aidiag/tools/registry.py` | `build_toolkit` 加 `mcp_clients` 参数 → hybrid Toolkit（B4）。 |

### Phase 3 — MCP 控制面（真 server，A3 主体）
| 文件 | 改动 |
|---|---|
| `aidiag/mcp/store.py`（新） | `MCPStore`：表结构/方法照 B1；`list_enabled/get/update_tools`。 |
| `aidiag/mcp/manager.py`（新） | port `MCPClientManager`：load/refresh/evict/close_all + `clients_for(profile)` + `allow_names_for(profile)` + `_filter_connected_mcps`（B3）。 |
| `aidiag/mcp/profiles.py`（新） | `case_type → {server_ids: set}`（trace_code = {app-log, git}；k8s = {}）。app→repo 映射暂以配置行 `cwd` + repo-state 绑定（开放项，见 D）。 |
| `aidiag/api.py` | lifespan 建 store + manager + seed + load；`_default_specs` 改为 `resolve_specs(issue)`（function specs + case_type profile 的 MCP clients）；`/approve` approve 分支去 `apply_fix`。 |
| `aidiag/agents.py` / `aidiag/diag/runner.py` | `build_agent`/`run_diagnose` 接受 MCP clients + 解析后 specs；recorder 迁 middleware（B6）。 |

### Phase 4 — 测试
| 文件 | 改动 |
|---|---|
| `tests/` | ① sha 幻觉单测（fake 工具返回含/不含某 sha）；② 发现型 golden：不预烤 strategy、断言真调 `fetch_strategy(<name>)`（include_tool_calls 类断言）；③ 既有 crashloop fixture 全部显式 `case_type:"k8s"`（DESIGN §10）；④ REST 冒烟：真 MCP server（`scripts/mock_mcp_server.py` 形态的 app-log + git 桩）走状态机。 |
| `scripts/mock_mcp_server.py`（或新 `scripts/mock_git_mcp.py`） | 真 MCP 形态的桩：app-log（`get_trace/query_logs`）+ git（只读白名单工具集），供离线/冒烟两层复用；live 层换真实 url。 |

### Phase 5 — 文档同步
| 文件 | 改动 |
|---|---|
| `README.md` / `spike_plan_v4.md` | 场景/验证命令更新（trace_code + 真 MCP）。 |
| memory `aiops-spike.md` | 实现进度恢复点刷新（实现并验证后）。 |

---

## D. 分层验证（不变）

1. **① 离线机制**（无需 key/LLM）：domain/sha 校验/registry/fetch_strategy allowlist/MCP manager 用 `ScriptedJsonModel` 单测。
2. **② REST 冒烟**（无需 key）：真 MCP **桩** server + ScriptedJsonModel 走状态机（POST /diagnose case_type:"trace_code" → 轮询 → approve/reject）。
3. **③ live 真 DeepSeek**（需 `DEEPSEEK_API_KEY` + 真 app-log/git MCP url）：`make golden`/`make live`。断言基线沿用"唯一 token + recommended_fix target 落 checkout 范围 + evidence ≥2"。

## E. 依赖 / 开放项（实现前要补）

- [ ] app-log / git **真实 MCP server 的 url / stdio command 与只读工具集**（spike 桩先占位，live 换真）。
- [ ] app→repo 映射放哪层：spike 先以「每 repo 一行 git 配置 + `issue.repo` 选择行」收敛，不做服务发现。
- [x] AgentScope 2.0.3 middleware 工具级回调——**2026-09-09 探针通过**：`MiddlewareBase.on_acting` 包整个 `toolkit.call_tool`（FunctionTool 与 MCP 共用的唯一分发，`_agent.py _acting/_acting_impl`），实测捕获 name/args/result(state=success)。→ B6 走首选路径：SessionRecorder 迁 `on_acting` middleware，无需退路。
- [ ] `trace_slow` 是否纳入后续（默认 stretch）。
- [x] `case_type` 破坏性改动对既有 crashloop 冒烟/golden fixture 的更新（显式传 `"k8s"`）——**2026-09-09 完成**：`evals/golden_crashloop.yaml` 显式 `case_type: k8s`；`tests/test_rest_trace_smoke.py` 新增 k8s 默认回退冒烟；DiagnoseRequest 默认 `case_type="k8s"` 保旧。
