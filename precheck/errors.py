"""工具包内部的预期错误：以退出码 2 报告，不打印堆栈。"""


class PrecheckError(Exception):
    """参数错误、读文件错误等预期内的失败。"""
