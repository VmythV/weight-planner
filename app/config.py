"""运行配置：环境变量，或项目根目录的 .env 文件（环境变量优先）。

.env 每行一个 KEY=VALUE，# 开头为注释，值可以用引号包起来。模板见 .env.example。
可以用环境变量 WP_ENV_FILE 指定别的配置文件。
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_DIR / ".env"


def read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.startswith("export "):
            key = key[len("export ") :]
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def load_config() -> dict[str, str]:
    path = Path(os.environ.get("WP_ENV_FILE", DEFAULT_ENV_FILE))
    return {**read_env_file(path), **os.environ}
