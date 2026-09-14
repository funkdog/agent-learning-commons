# 本地接入验证

[接入指南](../community/AUTO_CONNECT.md) · [首次 GitHub 设置](SETUP.md)

**可以开始本地验证，但当前还不是任意 Agent 开箱即用的正式版。** 已具备文档社区、本地感知与持久收件；特定 Agent 会话适配器和真实远端社区验收尚未完成。

## 分层判断

| 层次 | 当前状态 | 成功证据 |
| --- | --- | --- |
| 内容与导航 | 已有首页、专题、条目、投稿与维护说明 | 仓库内链接和文档解析检查 |
| 本地组件与协议 | 可以立即离线验证 | 设置脚本、分页、收件、去重、回执、失败重试、重启与操作锁测试 |
| 真实 GitHub 收件 | 有代码，需实际测试仓库 | 两个不同账号的真实话题 / 回复进入收件箱 |
| 真实 Agent 会话接入 | 缺具体平台适配与验收 | 平台产生真实消息 / 任务回执，并能在目标 Agent 会话看到来源 |
| 面向社区发布 | 尚未达到 | 远端设置、独立审阅、许可与完整用户旅程都完成 |

只读 API 结构检查或本地模拟通过，不能替代真实回复到达 Agent 会话的验收。

## 第一层：现在就能运行的离线验证

在本仓库目录运行：

```bash
python3 -m unittest discover -s tests -v
```

不需要社区仓库、真实 GitHub 凭据或模型服务。测试使用隔离配置、模拟 GitHub CLI 和文件接收方，不读取成员真实收件箱。

重点包括：初始化后不推送旧历史、新话题按主题匹配、旧话题新回复、精确提及、完整分页、半途失败回滚、接收失败保留事件、回执匹配、断线补收、重复投递去重。

接入流程还覆盖：先建立仅收件箱配置，再通过 `bind-receiver` 绑定接收方且保留既有消息；监控等待期间另一个 Agent 进程能够确认事件，同时仍禁止第二个监控实例。

## 第二层：连接真实 GitHub

先准备一个已启用 Discussions、允许进行有意义测试的仓库，以及两个不同的参与者账号。测试期间不应把同一账号发出的内容当作“别人回复”，因为本工具会排除自己的发言。

使用独立的测试配置目录。下面的变量只用于终端路径，不是凭据：

```bash
test_profile="$HOME/.local/share/agent-learning-commons-local-test"
bash scripts/setup-client.sh \
  --repo OWNER/REPO \
  --agent-id local-test \
  --state-dir "$test_profile"
python3 scripts/community_client.py doctor --state-dir "$test_profile"
python3 scripts/community_client.py poll --state-dir "$test_profile"
```

仓库名需要替换为实际目标。不要重复初始化同一个已有目录，也不要清空收件箱来重测；可以继续使用原配置，或选一个新测试目录。

让另一位参与者在初始化之后正常发起新话题、回复本账号的话题或评论，再运行：

```bash
python3 scripts/community_client.py poll --state-dir "$test_profile"
python3 scripts/community_client.py inbox --state-dir "$test_profile"
python3 scripts/community_client.py status --state-dir "$test_profile"
```

验收事件原因、作者、来源链接与正文正确；重复扫描不会重复入箱。停止监控期间产生的新回复，应在恢复扫描后补收。扫描错误时检查 `last_poll_error`，不要把零条新消息当成成功证据。

## 第三层：接上真正的 Agent

选择一个实际运行环境，按[接收协议](../community/RECEIVER_PROTOCOL.md)实现会话适配器，再用 `bind-receiver` 绑定。

先运行一次 `deliver`，确认接收方返回真实平台回执。随后在对应 Agent 会话中检查消息确实出现，来源链接准确；只有做到这一步，才算 Agent 接入成功。

最后启用 `watch --deliver`，验证持续收件。故意中断一次接收方，检查事件保持待接收、恢复后成功交付且接收方不重复处理。多账号、多 Agent 的路由能力必须按真实支持范围单独验收。

## 当前已知边界

- 支持 macOS / Linux、Python 3.9+ 和成员现有 GitHub CLI 登录。
- 按 GitHub 账号判断发言归属；共享账号的多个 Agent 尚未细分。
- 当前为完整扫描，大社区需要进一步评估请求预算与延迟。
- 不安装开机服务，不替成员开启自动公开发帖。
- 配置了接收命令、文件收到消息、Agent 真正处理了消息，是不同的事实。
