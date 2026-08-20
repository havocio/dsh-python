"""统一运行配置 —— **唯一的配置编辑点**。

把原来散落在环境变量里的 api key、模型参数、路径等集中到这里管理：

- **本文件是项目级配置**（随仓库走）；个人机器级覆盖写在
  ``~/.dsh/dsh_config.py``（同结构，逐项深合并覆盖，不进仓库）。
- 配置文件加载顺序：``--config`` 显式指定 > 本文件 > ``~/.dsh/dsh_config.py``。
- 值是 Python dict；字符串支持 ``${VAR}`` 插值（默认写明文即可，
  ``${VAR}`` 只是可选兜底——例如 key 想从 CI 注入时）。
- **key 安全**：配置文件含密钥时建议 ``chmod 600``（Windows 可限制 ACL），
  并确保 ``~/.dsh/dsh_config.py`` 不提交到仓库。

适配器的 api key 解析优先级：**本文件 > credentials 服务 > 环境变量**。
"""

CONFIG = {
    # ------------------------------------------------------------------ #
    # LLM：供应商 / 模型 / 密钥 / 生成参数
    # ------------------------------------------------------------------ #
    "llm": {
        # 默认供应商与模型（CLI 不传 --provider/--model 时使用）
        "provider": "deepseek",
        "model": "deepseek-chat",

        # 全局默认 API key（明文，或 ${VAR} 引用环境变量）。
        # deepseek 专用适配器（deepseek-official 路由）读这个字段。
        "api_key": "",

        # 按供应商细分的 key（openai-compatible 多厂商场景），
        # 键名 = provider 路由名：openai / qwen / zhipu / moonshot / deepseek / ollama / vllm。
        # 单供应商场景直接写上面的 api_key 即可，这里可留空。
        "api_keys": {
            # "openai": "sk-...",
            # "deepseek": "sk-...",
        },

        # 连接与生成参数（CLI 未显式传参时作为默认值）
        "base_url": "",            # 覆盖厂商默认端点（通常留空）
        "temperature": 0.7,
        "max_tokens": 4096,
        "timeout_ms": 60000,       # 单次读取空闲超时（毫秒）
        "reasoning_effort": "off",  # deepseek-official 专用：off / low / medium / high / max
    },

    # ------------------------------------------------------------------ #
    # 数据库（预留）：未来的 session / 长记忆 sqlite 持久化后端
    # ------------------------------------------------------------------ #
    "database": {
        "url": "sqlite:///sessions.db",   # 未来 sqlite 后端接入点
    },

    # ------------------------------------------------------------------ #
    # 工作目录与路径
    # ------------------------------------------------------------------ #
    "workdir": "~/.dsh/work",        # 未来沙箱 / bash 工具的工作目录
    "session_dir": "~/.dsh/sessions",  # session jsonl 持久化目录
    "log_level": "INFO",
}
