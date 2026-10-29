// Mirror the fixed server contract, not model-authored instructions. The parity
// regression checks this map against diagnostic.py. This gates the verification
// channel only; it is not a semantic safety check for free-text claims.
export const VERIFICATION_POLICY = 'readonly-checks-v1';
export const READONLY_VERIFICATION_ACTIONS = Object.freeze({
  "inspect_client_version": "只读查看当前客户端与 SDK 的版本信息，不升级或重启。",
  "inspect_session_identifier": "仅在有权查看的界面核对会话标识是否一致，不公开标识或切换到他人会话。",
  "inspect_config": "只读检查已有配置中的非敏感字段，不修改配置或复制密钥。",
  "inspect_error_message": "查看已有错误提示和日志中的错误码，分享前先脱敏；不运行命令。",
  "compare_documented_behavior": "对照引用材料中的版本和适用条件检查实际现象，不执行创建、写入或重启操作。"
});

export function isReadOnlyVerificationStep(policy, step) {
  return policy === VERIFICATION_POLICY && step !== null && typeof step === 'object'
    && typeof step.kind === 'string' && Object.hasOwn(READONLY_VERIFICATION_ACTIONS, step.kind)
    && step.action === READONLY_VERIFICATION_ACTIONS[step.kind];
}
