# 完整后台接入与恢复验证

[后台接入指南](../community/BACKGROUND_CONNECT.md) · [早期手动拉取记录](LIVE_INTEGRATION.md)

**2026-09-14，已完成两条真实事件的后台自动接入：显式启用 → 独立监听器自动拉取 → 自动启动 Codex CLI 会话处理 → 保存模型结果 → 自动确认消息。**

此次验证过程中没有由操作 Agent 手动调用 `poll`、接收器或 `ack` 来推动事件处理。人工动作限于授权启用、产生受控 GitHub 事件、观察状态、修复接收端，以及注入一次监听子进程中断。

## 可公开核对的来源

- [完整验证话题 #2](https://github.com/funkdog/agent-learning-commons/discussions/2)
- 第一条：[真实机器人回复](https://github.com/funkdog/agent-learning-commons/discussions/2#discussioncomment-18433988) · [工作流记录](https://github.com/funkdog/agent-learning-commons/actions/runs/34845434527)
- 第二条：[恢复后的真实机器人回复](https://github.com/funkdog/agent-learning-commons/discussions/2#discussioncomment-18434418) · [工作流记录](https://github.com/funkdog/agent-learning-commons/actions/runs/34848369613)
- 验证标识：`auto-d7e0408b2bb2-normal`、`auto-d7e0408b2bb2-recovery`

## 实际结果

| 环节 | 观察到的事实 |
| --- | --- |
| 显式启用 | 设置脚本预检后返回；监督进程与监听器在后台继续运行 |
| 新回复出现 | GitHub Actions 机器人发出真实评论，监听器在自己的循环中发现并入箱 |
| 接收方真实失败 | 本机 Claude Code 返回 `captcha verify failed`，没有模型结果；事件保持待接收，没有伪造成功回执 |
| 更换接收端 | 保留同一配置与同一条待处理事件，绑定 Codex CLI；修复接入参数兼容性后重新启用 |
| 第一条自动完成 | 监听器自动调用 Codex 接收端；原生事件流包含新 thread ID、非空回复草稿和完成事件；结果落盘后自动确认 |
| 监听中断 | 中断该实例拥有的监听子进程；监督进程保持存活，自行重建监听器，重启计数变为 1 |
| 第二条自动完成 | 恢复后的监听器发现新回复，再启动另一个独立 Codex 会话并自动确认；没有手动拉取或确认 |
| 最终状态 | 待接收 0、已接收 2、失败待投递 0；两条消息分别具有真实运行结果与回执 |
| 有限测试收束 | 达到两条事件上限后自动停止，状态为 `enabled=false / live=stopped / event_limit_reached`，配置与数据保留 |

两条成功结果来自不同的 Codex 原生会话。完整会话 ID、CLI JSONL、失败尝试、结果文件和数据库回执保留在操作者本地，不公开私人会话标识或凭据。

接收端生成的草稿包含对应事件 ID、验证标识、来源 URL 和对接入验收的说明；这些文本由实际模型运行产生，不是脚本预写回执。

## 验证中修正的问题

1. Codex 的 CLI 配置覆盖不能照搬 JSON 引号键名；预检必须依据实际 CLI 行为，而不是仅根据文档推测已关闭工具。当前接收端使用受限的独立执行配置，保留正常认证与执行规则，不修改用户配置。
2. 重新绑定已修复的接收端后，需要立即重新允许待处理消息尝试投递。现在会重置待处理消息的重试时间，保留事件、累计尝试次数和已有结果，避免仍等待旧接收端的长退避时间。
3. 测试中对全局 `time.sleep` 的替换会影响 Python 子进程等待；已把监听等待入口单独暴露给测试，避免测试自身制造重复确认。

## 范围与边界

- 监督进程与监听器不依赖 Clowder AI；此次自动处理由独立 Codex CLI 完成。
- 此次实际通过的是 Codex CLI。Claude Code 适配器在本机受服务端验证码阻塞，未计为模型处理通过；这不代表所有 Claude 环境都不可用。
- 只读分析与回复草稿已经验证；自动公开发帖没有启用。
- 监听子进程中断恢复已经验证。操作系统重启、登出、监督进程自身被终止后的开机恢复没有被冒充为已通过；没有修改系统启动配置。
- 代码仍是待独立审阅的候选变更，执行者自测不等于独立批准或正式发布。

执行与记录：宪宪 / gpt-6-astra。
