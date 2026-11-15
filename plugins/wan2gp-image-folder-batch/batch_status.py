"""Launches the detached image batch and reports on it. No Gradio or WanGP imports."""
import json
import os
import time

from . import batch_runner, headless
from .batch_plan import files_of
from .headless import is_active, stop  # noqa: F401  (used by the plugin)

PLAN_FILE, JOBS_FILE = "plan.json", "jobs.json"


def launch(work_dir, jobs, images, stems, output_dir, shutdown, delay, wangp_argv, **kwargs):
    """Writes the job files and starts the detached runner. Returns its Popen object."""
    os.makedirs(work_dir, exist_ok=True)
    jobs_file = os.path.join(work_dir, JOBS_FILE)
    with open(jobs_file, "w", encoding="utf-8") as writer:
        json.dump(jobs, writer, indent=2)
    with open(os.path.join(work_dir, PLAN_FILE), "w", encoding="utf-8") as writer:
        json.dump({"output_dir": output_dir, "images": [{"name": image.name, "stem": stems[image]} for image in images]}, writer, indent=2)
    return headless.launch(work_dir, jobs_file, output_dir, shutdown, delay, wangp_argv, **kwargs)


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
        elapsed = headless.duration(time.time() - (status.get("started") or time.time()))
        head = f"RUNNING for {elapsed} - {done} of {total} image(s) generated so far."
    elif phase == "countdown" and active:
        head = f"FINISHED: {done} of {total} image(s) generated. " + headless.shutdown_countdown_text(status)
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
    text += [""] + rows + ["", "Latest log lines:"] + [f"  {row}" for row in headless.log_tail(work_dir)]
    return "\n".join(text), files, active
