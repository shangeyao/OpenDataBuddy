"""Harness：DeepSeek 之上的 Agent 编排层。

六层能力（与商业版 Harness 框架对齐，但全部本地实现）：

    context    上下文装配 —— system prompt / 历史裁剪 / 指标与表结构注入
    knowledge  知识检索   —— 文档 + 元数据混合召回
    memory     长期记忆   —— 用户偏好与口径约定
    skills     工具注册   —— function calling 的工具集与风险分级
    guardrails 护栏       —— 三要素判定、高危操作人工确认、PII 脱敏
    trace      轨迹评测   —— 每次工具调用的耗时/成败，用于诊断与调优

各子模块按需导入，包本身不做 eager import —— 避免 tools ↔ runtime 的循环依赖。
"""
