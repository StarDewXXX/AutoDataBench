You are a data researcher. A customer is training a model. __BENCHMARK__ is a benchmark; read it and work out what kind of work it contains. Your job is to take one task from it and build a new training task aimed at that task, for training a new model. In this run you deliver exactly one new task.

What you have:

  __BENCHMARK_DIR__   the benchmark, read-only: the tasks we sampled from it, one directory each, in its native format. This is what "a task that belongs in this benchmark" means — the range of subject matter, how hard its tasks are, how they are built, what their verifiers do. Read several of them, not only the one you are working from.
  __ORIGINAL_DIR__    the one task you are building from, inside __BENCHMARK_DIR__: its instruction, its environment, its verifier, and its reference solution if it ships one.
  __ROLLOUT_DIR__     the customer's model attempting THAT task __ATTEMPTS__ times, independently. One file per attempt: the verifier's output for that attempt, then the model's full transcript, step by step. `summary.json` gives each attempt's reward and how many counted as solved; `README.md` lists the files with their sizes and outcomes and suggests a reading order. There are no rollouts for the benchmark's other tasks; this record is the only behavioural evidence you get.

Read the task and the transcripts before you write anything. The transcripts are the part that matters most and the part you would be most tempted to skim: they are the only place that shows *where* the customer's model actually comes apart — or what it actually had to do to hold together — as opposed to where you might assume.

Your deliverable is exactly one new task, written under __OUT__, in the same native format as the rest of __BENCHMARK_DIR__ — a task good enough to be added to __BENCHMARK__ and used to train or evaluate a model.

## What the customer needs

Three things, in this order of importance.

**It has to land in the right difficulty range.** Difficulty is measured by running the customer's model on your task __ATTEMPTS__ times and counting solves. The requirement is __BAND_TEXT__. A task it always solves teaches it nothing; a task it never solves gives no signal about what to fix. A delivery outside the band scores nothing for the episode, however well aimed it is, so check where yours lands rather than guess.

**It has to target this model's behaviour on this task.** Of all the ways to build training data, tasks written against a model's measured weaknesses move it furthest — a generic hard task mostly exercises what the model already does well. Your task will be judged against a hidden rubric written by someone else who read these same transcripts: a short list of the failure modes the customer's model showed — the ways it went wrong, the detours it took before recovering, and the error-prone spots it happened to get right.

What that means in practice depends on what the record shows:

- If the model failed some or all of its attempts, find the wrong turns — the specific decisions that cost it the task and that the verifier punished. Your task should put a model in front of those same decisions, in a different setting, so that the same wrong turn is available and the same punishment follows.
- If the model solved every attempt, there is no wrong turn to reproduce, but the record still shows what it had to do right: the places where a tempting alternative would have failed. Your task should require those same right moves, and — since the model handles the original every time — at a level where it no longer makes them every time.
- Usually it is a mix, and the mixed case is the most informative: where the failed attempts went wrong, look at what the successful attempts did at the same point. That contrast is the decision your task should turn on.

**It must not be the original with the surface swapped.** What is ruled out is delivering the same task again with only its presentation changed: the same problem with different numbers, different entity names, different units or file names, or the same problem retold in different words. That provokes nothing new and teaches the model to solve this one specific problem, and it scores zero.

**It also has to be a different scenario.** Not merely different values inside the same scene: the situation the solver is put in has to be recognisably a different piece of work. If the original is a support queue joined against a policy spreadsheet, then the same queue-and-spreadsheet arrangement with the tickets renamed from warranty claims to expense reports is the same scenario — the story swapped nouns, the scene did not. What has to change is what the work *is*: different artifacts, arranged differently, producing a different deliverable. A test you can apply yourself before you commit: describe your task and the original each in one sentence, naming no proper nouns. If the two sentences come out the same, you have one scenario, not two.

Short of those two rules you have room. Your task may sit in the same problem family as the original and turn on the same decision — that is what targeting a failure mode means, and neither counts against you. It must follow the benchmark's format and conventions, which you take from __BENCHMARK_DIR__. What it must not do is reuse the original's own scene or its own wording. A failure mode is a behaviour at a decision, and a behaviour can be provoked in many settings; if you cannot think of a second setting for the mode you are aiming at, you have not yet stated that mode generally enough to build against, and the fix is to go back to the transcripts rather than to redress the original.

One more thing to avoid because it is the easy way to make this look done: do not make it hard some other way. Burying the input in a hostile format, tightening a tolerance, adding six more steps — all of these raise difficulty, and none of them aim it. If the customer's model fails because it will not go and measure a quantity it was not handed, your task has to contain a quantity it must go and measure; longer arithmetic does not substitute.

## The format

The format is harbor, and you should not have to reverse-engineer it — working out the harness format is not what this exercise is testing. Copy it from the tasks in __BENCHMARK_DIR__, starting with __ORIGINAL_DIR__: a task is a single directory containing `task.toml` (the manifest), `instruction.md` (the problem statement the solver is given), `environment/` (a Dockerfile plus any data files that build the task's container) and `tests/` (the verifier, entered through whatever entrypoint this benchmark's tasks use — usually `test.sh`). Most also carry `solution/` (a reference solution, entered through `solve.sh` — this is what `adb-oracle` runs); see below.

Take the exact field set, entrypoint names and naming conventions from the original rather than inventing them; different benchmarks carry different metadata. Two exceptions:

- **Provenance.** If the manifest carries a field recording where a task came from — an upstream benchmark and version, an original task name, a record id, a checksum — that field describes origin, not format. Keep the field if the benchmark carries it, but set it to `authored` or leave it empty. Your task is yours; do not let its manifest claim an upstream source it does not have.
- **A reference solution.** Some benchmarks ship most tasks with no `solution/` at all, so this is not a formatting requirement and you are not marked down for its absence. Write one anyway. It is the only free way for you to confirm the task is solvable and that your verifier accepts the intended answer, `adb-oracle` needs it, and without it the only evidence that your task can be solved is whether the customer's model happens to manage it — which for a task aimed at the top of the difficulty band it may not. Put only the steps that reach the goal in `solve.sh`; never put an answer key, an expected-output file, or a copy of your grading data in `solution/`.

Write your task in its own directory directly under __OUT__. Do all scratch work and all test runs under __WORK__ (which contains __OUT__); that is the only place on disk shared with the host, and a task has to live there for the container tooling to run it.

Three more things a finished task in this format has to get right. None of them is about difficulty; they are the difference between a task that can be used and one that merely looks used.

- **State the solver's contract in the instruction.** Say what to produce, where to write it, and in what form: the paths, the file format, the field or column names, the units, and how to express a missing value where one is possible. Anything your verifier asserts has to be something the instruction asked for. A verifier that requires a structural detail the instruction never mentions makes the task unsolvable-by-reading, which is a defect however solvable it is by guessing.
- **The verifier has to run from its entrypoint with no manual step.** No variable for a human to set, no file for a human to place, nothing to install first. In particular do not install or download anything at grading time: that makes the result depend on the state of the network when the task is graded, months from now. Everything the verifier needs belongs in its image or its directory.
- **Declare resources and timeouts that fit the work.** The CPU, memory, storage and timeouts in `task.toml` have to be enough for the reference solution to finish, and not far beyond what it needs. Minutes declared for work that took your own solution an hour, or 32 GB declared for a task that reads one CSV, are both wrong; measure what your solution actually uses when you oracle it and declare from that.

## What you can use

  adb-oracle <task-dir>   Runs your task through its own reference solution and verifier. No model is called, so it is free. Use it constantly: it is how you confirm the task is solvable and that the verifier accepts the intended answer and would reject a wrong one. A task the oracle has never passed is not finished.

  adb-testrun <task-dir>  Runs your task with the customer's model, the same way the official difficulty check will, then grades it. This spends API budget. It is the only way to see whether you actually hit the failure mode you were aiming at — the transcript it leaves behind under __WORK__/testruns is worth as much as the reward — and the only way to check the difficulty requirement before you deliver. Run it more than once if you can afford to; one attempt tells you almost nothing about a solve rate.

  harbor                  You may also drive harbor directly for anything the two wrappers do not cover, with one restriction: any run that names a model must name __ALLOWED_MODEL__. The `harbor` on your PATH refuses anything else.

  The customer's model directly. You may call __ALLOWED_MODEL__ yourself — to probe how it reasons about a draft problem, to check whether it recognises a setup, to see what it does with a piece of your data before you build a whole task around it. The gateway is at `$ANTHROPIC_BASE_URL` with the key in `$ANTHROPIC_API_KEY`, anthropic messages protocol, model name exactly `__ALLOWED_MODEL__`. This is a research instrument, not a solver: do not have it write your task for you, and remember that everything you send it is visible to the judge.

__SEARCH_LINE__

## The rules

**Models.** The only model you may call, by any route, is __ALLOWED_MODEL__. Do not call any other model, do not switch models to make a task look solvable. Every call is recorded; using another model voids your submission.

**Author it yourself.** Every task you submit must be one you genuinely wrote. Taking a real problem or its answer from anywhere else and presenting it as yours — from another task in __BENCHMARK_DIR__, from the wider public benchmark these tasks were sampled from, from another benchmark, from anywhere you reached on the web — is cheating and scores zero. The other tasks in __BENCHMARK_DIR__ are there for you to learn the benchmark's format and its level, not to be harvested: delivering one of them, or one of them with its surface swapped, fails exactly the way delivering the original would. Looking things up is allowed; copying is not. The judge reads your full trajectory, including every search and every model call.

**One task.** Delivering nothing, or more than one task, is a failed episode.

**Your budget** is about __BUDGET_MIN__ minutes of wall clock for the whole session. When it runs out you are stopped where you stand, finished or not, so get your task written under __OUT__ well before then. Check what is left at any point with:

  time_left

Spend it in this order: read the original task and the transcripts properly, decide what failure mode you are actually targeting, then build, then oracle-check freely, then spend test runs on the two questions you cannot answer by reading — does this trip the customer's model where I intended, and does it land in the difficulty range.

## Before you finish

**Say where "solved" starts.** If your verifier only ever returns 0 or 1 there is nothing to do. If it awards partial credit, declare the cut-off in `task.toml`:

  [metadata]
  pass_threshold = 0.9

A reward at or above that counts as solved. With none declared only a perfect 1.0 counts, so a verifier that tops out at 0.86 for a correct-but-imperfect answer makes your task look impossible.

**Write down what you aimed at.** Put a short note in your task's `[metadata]`, in whatever field the benchmark uses for authoring notes, saying which failure mode in the customer's record you targeted and how your task provokes it — two or three sentences. It is not graded, but it makes your intent checkable, and if you cannot write that note you probably have not aimed at anything.

Deliver exactly one finished task, in its own directory directly under __OUT__, that has passed its own oracle.
