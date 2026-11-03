# ai-flows

把一次开发需求变成有方案的 Issue、可测试的 PR、独立 agent 的审查报告，以及针对问题的修复和复审。

第一版运行在 **ChatGPT / Codex 的云端会话内**。宿主 agent 负责调用真实的规划者、实现者、审查者和修复者；Python CLI 负责 GitHub 记录和状态校验。它不需要额外 OpenAI API key，也不依赖另起 `codex exec` 进程。

## 在云端开始使用

在能够执行命令并调用独立 agent 的云端会话中发送：

> 请使用 Yeap-Capital/ai-flows 的工作流处理这个任务。先读取 AGENTS.md 和 skills/ai-flow/SKILL.md，为目标仓库 OWNER/REPO 创建带方案和验收条件的 Issue，完成实现、PR、独立 review 和必要修复，最多三轮修复，最后保留 PR 等我合并。任务是：……

首次使用时，让 agent 克隆本仓库并在仓库根目录执行：

```sh
python3 -m ai_flows doctor --repo OWNER/REPO
```

需要 Python 3.11+、Git、已登录并拥有目标仓库相应权限的 GitHub CLI，以及宿主提供的独立 agent 能力。创建 Issue、推送分支和发布 review 的权限要分别验证，不能只根据能读取仓库判断写入可用。

CLI 命令从 `ai-flows` 仓库根目录运行。目标仓库可以是另一个仓库，由 agent 在单独的目录或 worktree 中实现；执行 Git 命令时切换到目标代码目录。

## 一次任务如何推进

```text
需求 → 方案与验收条件 → Issue → 实现、测试 → PR
                                            ↓
                                    独立 agent 审查
                                      ↓          ↓
                                 有阻塞问题    当前提交通过
                                      ↓          ↓
                                 修复同一 PR   等待人工合并
                                      ↓
                                    再次审查
```

每个任务只有一个协调者。实现者和审查者使用独立会话；实现者可以继续承担修复。审查报告包含做得好的地方、有证据的阻塞问题、可选建议、验收结果和测试证据。只有需要解决的问题进入修复循环，可选建议不会迫使 agent 不断重写正确代码。

## CLI

CLI 不会自行调用模型，也不会自行生成实现或审查结论。宿主 agent 按[技能文件](skills/ai-flow/SKILL.md)调用下列命令，并负责实际的 Git 操作与 agent 调度。

先让规划者生成需求和方案文件。方案应包含范围、验收条件、验证方式；可参考 [task 示例](examples/task.md)和 [plan 示例](examples/plan.md)。

```sh
python3 -m ai_flows start \
  --repo OWNER/REPO \
  --key example-task-001 \
  --title "任务标题" \
  --task-file examples/task.md \
  --plan-file examples/plan.md \
  --max-fix-rounds 3
```

`--key` 是本次任务的稳定标识。恢复或重试时复用同一标识，新任务使用新标识，避免重复创建 Issue。同 key 的不同需求会被拒绝，不能借重试覆盖原需求。

已经存在的 Issue 可以保留正文并纳入流程：

```sh
python3 -m ai_flows adopt \
  --repo OWNER/REPO --issue 123 --key existing-task-001 --max-fix-rounds 3
```

默认修复上限和必要检查由 `.ai-flows.json` 配置；`start` 和 `adopt` 将配置快照保存到 Issue。对其他仓库使用时，先把必要检查改成该仓库真实存在的检查名称，不能用空检查列表回避 CI。

实现者创建分支、完成实现和测试并提交、推送后，关联 Issue 打开 PR：

```sh
python3 -m ai_flows pr \
  --repo OWNER/REPO \
  --issue 123 \
  --branch ai/issue-123-task \
  --base main
```

再次执行时应复用已有 PR。修复继续提交到同一分支，不能每轮创建一个新 PR。

独立审查者针对实际最新提交生成 JSON 报告后，协调者发布并查询状态：

```sh
python3 -m ai_flows review --repo OWNER/REPO --pr 456 --report /tmp/review.json
python3 -m ai_flows status --repo OWNER/REPO --pr 456
```

这些命令需要填写真实的仓库、Issue、PR 和分支。报告格式见[审查者提示词](prompts/reviewer.md)。同一个 GitHub 账号可能同时承载实现者和审查者，因此使用带独立 agent 标识的 `COMMENT` review；这不是 GitHub 的人工批准，也不能替代仓库保护规则。

| 状态 | 协调者的下一步 |
|---|---|
| `needs_review` | 为最新提交启动独立审查者 |
| `needs_fix` | 核实阻塞问题，修复、补测并更新原 PR |
| `waiting_ci` | 等待该提交的必要 CI 完成，不能报告成功 |
| `ci_failed` | 查看失败日志，修复后重新测试和审查 |
| `ready` | 向用户交付 PR、测试与审查结果，等待人工合并 |
| `paused` | 按记录的原因交接，等待用户决策 |
| `limit_reached` | 交付剩余问题，不再自动修复 |

无法继续时明确暂停：

```sh
python3 -m ai_flows pause \
  --repo OWNER/REPO --pr 456 \
  --reason "剩余问题需要明确产品决策，详见当前 review。"
```

## 完成条件与恢复

只有当前 HEAD 已被独立审查、没有阻塞问题、验收及测试通过、必要 CI 全部通过，状态才能进入 `ready`。本仓库的必要检查为 `unit-tests` 和 `workflow-validation`。缺失、进行中、失败或未知的检查都不能放行。

默认三轮修复意味着初审一份报告，加上最多三次修复后的复审，最多四份报告。反复出现同一问题、没有新提交或没有可证明的进展时，协调者应提前暂停，不能通过重复发表报告消耗轮次或伪造进展。

Issue 和 PR 保存持久记录。新会话读取关联 Issue、PR、最新提交、已有 review 和 CI，再执行 `status` 继续；不依赖上一段聊天的记忆或某个临时 JSON 文件。每次写入前重新核对目标仓库、分支和提交，避免两个协调者同时推进同一 PR。换 GitHub 协调账号时需要明确交接，不能静默信任另一账号发布的记录。

## 运行边界

- 云端执行任务不要求你的电脑保持开机，执行仍受宿主的任务时限、额度、工具和权限约束。
- 本仓库没有部署常驻服务，也没有配置 GitHub webhook 或会话结束后的自动唤醒。关闭普通聊天不会把它变成永久在线的后台流水线。
- `AGENTS.md` 和技能文件是可执行工作的指令，实际调用由宿主完成。没有独立 agent 能力时应暂停，不假装完成审查。
- 不自动合并 PR，不新增付费资源，不复制或输出登录凭证。

后续接入调度器时，应继续以 GitHub 中的 Issue、PR、提交 SHA 和 review 为状态依据，并单独验证事件触发、云环境恢复及写入权限。

## 开发与本次落地记录

```sh
python3 -m unittest discover -s tests -v
```

本次落地从 [Issue #1](https://github.com/Yeap-Capital/ai-flows/issues/1) 开始。关联 PR 保存本次实现、独立审查、修复和最新提交的复审记录。
