# Base prompt versions and restarted sessions

For a generated custom Agent, open its details and Prompt versions. Saving creates an immutable revision; it does not restart a running Agent. Restart applies the selected revision to the new process. A restored conversation may still contain its earlier system instructions, so its actual base prompt can differ from the process launch version.

To check this distinction, save Prompt A, start an Agent, and create a conversation. Save Prompt B and restart. Continue the original conversation, then create another conversation. Inspect the actual Base prompt in each History turn rather than relying only on a response prefix. The original conversation may keep A while the new one uses B.

Prompt revisions support viewing, comparing, and restoring. Restore creates a new revision rather than deleting history. If another edit changed the current revision, reload before saving again. Older history without recorded evidence must remain unknown.

修改基础提示词后必须重启才对新进程生效；旧会话不一定切换版本。编辑器显示的是配置选择，History 显示有证据的实际调用版本，两者不能混为一谈。
