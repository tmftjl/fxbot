"""配置边界可以回退的明确错误，不包含程序内部异常。"""


class ConfigFormatError(ValueError):
    """文件不是有效 UTF-8 JSON 对象。"""


class ConfigValidationError(ValueError):
    """配置校验器按契约拒绝输入值。"""
