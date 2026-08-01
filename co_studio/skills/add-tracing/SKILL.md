---
name: add-tracing
title: Add Tracing
description: Instrument code with structured logging or tracing to diagnose a runtime
  issue — spans around suspect calls, correlation IDs, timing, and key state at boundaries.
  Use when a bug only reproduces at runtime and the cause is unclear, when diagnosing
  why something is slow or intermittently fails, or when the user says "add logging",
  "add tracing", "instrument this", or "figure out what's happening at runtime".
category: debug-understand
tools:
- read_file
- glob
- grep
- edit_file
- Bash(python *)
- Bash(node *)
- Bash(go run *)
- Bash(tail *)
---

# Add Tracing

Add structured, targeted instrumentation to reveal what the code actually does at runtime, then use it to localize the fault.

## Step 1: Frame the question

Write down exactly what the trace must answer, e.g.: which branch executes, what value a variable holds at a boundary, how long a call takes, or how often a loop runs. Instrument to answer that — not everything.

## Step 2: Discover the existing logging setup

- `grep -rn "logging\|logger\|log\.\|console\.\|slog\|zap\|winston\|tracing\|opentelemetry"` to find the framework, format, and levels already in use.
- Reuse the project's logger and format (plain, key=value, or JSON). Do not introduce a new logging stack for a diagnosis.

## Step 3: Choose instrumentation points

Instrument boundaries and suspects only:
- Entry/exit of the suspect function (log inputs on entry, result/duration on exit).
- Each branch of the decision that seems wrong.
- Error/exception handlers (log the caught error with context, not just a bare message).
- External calls (DB, HTTP, queue) and the top of hot loops.

## Step 4: Add structured logs

- Emit key=value or structured fields, not string concatenation: `logger.info("order.validate", order_id=id, status=status, elapsed_ms=dt)`.
- Thread a correlation/request ID through the path so lines can be tied together.
- Use levels deliberately: `debug` for verbose diagnosis, `warn`/`error` for anomalies.
- NEVER log secrets, tokens, passwords, or PII — redact or log only lengths/hashes.

## Step 5: Add timing for performance issues

- Wrap suspect sections with a start/stop timer and log `elapsed_ms`.
- If the framework supports spans (OpenTelemetry, tracing), open a span per unit of work with attributes instead of ad-hoc timers.

## Step 6: Reproduce and capture

- Run the failing scenario with the diagnostic level enabled.
- Capture output: `tail -f <logfile>` or the run's stdout. Save the relevant lines.

## Step 7: Analyze

- Follow the correlation ID through the lines; compare observed values/timings against expected.
- Pinpoint the first line where reality diverges from expectation — that localizes the fault.

## Step 8: Clean up

- Remove or downgrade noisy temporary `debug` logs added purely for this hunt.
- Keep a few durable, well-leveled logs at genuine boundaries if they add lasting value.
- Show the diff and summarize what the trace revealed and the next action.
