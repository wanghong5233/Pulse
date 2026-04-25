# Pulse 工程执行约定

根级 [code-review-checklist.md](../code-review-checklist.md) **§4** 原则在本仓库的落地项;测试方法见 [testing-guide.md](./testing-guide.md)。

---

## 1. 配置与依赖

- 配置通过 `Settings` 统一管理,**不** 在业务代码里 `os.getenv()`
- 依赖通过构造函数注入,**不** 在 `__init__` 里读全局状态
- `.env.example` 与 `.env` 字段同步

## 2. 错误处理与日志

- 日志用 `logging.getLogger(__name__)`,**不** 用 `print`
- 关键路径有日志(启动 / 连接 / 收发消息);异常路径有 WARNING/ERROR
- 日志**不** 打印完整密钥(最多前 10 位)

## 3. 异步与长程任务

- 协程没有遗漏 `await`
- 后台任务保存引用,shutdown 时正确取消
- 长程调度 / 熔断 / 活动时段 等 见 [Pulse-AgentRuntime设计.md](../Pulse-AgentRuntime设计.md)

## 4. 清洁度

- 没有未使用的 import
- 没有注释掉的代码块
- 没有临时调试代码(`print`、硬编码测试值)
