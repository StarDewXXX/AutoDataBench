#!/usr/bin/env python3
"""Shared plumbing for the AutoDataBench v2 harness.

Everything here is machinery, not policy: how to reach the gateway, how to ask
harbor to run a task, how to read a reward back out, where the pieces of a run
live on disk. The scoring policy itself is in run_score.py, and the prompts that
carry the actual judgement criteria are under agents/ and rubrics/.

Most of this is carried over from the v1 harness (no longer in this repository), where
each piece was arrived at by hitting the failure it prevents; the comments that
record those reasons are kept because the reasons have not changed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # <repo>
# Kept as an alias rather than removed: v2 lived one level down (<repo>/v2), so a
# handful of call sites resolved repo-relative paths through REPO. After the tree
# was flattened in 2026-09-10 the two are the same directory, and a relative path
# in a config now means "relative to the repository root" either way.
REPO = ROOT


# --------------------------------------------------------------------------- #
# logging / config
# --------------------------------------------------------------------------- #

def log(tag: str, msg: str) -> None:
    print(f"[{tag}] {msg}", file=sys.stderr, flush=True)


def rel(p: str | Path) -> str:
    """Path relative to the v2 root for display, or absolute when it is outside.

    Plain Path.relative_to raises when the path is not under the root, which
    happens whenever runs/ or prep/ is a symlink to another filesystem -- and a
    crash while formatting a log line would take down a run that had otherwise
    just succeeded.
    """
    p = Path(p)
    try:
        return str(p.resolve().relative_to(ROOT))
    except ValueError:
        return str(p)


def load_config(path: str | Path | None = None) -> dict:
    p = Path(path) if path else ROOT / "configs" / "default.json"
    cfg = json.loads(Path(p).read_text())
    key = cfg.get("gateway", {}).get("api_key", "")
    if isinstance(key, str) and key.startswith("env:"):
        var = key[4:]
        resolved = os.environ.get(var, "")
        if not resolved:
            raise SystemExit(
                f"gateway.api_key is '{key}' but ${var} is unset. "
                f"Export it before running (never commit the literal key)."
            )
        cfg["gateway"]["api_key"] = resolved
    return cfg


def task_timeout_sec(cfg: dict, benchmark: str) -> float:
    t = cfg.get("task_timeout_sec", {})
    return float(t.get(benchmark, t.get("default", 1800)))


# --------------------------------------------------------------------------- #
# reading a harbor task directory
# --------------------------------------------------------------------------- #

def is_task_dir(d: Path) -> bool:
    return (d / "task.toml").is_file()


def discover_tasks(root: Path) -> list[Path]:
    """Task directories under `root`, at any depth, outermost-first.

    A task.toml nested INSIDE another task's directory is ignored, so a task
    shipping fixtures that happen to contain a task.toml cannot spawn phantom
    entries. Used both to walk an upstream benchmark (which may nest tasks under
    domain directories) and to find what a researcher wrote under out/.
    """
    found = sorted(p.parent for p in root.rglob("task.toml"))
    return [d for d in found if not any(t in d.parents for t in found)]


def _toml_scalar(task_dir: Path, table: str, key: str) -> str | None:
    """Read one scalar out of a task.toml without a TOML dependency.

    Deliberately a line scanner: the harness must read manifests written by an
    agent we do not control, and a strict parser turning a stray character into
    an exception would fail a whole run over a formatting slip.
    """
    try:
        lines = (task_dir / "task.toml").read_text().splitlines()
    except OSError:
        return None
    cur = ""
    for raw in lines:
        s = raw.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1].strip()
            continue
        if cur != table or "=" not in s or s.startswith("#"):
            continue
        k, _, v = s.partition("=")
        if k.strip() != key:
            continue
        v = v.strip().strip(",").strip()
        if v[:1] in ("'", '"') and v[-1:] in ("'", '"'):
            v = v[1:-1]
        return v
    return None


def task_name(task_dir: Path) -> str:
    return _toml_scalar(task_dir, "task", "name") or task_dir.name


def task_cpus(task_dir: Path, default: int = 4) -> int:
    v = _toml_scalar(task_dir, "environment", "cpus")
    try:
        return max(1, int(float(v)))            # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def task_pass_threshold(task_dir: Path, default: float = 1.0) -> float:
    """Reward at or above which an attempt counts as solved.

    A task whose verifier awards partial credit declares its own cut-off in
    [metadata] pass_threshold; with none declared only a perfect 1.0 counts, so a
    verifier that tops out at 0.86 for a correct answer would otherwise look like
    a task nobody can solve.
    """
    v = _toml_scalar(task_dir, "metadata", "pass_threshold")
    try:
        f = float(v)                            # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return f if 0.0 < f <= 1.0 else default


def agent_timeout_sec(task_dir: Path, default: float = 1800.0) -> float:
    v = _toml_scalar(task_dir, "agent", "timeout_sec")
    try:
        return float(v)                         # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def verifier_env_vars(task_dir: Path) -> set[str]:
    """Names declared in [verifier.env]. harbor interpolates their ${...} values
    from its OWN process env and refuses the task if one is unset, so the harness
    has to know what a task demands before launching it."""
    out: set[str] = set()
    try:
        lines = (task_dir / "task.toml").read_text().splitlines()
    except OSError:
        return out
    inside = False
    for raw in lines:
        s = raw.strip()
        if s.startswith("[") and s.endswith("]"):
            inside = s[1:-1].strip() == "verifier.env"
            continue
        if inside and "=" in s and not s.startswith("#"):
            out.add(s.split("=", 1)[0].strip())
    return out


# --------------------------------------------------------------------------- #
# reading results back out of a harbor run
# --------------------------------------------------------------------------- #

def newest_first(paths) -> list[Path]:
    """Candidate files, newest by mtime first.

    An attempt directory can hold more than one harbor job: a run that was killed
    or crashed leaves its timestamped job dir behind, and a rerun of the same
    attempt adds another beside it. Reading the FIRST match in path order then
    picks the oldest -- which produced the failed transcript -- while the reward
    comes from the newest, silently pairing one attempt's outcome with another's
    trace. Everything that reads an attempt dir goes newest-first for that reason.
    """
    out = [Path(x) for x in paths]
    out.sort(key=lambda x: (x.stat().st_mtime if x.exists() else 0), reverse=True)
    return out


def read_reward(job_out: Path) -> float | None:
    """The reward of a single-task harbor run, or None if it produced none.

    None means "no verdict" (build failure, timeout, crash) and is deliberately
    distinct from 0.0 ("ran and failed"): the two must not be averaged together.
    """
    for res in newest_first(job_out.rglob("result.json")):
        try:
            data = json.loads(res.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        r = _dig_reward(data)
        if r is not None:
            return r
    for rw in newest_first(job_out.rglob("reward.txt")):
        try:
            return float(rw.read_text().strip())
        except (OSError, ValueError):
            continue
    return None


def _dig_reward(obj) -> float | None:
    """harbor's result.json nests the reward differently across versions, and in
    some shapes the reward is a KEY mapping to the trial names that scored it."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "reward":
                if isinstance(v, (int, float)):
                    return float(v)
                if isinstance(v, dict):
                    for kk in v:                       # {"1.0": [trial, ...]}
                        try:
                            return float(kk)
                        except (TypeError, ValueError):
                            pass
            r = _dig_reward(v)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _dig_reward(v)
            if r is not None:
                return r
    return None


def rollout_dirs(rollout_root: Path) -> list[Path]:
    return sorted(
        (p for p in rollout_root.glob("attempt-*") if p.is_dir()),
        key=lambda p: int(re.sub(r"\D", "", p.name) or 0),
    )


# --------------------------------------------------------------------------- #
# harbor invocation
# --------------------------------------------------------------------------- #

# Pin numeric-library thread pools inside every task container to the container's
# CPU quota. `--cpus` throttles CPU time but does not shrink the core count the
# container sees: nproc stays at the host's, so numpy/scipy/BLAS default to that
# many threads and pile them onto a 1-2 core quota -- context-switch churn and a
# load-average spike with no speedup.
THREAD_CAP = os.environ.get("ADB_THREAD_CAP", "4")
_THREAD_VARS = (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "OMP_THREAD_LIMIT",
)


def thread_flags(flag: str, cap: int | str | None = None) -> list[str]:
    c = THREAD_CAP if cap is None else str(cap)
    out: list[str] = []
    for v in _THREAD_VARS:
        out += [flag, f"{v}={c}"]
    return out


# Which harnesses speak the OpenAI protocol. claude-code speaks Anthropic.
OPENAI_PROTOCOL_AGENTS = {"codex", "mini-swe-agent"}


def agent_route(agent: str, model: str, base_url: str, api_key: str) -> tuple[str, list[str]]:
    """Map a role's (harness, configured model) to harbor's -m value and the
    protocol-appropriate credential pairs. Same gateway URL and key for both
    protocols; only the model-name form and the env-var names change.

    codex and mini-swe-agent need the OpenAI-compatible base at '{root}/v1' --
    the root serves a web app there, so without the suffix a completion request
    gets an HTML page back.
    """
    if agent in OPENAI_PROTOCOL_AGENTS:
        bare = model.split("/")[-1]
        if bare.startswith("anthropic-"):
            bare = bare[len("anthropic-"):]
        openai_base = base_url.rstrip("/") + "/v1"
        return f"openai/{bare}", [
            f"OPENAI_BASE_URL={openai_base}", f"OPENAI_API_BASE={openai_base}",
            f"OPENAI_API_KEY={api_key}",
        ]
    return model, [f"ANTHROPIC_BASE_URL={base_url}", f"ANTHROPIC_API_KEY={api_key}"]


def flag_pairs(flag: str, pairs: list[str]) -> list[str]:
    out: list[str] = []
    for p in pairs:
        out += [flag, p]
    return out


class Harbor:
    """Thin wrapper around the harbor CLI: one method per role we launch."""

    def __init__(self, harbor_bin: str, base_url: str, api_key: str,
                 setup_mult: float, inner_verifier_model: str,
                 claude_code_version: str | None = None):
        self.bin = harbor_bin
        self.base_url = base_url
        self.api_key = api_key
        self.setup_mult = setup_mult
        self.inner_verifier_model = inner_verifier_model
        # Pin the claude-code release. harbor installs it per container from
        # `bootstrap.sh` with no version, so every container takes whatever is
        # latest at that moment -- and on 2026-09-09 at 06:31 the newly published
        # 2.1.265 installed without putting `claude` on PATH, so every run started
        # after that minute died with "claude: command not found": 8 syntheses and
        # 5 judge runs lost. A pinned version is passed as an agent kwarg.
        self.claude_code_version = claude_code_version

    def _agent_kwargs(self, agent: str) -> list[str]:
        if agent == "claude-code" and self.claude_code_version:
            return ["--ak", f"version={self.claude_code_version}"]
        return []

    def _env(self) -> dict:
        env = dict(os.environ)
        # claude-code reads these from harbor's own process env.
        env["ANTHROPIC_BASE_URL"] = self.base_url
        env["ANTHROPIC_API_KEY"] = self.api_key
        return env

    def run(self, args: list[str], out_dir: Path, timeout: float) -> int:
        out_dir.mkdir(parents=True, exist_ok=True)
        # --max-retries 0 is harbor 0.20.0's own default, and is passed explicitly so
        # that it stays the default for us whatever a later harbor decides. harbor's
        # retry does not clear the task's output directory before the second attempt,
        # so a retried researcher session writes its task beside the dead session's,
        # and a two-task delivery is gate 0 -- charged to the researcher for something
        # the harness did. That is what happened to the batch a friend ran with -r on
        # 2026-09-11. Every retry decision here is made after the fact by
        # harness/retry.py, which can tell a timeout from a fault and clears the
        # episode before redoing it.
        cmd = [self.bin, "run", *args, "--max-retries", "0", "-o", str(out_dir), "--yes"]
        with open(out_dir / "harbor.log", "w") as logf:
            proc = subprocess.run(
                cmd, stdin=subprocess.DEVNULL, stdout=logf,
                stderr=subprocess.STDOUT, env=self._env(), timeout=timeout,
            )
        return proc.returncode

    def customer(self, task_dir: Path, model: str, out_dir: Path, timeout: float,
                 agent_timeout_mult: float | None = None,
                 agent: str = "claude-code", closed_book: bool = True) -> float | None:
        """One attempt at solving `task_dir` with the customer model.

        Named for the role it plays in v2: this is the model whose failures we are
        trying to reproduce, and the same call is used twice -- on the original
        task (to collect the failure modes) and on the synthesized task (to score
        difficulty and to give the judge something to read).
        """
        m, creds = agent_route(agent, model, self.base_url, self.api_key)
        cpus = task_cpus(task_dir)
        extra: list[str] = []
        if agent_timeout_mult is not None:
            extra += ["--agent-timeout-multiplier", f"{agent_timeout_mult:.4f}"]
        extra += self._agent_kwargs(agent)
        if closed_book and agent == "claude-code":
            # claude-code's WebSearch/WebFetch run model-side, so a container
            # network clamp in the task's toml does NOT stop them; a task cannot
            # declare this, so it is enforced here.
            extra += ["--ak", "disallowed_tools=WebSearch,WebFetch"]
        self.run([
            "-p", str(task_dir), "-a", agent, "-m", m,
            "--n-concurrent", "1",
            "--agent-setup-timeout-multiplier", str(self.setup_mult),
            *extra,
            *flag_pairs("--ae", creds),
            # A task's own verifier may reach the gateway -- some grade free-form
            # output with a model -- independent of the solver harness.
            "--ve", f"ANTHROPIC_API_KEY={self.api_key}",
            "--ve", f"ANTHROPIC_BASE_URL={self.base_url}",
            "--ve", f"ANTHROPIC_MODEL={self.inner_verifier_model}",
            *thread_flags("--ae", cpus),
            *thread_flags("--ve", cpus),
        ], out_dir, timeout)
        return read_reward(out_dir)

    def agent_task(self, task_dir: Path, agent: str, model: str, out_dir: Path,
                   timeout: float, mounts_json: str, agent_env: list[str],
                   agent_timeout_mult: float | None = None,
                   disable_verification: bool = True,
                   agent_kwargs: list[str] | None = None) -> int:
        """Run one of OUR OWN harbor tasks (researcher / analyst / judge).

        These are scored on the host by reading the file the agent wrote, not by a
        verifier inside the task, hence --disable-verification by default.

        agent_kwargs are extra harbor --ak pairs the caller wants for this harness
        (e.g. codex's web_search). They are passed through verbatim rather than
        derived here, because whether a role gets the web is the caller's policy:
        the researcher is allowed it, the customer is closed-book.
        """
        m, creds = agent_route(agent, model, self.base_url, self.api_key)
        if agent in OPENAI_PROTOCOL_AGENTS:
            # Our own agent tasks ship helper scripts -- adb-testrun, adb-oracle,
            # adb-search -- and every one of them reaches the gateway over the
            # ANTHROPIC protocol regardless of which harness is driving the agent
            # that calls them: the trainee is claude-code and the serper proxy is
            # an anthropic-authenticated endpoint. Each script begins with a
            # `${ANTHROPIC_API_KEY:?}` guard, so an openai-protocol researcher
            # without this pair does not fail at startup -- it fails the first
            # time it tries to measure difficulty, which is the one thing the
            # researcher cannot do without. claude-code's own creds already carry
            # the pair, hence the guard: that path stays byte-identical.
            creds = creds + [f"ANTHROPIC_BASE_URL={self.base_url}",
                             f"ANTHROPIC_API_KEY={self.api_key}"]
        extra: list[str] = self._agent_kwargs(agent)
        for pair in (agent_kwargs or []):
            extra += ["--ak", pair]
        if agent_timeout_mult is not None:
            extra += ["--agent-timeout-multiplier", f"{agent_timeout_mult:.4f}"]
        if disable_verification:
            extra += ["--disable-verification"]
        return self.run([
            "-p", str(task_dir), "-a", agent, "-m", m,
            "--n-concurrent", "1",
            "--agent-setup-timeout-multiplier", str(self.setup_mult),
            "--mounts", mounts_json,
            *extra,
            *flag_pairs("--ae", creds),
            *flag_pairs("--ae", agent_env),
            *thread_flags("--ae"),
        ], out_dir, timeout)


def make_harbor(cfg: dict, setup_mult: float) -> "Harbor":
    """The one place a Harbor is built, so the pinned agent version is never missed."""
    return Harbor(resolve_harbor_bin(), cfg["gateway"]["base_url"], cfg["gateway"]["api_key"],
                  setup_mult, cfg["customer"]["model"],
                  claude_code_version=cfg.get("claude_code_version") or None)


def resolve_harbor_bin() -> str:
    """Absolute path to the harbor CLI.

    Never plain 'harbor' on PATH: PATH inside a task container is not the host's,
    and one of our own containers shadows the name with a wrapper.
    """
    env = os.environ.get("ADB_HARBOR_BIN")
    if env and Path(env).exists():
        return env
    for c in (
        Path.home() / "miniconda3/envs/agent/bin/harbor",
        Path("/usr/local/bin/harbor"),
    ):
        if c.exists():
            return str(c)
    from shutil import which
    w = which("harbor")
    if w:
        return w
    raise SystemExit("cannot find the harbor CLI; set ADB_HARBOR_BIN")


# --------------------------------------------------------------------------- #
# rendering our prompt templates
# --------------------------------------------------------------------------- #

def render_task(src: Path, dst: Path, repl: dict[str, str]) -> Path:
    """Copy one of our harbor task templates and substitute __PLACEHOLDERS__.

    Refuses to leave an unsubstituted placeholder behind: a stray __FOO__ in a
    prompt is a silent instruction to the model to invent something, which is far
    worse than a loud failure here.
    """
    from shutil import copytree, rmtree
    if dst.exists():
        rmtree(dst)
    copytree(src, dst)
    for p in dst.rglob("*"):
        if not p.is_file() or p.suffix not in (".md", ".toml", ".sh", ".txt", ".json"):
            continue
        try:
            s = p.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        out = s
        for k, v in repl.items():
            out = out.replace(k, v)
        if out != s:
            p.write_text(out)
    leftovers: list[str] = []
    for p in dst.rglob("*"):
        if p.is_file() and p.suffix in (".md", ".toml"):
            try:
                leftovers += [f"{p.name}:{m}" for m in
                              set(re.findall(r"__[A-Z0-9_]+__", p.read_text()))]
            except (OSError, UnicodeDecodeError):
                pass
    if leftovers:
        raise SystemExit(f"render_task: unsubstituted placeholders: {sorted(set(leftovers))}")
    return dst


# --------------------------------------------------------------------------- #
# benchmark metadata (v2 layout)
# --------------------------------------------------------------------------- #

META_NAME = "_meta.json"


# --------------------------------------------------------------------------- #
# per-customer-model artifact roots
# --------------------------------------------------------------------------- #

def model_slug(model: str) -> str:
    """Filesystem-safe directory name for a model id.

    Rollouts and the rubrics derived from them describe ONE customer model: the
    failure modes of deepseek are not the failure modes of qwen, and scoring a new
    task against the wrong model's rubric is a silent, plausible-looking error. So
    every prep artifact and every rubric lives under a directory named after the
    customer model that produced it, and the drivers derive that name from config
    rather than leaving it to the operator to remember.
    """
    out = re.sub(r"[^A-Za-z0-9._-]+", "-", model.strip().lower()).strip("-.")
    return out or "unknown-model"


def customer_slug(cfg: dict) -> str:
    return model_slug(cfg["customer"]["model"])


def _root_override(cfg: dict, key: str, env: str, default: str) -> Path:
    """Resolution order: explicit env var (set from a CLI flag), then config
    paths.<key>, then the default directory under ."""
    raw = os.environ.get(env) or (cfg.get("paths") or {}).get(key) or ""
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (ROOT / p)
    return ROOT / default


def prep_root(cfg: dict) -> Path:
    """Where this customer model's rollout bundles live."""
    return _root_override(cfg, "prep", "ADB_PREP_DIR", "prep") / customer_slug(cfg)


def rubrics_root(cfg: dict) -> Path:
    """Where this customer model's failure-mode rubrics live.

    The format and gate rubrics are model-independent and stay one level up, in
    rubrics/ itself; only the derived per-task rubrics are model-specific.
    """
    return _root_override(cfg, "rubrics", "ADB_RUBRICS_DIR", "rubrics") / customer_slug(cfg)


def shared_rubrics_dir(cfg: dict) -> Path:
    return _root_override(cfg, "rubrics", "ADB_RUBRICS_DIR", "rubrics")


def add_path_args(ap) -> None:
    """--prep-dir / --rubrics-dir on every driver that reads or writes them."""
    ap.add_argument("--prep-dir", default=None,
                    help="root for rollout bundles (default: prep); the customer model's "
                         "slug is appended automatically")
    ap.add_argument("--rubrics-dir", default=None,
                    help="root for rubrics (default: rubrics); the customer model's slug is "
                         "appended automatically for per-task rubrics")


def apply_path_args(args) -> None:
    """Push the flags into the environment so every helper sees them."""
    if getattr(args, "prep_dir", None):
        os.environ["ADB_PREP_DIR"] = args.prep_dir
    if getattr(args, "rubrics_dir", None):
        os.environ["ADB_RUBRICS_DIR"] = args.rubrics_dir


def add_budget_args(ap) -> None:
    """--researcher-budget-min on the drivers that author tasks.

    There is deliberately no per-benchmark table in the config for this, the way
    there is for task_timeout_sec: each benchmark already gets its own driver
    invocation, so a flag gives per-benchmark budgets for free and one number
    cannot silently apply to a benchmark it was not chosen for.
    """
    ap.add_argument("--researcher-budget-min", type=float, default=None,
                    help="override researcher.budget_min for this run, in minutes. The "
                         "authoring session's wall clock, the deadline the researcher is "
                         "told, and the multiplier on its harbor task all follow it")


def apply_budget_args(args, cfg: dict) -> None:
    """Push the flag into the loaded config before anything reads it.

    Overriding the config in one place beats threading a parameter through
    synthesize(): the deadline the researcher is given, the outer harbor wall
    clock, run.json and synth.json all read cfg["researcher"]["budget_min"], so
    this way they cannot disagree about what the budget was.
    """
    v = getattr(args, "researcher_budget_min", None)
    if v is None:
        return
    if v <= 0:
        raise SystemExit(f"--researcher-budget-min must be positive, got {v}")
    # Kept integral when it is a whole number: it is rendered into the researcher's
    # instruction as prose ("about 90 minutes"), where "90.0" reads like a typo.
    cfg["researcher"]["budget_min"] = int(v) if float(v).is_integer() else v


def benchmark_dir(benchmark: str) -> Path:
    return ROOT / "benchmarks" / benchmark


def load_meta(benchmark: str) -> dict:
    p = benchmark_dir(benchmark) / META_NAME
    if not p.is_file():
        raise SystemExit(f"no {META_NAME} for {benchmark}; run harness/sample_tasks.py first")
    return json.loads(p.read_text())


def sampled_tasks(benchmark: str) -> list[Path]:
    """The sampled task directories of a benchmark.

    v2 keeps tasks flat directly under benchmarks/<benchmark>/ with no domain
    directories, so anything whose name starts with '_' is framework material
    (_meta.json, _infra/) and everything else is a task.
    """
    root = benchmark_dir(benchmark)
    if not root.is_dir():
        raise SystemExit(f"no such benchmark: {root}")
    return sorted(d for d in root.iterdir()
                  if d.is_dir() and not d.name.startswith("_") and is_task_dir(d))


def benchmarks() -> list[str]:
    root = ROOT / "benchmarks"
    return sorted(d.name for d in root.iterdir()
                  if d.is_dir() and (d / META_NAME).is_file())
