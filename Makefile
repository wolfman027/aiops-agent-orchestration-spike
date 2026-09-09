.PHONY: install test api smoke golden live approve reject

install:
	uv sync --extra dev

# 离线机制测试（无 LLM / 无 key）
test:
	uv run pytest tests/ -m "not live"

# 启动 API 服务（host/port 读 .env：AIDIAG_API_HOST / AIDIAG_API_PORT）
# 需要热重载：make api ARGS="--reload"
api:
	uv run python scripts/run_api.py $(ARGS)

# M0 连通性（需 .env 填 DEEPSEEK_API_KEY）
smoke:
	uv run python scripts/smoke.py

# live golden 跑分（真实 DeepSeek，~60-90s/轮；需 key）
golden:
	uv run python scripts/run_golden.py

live:
	uv run pytest -m live tests/ -v

# Stage2 REST 全流程（先 make api 起服务）
approve:
	uv run python scripts/stage2_demo.py --decision approve

reject:
	uv run python scripts/stage2_demo.py --decision reject --feedback "DB 运维确认 checkout-db 正常，请复核配置指向"
