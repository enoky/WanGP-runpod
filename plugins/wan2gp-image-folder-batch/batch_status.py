"""Launches the detached batch and reports on it. No Gradio or WanGP imports."""
import json
import os
import signal
import subprocess
import sys
import time

from . import batch_runner
from .batch_plan import files_of

RUNNER = os.path.abspath(batch_runner.__file__)
PLAN_FILE, JOBS_FILE = "plan.json", "jobs.json"

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
    return status.get("phase") in ("running", "countdown") and alive(status.get("pid"))


def launch(work_dir, jobs, images, stems, output_dir, shutdown, delay, wangp_argv, python=sys.executable):
    """Writes the job files and starts the detached runner. Returns its Popen object."""
    os.makedirs(work_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(work_dir, JOBS_FILE), "w", encoding="utf-8") as writer:
        json.dump(jobs, writer, indent=2)
    with open(os.path.join(work_dir, PLAN_FILE), "w", encoding="utf-8") as writer:
        json.dump({"output_dir": output_dir, "images": [{"name": image.name, "stem": stems[image]} for image in images]}, writer, indent=2)
    with open(os.path.join(work_dir, batch_runner.LOG_FILE), "wb"):
        pass  # a new log for each batch
    batch_runner.write_status(work_dir, phase="running", pid=None, started=time.time(), finished=None, returncode=None,
                              shutdown=shutdown, shutdown_message="", deadline=None)
    command = [python, RUNNER, "--dir", work_dir, "--shutdown", shutdown, "--delay", str(delay), "--",
               python, "wgp.py", "--process", os.path.join(work_dir, JOBS_FILE), "--output-dir", output_dir,
               *headless_args(wangp_argv, own_rss_gb())]
    # Its own session: it survives the browser, and the web server if that goes away.
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    batch_runner.write_status(work_dir, pid=process.pid)
    return process


def stop(work_dir):
    """Stops the batch, or cancels the pod shutdown during its countdown. Returns a message."""
    status = batch_runner.read_status(work_dir)
    if not is_active(work_dir):
        return "No batch is running."
    if status.get("phase") == "countdown":
        with open(os.path.join(work_dir, batch_runner.CANCEL_FILE), "w", encoding="utf-8"):
            pass
        return "The pod will be left running."
    try:
        os.kill(int(status["pid"]), signal.SIGTERM)
    except (OSError, KeyError, ValueError):
        return "The batch could not be stopped."
    return "Stopping the batch..."


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


def report(work_dir):
    """Returns (text, result files, active) for the batch of work_dir."""
    status = batch_runner.read_status(work_dir)
    if not status:
        return "No batch has been run yet.", [], False
    try:
        with open(os.path.join(work_dir, PLAN_FILE), "r", encoding="utf-8") as reader:
            plan = json.load(reader)
    except (OSError, ValueError):
        plan = {"output_dir": "", "images": []}
    try:
        names = sorted(os.listdir(plan["output_dir"]))
    except OSError:
        names = []
    done, files, rows = 0, [], []
    for image in plan["images"]:
        outputs = files_of(image["stem"], names)
        done += bool(outputs)
        files += [os.path.join(plan["output_dir"], name) for name in outputs]
        rows.append(f"  {image['name']}: " + (", ".join(outputs) if outputs else "-"))

    phase, active = status.get("phase"), is_active(work_dir)
    total = len(plan["images"])
    if phase == "running" and active:
        elapsed = int(time.time() - (status.get("started") or time.time()))
        head = f"RUNNING for {elapsed // 3600}h{elapsed % 3600 // 60:02d}m - {done} of {total} image(s) generated so far."
    elif phase == "countdown" and active:
        remaining = max(0, int((status.get("deadline") or 0) - time.time()))
        verb = "terminated" if status.get("shutdown") == "terminate" else "stopped"
        head = f"FINISHED: {done} of {total} image(s) generated. The pod will be {verb} in {remaining}s - press Stop to keep it running."
    elif phase == "stopped":
        head = f"STOPPED: {done} of {total} image(s) generated."
    elif phase == "done":
        head = f"FINISHED: {done} of {total} image(s) generated."
        if status.get("returncode"):
            head += f" WanGP reported a problem (exit code {status['returncode']}): see the log below."
    else:
        head = f"INTERRUPTED (the batch process is gone): {done} of {total} image(s) generated."
    text = [head]
    if status.get("shutdown_message"):
        text.append(status["shutdown_message"])
    if done < total and not active:
        text.append("Press Start again to generate only the missing ones.")
    text += [""] + rows + ["", "Latest log lines:"] + [f"  {row}" for row in log_tail(work_dir)]
    return "\n".join(text), files, active
