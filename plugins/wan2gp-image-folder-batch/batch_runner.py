"""Runs a batch on its own, detached from the WanGP web server and from any browser.

Started by the plugin as a separate process:

    python batch_runner.py --dir WORK_DIR [--shutdown stop|terminate] [--delay 60] -- COMMAND...

It runs COMMAND (WanGP's headless queue processing), keeps WORK_DIR/status.json up
to date for the plugin's tab, and afterwards stops or terminates the pod if asked.
Standard library only.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pod_control  # noqa: E402

STATUS_FILE, LOG_FILE, CANCEL_FILE = "status.json", "batch.log", "cancel_shutdown"


def read_status(work_dir):
    try:
        with open(os.path.join(work_dir, STATUS_FILE), "r", encoding="utf-8") as reader:
            return json.load(reader)
    except (OSError, ValueError):
        return {}


def write_status(work_dir, **changes):
    status = read_status(work_dir)
    status.update(changes)
    path = os.path.join(work_dir, STATUS_FILE)
    with open(path + ".tmp", "w", encoding="utf-8") as writer:
        json.dump(status, writer, indent=2)
    os.replace(path + ".tmp", path)
    return status


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True)
    parser.add_argument("--shutdown", default=pod_control.KEEP)
    parser.add_argument("--delay", type=int, default=60)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    work_dir = args.dir

    stop = {"requested": False}
    child = None

    def on_signal(_signum, _frame):
        stop["requested"] = True
        if child is not None and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    try:
        os.remove(os.path.join(work_dir, CANCEL_FILE))
    except OSError:
        pass
    log = open(os.path.join(work_dir, LOG_FILE), "ab", buffering=0)

    def say(text):
        log.write(f"\n[batch] {text}\n".encode("utf-8"))

    say("Starting: " + " ".join(command))
    write_status(work_dir, phase="running", pid=os.getpid(), started=time.time(), finished=None, returncode=None,
                 shutdown=args.shutdown, shutdown_message="", deadline=None)
    try:
        child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    except OSError as error:
        say(f"Could not start WanGP: {error}")
        write_status(work_dir, phase="done", finished=time.time(), returncode=-1)
        return 1
    while True:
        try:
            returncode = child.wait(timeout=1)
            break
        except subprocess.TimeoutExpired:
            if stop["requested"] and child.poll() is None:
                # Give WanGP a moment to end by itself, then insist.
                try:
                    returncode = child.wait(timeout=20)
                    break
                except subprocess.TimeoutExpired:
                    child.kill()

    if stop["requested"]:
        say("Stopped on request.")
        write_status(work_dir, phase="stopped", finished=time.time(), returncode=returncode)
        return 0
    say(f"WanGP finished with exit code {returncode}.")

    message = ""
    if args.shutdown in (pod_control.STOP, pod_control.TERMINATE):
        deadline = time.time() + args.delay
        write_status(work_dir, phase="countdown", returncode=returncode, deadline=deadline)
        cancelled = False
        while time.time() < deadline:
            if stop["requested"] or os.path.exists(os.path.join(work_dir, CANCEL_FILE)):
                cancelled = True
                break
            time.sleep(1)
        if cancelled:
            message = "Pod shutdown cancelled: the pod was left running."
        else:
            ok, detail = pod_control.shutdown(args.shutdown)
            verb = "terminated" if args.shutdown == pod_control.TERMINATE else "stopped"
            message = f"The pod is being {verb}." if ok else detail
        say(message)
    write_status(work_dir, phase="done", finished=time.time(), returncode=returncode, shutdown_message=message, deadline=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
