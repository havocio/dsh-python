"""webui 全面板点亮示例 profile。

启动方式::

    python -m dsh_py.gateway --port 8080 --mock --webui --patch examples/webui-demo-profile.py

然后浏览器打开 ``http://127.0.0.1:8080/``——侧栏「目标/技能/工作区/任务」面板
与「命令面板」（Ctrl+K）全部可用；缺这个 profile 时各面板显示装配引导。

按需裁剪：只想要某几个面板就只留对应行（每一行对应一个 `console.*` 方法面，
见 README §6.3 的面板表）。
"""

PROFILE = [
    # 命令面板（console/commands/list · execute）
    {"id": "commands", "plugin": "dsh_py.services.commands:apply"},
    # 目标面板（console/goals/get）
    {"id": "goal", "plugin": "dsh_py.services.goal:apply"},
    # 技能面板（console/skills/list · get）
    {"id": "skills", "plugin": "dsh_py.services.skill:apply"},
    {"id": "skill-fs", "plugin": "dsh_py.services.skill_filesystem:apply"},
    {"id": "skill-badge", "plugin": "dsh_py.services.skill_badge:apply"},
    # 后台任务面板（console/jobs/list）
    {"id": "jobs", "plugin": "dsh_py.services.jobs_local:apply"},
    # 工作区面板 + 侧栏工作区列表（console/workspaces/list）
    {"id": "storage", "plugin": "dsh_py.services.storage:apply"},
    {"id": "storage-json", "plugin": "dsh_py.services.storage_json:apply"},
    {"id": "storage-domain", "plugin": "dsh_py.services.storage_domain:apply",
     "config": {"backend": "json"}},  # 必须指定默认 backend（挂 json 后端）
    {"id": "session-persistence", "plugin": "dsh_py.services.session_persistence:apply"},
    {"id": "workspace", "plugin": "dsh_py.services.workspace_registry:apply"},
]
