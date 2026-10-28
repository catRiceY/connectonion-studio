# History and execution diagnosis

History presents saved Agent sessions as turns and execution events. One session can contain multiple turns; a model call or tool event is not a separate conversation. Select the Agent, then a session and a turn, and expand View execution to inspect model calls, tool names, arguments, results, errors, and durations.

The original ConnectOnion session files remain the source. When PostgreSQL history archiving is configured, Studio imports validated captures and the same History API reads them. SSE notifies the UI when records change; it does not replace the client's Agent chat transport or stream model tokens by itself.

Missing historical answers or durations remain Not recorded. Prompt provenance is shown only when there is evidence from the corresponding execution; it is not filled from the current editor selection. A successful HTTP response or a tool success is not proof that an entire user task was completed correctly.

历史记录按会话、轮次、事件分层。排查工具失败时先核对调用参数、错误与耗时；旧记录缺少的信息保持未知，不补造。断开客户端后已经持久化的历史仍可查看。
