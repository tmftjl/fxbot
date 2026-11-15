"""平台和传输共同遵循的失败语义，不携带上游敏感响应。"""


class BotOffline(ConnectionError):
    """当前连接不可用，不能提交平台动作。"""


class ActionFailed(RuntimeError):
    """平台明确拒绝动作；结果不确定时不得自动重放。"""
