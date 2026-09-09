"""MCP 控制面（A3）：store（配置）+ profiles（case_type 绑定）+ manager（连接生命周期）。

移植参考 multi-agent-workflow 的 ``MCPStore`` / ``MCPClientManager`` 语义；spike 收敛为
**进程内配置**（不落 DB/Postgres）——store 只做内存 CRUD，seed 行来自 profiles（真 server
url / stdio command 用 env 或默认 mock 桩覆盖，live 换真实地址）。
"""
