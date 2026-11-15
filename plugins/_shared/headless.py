"""Starts, stops and inspects a detached headless WanGP run. No Gradio or WanGP imports.

Shared by the plugins of this repository: setup.sh copies this file into each of them.
Only one headless run can be active at a time, whichever plugin started it.
"""
import os
import signal
import subprocess
import sys
import time

from . import batch_runner

RUNNER = os.path.abspath(batch_runner.__file__)
ACTIVE_MARKER = ".headless_run"  # in the WanGP folder: the work folder of the latest run

# Options of the web server's command line that also apply to a headless run.
PASS_VALUE = {"--max-ram", "--max-vram", "--profile", "--attention", "--config", "--settings", "--loras", "--lora-config",
              "--vram-allocator", "--ram-allocator", "--perc-reserved-mem-max", "--gpu", "--verbose", "--vae-config"}
PASS_FLAG = {"--compile", "--fp16", "--bf16", "--offline"}


def own_rss_gb():
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as reader:
            for line in reader:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024 / 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def headless_args(argv, used_gb=0.0):
    """The web server's options worth passing on; its RAM budget is reduced by what the server itself keeps using."""
    kept, index = [], 0
    while index < len(argv):
        arg = argv[index]
        name, has_value, value = arg.partition("=")
        if name in PASS_VALUE:
            if not has_value and index + 1 < len(argv):
                index += 1
                value = argv[index]
            if name == "--max-ram":
                try:
                    value = str(max(8, int(float(value) - used_gb - 1)))
                except ValueError:
                    pass
            kept += [name, value]
        elif arg in PASS_FLAG:
            kept.append(arg)
        index += 1
    return kept


def alive(pid):
    try:
        with open(f"/proc/{int(pid)}/stat", "r", encoding="utf-8") as reader:
            return reader.read().rsplit(")", 1)[-1].split()[0] != "Z"  # a zombie has finished
    except (OSError, ValueError, TypeError, IndexError):
        return False


def is_active(work_dir):
    status = batch_runner.read_status(work_dir)
    return status.get("phase") in ("running", "uploading", "countdown") and alive(status.get("pid"))


def other_active_run(work_dir):
    """The work folder of a run started elsewhere (by another plugin) that is still going, or None."""
    try:
        with open(ACTIVE_MARKER, "r", encoding="utf-8") as reader:
            marked = reader.read().strip()
    except OSError:
        return None
    if marked and os.path.abspath(marked) != os.path.abspath(work_dir) and is_active(marked):
        return marked
    return None


def launch(work_dir, queue_file, output_dir, shutdown, delay, wangp_argv, python=sys.executable, upload_to=""):
    """Starts the detached runner on queue_file (already written in work_dir). Returns its Popen object."""
    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(work_dir, batch_runner.LOG_FILE), "wb"):
        pass  # a new log for each run
    batch_runner.write_status(work_dir, phase="running", pid=None, started=time.time(), finished=None, returncode=None,
                              shutdown=shutdown, shutdown_message="", upload_message="", deadline=None, current=0, total=0,
                              completed=[], failed=[], skipped=[])
    upload = ["--upload-source", output_dir, "--upload-to", upload_to] if upload_to else []
    command = [python, RUNNER, "--dir", work_dir, *upload, "--shutdown", shutdown, "--delay", str(delay), "--",
               python, "wgp.py", "--process", queue_file, "--output-dir", output_dir, *headless_args(wangp_argv, own_rss_gb())]
    # Its own session: it survives the browser, and the web server if that goes away.
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    batch_runner.write_status(work_dir, pid=process.pid)
    with open(ACTIVE_MARKER, "w", encoding="utf-8") as writer:
        writer.write(os.path.abspath(work_dir))
    return process


def stop(work_dir):
    """Stops the run, or cancels the pod shutdown during its countdown. Returns a message."""
    status = batch_runner.read_status(work_dir)
    if not is_active(work_dir):
        return "Nothing is running."
    if status.get("phase") == "uploading":
        return "The results are being uploaded: wait for the upload to finish."
    if status.get("phase") == "countdown":
        with open(os.path.join(work_dir, batch_runner.CANCEL_FILE), "w", encoding="utf-8"):
            pass
        return "The pod will be left running."
    try:
        os.kill(int(status["pid"]), signal.SIGTERM)
    except (OSError, KeyError, ValueError):
        return "The run could not be stopped."
    return "Stopping..."


def log_tail(work_dir, lines=12):
    try:
        with open(os.path.join(work_dir, batch_runner.LOG_FILE), "rb") as reader:
            reader.seek(0, os.SEEK_END)
            reader.seek(max(0, reader.tell() - 20000))
            text = reader.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    # Progress bars rewrite their line with carriage returns: keep the last state of each line.
    rows = [row.rsplit("\r", 1)[-1].rstrip() for row in text.replace("\r\n", "\n").split("\n")]
    return [row[:200] for row in rows if row.strip()][-lines:]


def duration(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


def shutdown_countdown_text(status):
    remaining = max(0, int((status.get("deadline") or 0) - time.time()))
    verb = "terminated" if status.get("shutdown") == "terminate" else "stopped"
    return f"The pod will be {verb} in {remaining}s - press Stop to keep it running."
