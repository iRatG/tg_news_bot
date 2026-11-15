"""
Деплой на прод-VPS: выкладывает закоммиченный HEAD и перезапускает бота.

Что выкладывается — ровно `git archive HEAD` (все tracked-файлы коммита).
Раньше был ручной список UPLOAD_PATHS: забытый в нём файл тихо не попадал на
прод, контейнер рестартовал со старым кодом без единой ошибки. Теперь такого
класса ошибок нет. Незакоммиченные правки по умолчанию запрещены — на проде
всегда конкретный коммит, он записывается в /opt/tg_news_bot/REVISION.

Режимы:
    python deploy.py                # fast: код → в работающий контейнер → restart
    python deploy.py --rebuild      # full: docker build + пересоздание контейнера
                                    #   (нужно при изменении Dockerfile/requirements.txt
                                    #    или .env — restart не перечитывает --env-file)
    python deploy.py --allow-dirty  # выложить и незакоммиченные правки (рабочее дерево)

После перезапуска ждём /health; если бот не поднялся — exit code 1.

Доступ: SSH-ключ VPS_KEY_FILE (по умолчанию ~/.ssh/tg_news_bot_de). Ключ хоста
сверяется с ~/.ssh/known_hosts — неизвестный хост отклоняется (защита от MITM;
первый раз зайдите вручную: `ssh root@<VPS_HOST>`). Пароль — только запасной вариант.
"""
import io
import os
import subprocess
import sys
import tarfile
import time

import paramiko

LOCAL_DIR  = os.path.dirname(os.path.abspath(__file__))
REMOTE_DIR = "/opt/tg_news_bot"
CONTAINER  = "tg_news_bot"
IMAGE      = "tg_news_bot"
# Только localhost: docker-порты обходят ufw. Наружу админку отдаёт Caddy (HTTPS),
# см. deploy/Caddyfile.
PORT_BIND  = "127.0.0.1:8010:8010"
HEALTH_URL = "http://127.0.0.1:8010/health"
HEALTH_TIMEOUT_SEC = 90


def _load_env() -> dict:
    """Читает .env из корня проекта (локальные параметры деплоя)."""
    env = {}
    env_path = os.path.join(LOCAL_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
    return env


_env = _load_env()
VPS_HOST     = _env.get("VPS_HOST", "")
VPS_USER     = _env.get("VPS_USER", "root")
VPS_PASSWORD = _env.get("VPS_PASSWORD", "")
VPS_KEY_FILE = os.path.expanduser(_env.get("VPS_KEY_FILE") or "~/.ssh/tg_news_bot_de")


# ── git ───────────────────────────────────────────────────────────────────────

def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=LOCAL_DIR, text=True).strip()


def _build_release(allow_dirty: bool) -> tuple[bytes, str]:
    """
    Собирает tar.gz релиза.

    Returns:
        (байты архива, ревизия) — ревизия вида '709502d' или '709502d-dirty'.
    """
    dirty = bool(_git("status", "--porcelain", "--untracked-files=no"))
    rev = _git("rev-parse", "--short", "HEAD")

    if dirty and not allow_dirty:
        sys.exit(
            "[DEPLOY] Есть незакоммиченные изменения — закоммитьте их "
            "или запустите с --allow-dirty."
        )

    if not dirty:
        data = subprocess.check_output(
            ["git", "archive", "--format=tar.gz", "HEAD"], cwd=LOCAL_DIR
        )
        return data, rev

    # --allow-dirty: tracked-файлы в их текущем состоянии из рабочего дерева
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in _git("ls-files").splitlines():
            full = os.path.join(LOCAL_DIR, rel)
            if os.path.isfile(full):
                tar.add(full, arcname=rel)
    return buf.getvalue(), rev + "-dirty"


# ── SSH ───────────────────────────────────────────────────────────────────────

def connect(retries: int = 5) -> paramiko.SSHClient:
    if not VPS_HOST:
        sys.exit("[DEPLOY] VPS_HOST не задан в .env")
    last_exc = None
    for attempt in range(1, retries + 1):
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            if os.path.exists(VPS_KEY_FILE):
                client.connect(VPS_HOST, username=VPS_USER, key_filename=VPS_KEY_FILE,
                               look_for_keys=False, allow_agent=False, timeout=15)
            else:
                client.connect(VPS_HOST, username=VPS_USER, password=VPS_PASSWORD,
                               look_for_keys=False, allow_agent=False, timeout=15)
            print(f"[SSH] Connected to {VPS_HOST} (attempt {attempt})")
            return client
        except paramiko.SSHException as exc:
            if "not found in known_hosts" in str(exc):
                sys.exit(f"[SSH] {exc}. Зайдите один раз вручную: ssh {VPS_USER}@{VPS_HOST}")
            last_exc = exc
        except Exception as exc:  # сеть, таймаут
            last_exc = exc
        print(f"[SSH] Attempt {attempt} failed: {type(last_exc).__name__}: {last_exc}")
        time.sleep(4)
    sys.exit(f"[SSH] Could not connect to {VPS_HOST}: {last_exc}")


def run(ssh, cmd: str, desc: str = "", timeout: int = 120, check: bool = True) -> str:
    print(f"[CMD] {desc or cmd[:80]}")
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    code = stdout.channel.recv_exit_status()
    if out:
        print(out)
    if err:
        print("[STDERR]", err[-1500:])
    if check and code != 0:
        sys.exit(f"[DEPLOY] Команда упала (exit {code}): {desc or cmd[:80]}")
    return out


# ── Шаги деплоя ───────────────────────────────────────────────────────────────

def upload_release(ssh, data: bytes, rev: str) -> str:
    """Загружает архив и распаковывает в REMOTE_DIR. Возвращает путь архива на сервере."""
    remote_tar = f"{REMOTE_DIR}/.release.tar.gz"
    run(ssh, f"mkdir -p {REMOTE_DIR}/data {REMOTE_DIR}/logs", "Ensure remote dirs")
    sftp = ssh.open_sftp()
    with sftp.open(remote_tar, "wb") as f:
        f.write(data)
    sftp.close()
    print(f"[SFTP] release {rev}: {len(data) // 1024} KB")
    run(ssh, f"tar -xzmf {remote_tar} -C {REMOTE_DIR} && echo {rev} > {REMOTE_DIR}/REVISION",
        "Unpack release")
    return remote_tar


def wait_healthy(ssh) -> None:
    run(ssh,
        f"for i in $(seq 1 {HEALTH_TIMEOUT_SEC // 3}); do "
        f"  curl -fs -m 3 {HEALTH_URL} >/dev/null && echo 'health OK' && exit 0; sleep 3; "
        f"done; echo 'health FAILED'; docker logs --tail 40 {CONTAINER}; exit 1",
        "Wait for /health", timeout=HEALTH_TIMEOUT_SEC + 30)


def deploy_fast(ssh, remote_tar: str) -> None:
    """Код → в работающий контейнер (одной tar-распаковкой) → restart."""
    running = run(ssh, f"docker ps -q -f name=^{CONTAINER}$", "Check container", check=False)
    if not running:
        print("[DEPLOY] Контейнер не запущен — переключаюсь на --rebuild")
        deploy_rebuild(ssh)
        return
    run(ssh, f"gunzip -c {remote_tar} | docker exec -i {CONTAINER} tar -xm -C /app",
        "Copy code into container")
    run(ssh, f"docker restart {CONTAINER}", "Restart container", timeout=60)


def deploy_rebuild(ssh) -> None:
    """docker build + пересоздание контейнера (перечитывает .env)."""
    run(ssh, f"test -f {REMOTE_DIR}/.env", "Check .env exists on server")
    run(ssh, f"cd {REMOTE_DIR} && docker build -q -t {IMAGE} .", "Docker build", timeout=900)
    run(ssh, f"docker rm -f {CONTAINER} >/dev/null 2>&1; echo removed", "Remove old container")
    run(ssh,
        f"docker run -d --name {CONTAINER} --restart unless-stopped "
        f"-v {REMOTE_DIR}/data:/app/data -v {REMOTE_DIR}/logs:/app/logs "
        f"--env-file {REMOTE_DIR}/.env "
        f"-p {PORT_BIND} {IMAGE}",
        "Start new container")


def main() -> None:
    rebuild     = "--rebuild" in sys.argv
    allow_dirty = "--allow-dirty" in sys.argv

    data, rev = _build_release(allow_dirty)
    ssh = connect()
    try:
        print(f"[DEPLOY] {'Rebuild' if rebuild else 'Fast'} mode, revision {rev}")
        remote_tar = upload_release(ssh, data, rev)
        if rebuild:
            deploy_rebuild(ssh)
        else:
            deploy_fast(ssh, remote_tar)
        wait_healthy(ssh)
        run(ssh, f"rm -f {remote_tar}; docker ps --filter name=^{CONTAINER}$ "
                 f"--format '{{{{.Names}}}} {{{{.Status}}}} {{{{.Ports}}}}'", "Status")
    finally:
        ssh.close()
    print(f"[DEPLOY] Done: {rev}")


if __name__ == "__main__":
    main()
