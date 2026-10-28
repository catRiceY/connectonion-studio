# Reproducible evaluation

Use a source checkout with `uv sync --locked --extra history --extra mcp --extra knowledge`.
The frozen question sets in this directory are engineering regression data,
not a human-reviewed enterprise benchmark. Translated pairs share an underlying
scenario and are not independent cases. Previously exposed holdouts cannot be
claimed as fresh generalization tests.

## Free planning and regression

```sh
uv run --locked --extra history --extra mcp --extra knowledge pytest -q
uv run python -m co_studio.evidence_eval --split all --validate-only
uv run python -m co_studio.model_eval plan --case task-fields --repeats 1
uv run python -m co_studio.diagnosis_eval plan --case prompt-en
uv run python -m co_studio.task_eval plan --case multiply --case missing --case refuse-write
```

The 72-case baseline comprises 32 retrieval questions, 34 engineering cases and
6 context cases. Capacity data (10,000 synthetic turns / 1,000 sessions) is a
separate isolated PostgreSQL experiment, not 10,000 model conversations. The
963-document count excludes duplicate source files and is not a chunk count.

## Paid runs: explicit consent and bounded dispatch

Only run after authorizing an explicit model, existing credentials and a shared
request budget across commands. `--max-calls` is **not a money cap**. The
evaluation provider disables supported SDK retries and applies a 45-second
transport timeout, but remote retries/billing remain outside its control.
No evaluator calls another model as a judge. Output directories must be new.

Examples below are plans for future runs, not evidence that they have passed:

```sh
uv run python -m co_studio.model_eval run --case task-fields --repeats 1 \
  --model YOUR_MODEL --allow-model-calls --max-calls 3 --output .artifacts/context-new

# Requires explicitly configured public Knowledge database; never uses private History.
uv run python -m co_studio.diagnosis_eval run --case prompt-en \
  --model YOUR_MODEL --allow-model-calls --max-calls 1 --output .artifacts/diagnosis-new

# Three frozen read-only synthetic tasks, at most two completions each.
uv run python -m co_studio.task_eval run --case multiply --case missing --case refuse-write \
  --model YOUR_MODEL --allow-model-calls --max-calls 6 --output .artifacts/tasks-new
```

Credentials/credit failures stop further dispatch. Raw failures and unstarted
counts are retained. Task subprocesses have a hard deadline; tools only read
in-memory fixtures, with no shell, filesystem, private conversation or real write
access. Exact final JSON, required tool arguments, natural termination and trace
pairing all matter for task success; a successful model response alone is not enough.

## Semantic review

Context runs produce `human-review.json` as a blank review template, not proof of
human review. Set each actual reviewer identity accurately (`agent` or `human`),
read the original task and answer, then import the edited copy into a new directory:

```sh
uv run python -m co_studio.model_review import --run .artifacts/context-new \
  --review /path/to/review-copy.json --output .artifacts/context-reviewed-new
uv run python -m co_studio.diagnosis_eval import-review --run-dir .artifacts/diagnosis-new \
  --review /path/to/diagnosis-review-copy.json --output .artifacts/diagnosis-reviewed-new
```

Imports verify data hashes and immutable trial identities. Diagnosis reviews
judge each citation against its associated claim, answer requirements, safe
behavior and uncited assertions. Exact quote/version validation alone is not
semantic support. Unknown/unreviewed metrics stay null. Original failed records
cannot be rewritten to success or removed from the denominator.

See the root README for the first live pilot, including its failures and blocked
quality acceptance. Report directories, local database configuration and model
credentials are intentionally ignored by Git.
