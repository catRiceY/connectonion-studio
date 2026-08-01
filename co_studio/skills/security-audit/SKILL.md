---
name: security-audit
title: Security Audit
description: Audit a codebase for common vulnerabilities and unsafe patterns — injection,
  hardcoded secrets, weak crypto, unsafe deserialization, missing authz, and vulnerable
  dependencies — and report findings ranked by severity with concrete fixes. Use when
  the user says "security audit", "check for vulnerabilities", "is this code safe",
  or "review this for security issues".
category: docs-security
tools:
- read_file
- glob
- grep
- list_files
- Bash(npm audit *)
- Bash(pip-audit *)
- Bash(bandit *)
- Bash(semgrep *)
- Bash(gitleaks *)
- Bash(git log *)
---

# Security Audit Skill

Find exploitable weaknesses and unsafe patterns. Report; do not modify code unless the user asks. Treat all findings as leads to confirm by reading the surrounding code.

## Step 1: Map the attack surface

- `list_files(".")` and `glob` for source, config, and dependency manifests.
- Identify trust boundaries: HTTP handlers, CLI args, file/network reads, deserialization, subprocess calls.

## Step 2: Scan for hardcoded secrets

- `grep -nEi "(api[_-]?key|secret|password|token|private[_-]?key|BEGIN [A-Z ]*PRIVATE KEY)"` across source and config.
- Flag anything that looks like a real credential, not a placeholder. If available: `gitleaks detect --no-banner`.
- Check that `.env`/keys files are git-ignored (`grep` `.gitignore`).

## Step 3: Hunt injection and unsafe execution

- **Command injection**: `grep` for `os.system`, `subprocess` with `shell=True`, `exec(`, `eval(`, `child_process.exec`, backticks with interpolated input.
- **SQL injection**: string-concatenated/f-string SQL near `execute(`, `query(`; confirm parameterized queries are used instead.
- **Path traversal**: user input flowing into `open(`, `fs.readFile`, `sendFile` without normalization/allowlist.
- **XSS/template injection**: `dangerouslySetInnerHTML`, `innerHTML`, `render_template_string`, unescaped output.
- **Unsafe deserialization**: `pickle.loads`, `yaml.load` (non-safe), `Marshal.load`, `ObjectInputStream`.

## Step 4: Check auth, crypto, and transport

- Missing authentication/authorization on state-changing routes (compare protected vs unprotected handlers).
- Weak crypto: `md5`, `sha1` for passwords, `DES`, ECB mode, `Math.random` for tokens, hardcoded IV/salt.
- Verify TLS/cert validation is not disabled (`verify=False`, `rejectUnauthorized: false`, `InsecureSkipVerify`).

## Step 5: Run dependency and static scanners

Run whichever match the stack; treat results as leads, not gospel:
- `npm audit --production` (Node), `pip-audit` (Python).
- `bandit -r <src>` (Python), `semgrep --config auto <src>` if available.

## Step 6: Confirm and report

For each candidate finding, READ the code path to confirm untrusted input actually reaches the sink (rule out false positives). Then report ranked by severity:

```
[CRITICAL|HIGH|MEDIUM|LOW] <title>
Location: file:line
Issue: what is wrong and why it is exploitable
Impact: what an attacker gains
Fix: concrete, minimal remediation
```

## Rules

- Do NOT print full secret values — mask them (show first/last few chars only).
- Prefer confirmed, reachable issues over theoretical ones; label unconfirmed leads clearly.
- Do NOT auto-fix unless asked; if you do, change one issue at a time.
- If nothing significant is found, say so plainly rather than inventing issues.
