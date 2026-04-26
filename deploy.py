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
    "core/pipeline.py",
    "core/config.py",
    "core/publisher.py",
    "scripts/run_once.py",
]


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


def deploy_fast(ssh):
    """Upload files via SFTP, copy into running container, restart."""
    sftp = ssh.open_sftp()
    for rel_path in UPLOAD_PATHS:
        local_path  = os.path.join(LOCAL_DIR, rel_path.replace("/", os.sep))
        remote_path = f"{REMOTE_DIR}/{rel_path}"
        sftp.put(local_path, remote_path)
        print(f"[SFTP] {rel_path}")
    sftp.close()

    # Copy each file directly into the running container
    for rel_path in UPLOAD_PATHS:
        run(ssh,
            f"docker cp {REMOTE_DIR}/{rel_path} {CONTAINER}:/app/{rel_path}",
            desc=f"docker cp {rel_path}")

    run(ssh, f"docker restart {CONTAINER}", "Restart container", timeout=30)
    time.sleep(5)
    run(ssh, f"docker ps | grep {CONTAINER}", "Status")


def deploy_rebuild(ssh):
    """Full rebuild: docker build + stop old containers + run new."""
    sftp = ssh.open_sftp()
    for rel_path in UPLOAD_PATHS:
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
