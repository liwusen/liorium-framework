"""Liorium Agent 运行时。

模块职责（改动前先读 ``docs/design/event-stream.md``）：

- ``hub``        —— 事件扇出中心（``EventHub``：每订阅者无界队列、永不阻塞）
- ``inbox``      —— 入站队列（``Bucket`` / ``Flag`` / ``BatchWindow``，同 id 原位替换）
- ``worker``     —— 常驻单执行位（取条目 → 跑回合 → 发终态帧；维护操作插队）
- ``scheduler``  —— 退化为 ``ExecSlot``（单执行位 + exclusive + 维护队列）
- ``loop``       —— 一个回合的模型/工具循环（``execute_turn``，含 ``IMMEDIATE`` 抢占）
- ``stream``     —— LangChain 事件 → Liorium 事件
- ``prompt``     —— system prompt 分段拼装
- ``checkpoint`` —— ``AsyncSqliteSaver`` 打开 / 播种 / 重置
- ``tools``      —— 工具集解析 + 输出拦截（OutputStore）
- ``output_store`` —— 工具输出完整内容存储（``artifact://``）
- ``middleware`` —— 基座中间件链（顺序由库固定）
"""
