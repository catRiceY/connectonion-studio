"""Launch an agent script with the studio's safe get_ips patch applied before it runs."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


def _patch_scoped_trust() -> None:
    """Route generated agents through Studio's per-agent trust-state adapter."""
    import connectonion

    from co_studio.scoped_trust import scope_host

    connectonion.host = scope_host(connectonion.host)


def _patch_get_ips() -> None:
    """Announce a few real IPv4s only (relay caps 10 endpoints) and never call the blocking ipify API."""
    import ifaddr
    import connectonion.network.announce as _announce

    if getattr(_announce.get_ips, "_co_studio_patched", False):
        return

    # VPN / VM / container / bridge adapters to skip, across macOS, Windows and Linux.
    skip = ("utun", "awdl", "llw", "bridge", "vmnet", "vnic", "vbox", "tap", "tun",  # macOS
            "vethernet", "hyper-v", "vmware", "virtualbox", "loopback", "npcap", "wsl",  # Windows
            "veth", "docker", "br-", "virbr")  # Linux
    prefer = ("en0", "en1", "eth", "wlan", "wi-fi", "wifi", "ethernet")  # physical-looking NICs

    def _get_ips() -> list[str]:
        preferred: list[str] = []
        rest: list[str] = []
        for adapter in ifaddr.get_adapters():
            # On Windows adapter.name is a GUID and nice_name the friendly label; check both.
            label = f"{getattr(adapter, 'nice_name', '') or ''} {adapter.name}".lower()
            if any(bad in label for bad in skip):
                continue  # VPN / VM / AirDrop / container adapters
            is_preferred = any(good in label for good in prefer)
            for ip in adapter.ips:
                if not isinstance(ip.ip, str):
                    continue  # IPv6 arrives as a tuple — IPv4 only
                if ip.ip.startswith("127.") or ip.ip.startswith("169.254."):
                    continue  # loopback dupe / link-local junk
                (preferred if is_preferred else rest).append(ip.ip)
        seen: set[str] = set()
        ips = [ip for ip in ["localhost", *preferred, *rest] if not (ip in seen or seen.add(ip))][:4]
        print(f"[co-studio] announce ips={','.join(ips)} endpoints={2 * len(ips)}", flush=True)
        return ips

    _get_ips._co_studio_patched = True  # type: ignore[attr-defined]
    _announce.get_ips = _get_ips


def main() -> None:
    """Patch announce.get_ips, then run the agent script as __main__ in this process."""
    if len(sys.argv) != 2:
        sys.exit("usage: co_studio_runner.py <agent.py>")
    # 1.9.2 loads project credentials in host(), but the co-ai preset constructs
    # its LLM before calling host. Cover existing generated scripts as well.
    from connectonion.environment import load_project_env

    script = Path(sys.argv[1]).resolve()
    load_project_env(script.parent / ".env")
    try:
        _patch_scoped_trust()
    except Exception as exc:  # noqa: BLE001 — surface compatibility failures in the run log
        sys.exit(f"[co-studio] scoped trust setup failed: {exc!r}")
    # Freeze a selected custom-Agent base Prompt for this process. The adapter
    # leaves host untouched when there are no saved revisions, and never rewrites
    # agent.py or claims that restored historical sessions use this revision.
    try:
        import connectonion
        from co_studio.mcp_agent import wrap_host_for_studio_tools
        from co_studio.context_runtime import wrap_host_for_context
        from co_studio.context_policy import ContextPolicy
        from co_studio.approvals import tool_config
        from co_studio.prompt_versions import wrap_host_for_prompt

        connectonion.host = wrap_host_for_studio_tools(connectonion.host, script)
        budget = tool_config(script.parent)["context_budget"]
        policy = ContextPolicy(input_budget=budget) if budget is not None else None
        connectonion.host = wrap_host_for_context(connectonion.host, script, policy=policy)
        # Prompt is outermost: its guard must inspect the original factory.
        connectonion.host = wrap_host_for_prompt(connectonion.host, script)
    except Exception:
        sys.exit("[co-studio] selected Prompt or Studio tool/context configuration is unavailable; launch stopped")
    try:
        _patch_get_ips()
    except Exception as exc:  # noqa: BLE001 — a framework refactor must not block launch
        print(f"[co-studio] get_ips patch failed: {exc!r}", flush=True)
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
