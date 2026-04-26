"""
Deploy script: upload project files to VPS via SFTP, rebuild Docker container.
Usage: python deploy.py [--no-rebuild]
"""
import os
import sys
import time
import paramiko

VPS_HOST = os.environ.get("VPS_HOST", "")
VPS_USER = os.environ.get("VPS_USER", "root")
VPS_PASSWORD = os.environ.get("VPS_PASSWORD", "")
REMOTE_DIR = os.environ.get("VPS_REMOTE_DIR", "/opt/tg_news_bot")
LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))

# Files/dirs to upload (relative paths)
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


def connect(retries=5):
    for attempt in range(1, retries + 1):
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(VPS_HOST, username=VPS_USER, password=VPS_PASSWORD, timeout=15)
            print(f"[SSH] Connected on attempt {attempt}")
            return client
        except Exception as e:
            print(f"[SSH] Attempt {attempt} failed: {e}")
            if attempt < retries:
                time.sleep(3)
    raise RuntimeError("Could not connect to VPS after retries")


def upload_files(ssh):
    sftp = ssh.open_sftp()
    for rel_path in UPLOAD_PATHS:
        local_path = os.path.join(LOCAL_DIR, rel_path.replace("/", os.sep))
        remote_path = f"{REMOTE_DIR}/{rel_path}"
        # Ensure remote dir exists
        remote_dir = remote_path.rsplit("/", 1)[0]
        try:
            sftp.stat(remote_dir)
        except FileNotFoundError:
            sftp.mkdir(remote_dir)
        sftp.put(local_path, remote_path)
        print(f"[SFTP] {rel_path} -> {remote_path}")
    sftp.close()


def run(ssh, cmd, desc=""):
    print(f"[CMD] {desc or cmd}")
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=300)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    if out.strip():
        print(out.strip())
    if err.strip():
        print("[STDERR]", err.strip())
    return out


def main():
    no_rebuild = "--no-rebuild" in sys.argv

    ssh = connect()
    try:
        upload_files(ssh)

        if not no_rebuild:
            run(ssh, f"cd {REMOTE_DIR} && docker build -t tg_news_bot . 2>&1 | tail -5", "Docker build")
            # Stop only OUR containers by known names — never touch other containers
            run(ssh,
                "docker stop tg_news_bot 2>/dev/null; docker rm tg_news_bot 2>/dev/null; "
                "docker stop newsbot 2>/dev/null; docker rm newsbot 2>/dev/null; "
                "echo 'old containers removed'",
                "Stop old bot containers")
            run(ssh,
                "docker run -d --name tg_news_bot --restart unless-stopped "
                f"-v {REMOTE_DIR}/data:/app/data "
                f"--env-file {REMOTE_DIR}/.env "
                "-p 8010:8010 tg_news_bot 2>&1",
                "Start new container")
            time.sleep(3)
            run(ssh, "docker ps | grep tg_news_bot", "Check container running")
        else:
            run(ssh, "docker restart tg_news_bot", "Restart container")
            time.sleep(3)
            run(ssh, "docker ps | grep tg_news_bot", "Check container running")

    finally:
        ssh.close()
    print("[DEPLOY] Done.")


if __name__ == "__main__":
    main()
