# Studio tool access and one-call approval

Studio's built-in MCP document tools read only the public operation guides shipped with the package. They accept fixed document IDs, not arbitrary paths or commands. Discovery annotations such as readOnlyHint do not grant permission; the local allowlist and schemas are authoritative. These tools are separate from a vector knowledge index and are not RAG search.

Optional local approvals bind a decision to one Agent process, session, turn, tool call ID, and argument digest. Approval authorizes only that displayed call, not all future calls to the tool. A changed argument, expired request, consumed authorization, or opposite repeated decision cannot reuse the approval. The framework's existing explicit denials and workspace protections still apply.

While an approval is pending, inspect the tool name and arguments in Studio. Approve once or reject. A rejected or expired request must not execute. If the Agent process dies after authorization is consumed, the outcome is unknown; never assume exactly-once external side effects across a process crash. Cancellation cannot forcibly undo arbitrary external code.

工具审批与连接信任不同：允许客户端连接不代表允许它执行所有工具。审批开启后，除本次挂载的受控只读工具外，其它工具需要本机确认；未知工具不自动放行。配置改变在下次启动生效。
