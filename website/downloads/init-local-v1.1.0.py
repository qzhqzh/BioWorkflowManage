#!/usr/bin/env python3
"""Initialize a NEW v1.1.0 source checkout for loopback-only evaluation.

Does not install dependencies, start containers, change existing credentials,
create users, or alter an existing deployment. Read before running.
"""
import os
import re
import secrets
import sys
from pathlib import Path


def main():
    root = Path.cwd()
    env = root / '.env'
    if env.exists() or env.is_symlink():
        raise SystemExit('停止：.env 已存在。保留已有配置，按文档手动核对；不会覆盖。')
    for name in ('pyproject.toml', '.env.example', 'docker-compose.yml', 'backend/manage.py'):
        if not (root / name).is_file():
            raise SystemExit('请在解压或克隆的 BioWorkflowManage v1.1.0 仓库根目录运行。')
    if not re.search(r'^version\s*=\s*"1\.1\.0"\s*$', (root / 'pyproject.toml').read_text(), re.M):
        raise SystemExit('此初始化器仅适用于基础版 v1.1.0。')
    if not hasattr(os, 'getuid') or sys.platform != 'linux':
        raise SystemExit('此快速初始化路径适用于 Linux；其他环境请手动配置。')
    if os.getuid() == 0:
        raise SystemExit('请使用拥有 Docker 权限的普通用户运行，避免执行目录归属 root。')
    directories = [
        'workspace/rawdata', 'workspace/databases', 'data/analysis-runs',
        'data/input-staging', 'data/analysis-cache', 'data/miniwdl/work',
        'data/miniwdl-certs/client', 'data/miniwdl-engine',
        'secrets/object-storage', 'secrets/artifact-export',
    ]
    for relative in directories:
        path = root / relative
        for candidate in (path, *path.parents):
            if candidate == root:
                break
            if candidate.is_symlink():
                raise SystemExit('停止：初始化路径含符号链接，请按部署文档手动配置。')
        if path.exists() and not path.is_dir():
            raise SystemExit('停止：预期目录的位置已存在其他文件：' + relative)
    values = {
        'POSTGRES_PASSWORD': secrets.token_hex(32),
        'DJANGO_SECRET_KEY': secrets.token_hex(32),
        'APP_PORT': '127.0.0.1:8082',
        'DJANGO_SESSION_COOKIE_SECURE': '0',
        'DJANGO_CSRF_COOKIE_SECURE': '0',
        'DJANGO_ALLOWED_HOSTS': 'localhost,127.0.0.1,[::1]',
        'DJANGO_CSRF_TRUSTED_ORIGINS': 'http://localhost:8082,http://127.0.0.1:8082',
        'CORS_ALLOWED_ORIGINS': 'http://localhost:8082,http://127.0.0.1:8082',
        'MINIWDL_UID': str(os.getuid()),
        'MINIWDL_GID': str(os.getgid()),
    }
    socket = Path('/var/run/docker.sock')
    if socket.exists():
        values['MINIWDL_DOCKER_GID'] = str(socket.stat().st_gid)
    content = (root / '.env.example').read_text()
    for key, value in values.items():
        pattern = '^' + re.escape(key) + '=.*$'
        if re.search(pattern, content, re.M):
            content = re.sub(pattern, key + '=' + value, content, flags=re.M)
        else:
            content += '\n' + key + '=' + value + '\n'
    for relative in directories:
        (root / relative).mkdir(parents=True, exist_ok=True)
    descriptor = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(content)
    print('已创建 .env（权限 0600）和试部署目录。密钥仅写入本机文件。')
    print('访问范围：127.0.0.1:8082；HTTP Cookie 仅用于本机或 SSH 隧道试用。')
    print('尚未启动服务。下一步：docker compose config --quiet')


if __name__ == '__main__':
    main()
