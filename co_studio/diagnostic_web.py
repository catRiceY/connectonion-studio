"""Opt-in web diagnosis in a bounded, cancellable local subprocess.

The model provider is imported by the worker only, after consent and retrieval.
Killing a local request cannot reverse a charge already incurred remotely.
"""
import asyncio
import contextlib
import json
import os
import signal
import sys

from .knowledge import KnowledgeError

TIMEOUT_SECONDS = 90.0
MAX_ACTIVE = 2
MAX_RESULT_BYTES = 256_000

# Worker exceptions can contain credentials, provider response bodies and user
# input. Send only a fixed category across the subprocess boundary; the parent
# supplies its own text and status, never a message supplied by the provider.
PROVIDER_FAILURES = {
    "insufficient_credits": (402, "ConnectOnion 模型账户额度不足。请补充额度或明确配置其他模型；未自动重试，仍可使用免费证据检索。"),
    "paid_account_required": (402, "当前模型需要已开通的付费账户。请检查模型权限；未自动切换模型或账户。"),
    "authentication_failed": (502, "模型服务鉴权失败。请检查所选模型的凭据；不会在页面显示密钥或自动更换账户。"),
    "permission_denied": (502, "模型服务拒绝访问。请核对模型和账户权限；未自动重试。"),
    "output_truncated": (502, "模型输出达到本次选择的长度上限，回答未完成。未自动提高预算或重试；如需调整，请返回确认区选择输出上限并重新确认费用。本次请求仍可能计费。"),
    "schema_validation": (502, "模型回答不符合约定结构，已隐藏。未自动重试；本次请求仍可能计费。"),
    "citation_validation": (502, "引用编号、原文或来源版本未通过检查，回答已隐藏。未自动重试；请重新检索确认来源。"),
    "answer_validation": (502, "回答缺少必需的依据或澄清内容，已隐藏。未自动重试。"),
}
_ERROR_CATEGORIES = {
    "InsufficientCreditsError": "insufficient_credits",
    "PaidModelRequiredError": "paid_account_required",
    "LLMAuthenticationError": "authentication_failed",
    "AuthenticationError": "authentication_failed",
    "PermissionDeniedError": "permission_denied",
    "TruncatedResponseError": "output_truncated",
    "LengthFinishReasonError": "output_truncated",
}


def _failure_envelope(error):
    error_type = getattr(error, "provider_error_type", type(error).__name__)
    code = _ERROR_CATEGORIES.get(error_type) if isinstance(error_type, str) else None
    if code:
        return {"error": code}
    phase = getattr(error, "failure_phase", None)
    if isinstance(phase, str) and phase in {"schema_validation", "citation_validation", "answer_validation"}:
        result = {"error": phase}
        issues = _safe_validation_issues(getattr(error, "validation_errors", None))
        if issues:
            result["issues"] = issues
        return result
    return None


def _safe_validation_issues(value):
    """Keep fixed schema names/categories only, even for a compromised child."""
    from .diagnostic import _SCHEMA_FIELDS, _VALIDATION_TYPES
    if not isinstance(value, list):
        return []
    issues = []
    for row in value[:10]:
        if not isinstance(row, dict) or set(row) != {"type", "loc"} or not isinstance(row["loc"], list):
            continue
        kind = row["type"] if isinstance(row["type"], str) and row["type"] in _VALIDATION_TYPES else "validation_error"
        path = [part if (type(part) is int and 0 <= part <= 1000
                         or isinstance(part, str) and part in _SCHEMA_FIELDS)
                else "<field>" for part in row["loc"][:8]]
        issues.append({"type": kind, "loc": path})
    return issues


async def _stop(process):
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
    await process.wait()


async def _exchange(process, body):
    process.stdin.write(json.dumps(body, ensure_ascii=False).encode())
    await process.stdin.drain()
    process.stdin.close()
    data = bytearray()
    while part := await process.stdout.read(8192):
        data.extend(part)
        if len(data) > MAX_RESULT_BYTES:
            raise KnowledgeError("Diagnosis exceeded its output limit; result withheld")
    await process.wait()
    try:
        result = json.loads(data)
    except (ValueError, UnicodeError):
        raise KnowledgeError("Invalid diagnostic response; result withheld") from None
    if process.returncode:
        if isinstance(result, dict) and set(result) in ({"error"}, {"error", "issues"}):
            code = result["error"]
            if isinstance(code, str) and code in PROVIDER_FAILURES:
                from fastapi import HTTPException
                status, detail = PROVIDER_FAILURES[code]
                issues = _safe_validation_issues(result.get("issues"))
                if issues and code == "schema_validation":
                    detail += " 结构问题：" + "; ".join(
                        f"{'.'.join(str(part) for part in row['loc'])}: {row['type']}"
                        for row in issues[:3])
                raise HTTPException(status, detail)
        raise KnowledgeError("Model diagnosis failed; no unverified result was returned")
    if isinstance(result, dict) and result.get("protocol_version") == "diagnostic-v2":
        from .diagnostic import DEFAULT_MAX_OUTPUT_TOKENS, validate_result_v2
        validate_result_v2(result)
        if (result.get("collection") != body.get("collection")
                or type(result.get("max_output_tokens")) is not int
                or result["max_output_tokens"] != body.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
                or result.get("model_called") and result.get("model") != body.get("model")):
            raise KnowledgeError("Diagnostic response does not match the requested scope, model or budget")
        return result
    if not isinstance(result, dict) or "protocol_version" in result or result.get("status") not in {
        "hypotheses", "insufficient_evidence", "conflicting_evidence"
    }:
        raise KnowledgeError("Invalid diagnostic response; result withheld")
    return result


async def run(request, body, *, spawn=None):
    """Keep admission slot until the worker really exits, including cancellation."""
    state = request.app.state
    if getattr(state, "diagnosis_active", 0) >= MAX_ACTIVE:
        from fastapi import HTTPException
        raise HTTPException(429, "Two diagnoses are already running; try again later")
    state.diagnosis_active = getattr(state, "diagnosis_active", 0) + 1
    process = exchange = spawning = None
    try:
        if await request.is_disconnected():
            from fastapi import HTTPException
            raise HTTPException(499, "Diagnosis cancelled before dispatch")
        spawn = spawn or asyncio.create_subprocess_exec
        async with asyncio.timeout(TIMEOUT_SECONDS):
            spawning = asyncio.create_task(spawn(
                sys.executable, "-m", "co_studio.diagnostic_web",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, start_new_session=os.name == "posix",
            ))
            process = await asyncio.shield(spawning)
            exchange = asyncio.create_task(_exchange(process, body))
            while not exchange.done():
                if await request.is_disconnected():
                    from fastapi import HTTPException
                    raise HTTPException(499, "Diagnosis cancelled; provider charges may already apply")
                await asyncio.wait({exchange}, timeout=0.1)
            return exchange.result()
    except TimeoutError:
        from fastapi import HTTPException
        raise HTTPException(504, "Diagnosis timed out; provider charges may already apply") from None
    except KnowledgeError as exc:
        from fastapi import HTTPException
        raise HTTPException(502, str(exc)) from None
    finally:
        # Repeated caller cancellation must not release capacity while the child lives.
        async def cleanup():
            child = process
            if child is None and spawning is not None:
                with contextlib.suppress(Exception):
                    child = await spawning
            if child is not None:
                await _stop(child)
            if exchange is not None:
                if not exchange.done():
                    exchange.cancel()
                with contextlib.suppress(BaseException):
                    await exchange
        owner = asyncio.create_task(cleanup())
        interrupted = False
        while not owner.done():
            try:
                await asyncio.shield(owner)
            except asyncio.CancelledError:
                interrupted = True
        try:
            owner.result()
        finally:
            state.diagnosis_active -= 1
        if interrupted:
            raise asyncio.CancelledError


def main():
    # Never echo raw provider exceptions/credentials into responses or server logs.
    try:
        body = json.loads(sys.stdin.buffer.read(16_384))
        with contextlib.redirect_stdout(sys.stderr):
            from .diagnostic import generate, DEFAULT_MAX_OUTPUT_TOKENS
            from .knowledge import configured_store
            result = generate(configured_store(), body["query"], model=body["model"],
                              collection=body["collection"], mode=body["mode"],
                              max_output_tokens=body.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS))
        encoded = json.dumps(result, ensure_ascii=False).encode()
        if len(encoded) > MAX_RESULT_BYTES:
            return 1
        sys.stdout.buffer.write(encoded)
        return 0
    except Exception as error:
        envelope = _failure_envelope(error)
        if envelope:
            sys.stdout.buffer.write(json.dumps(envelope).encode())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
