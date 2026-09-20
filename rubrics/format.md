# Format rubric

Eight checks on whether the delivered task is a well-formed member of its benchmark. Each is 1 or 0; the format score is the mean. These are hygiene, not quality — a task can pass all eight and still be worthless, which is what the quality rubric is for. Score them by reading the delivered task and comparing against the original task it was built from, whose layout is the reference for every convention question.

Score a check 0 whenever you cannot confirm it. "Probably fine" is 0.

1. **The required pieces are present.** `task.toml`, `instruction.md`, `environment/` with a Dockerfile, and `tests/` with the verifier entrypoint the benchmark uses. A benchmark whose originals use different names uses those instead; the original task in front of you is the authority.

   A reference solution is deliberately not part of this check. These benchmarks disagree about it — some ship one with every task, some ship none at all — so a missing `solution/` is not a format defect. Whether the task can be solved at all is a gate, and it is judged there on evidence rather than on the presence of a directory.

2. **The manifest carries the benchmark's own field set.** Compare `task.toml` against the original's field by field: the same tables, the same keys, values of the same kind. Extra keys the benchmark never uses, or missing keys it always has, score 0.

3. **Provenance says authored.** Any field recording where a task came from — upstream benchmark, original task name, record id, checksum of an original — is either absent, empty, or set to `authored`. A task whose manifest claims an upstream source it does not have scores 0 here regardless of what else is right.

4. **The task lives in one directory, at the expected depth.** One directory directly under the delivery root, containing everything the task needs. Nested task directories, or files left outside the task directory, score 0.

5. **The solver's contract is stated.** The instruction says what to produce, where to write it, and in what form — paths, file format, field or column names, units, and how to express a missing value where one is possible. A verifier that asserts a structural requirement the instruction never mentions scores 0.

6. **The verifier runs from the entrypoint without manual steps.** `test.sh` is executable and self-contained: it does not expect a human to set a variable, place a file, or install something first. If it installs or downloads at grading time, score 0 — that makes the result depend on the state of the network at grading time.

7. **Declared resources and timeouts are plausible for the work.** The declared CPU, memory, storage and timeouts are enough for the reference solution to finish, and not wildly beyond what it needs. A task declaring minutes for work that took the reference solution an hour, or 32 GB for a task that reads one CSV, scores 0.

8. **The solved threshold is coherent.** If the verifier can only return 0 or 1, nothing is declared and that is correct. If it awards partial credit, `[metadata] pass_threshold` is declared, and the reference solution's own reward reaches it. A partial-credit verifier with no declared threshold scores 0, because then only a perfect score counts as solved and the task looks harder than it is.
