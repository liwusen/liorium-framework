"""Liorium Agent 运行时。

模块职责（改动前先读 ``docs/design/INTERFACES.md``）：

- ``loop``       —— 一个回合的模型/工具循环（``execute_turn``）
- ``stream``     —— LangChain 事件 → Liorium 事件
- ``scheduler``  —— 单执行位排队、调用级优先级与三个标志（``FLAG``）
- ``prompt``     —— system prompt 分段拼装
- ``checkpoint`` —— ``AsyncSqliteSaver`` 打开 / 播种 / 重置
- ``tools``      —— 工具集解析 + 输出拦截（OutputStore）
- ``output_store`` —— 工具输出完整内容存储（``artifact://``）
- ``middleware`` —— 基座中间件链（顺序由库固定）
"""
