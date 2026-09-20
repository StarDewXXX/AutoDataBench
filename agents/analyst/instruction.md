You are analysing how one model behaved on one task, to produce a short list of **failure modes**: ways of going wrong that a task in this benchmark can provoke. The list becomes the hidden rubric against which newly authored training data is judged, so each mode has to be stated in a way that makes sense for *other* tasks in this benchmark, not only for this one.

What you are looking at:

  __BENCHMARK_DIR__   the benchmark __BENCHMARK__, read-only: the tasks we sampled from it, one directory each, including the one below. You need this to know what "benchmark level" means when you write a mode: skim enough of these tasks to see what kind of work the benchmark asks for and what its tasks have in common, then state each mode so that it could apply to them and not only to the one task you have transcripts for. There are no transcripts for the other tasks; do not speculate about how the model would behave on them.
  __TASK_DIR__        the task the transcripts are about, inside __BENCHMARK_DIR__: its instruction, its environment, its verifier, its reference solution if it ships one. Read the verifier — it defines what counts as right.
  __ROLLOUT_DIR__     the customer model's attempts at this task. One file per attempt: the verifier's output, then the agent's full transcript. `summary.json` gives each attempt's reward and how many were counted as solved; `README.md` lists the files with sizes and a reading order.
  __RESULT__          where you write your answer.

The customer model is __CUSTOMER_MODEL__. It attempted the task __ATTEMPTS__ times, independently, with nothing carried between attempts. Read both the failed and the successful attempts.

**Read, do not imagine.** Every mode you write must come from something you actually read — a step in a transcript, a line in the verifier, a field in the task's files. Read the task in full (instruction, environment data, verifier, reference solution) and read every attempt's transcript, including the successful ones. Do not write a mode because it is a plausible way a model could fail at this kind of work, because it is a well-known agent weakness, or because the tool schema or the verifier makes it look possible: if you cannot point at the moment in a transcript, it does not go in the list. Guessing is worse than a short list — a mode nobody can find in the record will be scored against a new task anyway, and the score becomes noise.

This applies to all three sources below, including `handled`: "the model could have gone wrong here" is not enough. There has to be something in the record that shows the fault line is real — a run that varied its approach at that point, a run that hesitated or checked, a run that did it a different way from the others. If every run walked the same path without a wobble and you are only inferring that an alternative existed, leave it out.

## Why this matters

Of all the ways to build training data for a model, targeting its measured failure patterns is among the most effective: a task written against a specific, observed weakness moves the model further than one that is merely hard, novel, or in the right domain, because the training signal concentrates on the part the model does not already handle. Downstream, a researcher will be asked to write a new task for this benchmark, and that task will be scored by whether it exercises the failure modes you name here. So each mode has to be something a *different* task could exercise — a statement about how this model goes wrong on this kind of work, not a retelling of what happened on this task.

## What a failure mode is

A failure mode is one general way of going wrong, stated in one or two sentences, at a level where it applies across tasks in this benchmark. Good statements name a *behaviour at a decision*, not a fact about this task's content:

- "When two sources give conflicting instructions, decides which one governs by comparing their timestamps instead of reading them for explicit supersession language."
- "Treats an empty search result as a fact about the world rather than as a sign the query was malformed, and builds the rest of the work on the assumption that the data is not there."
- "Reports aggregates (counts, totals) where the deliverable needed the individual items listed under each group."
- "Having already taken an irreversible action under one reading of an ambiguous input, adds the actions of the other reading on top rather than resolving the ambiguity first."

Bad statements are either symptoms ("confused Roberto's exemption date") or too vague to build a task around ("made a reasoning error", "did not read carefully"). The test: could a researcher who has never seen this task read your sentence and deliberately build a task that provokes it? If not, it is not stated at the right level yet.

Each mode gets **two or three short scenario examples**: one line each, showing the same mistake in a different concrete setting within this benchmark's kind of work. These are what keep the statement from being read too narrowly or too loosely, and they are what the judge will use to recognise the mode when it shows up in a new task with a different surface. Make them genuinely different from this task's situation.

## Where failure modes come from

Three sources, and you should use all that the transcripts support:

1. **Attempts that failed.** The wrong turn that cost the task. The primary source when there are failures.
2. **Detours in attempts that succeeded.** A run that went the wrong way, noticed, and recovered still shows a failure mode — with fewer retries, or a slightly less forgiving task, it would have failed. Look for backtracking, self-corrections, a plan abandoned after evidence contradicted it, a first answer revised before submission.
3. **Error-prone spots the model handled correctly.** When the attempts went right at a point where a tempting alternative would have failed the verifier, the alternative is still a failure mode for this benchmark. But this source is the easiest to invent from, so it carries an extra requirement: the record must show the spot is a real fault line, not one you reasoned your way to. Acceptable evidence is a difference between runs at that point — one run skipped a check the others made, one added a filter the others did not, the runs disagreed on how to address the same thing — or a run visibly stopping to consider it. You must also name the tempting alternative concretely and say which verifier check it would have failed. A spot where every reasonable approach does the same thing is not error-prone, it is just the task; a spot where you can only argue an alternative *exists* is not evidence.

Record which source each mode came from and which attempts show it. When all attempts solved the task, your list will come mostly from sources 2 and 3, and that is fine — say so in `notes`.

**Aim for three to five modes; never more than six.** Each one becomes a line a new task is scored against, so a padded list dilutes the rubric and pushes a researcher towards covering everything shallowly. If the record supports only two, write two. Keep each `content` to one or two sentences.

Not failure modes, and to be kept out of the list: harness or environment failures (container died, dependency missing, run cut off before the model reached an answer — mention them in `notes`), and task defects (the instruction is ambiguous, the verifier demands something never asked for, the tolerance is impossible, the answer key looks wrong). Task defects go under `task_defects`, briefly, because a later stage needs to know this rubric stands on soft ground; but a model losing to a broken task is not a failure mode worth reproducing.

If several attempts went wrong the same way, that is one mode. If two attempts went wrong in ways that only look similar — same symptom, different mechanism — they are two modes.

Rank the modes: the first should be the one that most decides whether tasks of this kind get solved.

## Writing your answer

Write JSON to exactly this path:

  __RESULT__

with exactly this shape:

{
  "task": "__TASK_NAME__",
  "customer_model": "__CUSTOMER_MODEL__",
  "attempts": __ATTEMPTS__,
  "n_solved": <how many attempts the summary counts as solved>,
  "modes": [
    {
      "id": "short-kebab-case-handle",
      "content": "one or two sentences stating the failure mode at benchmark level: the behaviour at the decision, not this task's facts",
      "examples": [
        "one line: the same mistake in a different concrete setting",
        "one line: another setting"
      ],
      "source": {
        "observed_as": "failure | detour | handled",
        "attempts": [0, 3, 5],
        "where": "one sentence pointing at where in those attempts it shows: what the model did, and the verifier check or step that shows it"
      }
    }
  ],
  "task_defects": [
    {"what": "", "why_it_matters": ""}
  ],
  "notes": "which sources the list mostly came from; harness failures if any; anything else a reader of this rubric needs"
}

`observed_as` is exactly one of `failure`, `detour`, `handled` — a single word, never a combination. A mode often shows up more than one way across attempts (some runs failed at that point, others recovered, others got it right); when that happens pick the strongest one seen, `failure` over `detour` over `handled`, list every attempt that shows the mode in `attempts`, and describe the mix in `where`. Writing something like "failure + handled" makes the field unreadable to the harness. If a mode shows up in more than one way, use the strongest (`failure` over `detour` over `handled`) and list all the attempts. `attempts` must be real attempt numbers from the bundle. `where` must be concrete enough that someone opening those transcripts can find the moment.

Use an empty list for `task_defects` when there are none.

Before you write, check each mode once more against the record: which attempt, which step, what the model actually did there. Drop any mode that does not survive that check. Then write the file, verify it is valid JSON, and make that your final action.
