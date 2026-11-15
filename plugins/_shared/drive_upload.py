"""Copies a results folder to Google Drive (or any rclone remote) with rclone.

rclone and its configuration are set up once with "bash setup.sh drive-setup".
Files already present at the destination are not uploaded again, so the same
folder can be uploaded as often as wanted. Standard library only.
"""
import os
import shutil
import subprocess
import time

DEFAULT_DESTINATION = "gdrive:WanGP-outputs"
UPLOAD_LOG, UPLOAD_PID = "upload.log", "upload.pid"


def rclone_path():
    workspace = os.environ.get("WORKSPACE") or "/workspace"
    return shutil.which("rclone") or next((path for path in (os.path.join(workspace, ".rclone", "rclone"),) if os.access(path, os.X_OK)), None)


def config_path():
    workspace = os.environ.get("WORKSPACE") or "/workspace"
    return os.environ.get("RCLONE_CONFIG") or os.path.join(workspace, "rclone.conf")


def remote_names():
    """The remotes defined in the rclone configuration (read directly: sections of an ini file)."""
    try:
        with open(config_path(), "r", encoding="utf-8") as reader:
            return [line.strip()[1:-1] for line in reader if line.strip().startswith("[") and line.strip().endswith("]")]
    except OSError:
        return []


def unavailable_reason(destination=None):
    """Why nothing can be uploaded, or an empty string."""
    if not rclone_path():
        return 'rclone is not installed: run "bash setup.sh drive-setup" in a terminal on the pod'
    remotes = remote_names()
    if not remotes:
        return 'Google Drive is not connected yet: run "bash setup.sh drive-setup" in a terminal on the pod'
    if destination is not None:
        remote, separator, _folder = destination.strip().partition(":")
        if not separator or not remote:
            return f'the destination must look like "{DEFAULT_DESTINATION}" (remote name, colon, folder)'
        if remote not in remotes:
            return f'the rclone remote "{remote}" does not exist (available: {", ".join(remotes)})'
    return ""


def command(source, destination):
    return [rclone_path(), "copy", source, destination.strip(), "--config", config_path(),
            "--transfers", "4", "--stats", "30s", "--stats-one-line", "-v"]


def upload(source, destination, log):
    """Uploads and waits. `log` is a binary file object. Returns (ok, message)."""
    reason = unavailable_reason(destination)
    if reason:
        return False, f"Nothing was uploaded: {reason}."
    if not os.path.isdir(source):
        return False, f"Nothing was uploaded: the folder {source} does not exist."
    try:
        done = subprocess.run(command(source, destination), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    except OSError as error:
        return False, f"The upload could not be started: {error}"
    if done.returncode != 0:
        return False, f"The upload to {destination} failed (rclone exit code {done.returncode}): see the log."
    return True, f"The results were uploaded to {destination}."


# ---------------------------------------------------------------- "Upload now"
def _pid_alive(pid):
    try:
        with open(f"/proc/{int(pid)}/stat", "r", encoding="utf-8") as reader:
            return reader.read().rsplit(")", 1)[-1].split()[0] != "Z"
    except (OSError, ValueError, TypeError, IndexError):
        return False


def manual_upload_running(work_dir):
    try:
        with open(os.path.join(work_dir, UPLOAD_PID), "r", encoding="utf-8") as reader:
            return _pid_alive(reader.read().strip())
    except OSError:
        return False


def start_manual_upload(source, destination, work_dir):
    """Starts an upload that does not depend on the browser. Returns a message."""
    reason = unavailable_reason(destination)
    if reason:
        return f"Nothing was uploaded: {reason}."
    if not os.path.isdir(source):
        return f"Nothing was uploaded: the folder {source} does not exist."
    if manual_upload_running(work_dir):
        return "An upload is already running."
    os.makedirs(work_dir, exist_ok=True)
    with open(os.path.join(work_dir, UPLOAD_LOG), "wb") as log:
        log.write(f"[upload] {time.strftime('%Y-%m-%d %H:%M:%S')} {source} -> {destination}\n".encode("utf-8"))
        process = subprocess.Popen(command(source, destination), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                   start_new_session=True)
    with open(os.path.join(work_dir, UPLOAD_PID), "w", encoding="utf-8") as writer:
        writer.write(str(process.pid))
    return f"Upload started: {source} -> {destination}. It continues if you close this page."


def manual_upload_lines(work_dir, lines=3):
    """The state of the latest "Upload now", as report lines (empty when there was none)."""
    path = os.path.join(work_dir, UPLOAD_LOG)
    try:
        with open(path, "rb") as reader:
            reader.seek(0, os.SEEK_END)
            reader.seek(max(0, reader.tell() - 6000))
            rows = [row.strip()[:200] for row in reader.read().decode("utf-8", errors="replace").splitlines() if row.strip()]
    except OSError:
        return []
    state = "RUNNING" if manual_upload_running(work_dir) else "ended"
    return [f"Manual upload ({state}):"] + [f"  {row}" for row in rows[-lines:]]
