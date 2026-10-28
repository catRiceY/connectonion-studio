# Agent restart and connectivity checks

Studio hosts its UI separately from each Agent's listening port. A phone connects to the Agent endpoint, not the Studio UI port. After changing a base prompt or tool configuration, restart the Agent and verify its endpoint is unchanged before testing a conversation.

A recently closed TCP connection can remain in TIME_WAIT even when no service is listening. Studio's port probe uses server-compatible address reuse to distinguish residual connections from a real listener. A genuinely occupied port still cannot be taken from another process. Do not kill an unrelated process to recover a port.

If direct connection fails, check that phone and computer can reach the same network address and Agent port, and inspect startup logs. Relay connectivity is a separate path; a SOCKS proxy requires its optional Python transport support. A local online state does not prove the relay is connected. Editing a client address must also update existing chat references.

手机显示离线时先核对 Agent 地址与端口，再检查服务监听和启动错误。Restart 与 Stop/Start 应保持相同端口；中继失败和本地监听成功可以同时发生，不应仅凭一个状态猜测原因。
