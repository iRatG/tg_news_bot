"""
Deploy script: upload Python files to VPS via SFTP + docker cp + restart.
No Docker rebuild needed for .py-only changes.
Use --rebuild only when Dockerfile or requirements.txt changed.

Usage:
    python deploy.py             # fast: sftp + docker cp + restart
    python deploy.py --rebuild   # full: sftp + docker build + stop/run
"""
import os
import sys
import time
import paramiko

def _load_env() -> dict:
    """Read .env file from project root."""
    env = {}
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
    return env

_env = _load_env()

VPS_HOST  = _env.get("VPS_HOST", "")
VPS_USER  = _env.get("VPS_USER", "root")
VPS_PASSWORD = _env.get("VPS_PASSWORD", "")
REMOTE_DIR = "/opt/tg_news_bot"
CONTAINER  = "tg_news_bot"
LOCAL_DIR  = os.path.dirname(os.path.abspath(__file__))

UPLOAD_PATHS = [
    "agents/researcher.py",
    "agents/arxiv_agent.py",
    "agents/fact_checker.py",
    "agents/writer.py",
    "agents/formatter.py",
    "agents/analyst.py",
    "core/pipeline.py",
    "core/config.py",
    "core/publisher.py",
    "core/dedup.py",
    "core/scheduler.py",
    "scripts/run_once.py",
    "web",   # директория — разворачивается рекурсивно, см. _expand_paths()
]


def _expand_paths(paths):
    """
    Разворачивает директории из UPLOAD_PATHS в список отдельных файлов.

    Раньше UPLOAD_PATHS содержал только точечные файлы — когда чинили
    core/dedup.py или web/admin.py, про них забывали добавить в список,
    и deploy_fast()/deploy_rebuild() тихо не выгружали изменения на прод
    (контейнер рестартовал со старым багованным кодом внутри, без единой
    ошибки в выводе). Директории здесь разворачиваются рекурсивно, поэтому
    новые файлы внутри уже перечисленной директории (например, новый шаблон
    в web/templates/) подхватятся сами, без риска повторить эту ошибку.
    """
    expanded = []
    for rel_path in paths:
        local_path = os.path.join(LOCAL_DIR, rel_path.replace("/", os.sep))
        if os.path.isdir(local_path):
            for root, _dirs, files in os.walk(local_path):
                for fname in files:
                    full = os.path.join(root, fname)
                    rel = os.path.relpath(full, LOCAL_DIR).replace(os.sep, "/")
                    expanded.append(rel)
        else:
            expanded.append(rel_path)
    return expanded


def connect(retries=8):
    for attempt in range(1, retries + 1):
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(VPS_HOST, username=VPS_USER, password=VPS_PASSWORD, timeout=15)
            print(f"[SSH] Connected on attempt {attempt}")
            return client
        except Exception as e:
            print(f"[SSH] Attempt {attempt} failed: {type(e).__name__}")
            if attempt < retries:
                time.sleep(4)
    raise RuntimeError("Could not connect to VPS after retries")


def run(ssh, cmd, desc="", timeout=60):
    print(f"[CMD] {desc or cmd[:80]}")
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    if out:
        print(out)
    if err:
        print("[STDERR]", err[:300])
    return out


def _ensure_dirs(ssh, upload_paths):
    """mkdir -p для всех директорий, встречающихся в upload_paths — и на VPS, и внутри контейнера."""
    dirs = sorted({
        os.path.dirname(rel_path) for rel_path in upload_paths
        if os.path.dirname(rel_path)
    })
    if not dirs:
        return
    run(ssh, "mkdir -p " + " ".join(f"{REMOTE_DIR}/{d}" for d in dirs),
        "Ensure remote directories")
    run(ssh, f"docker exec {CONTAINER} mkdir -p " + " ".join(f"/app/{d}" for d in dirs),
        "Ensure directories inside container")


def deploy_fast(ssh):
    """Upload files via SFTP, copy into running container, restart."""
    upload_paths = _expand_paths(UPLOAD_PATHS)
    _ensure_dirs(ssh, upload_paths)

    sftp = ssh.open_sftp()
    for rel_path in upload_paths:
        local_path  = os.path.join(LOCAL_DIR, rel_path.replace("/", os.sep))
        remote_path = f"{REMOTE_DIR}/{rel_path}"
        sftp.put(local_path, remote_path)
        print(f"[SFTP] {rel_path}")
    sftp.close()

    # Copy each file directly into the running container
    for rel_path in upload_paths:
        run(ssh,
            f"docker cp {REMOTE_DIR}/{rel_path} {CONTAINER}:/app/{rel_path}",
            desc=f"docker cp {rel_path}")

    run(ssh, f"docker restart {CONTAINER}", "Restart container", timeout=30)
    time.sleep(5)
    run(ssh, f"docker ps | grep {CONTAINER}", "Status")


def deploy_rebuild(ssh):
    """Full rebuild: docker build + stop old containers + run new."""
    upload_paths = _expand_paths(UPLOAD_PATHS)
    dirs = sorted({os.path.dirname(rp) for rp in upload_paths if os.path.dirname(rp)})
    if dirs:
        run(ssh, "mkdir -p " + " ".join(f"{REMOTE_DIR}/{d}" for d in dirs),
            "Ensure remote directories")

    sftp = ssh.open_sftp()
    for rel_path in upload_paths:
        local_path  = os.path.join(LOCAL_DIR, rel_path.replace("/", os.sep))
        remote_path = f"{REMOTE_DIR}/{rel_path}"
        sftp.put(local_path, remote_path)
        print(f"[SFTP] {rel_path}")
    sftp.close()

    run(ssh, f"cd {REMOTE_DIR} && docker build -t {CONTAINER} . 2>&1 | tail -5",
        "Docker build", timeout=300)

    # Stop only OUR containers — never touch timemirror or other services
    run(ssh,
        f"docker stop {CONTAINER} 2>/dev/null; docker rm {CONTAINER} 2>/dev/null; "
        "docker stop newsbot 2>/dev/null; docker rm newsbot 2>/dev/null; "
        "echo 'old containers removed'",
        "Stop old bot containers")

    run(ssh,
        f"docker run -d --name {CONTAINER} --restart unless-stopped "
        f"-v {REMOTE_DIR}/data:/app/data "
        f"--env-file {REMOTE_DIR}/.env "
        f"-p 8010:8010 {CONTAINER} 2>&1",
        "Start new container")
    time.sleep(5)
    run(ssh, f"docker ps | grep {CONTAINER}", "Status")


def main():
    rebuild = "--rebuild" in sys.argv
    ssh = connect()
    try:
        if rebuild:
            print("[DEPLOY] Full rebuild mode")
            deploy_rebuild(ssh)
        else:
            print("[DEPLOY] Fast mode (docker cp + restart)")
            deploy_fast(ssh)
    finally:
        ssh.close()
    print("[DEPLOY] Done.")


if __name__ == "__main__":
    main()
