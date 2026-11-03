---
name: ai-flow
description: 在有独立 agent 工具的云端会话中，完成 Issue、实现、PR、独立审查、修复和复审。
---

# 云端开发流程

此技能由当前宿主 agent 执行，不启动永久服务，也不要求另起模型 CLI。协调者管理流程，Python CLI 管理 GitHub 状态；二者都不能替代真实的实现和审查工作。

## 开始前

1. 读取目标仓库适用的 `AGENTS.md`，确认任务、仓库和当前工作区状态。保留用户已有改动。
2. 在本工具仓库根目录执行 `python3 -m ai_flows doctor --repo OWNER/REPO`。确认宿主能执行命令、调用真实独立 agent，并能访问目标仓库。缺少能力时暂停并指出具体缺失，不改用未获授权的 API 或账号。
3. 当前任务只能有一个协调者。为不同任务准备独立分支或 worktree，明确每个写入者的文件范围。
4. 读取 `.ai-flows.json`，确认必要检查对应目标仓库实际的 CI。为其他仓库准备独立配置文件，在 `doctor`、`start`、`adopt` 命令中传入 `--config /path/to/target-config.json`，无需修改工具仓库默认配置。新任务会保存当前配置快照；后续不能通过更改配置放宽已有任务。

## 规划并创建 Issue

使用 `../../prompts/planner.md` 整理需求、方案、范围、验收条件和验证方式。每个新任务创建一个 Issue，补充和修复使用原 Issue。

```sh
python3 -m ai_flows start --repo OWNER/REPO --key STABLE_TASK_KEY \
  --title "任务标题" --task-file /tmp/task.md --plan-file /tmp/plan.md \
  --max-fix-rounds 3
```

记录返回的 Issue。中断重试复用相同 key，不通过更换 key 回避重复任务检测。任务 key 使用 GitHub 返回的规范仓库名称生成，同仓库的大小写写法等价。用户已经明确提供且内容完整的需求可以直接形成计划；仅在缺失信息影响行为或授权时询问。

如果已有合适的 Issue，先核对正文覆盖需求、方案和验收，再保留正文并纳入流程：

```sh
python3 -m ai_flows adopt --repo OWNER/REPO --issue N \
  --key STABLE_TASK_KEY --max-fix-rounds 3
```

## 实现与 PR

将 Issue 和验收条件交给实现者。实现者读取相关代码，进行范围内修改，运行适当测试，提交并推送 `ai/issue-N-slug` 分支。空仓库可以先建立最小默认分支作 PR 基线，实际功能仍进入开发分支。

```sh
python3 -m ai_flows pr --repo OWNER/REPO --issue N \
  --branch ai/issue-N-slug --base main
```

PR 正文写清行为变化、测试结果和已知限制。CLI 按 Issue 和任务 key 找原 PR；传入另一分支会拒绝并提示原 PR 和分支，不能另建 PR 重置历史。已关闭或身份重复的任务 PR 必须先人工核对。修复必须更新同一 PR。向用户汇报时，区分本地测试、实际 GitHub CI 和尚未验证的能力。

## 独立审查与修复循环

1. 执行 `status`，读取实际最新提交 SHA 和 `next_round`。用宿主的 `spawn_agent` 或等价工具启动独立审查者，按 `../../prompts/reviewer.md` 审查 Issue、最新代码、PR diff 和测试证据。审查者只读，不参与本轮实现。可以复用此前的独立审查者复审，但必须重新读取最新代码。
2. 将审查报告保存到临时 JSON 文件。协调者不能替审查者写一个“通过”结论，也不能只根据实现者的总结编写报告。
3. 发布报告并再次读取状态：

   ```sh
   python3 -m ai_flows review --repo OWNER/REPO --pr N --report /tmp/review.json
   python3 -m ai_flows status --repo OWNER/REPO --pr N
   ```

   脚本可使用 `status --repo OWNER/REPO --pr N --require-ready` 作为门槛；非 `ready` 状态退出码为 `3`，普通状态查询成功不代表任务已完成。

4. `needs_fix` 时，按 `../../prompts/fixer.md` 逐项核实阻塞问题，交给实现者或另一个修复者修改、补测、提交并推送。记录每项处理和证据，随后回到第 1 步。如果没有代码阻塞问题，只有验收或测试证据未通过，先判断是否只需重新验证；不要为推进流程制造空提交。
5. `needs_review` 表示最新提交没有有效审查，重新审查。报告中的 SHA 过期时重新获取代码，不修改报告中的 SHA 冒充复审。
6. `waiting_ci` 时等待；`ci_failed` 时查看日志并修复。CI 变化与代码变化都需要重新检查状态。超时或无法取得结果必须标为未验证，不能当作通过。
7. `ready` 时交付 Issue、PR、测试和最新审查结果，等待人工合并。不自动合并、不替用户批准自己的 PR。

优点和非阻塞建议保留在报告中，不驱动修复循环。发现范围外问题时记录或另建后续任务，不扩大当前 PR。

同 SHA 的证据刷新仍由真实独立 reviewer 执行，必须实际重新验证。新旧报告都不得含阻塞问题，全部验收 `criterion` 和测试 `command` 必须同序保留，原来通过的项目不得回退，至少一项从未通过变为通过。使用 `status.next_round` 作为 `round`，此时它保持当前轮次；GitHub 留存各版报告，逻辑轮次和修复预算不增加，原样重试复用原记录。不能删掉阻塞问题、验收项或测试来获得刷新资格。修改代码后必须针对新 SHA 进入下一轮审查。

## 暂停、上限与恢复

默认最多三轮修复，即初审加最多三次修复后的复审，最多四个代码审查轮次；同 SHA 的合规证据刷新不新增轮次。不能通过新建 PR、换任务 key 或重置轮次规避上限。相同问题反复出现且无进展、agent 无法完成、权限失败或需要用户作决定时提前暂停：

```sh
python3 -m ai_flows pause --repo OWNER/REPO --pr N --reason "明确原因和待决定事项"
```

保留 Issue、PR、最新 SHA、最后一份 review、CI 和剩余问题。新会话先重新读取 GitHub 记录并执行 `status`，再确定下一步。本地临时文件和聊天记忆不是持久状态。更换协调 GitHub 账号需要明确交接，不能直接接管另一账号的记录。

此技能保证的是宿主可继续执行期间的会话内协作。后台定时或 GitHub 事件唤醒需要另外配置和验证，不能仅凭创建技能文件就宣称已启用。
