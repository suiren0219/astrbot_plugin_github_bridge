# AstrBot 开发 Wiki 对照审查 · 1.2.0

审查日期：2026-11-16；个人预设清理补充于 2026-11-17。接口基准固定为 AstrBot `v4.29.4`，不将“静态符合规范”视为真实宿主联调通过。

## 官方依据

- [插件开发指南](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/docs/zh/dev/star/plugin-new.md)：元数据、依赖、数据存放、异步网络、测试、Ruff 与来源声明。
- [插件配置](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/docs/zh/dev/star/guides/plugin-config.md)：`_conf_schema.json`、默认值与 `AstrBotConfig`。
- [插件可视化视图](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/docs/zh/dev/star/guides/plugin-pages.md)：`views/<name>/index.html`、module 脚本、bridge、受限 iframe、主题同步及静态资源路径。
- [插件发布](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/docs/zh/dev/star/plugin-publish.md)：公开仓库、metadata 自动解析、包体积限制。
- [Context 实现](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/astrbot/core/star/context.py)：`register_web_api(route, handler, methods, desc)`，相同路由和方法替换已有注册。
- [Web API 实现](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/astrbot/api/web.py)：请求上下文、`body()`、`query.get(..., type=int)`、`json_response` 和 `error_response`。

## 1.2.0 中文问题工作台补充

- 原生 Dashboard 视图新增“问题”入口：仓库选择、问题状态筛选、分页、本页搜索、详情和评论，支持创建、评论、关闭及重开。
- 继续通过官方 bridge 调用已鉴权的 `register_web_api` 路由；后端只使用固定 GitHub REST origin，所有写入限定本人账号、允许列表和非归档仓库，PR 编号被排除。
- 写入分为预览与确认。草稿绑定 Dashboard 用户、10 分钟有效、确认一次即消费；确认前及远端只读检查后重新验证配置签名，避免权限变化期间继续写入。超时或取消不自动重放写请求。
- 详情和预览均使用纯文本节点，不执行远端 HTML；提供可复制链接以适配受限 iframe。评论预览存在上限，截断时明确告知。
- 生产页面在隔离内存 API 上完成创建、评论、关闭、重开；确认前列表不变，确认后列表与详情刷新。只读/归档写入按钮禁用，桌面分栏和 390px 手机布局已检查。
- 新增工作台单元与端到端回归，当前共 150 项通过。全部写入仅发生在模拟客户端和临时演示数据中，没有写入真实 GitHub。

## 1.1.0 AI 总结补充

- 复用官方 `Context.get_current_chat_provider_id(target_umo)` 与 `Context.llm_generate(chat_provider_id=..., prompt=..., system_prompt=...)`，读取 `completion_text`；接口已与官方 `v4.29.4` 源码核对。
- 每条公开动态追加中文摘要，保留原文和链接；不传会话历史、不开放工具、不访问动态链接。输入截断并隐藏常见凭据，输出移除推理标签并限制长度。
- 私有或可见性未知内容默认不调用模型；公开内容要求入队时和当前均明确为公开，私有内容需显式开启。
- 摘要以独立 SQLite 表缓存，发送重试/重启复用；失败、空响应或超时降级为原始通知，短暂退避。生成阶段取消不把未发送消息标成发送中。
- 管理页可保存总结开关、模型 ID、超时及私有内容选项；版本为 1.1.0。原有未提交的通知标题去重优化与更新日志一并保留。
- 104 项测试通过，包含模型调用桩、实际超时取消、旧数据库兼容、缓存复用、可见性变化和配置持久化。受限 iframe 已验证新表单与模型 ID 保存。
- 本轮未使用真实模型或 QQ；当前变更通过功能分支 PR 审查，是否发布以主分支和 PR 合并状态为准。

## 1.0.2 个人预设清理补充

1.0.1 只清空了默认私聊目标，仍残留 GitHub 用户名、Issue 仓库允许列表和相应 placeholder；已有 settings.json 还会加载旧值。1.0.2 补齐以下修复：

- 新安装的用户名、Token、私聊目标、Issue 允许列表均为空；允许列表留空不允许创建 Issue。通用的 120 秒 / 每轮 5 条默认值保留。
- 页面 placeholder、命令帮助、README 和演示仓库采用通用内容；项目作者署名及真实仓库地址保留。
- 对没有新版配置标记的旧 settings.json，仅清理与旧内置值完全相同的字段。先以 0600 权限备份原文，再清空并暂停监控；备份失败时原配置不改写。
- 自定义的其他值、Token 和通知数据库保留。配置版本标记使清理只执行一次，用户之后明确输入同样的值也不会再次被清空。
- 页面显示清理提示，允许暂停状态保存空配置；清理备份被 Git 与安装包排除。
- `settings_migration.py` 只保留历史值的固定 SHA-256 指纹，已从旧 Git 提交计算并核对；精确匹配时清理，不保留个人明文预设。指纹只是匹配方式，不是凭据保护或匿名化措施。

已用隔离生产 iframe 验证新安装字段为空、升级后提示清理且 Token 只显示“已配置”、空字段可保存；新增 12 项升级测试通过。源码测试和演示夹具已换为通用账号/仓库。

## 1.0.1 Wiki 整理修复

| 问题 | 修复 | 验证 |
| --- | --- | --- |
| 生产页面移除了登录 DOM，但脚本仍绑定这些节点，初始化直接异常 | 删除独立登录、session/logout、直连 fetch 和假 CSRF 分支；生产视图仅使用官方 bridge，module 脚本在 SDK 注入后运行 | 浏览器直接加载 `views/dashboard`，受限 iframe 成功初始化并显示状态 |
| `monitor_enabled` 配置未使用 | 初始化时读取；false 会先把已保存的监控状态设为暂停，true 保留原状态；当前会话仍可手动 resume | 包含已有 `enabled=true` 数据的加载测试 |
| 新安装默认收件人为真实个人账号 | 默认目标为空；启用或导入前要求配置管理员私聊目标；文档、placeholder、测试改为匿名示例 | 默认空目标、群目标拒绝、启用前校验测试 |
| 通知筛选请求被 `eventsLoading` 丢弃 | 每个筛选请求独立递增代号，仅最新响应可写入页面；修正链接默认跳转导致筛选重置的问题 | 人为延迟“全部动态”12 秒，先切“失败”，旧响应晚到后仍显示正确失败列表 |
| “立即检查”只刷新排队时的状态 | 返回检查前时间基线；前端轮询到检查结束后更新仓库和通知，退出视图取消定时器 | 浏览器观察运行时禁用按钮，完成后显示结束提示并更新统计 |
| 预览只测试另一份页面，不能覆盖生产 DOM | 删除 `web/` 副本；预览直接加载 `views/dashboard`，模拟官方 bridge 并使用相同 iframe sandbox | 初始化、导航、保存配置、状态筛选和异步刷新均在生产页面验证 |
| 预览适配器混入生产模块 | 迁到 `tools/preview_server.py`；生产 `PanelAPI` 默认使用 `astrbot.api.web` | 生产请求边界测试与包清单检查 |
| 启动失败及卸载的资源边界 | 初始化失败清理部分服务；卸载先撤下业务入口、清空草稿，再关闭任务、客户端和数据库 | 启动配置测试、terminate 调用测试、实际后台任务取消与数据库关闭测试 |
| 发布包可能混入本地文件 | 排除环境配置、私钥、数据库、日志、压缩包、缓存、开发工具；添加 export-ignore 与统一 LF | 打包回归验证仅保留运行文件；ZIP CRC 与目录清单检查 |

## 规范对应结果

- `main.py` 使用 `Star`、`initialize()`、`terminate()` 和官方消息发送接口；命令内再次校验 AstrBot 管理员及私聊。
- Web API 在官方示例使用的构造阶段注册，路径包含插件名；本版本 Context 不要求额外的注册归属绑定。未启动独立生产端口。
- `metadata.yaml` 的名称、版本、仓库、最低 AstrBot 版本和平台声明齐全；包内版本统一为 `1.2.0`。
- 配置 schema 声明的启动开关已被消费，说明与实际行为一致。
- `StarTools.get_data_dir(PLUGIN_NAME)` 决定数据目录；设置和 SQLite 不写入插件源目录。
- GitHub 网络使用 `aiohttp`；第三方依赖已在 `requirements.txt` 声明，没有 `requests` 同步网络调用。
- 生产 iframe 通过 `AstrBotPluginView.apiGet/apiPost` 通信；相对静态资源、hash 导航、宿主主题同步，不读取 Dashboard 的登录凭据。
- README 补齐 SukiSU-Ultra 视觉参考和 Shell 旧监控数据结构来源链接；许可为 AGPL-3.0-or-later。
- 项目 Ruff 规则以 Python 3.10 为目标，已运行导入检查、未使用变量检查和格式化。

## 已完成验证

- **150 项 unittest 全部通过**，含中文工作台、一次性确认、接口端到端流程、AI 摘要、通知格式、GitHub mock、存储、迁移、QQ 命令、Web API、生命周期和打包测试。
- `python -m ruff check .`、`python -m ruff format --check .`、`compileall` 和 `git diff --check`。
- 浏览器加载的是生产 `views/dashboard`；宿主采用 `sandbox="allow-scripts allow-forms allow-downloads"`，没有 `allow-same-origin`。
- 配置保存、首次加载、通知筛选、延迟响应竞态及检查排队后的刷新已在该 iframe 中验证。

## 尚未完成的验证

本机没有安装完整 AstrBot，也未连接真实 GitHub PAT 或 QQ。官方 Dashboard 外壳、实际加载器热重载、真实 GitHub 权限、QQ 送达仍需部署环境联调。当前浏览器结果证明生产页面在相同 iframe 限制和模拟 bridge 下正常工作，不等于整个 AstrBot 实例已验收。

插件市场审核尚未提交。安装包以工作区交付文件及对应验收记录中的 SHA-256 为准；代码合并状态以 GitHub PR 页面为准。
