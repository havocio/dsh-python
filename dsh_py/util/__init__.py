"""dsh_py/util：底层共享工具原语（对标 dsh 的 packages/util 家族）。

这些是**零上下文纯原语**（brand / timeout / atomic-write / home-paths /
output-retention / native-command）与**启动期环境快照**（launch-environment），
被多个能力家族共享；业务语义仍归各个消费方所有。

每个模块保持依赖面最小；需要第三方库时（如 future 的 openpyxl）只允许在
util 的宿主模块内引入，且 docstring 注明用途。
"""
