<h1 align="center">AutoDataBench</h1>

<p align="center">
  <b>Can agents write the data that feeds the self-improvement loop?</b>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> &nbsp;·&nbsp;
  <a href="#why-this-benchmark">Why</a> &nbsp;·&nbsp;
  <a href="#how-an-episode-works">How it works</a> &nbsp;·&nbsp;
  <a href="#scoring">Scoring</a> &nbsp;·&nbsp;
  <a href="LICENSE">Apache-2.0</a>
</p>

<p align="center">
  <img src="docs/figs/overview.png" alt="What AutoDataBench measures" width="820">
</p>

Data production today is a team of people working alongside a coding agent, reading
an existing suite of executable tasks and writing new ones, which a quality check
accepts or sends back before any training run. AutoDataBench replaces that team with
the single agent under evaluation and holds everything else fixed: the suite, the
tools, and the check itself. Only the output of the check differs, a score for the
agent rather than a delivery decision.

<p align="center">
  <img src="docs/figs/results.png" alt="Scores" width="680">
</p>

Five agents, each authoring against the same target model on the same 24 original
tasks. None passes 0.2, and the decomposition puts the failure away from targeting:
rubric coverage is close to saturated while the pass rate fails at both ends, with
delivered tasks tending to be solved on every attempt or on none.

## Quick start

Needs Python 3.11+, Docker, and [harbor](https://github.com/laude-institute/harbor).

Before an episode can run, the target model has to have attempted each original
task six times, and an analyst has to have turned that record into a rubric. Those
144 attempts are setup cost, not measurement, so ours are published and you can
start from them:

```bash
curl -L -o rollouts.tar.gz \
  https://huggingface.co/datasets/LordNoah/AutoDataBench/resolve/main/rollouts-deepseek-v4-pro.tar.gz
tar xzf rollouts.tar.gz

# the rollouts and the rubrics derived from them live in separate trees
for b in automationbench terminal-bench tb-science; do
  for t in rollouts-deepseek-v4-pro/$b/*/; do
    n=$(basename "$t")
    mkdir -p "prep/deepseek-v4-pro/$b/$n" "rubrics/deepseek-v4-pro/$b/$n"
    cp -r "$t/rollout"   "prep/deepseek-v4-pro/$b/$n/"
    cp    "$t/modes.json" "rubrics/deepseek-v4-pro/$b/$n/"
  done
done
```

Then point `gateway.base_url` in `configs/default.json` at an endpoint that speaks
the Anthropic Messages API, and:

```bash
export AUTODATABENCH_API_KEY=...        # never written to a config
export HARBOR_REAL=$(which harbor)

python3 harness/run_episode.py --benchmark tb-science --episodes 3
python3 harness/aggregate.py   --run-id <run_id>
```

Output lands in `runs/<run_id>/<benchmark>/<task>/<ep>/`, one directory per
episode, with `score.json` at its root.

To evaluate against a different target model, generate your own record instead:
`run_rollout.py` then `run_analyst.py`. A rubric states one model's failure modes,
and scoring a delivery against another model's rubric is a quiet, plausible-looking
error.

## Why this benchmark

Recent gains in language model capability have come more from data than from
architecture, and for agentic reinforcement learning the unit of data is not a text
pair but a task: an executable environment, a verifier that decides whether the work
was done, and a difficulty suited to the model being trained. Producing those is
expert work, so the volume of training data is tied to the supply of experts.
Automating the step would let training data scale with compute instead, and it is an
essential link in recursive self-improvement, where a model writes the data used to
train its successor.

A training pipeline accepts a delivery against criteria set in advance, not against
the outcome of a training run: one task's contribution is not separable from the
data mixture, the schedule and the base model, so a training-based verdict on a
single artifact is unavailable in principle. Three criteria stand in for it, and
AutoDataBench turns each into one term.

| criterion | term | how it is decided |
|---|---|---|
| usable at all | **gate** | a judge checks seven disqualifying defects |
| difficulty in range | **difficulty** | by execution: the target model attempts the new task K times |
| elicits the original's behaviour | **quality** | a judge reads the new task's transcripts against a hidden rubric |

Existing evaluations of this capability measure it through the model the data
produces, so no individual artifact is ever judged the way a pipeline would judge
it. Here the artifact is the unit, and the target model is held fixed across every
agent evaluated, so scores are comparable.

## How an episode works

| role | what it is | config key |
|---|---|---|
| **target model** | the model being trained; the one whose weaknesses we are trying to hit | `customer.model` |
| **agent under evaluation** | authors the new task | `researcher.model` |
| **analyst / judge** | read transcripts and decide what happened | `analyst.model`, `judge.model` |

The code calls the target model the *customer*, and that name appears in paths and
config keys.

<p align="center">
  <img src="docs/figs/episode.png" alt="One episode" width="860">
</p>

The analyst reduces the target model's record of the original task to a hidden
rubric, which the agent under evaluation never sees. The agent delivers one new
task; the target model then attempts it under that task's own verifier, which fixes
the difficulty term by execution. The judge reads those new transcripts, not the
task's appearance, and decides the gate and rubric coverage.

The agent is given the whole sampled subset read-only, so it can learn the suite's
format and level from sibling tasks. It sees the transcripts for exactly one of
them, and delivers one task.

## Scoring

<p align="center">
  <b>score&nbsp; = &nbsp;gate &times; difficulty &times; quality</b>
</p>

The terms multiply because a task that leaks its answer and a task the target model
always solves are both unusable whatever their quality.

**gate** (0/1) — the seven defects in `rubrics/gate.md`. The one this exercise turns
on is the **surface swap**: the original with only its presentation changed, the same
problem retold. Asking for one new task per original makes cloning the obvious
shortcut. What the gate does *not* forbid is staying in the original's problem family
or turning on the same decision — that is what targeting a failure mode means. The
line is whether a solver of the original would still have something to work out.

Before the judge sees anything, the harness writes a mechanical word-level diff of
the two instructions. If every differing span is a renaming, the gate fires and no
amount of same-family reasoning overrides it.

**difficulty** (0/1) — the target model attempts the delivered task K times under
that task's own verifier. 1 when the solve rate lands inside `[band_lo, band_hi]`.
A task always solved and a task never solved are equally useless as training data.

**quality** ([0,1]) — coverage of a hidden rubric the agent never sees, judged from
the **new task's transcripts** rather than from how the task reads. Per mode the
judge answers one question: did the new task put the target model at this decision?
It must cite an attempt, a step and a quote to answer yes.

The score is not the raw fraction. With `N` scoreable modes and a coverage target
`a`, `quality = min(1, covered / ceil(a * N))`, so at the default `a = 0.6` three
modes of five earn full marks. Full coverage is the wrong thing to ask for: one new
task cannot stage every mode of the task it came from without being that task. The
judge is never told `a`.

Format is also measured, and recorded in `score.json`, but does not enter the score:
across in-band deliveries it concentrates between 0.75 and 1.0, too narrow to
separate agents, and a delivery that fails those checks badly enough is caught by the
gate instead.

`aggregate.py` averages episodes per original task first, then tasks into a benchmark
score, so a task that happened to get more repeats does not weigh more.

## Configuration

Everything is in `configs/default.json`. Copy it to `configs/<name>.local.json`
(gitignored) and pass `--config` to override anything. The gateway key is never
committed: `api_key` stays `"env:AUTODATABENCH_API_KEY"` and is resolved from the
environment at load time, failing loudly if unset.

The agent gets a web search tool inside its container as `adb-search`, capped at
`researcher.search_max_calls` per session and logged next to its trajectory.
`researcher.search_script` points at a reference implementation; swap it for any
executable honouring the contract in that file's header.

## Licence

Apache-2.0; see [LICENSE](LICENSE). `benchmarks/` is third-party material under its
own terms, recorded in [ATTRIBUTION.md](ATTRIBUTION.md) together with the upstream
commit and sampling seed for each benchmark.
