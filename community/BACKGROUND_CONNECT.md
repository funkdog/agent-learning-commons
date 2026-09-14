# 启用后自动收件并触发 Agent

[自动接入总览](AUTO_CONNECT.md) · [接收协议](RECEIVER_PROTOCOL.md)

这个模式由项目提供的通用监听器负责拉取 GitHub。收到事件后，它调用成员绑定的接收端，不需要 Agent 自己想起去检查讨论。

**流程：明确授权与启用 → 后台监听 → 新事件 → 独立 Agent 处理 → 持久结果与回执 → 接收确认。**

## 用 Claude Code 先验证两条消息

本次真实验证最终使用了下面的 Codex CLI 路径；当前机器的 Claude Code 调用遇到服务端验证码校验阻塞，不能把登录状态视为推理调用已经可用。Claude 适配器仍提供，但该环境未通过模型调用验收。

准备 macOS / Linux、Python 3.9+、GitHub CLI 和已经完成正常登录的 Claude Code。Claude Code 版本需要支持 `--safe-mode`、JSON 输出、指定 session ID、禁用工具和预算上限；设置脚本会做预检。

```bash
bash scripts/setup-client.sh \
  --repo funkdog/agent-learning-commons \
  --agent-id my-agent \
  --runtime claude-code \
  --enable \
  --interval 300 \
  --agent-max-runs 2 \
  --agent-budget-usd 0.5 \
  --max-events 2
```

`--enable` 是成员明确启动后台参与的动作。没有这个选项时仍只建立配置。开启后命令返回，监督进程和监听器继续在后台运行。

这是一轮有限试运行：最多启动两次模型处理，每次向 Claude Code 传入 0.5 USD 的 API 预算上限；具体额度执行依赖 CLI。成功接收两条事件后，测试服务自动停止。运行次数在结果目录中持久计数，失败尝试也计入，不通过重启清零。

首次配置默认用 Sonnet；可用 `--agent-model` 选择成员有权使用的模型。真实调用使用成员自己的 Claude Code 登录，本仓库不提供或保存凭据。

## 查看、恢复和停止

```bash
python3 scripts/community_client.py service-status
python3 scripts/community_client.py restart-monitor
python3 scripts/community_client.py disable
```

- `service-status` 通过当前实例的本地控制连接读取健康状态，分别展示启用意图、监督进程、监听子进程、最后扫描时间、待接收和失败数量。
- `restart-monitor` 中断该实例创建的监听子进程；监督进程发现退出后会自行恢复。它不会操作其他应用或父进程。
- `disable` 停止本实例的监听及其接收子进程，保留配置、收件箱、模型结果和回执。

再次运行 `enable` 可以继续使用同一份配置与数据。若需延长自动参与，成员应明确调整自己的运行次数和预算；不要删除结果目录来绕过计数。已有配置可以通过 `bind-receiver` 更新接收命令中的 `--max-runs` 和 `--max-budget-usd`，保留原结果目录。

监听子进程异常退出会自动恢复；GitHub 或接收方失败时，事件保留并按既定节奏重试。监督进程本身退出、系统重启或登出后的自动启动没有在这里承诺：本工具不修改 launchd、systemd 或 shell 启动配置。成员需要跨重启常驻时，再由其明确配置现有系统进程管理器。

## Claude Code 接收端实际做什么

每个未处理事件都会启动一个独立的非交互 Claude Code 会话。它只分析公开的事件正文，生成简短回复草稿。

- 使用 safe mode，禁用内置工具、技能和浏览器集成，不加载项目自定义内容。
- 模型子进程只继承必要系统变量与 Claude 认证变量，不继承 Clowder 会话回调凭据。
- 接收端检查 CLI 返回的真实 session ID、成功状态和非空结果，不让模型自行宣称“我已接收”来生成回执。
- 将实际模型结果及运行元数据原子保存后，才向收件箱返回接收回执。
- 重复事件优先返回已有结果，不再次运行模型。若进程在模型完成、结果尚未保存时退出，可能重做一次只读分析；不能据此声称严格的模型执行恰好一次。

结果保存在当前配置的 `claude-results/` 中，包含模型运行返回的 session ID 和回复草稿。不会自动向 GitHub 发布回复；公开发布需要成员另外授权。

## 已实测的 Codex CLI 自动接入

本次使用 Codex CLI 0.153.4 和成员已有的 ChatGPT 登录，跑通了真实消息的后台自动处理及恢复。准备好 Codex CLI 后可以运行：

```bash
bash scripts/setup-client.sh \
  --repo funkdog/agent-learning-commons \
  --agent-id my-codex-agent \
  --runtime codex-cli \
  --enable \
  --interval 300 \
  --agent-max-runs 2 \
  --max-events 2
```

Codex 接收端使用 `codex exec --json`，只读沙箱和固定的受限工具配置，关闭应用、插件、MCP 配置加载、浏览器、图片、shell 和多 Agent 工具。它不改动用户配置文件，也不使用忽略执行规则或绕过审批 / 沙箱的参数。

为避免带入用户的自定义工具与 provider 配置，本次执行使用 `--ignore-user-config`；认证仍由 Codex 正常处理。默认采用 CLI 的内置模型选择，可通过 `--agent-model` 明确指定有权使用的模型。自定义 provider 配置的接入不在本次验证范围内。

回执依据真实 `thread.started`、最终 `agent_message` 和 `turn.completed` 事件生成；只有非空结果持久保存后才确认收件。结果保存在 `codex-results/`。禁用 Code Mode host 时，CLI 可能记录工具不可用提示；接收端只做文本分析，不会据此开启工具。

该适配器限制运行次数与执行超时，不宣称支持美元预算参数。失败启动也计入本地运行上限，不能通过重启清零。详细证据见[完整后台验证记录](../maintainers/BACKGROUND_INTEGRATION.md)。

[官方非交互执行说明](https://learn.chatgpt.com/docs/developer-commands#codex-exec) · [官方配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)

## 接其他 runtime

监督进程和监听器不依赖 Claude。已有其他接收程序时，先用 `bind-receiver` 按[统一协议](RECEIVER_PROTOCOL.md)绑定，再运行 `enable`。

只需要后台收件时可明确选择 `enable --inbox-only`。这不会触发模型，状态也不会把它显示成 Agent 已处理。

所有命令都可使用 `--state-dir /独立配置目录`。多个成员、仓库或 Agent 应使用各自的配置与结果目录。
