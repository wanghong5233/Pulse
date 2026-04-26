# Pulse 业务层目录说明

> 内核代码在 `core/`,本文档只说明业务层的组织方式。

---

## 调用关系

```
用户消息 → Channel → Brain ─┬→ tools/                    （Ring 1：单步原子工具）
                             └→ modules/<domain>/<cap>    （Ring 2：领域技能包 + 子能力）
                                  └→ modules/<domain>/_connectors/<platform>/
                                                           （平台 IO 驱动，属于领域内部实现）
```

Brain 做两级路由:**先定位领域 (domain)**,再在领域内部 **定位具体子能力 (capability)**。

---

## 目录职责

### `tools/` — Ring 1 内置工具

Brain 直接调用的轻量级函数。一个文件一个工具,无状态,无生命周期。

| 工具 | 功能 |
|------|------|
| `weather.py` | 查天气 |
| `flight.py`  | 搜航班 |
| `alarm.py`   | 设闹钟 |
| `web.py`     | 网页搜索 |

触发方式:用户对话 → Brain 推理 → 直接调用。

### `modules/` — Ring 2 业务技能包

按 **领域 (domain)** 分组,每个领域是一个"技能包",内部包含若干 **子能力 (capability)**。每个子能力继承 `BaseModule`,有完整生命周期 (startup / shutdown)、定时任务 (patrol)、HTTP API、意图路由。

```
modules/
├── __init__.py
├── job/                        求职域
│   ├── skill.py                领域级 SKILL_SCHEMA(供 Brain 做两级路由)
│   ├── shared/                 领域内共享模型
│   ├── _connectors/            领域内平台驱动(job-specific)
│   │   ├── base.py             JobPlatformConnector 契约
│   │   └── boss/               Boss 直聘平台实现
│   ├── greet/module.py         job_greet(扫描 + 打招呼)
│   └── chat/module.py          job_chat (HR 对话处理)
├── intel/                      情报域(单 module 多主题)
│   ├── skill.py                领域级 SKILL_SCHEMA
│   ├── module.py               intel(确定性 6 步 workflow + 主题 YAML)
│   ├── pipeline/               fetch → dedup → score → summarize → diversify → publish
│   ├── sources/                SourceFetcher 实现(rss / web_search / ...)
│   ├── topics/                 主题 YAML 配置;新增主题 = 加文件
│   └── docs/                   模块内开发文档
├── email/                      邮件域
│   ├── skill.py
│   └── tracker/module.py       email_tracker
└── system/                     平台级系统域
    ├── skill.py
    ├── hello/module.py         hello(健康探针)
    └── feedback/module.py      feedback_loop(反馈进化)
```

**命名约定**

- 目录:`modules/<domain>/<capability>/`,小写、单数,层次反映路由结构。
- 类名:`<Domain><Capability>Module`,例如 `JobGreetModule`、`EmailTrackerModule`;单 capability 域(如 `intel`)直接 `IntelModule`。
- `BaseModule.name`:稳定业务标识 (如 `job_greet` / `intel` / `feedback_loop`),用于 intent 路由、日志、事件、存储 key,**一经设定不随目录改名**。
- `route_prefix`:与目录层级一致,例如 `/api/modules/job/greet`、`/api/modules/intel`。

**为什么要两层 (domain → capability) 而不是扁平?**

1. Brain 做意图路由时可以先做 **粗粒度领域分发**,再在领域内部做细粒度能力选择,避免一次性把所有子能力塞给大模型。
2. 领域内的 `_connectors/` 只服务于该领域 (Boss / 猎聘 / 智联招聘同属 `job/_connectors/`),天然隔离不同领域的依赖。
3. 新增平台 = 新增 `<domain>/_connectors/<platform>/`,子能力代码不变。
4. `skill.py` 作为 **领域级 schema**(不是 Markdown),同时被人类阅读和 Brain 消费。

### `modules/<domain>/_connectors/` — 领域内平台驱动

被领域内部的子能力 module 调用,封装所有平台通信细节 (HTTP、MCP、DOM 解析、反爬)。每个领域定义自己的 `JobPlatformConnector` / `IntelPlatformConnector` 契约,平台实现作为兄弟包挂在下面。

> 下划线前缀 `_connectors/` 有两重作用:
>
> 1. 目录语义上表明"这是领域内部实现,不对外暴露";
> 2. `ModuleRegistry.discover()` 会自动跳过以 `_` 开头的子包,避免把驱动当成业务模块注册进去。

**新增平台的决策**

- 同领域、新平台(Boss → 猎聘):在 `modules/job/_connectors/liepin/` 下实现 `JobPlatformConnector`。
- 新领域(如视频域、金融域):在 `modules/<new_domain>/` 下新开一个技能包,配 `skill.py`、`_connectors/`、若干 capability。

---

## 新增功能的决策树

```
需要新功能
  ├── 单步、无状态、Brain 直接调用?            → 加 tools/ 下一个文件
  ├── 多步、有状态、需要生命周期 / 定时任务?    → 加 modules/<domain>/<capability>/
  │     ├── 领域已存在(如 job):      直接在现有 domain 下加 capability
  │     └── 全新领域(如 finance):    新建 modules/<domain>/ + skill.py + _connectors/ + capability
  └── 需要跟新的外部平台通信?                 → 加 modules/<domain>/_connectors/<platform>/
```
