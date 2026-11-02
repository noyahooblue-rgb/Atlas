"""差异审查（下一期功能）预留模块。

本期 MVP 不实现两个版本之间的差异审查。此模块只定义接口，
避免下一期改动现有索引与检查代码的结构：

- ``compare(old_dir, new_dir, config)`` 应返回新增、删除、
  变更定义的编号清单，供审查页按版本对比展示。
"""


def compare(old_dir, new_dir, config):
    raise NotImplementedError("差异审查是下一期功能，尚未实现")
