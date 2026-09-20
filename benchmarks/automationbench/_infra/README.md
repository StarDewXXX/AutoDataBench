# automationbench（harbor 适配形态）

这是 AutomationBench 转成 harbor 任务后的形态——AutoDataBench 只保留适配后的形态，不保留上游那份 python 包。共 28 题，7 个业务域各 4 题（simple / sales / marketing / operations / support / finance / hr），从上游按域挑选（satisfiable 优先，同域内尽量不同 app / 不同动作以扩覆盖）。

## 布局

```
tasks/<domain>/<task>/     每题一个 harbor 任务：instruction.md + task.toml + environment/ + tests/
base-image/Dockerfile      每题 environment/Dockerfile 都 FROM automationbench-harbor-base:2
engine/                    abctl（shell bridge + grader）与 ab-init / ab-harden，烤进 base image
vendor/                    上游 automationbench 引擎（世界模型、rubric、打分），烤进 base image
build-base-image.sh        构建那个 base image（首次在新机器上跑一次）
```

## 跑之前先构建 base image

任务本身自包含（一个 `FROM` + 几 KB 世界 JSON），但都继承 `automationbench-harbor-base:2`。新机器上先建一次（需要联网装 pip，走 rootless daemon）：

```bash
benchmarks/automationbench/build-base-image.sh
```

之后 harbor 就能跑，例如用 oracle 验参考解：

```bash
harbor run -p benchmarks/automationbench/tasks -a oracle --yes -o runs/oracle   # 每题 reward 应为 1.0
```

## 上游与再转换

适配工具链在 `~/projects/automationbench-harbor/`（convert.py + setup.sh + 保真/静态门）。原始上游 python 包（含 `automationbench/domains/` 答案键）从 AutoDataBench 移到了 `~/projects/automationbench-upstream/`，需要新增题或改动重转时作 `setup.sh --upstream` 的源：

```bash
cd ~/projects/automationbench-harbor
./setup.sh --upstream ~/projects/automationbench-upstream --tasks <domain.task ...> --smoke 3
```

setup.sh 会过保真门（转出的每题与上游字节一致）+ 静态门 + oracle smoke。本批 28 题当时全过、smoke 3 道全 1.0。

## 评分接入状态

`prompts/automationbench/{rubric_true,rubric_brief}.md` 已备（当前为简单占位版，待专家细化），管线上已可跑，但还没真正端到端验过——已充分验证过的是 tb-science 和 terminal-bench。详见仓库根 README「已知限制」。
