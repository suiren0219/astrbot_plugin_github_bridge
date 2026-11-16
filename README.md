# GitHub Bridge · AstrBot

自动发现本人 GitHub 仓库，把 Commit、Release、Issue 和仓库变化汇总到管理员私聊。附 SukiSU-Ultra / KSU 风格管理页，以及 QQ issue 草稿确认入口。

支持 AstrBot **4.29.4+**、Python **3.10+**。前端为随包附带的 HTML/CSS/JS，部署无需 Node、npm 或外部 CDN；管理页是 **AstrBot Dashboard 原生插件视图**，不开放独立端口，不需要单独口令，沿用 Dashboard 登录态。

## 覆盖范围

| 需求 | 实现 |
| --- | --- |
| 自动发现本人全部仓库 | 周期重新发现，无须维护列表；私有仓库受 Token 可见范围限制，新仓库首次静音建基线 |
| 默认分支 Commit | 基于提交 SHA 增量识别，保留旧提交去重键 |
| Release | 含预发布，不通知草稿 |
| Issue | 新建、评论、关闭/重开/标签/负责人等状态变化，排除 PR |
| 仓库元信息 | Star / Fork 数、默认分支、描述、未关闭 Issue 数等快照变化 |
| 私聊通知 | 目标仅允许 `平台实例ID:FriendMessage:用户ID`，不向群聊主动推送 |
| 首次静音与重试 | SQLite 基线与通知队列在同一事务提交；每轮上限；失败退避；重复导入幂等 |
| 旧监控迁移 | Shell 插件的 `sent_notifications.json` 与 `failed_notifications.json`；过滤第三方仓库和群目标 |
| QQ 反馈 bug | 允许列表、管理员私聊、完整草稿预览、15 分钟内确认发布；支持提取最近群文本 |
| 状态 / 配置 / 日志 | `/gh status`、`/gh config`、`/gh logs`，仅输出本插件脱敏信息 |
| KSU UI | 绿色实底状态卡、扁平分组、玻璃悬浮导航；浅色/深色/系统/AMOLED；手机与桌面布局 |

原需求文档的 R1–R8 已通过现有 MCP 完成。本插件补齐 R9，并提供独立的 issue 反馈入口；不修改 `mcp_server.json`，不替换现有 `astrbot-mcp` / `github-mcp`。插件使用自身异步轮询，不再额外配置宿主 cron 或 AstrBot Open API Key。

## 安装与首次配置

1. 在 AstrBot 插件管理中上传本插件 ZIP，或将源码放入 `data/plugins/astrbot_plugin_github_bridge`，安装 `requirements.txt` 后重载。
2. 确认自己的 QQ 已加入 AstrBot 管理员名单。
3. 打开 **AstrBot Dashboard → 插件 → GitHub Bridge → 可视化视图 `dashboard`**。页面沿用 Dashboard 登录，无需额外端口、SSH 隧道、反向代理或独立口令。容器内也不再监听 `127.0.0.1:8765`。
4. 在管理页填写 GitHub 用户名、Token、私聊目标，再保存。默认值来自需求：用户 `suiren0219`，目标 `恩赫里亚:FriendMessage:1906986796`。第一段必须是当前 AstrBot 平台实例 ID，并非适配器名称；如平台实际 ID 不同，请修改。
5. 点击“测试连接”，确认账号与仓库可见范围；然后启用监控。默认每 120 秒检查一次，每轮最多发送 5 条，剩余通知保留在队列中。
6. 首轮只建立基线。新仓库或重新启用的事件类型也会静音建基线。后续新增事件才推送；已明确导入的旧失败项会正常重试。

Token 不预置；安装后监控默认暂停。`monitor_enabled` 插件配置可控制加载后是否自动启动轮询；监控参数在管理页保存，即时生效。检查进行中会拒绝同时改配置，请稍后重试。

### GitHub Token

推荐 GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens：

- Resource owner 选择本人；Repository access 选择 **All repositories** 才能自动覆盖新建私有仓库。选择仓库子集时，插件只能监控获授权部分。
- 仅监控：**Metadata: Read-only、Contents: Read-only、Issues: Read-only**。
- 需要 QQ 建 issue：将 **Issues** 改为 **Read and write**。
- 不需要 Administration、Actions 写权限，也不需要修改代码权限；先在 GitHub 仓库设置中启用 Issues。
- 设置合理过期日期。使用 classic PAT 时，私有仓库通常需要 `repo`，权限更宽，优先使用 fine-grained Token。

可以把 Token 作为容器环境变量 `GITHUB_BRIDGE_TOKEN` 注入；环境变量优先于文件，启用这种方式时应在容器中修改/清除 Token 后重启。管理页留空保存会保留当前 Token，API 仅返回 `token_configured`，不会回显 Token。

凭据文件在 Linux 下写为 `0600`，独立数据目录同样限制为 `0700`。AstrBot / 宿主备份可能包含本插件数据；不要分享整个数据目录。管理页 API 由 Dashboard 的插件扩展路由统一鉴权，页面中的 bridge 调用不会直接接触 Token 或 Dashboard Cookie。

## QQ 命令

只有 **AstrBot 管理员私聊**可以使用；群聊和普通用户调用不会得到本插件回复。命令前缀跟随 AstrBot 的 `wake_prefix`，下面以 `/` 为例。

```text
/gh help
/gh status
/gh panel
/gh check
/gh pause
/gh resume
/gh retry
/gh config
/gh logs
/gh baseline 仓库 确认
/gh bug gdust-v2 | 标题 | 正文
/gh fromgroup 群号 仓库 | 标题 | 补充说明
/gh confirm 草稿码
/gh cancel 草稿码
```

- 默认 issue 允许仓库为 `gdust-v2,astrbot_plugin_*`，所有条目限定在配置用户名之下；可以在管理页调整。
- `bug` / `fromgroup` **只创建内存草稿**，不会立即写 GitHub；确认后才提交。重载插件或超过 15 分钟后草稿失效。确认码绑定创建它的私聊。
- 群历史仅在管理员明确调用时读取最近 20 条，提取文本，不包含发送者 QQ、昵称、图片 URL。会隐藏常见 Token/密码格式，但仍须人工核对预览中的个人信息和业务秘密。没有 OCR，图片内容请补充到正文。
- 网络超时可能发生在 GitHub 已受理之后。插件不会自动重放 issue 写入；请先检查目标仓库，避免重复创建。
- `/gh panel` 只返回 Dashboard 插件视图入口，不再生成口令。

## 从旧 GitHub 监控迁移

1. **先暂停旧插件及旧 cron 脚本**，保留旧数据文件，不删除。
2. 新插件保持暂停，在管理页设置正确的本人用户名与管理员私聊目标。
3. 到旧 `data/plugin_data/GitHub监控插件` 目录取出 `sent_notifications.json`、`failed_notifications.json`；在新管理页的迁移入口选择文件或粘贴 JSON。
4. 查看导入数量、失败通知排队数和跳过原因，再启用新插件，建立首次基线。
5. 对照管理员私聊和新面板记录确认；旧数据继续作为只读历史保留。

兼容格式根据 Shell `astrbot_plugin_github_monitor_shell` v1.6.1 公开源码核实（提交 `4bce6022266dc70a56d489423c57b72361048e5a`），兼容有/无分支的仓库键，分支名可含 `/`。文档中的原部署为 v1.5.0，若其文件结构不同，先保持暂停并检查导入结果。

旧插件可能对部分目标发送成功、部分目标失败，因此已发送记录不能用来丢弃失败项。新插件保留当前管理员私聊的明确失败记录，按 Commit SHA 去重；群目标和第三方仓库被跳过。导入只读 JSON，不读取服务端任意路径，不修改原文件。重复导入不会重复排队。

需求记录中 `pending_forward_notifications.json` 为 0，故此版入口只导入已发送与失败文件。如果实际还有待转发项，可将它作为失败 JSON 导入（相同的 `repo_info/new_commits/targets` 结构），仅当前管理员私聊会被保留。旧 `repo_issues_state.json` 不导入，新机制直接静音建立新基线。

## 运行与边界

- GitHub 是轮询而非 webhook，延迟至少包含轮询周期和 API 响应时间。Star/Fork 是数量差异，轮询间发生又撤销、最终数量相同的变化无法识别，也不会提供每个 Star 的人员名单。
- GitHub API 的 `open_issues_count` 本身包含未关闭 PR，面板展示该原始统计值；Issue 动态推送单独排除 PR。
- 私有仓库和新仓库能否看见取决于 Token 授权。插件无法发现 GitHub 对 Token 完全隐藏的资源。
- 分页或提交历史无法完整确认时会保留旧游标并显示错误，不把截断的结果当完整结果推进。遇到体量很大的仓库可在管理页“高级连接设置”提高 `max_pages`（1–100，默认 20）；同时考虑 API 配额。发生强制推送或历史锚点删除后，可先 `/gh pause`，再 `/gh baseline owner/repo 确认`，最后 `/gh resume`；这会明确跳过尚未抓取的历史并静音重建基线，保留已入队通知。
- 失败发送指数退避，连续 8 次后等待手动重试。每轮重试也计入条数上限。修改目标后旧目标队列保留，不会误投到新账号；改回目标可继续处理。
- AstrBot `send_message=True` 表示适配器调用已成功返回，不是 QQ 客户端已读回执。如果进程恰在平台发送和本地记账之间退出，会把通知标为“送达状态未知”，等待管理员核对后手动重试，避免启动后直接重复发送。
- 本插件不会启停旧监控、修改 MCP 工具权限、重启 AstrBot、打开仓库 Issues 或创建真实验收 issue。运行这些外部操作前应在部署环境中明确决定。

## 数据位置

```text
AstrBot/data/plugin_data/astrbot_plugin_github_bridge/
  settings.json       # 监控配置与 Token，0600
  bridge.sqlite3      # 仓库快照、去重键、通知队列与轮询锁
```

备份数据库时暂停插件，或使用 SQLite 的备份接口；启用 WAL 时不要在运行中只复制主数据库文件。卸载插件不会由本插件删除数据。

## 本地开发 / 界面预览

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python tools/preview.py --port 8766
```

打开 `http://127.0.0.1:8766`。本地预览使用 `web/` 下的独立页面副本和临时数据，显著标注演示，不连接 GitHub、不发 QQ、不发布 issue。预览服务只监听本机，关闭后演示数据丢弃。生产安装包不包含 `web/`、`tools/` 或 `tests/`。

测试使用标准库 unittest 与 aiohttp test utilities，无需运行 AstrBot。插件入口测试按已核实的 AstrBot 4.29.4 API 模拟宿主；实际生产 QQ / GitHub 联调仍须在配置好凭据与平台的 AstrBot 实例完成。

发布包生成：`python tools/package.py`。包中不包含 Token、数据库、测试临时目录或下载的需求文档。

## 许可

AGPL-3.0-or-later，详见 LICENSE。界面按 KSU skill 提供的 SukiSU 风格参数重新实现，未复制上游 Compose / Miuix 源码。
