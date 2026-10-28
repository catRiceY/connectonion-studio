"""Readable synthetic Host snapshots for manual History inspection, not LLM output."""


def demo_rows():
    base = 1793059200.0

    def snapshot(identity, prompts, answer, *, order, timeout=False, missing=False):
        created = base + order * 600
        trace = [{"type": "plan", "id": "preflight", "entries": [], "ts": created}]
        for turn, prompt in enumerate(prompts, 1):
            ts = created + turn * 10
            trace.extend([
                {"type": "user_input", "id": f"u{turn}", "turn": turn, "content": prompt, "ts": ts},
                {"type": "llm_call", "id": f"l{turn}", "model": "demo-offline-model", "status": "running", "ts": ts + 1},
                {"type": "llm_result", "id": f"l{turn}", "model": "demo-offline-model", "status": "success", "duration_ms": 320.0, "usage": {"input_tokens": 180, "output_tokens": 45}, "ts": ts + 2},
                {"type": "tool_call", "id": f"tc{turn}", "tool_id": f"tool-{turn}", "name": "inspect_service", "args": {"service": "demo-agent", "read_only": True}, "ts": ts + 3},
                {"type": "tool_result", "id": f"tr{turn}", "tool_id": f"tool-{turn}", "name": "inspect_service", "status": "error" if timeout else "success", "timing_ms": 3000.0 if timeout else 18.0, "result": "【模拟错误】服务检查在 3 秒后超时，未执行修改操作。" if timeout else {"demo": True, "service": "demo-agent", "port": 8001, "reachable": True}, "ts": ts + 4},
                {"type": "turn_result", "id": f"end{turn}", "turn": turn, "reason": "natural", "ts": ts + 5},
            ])
            if timeout:
                trace[-2].update(error="DemoTimeout: simulated service timeout", error_type="TimeoutError")
        row = {
            "session_id": identity, "status": "done", "prompt": prompts[-1], "result": answer,
            "created": created, "updated_at": created + len(prompts) * 10 + 6,
            "revision": len(prompts), "duration_ms": 3500.0 if timeout else 420.0,
            "session": {"session_id": identity, "turn": len(prompts), "trace": trace},
        }
        if missing:
            row.pop("created"); row.pop("updated_at"); row.pop("duration_ms")
            for event in trace:
                for key in ("ts", "duration_ms", "timing_ms", "usage"):
                    event.pop(key, None)
        return row

    rows = [
        snapshot("demo-01-two-turn-diagnosis", [
            "【演示·第 1 轮】检查 demo-agent 的服务状态，先不要修改配置。",
            "【演示·第 2 轮】整理检查结果，并说明下一步如何排查。",
        ], "【模拟回答】demo-agent 的 8001 端口可访问。建议核对客户端地址与请求参数；本次没有修改文件。\n\n你可以点击 View events 查看模型调用和工具结果。第 1 轮的历史答案未单独保存，因此显示 Not recorded，不会伪造补齐。", order=100),
        snapshot("demo-02-tool-timeout", ["【演示】检查一个超时服务，并解释失败原因。"],
                 "【模拟回答】工具检查超时，无法确认服务是否正常。建议检查进程和连接；本次没有自动重试或修改配置。\n注意：源状态 done 只表示本轮结束，不表示工具成功。", order=99, timeout=True),
        snapshot("demo-03-pagination-23-turns", [f"【分页演示·第 {number} 轮】只读检查 demo-agent 并记录结果。" for number in range(1, 24)],
                 "【模拟回答】这是第 23 轮的最终回答。可以测试 Load more turns、Load more events 和按轮次筛选。", order=98),
    ]
    for number in range(4, 25):
        rows.append(snapshot(f"demo-{number:02d}-service-check", [f"【演示会话 {number}】检查 demo-agent，返回只读诊断结果。"],
                             f"【模拟回答 {number}】服务检查完成，未执行写操作。此记录用于验证会话列表分页。", order=90-number))
    rows.append(snapshot("demo-25-missing-fields", ["【演示】这份记录未保存时间、耗时和 usage，页面会如何显示？"],
                         "【模拟回答】缺失字段显示 Not recorded；usage 未保存，不会显示为 0。", order=1, missing=True))
    return rows
