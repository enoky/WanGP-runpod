"""Reads WanGP queue files and remembers which of their tasks are done. No Gradio or WanGP imports.

A queue file is either a queue.json (a list of {"id", "params"} tasks, or of plain
settings), or a queue .zip saved by WanGP (queue.json plus its media files).
"""
import hashlib
import json
import os
import shutil
import threading
import zipfile

SOURCE_BASENAME = "queue_source"   # the last queue file used, kept so that a run can be resumed
PROGRESS_FILE = "progress.json"    # {queue fingerprint: [numbers of the completed tasks]}


class QueueError(Exception):
    """A problem the user can fix (missing file, not a queue...)."""


def _manifest(data):
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not data or not all(isinstance(item, dict) for item in data):
        raise QueueError("This file does not contain a WanGP queue (a list of tasks).")
    if all("params" in item for item in data):
        tasks = data
    else:
        tasks = [{"id": number, "params": item} for number, item in enumerate(data, 1)]
    for number, task in enumerate(tasks, 1):
        if not isinstance(task.get("params"), dict):
            raise QueueError(f"Task {number} of the queue has no parameters.")
    return tasks


def load(path):
    """Returns (tasks, fingerprint) of a queue .json or .zip."""
    path = str(path)
    if not os.path.isfile(path):
        raise QueueError(f"Queue file not found: {path}")
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                if "queue.json" not in archive.namelist():
                    raise QueueError("This zip file is not a WanGP queue: it has no queue.json.")
                raw = archive.read("queue.json")
        else:
            with open(path, "rb") as reader:
                raw = reader.read()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        raise QueueError(f"The queue file could not be read: {error}")
    return _manifest(data), hashlib.sha256(raw).hexdigest()[:16]


def stored_source(work_dir):
    """The queue file kept from the previous run, or None."""
    for extension in (".json", ".zip"):
        path = os.path.join(work_dir, SOURCE_BASENAME + extension)
        if os.path.isfile(path):
            return path
    return None


def keep_source(path, work_dir):
    """Copies the queue file into the work folder (uploads are temporary) and returns the copy."""
    os.makedirs(work_dir, exist_ok=True)
    extension = ".zip" if zipfile.is_zipfile(path) else ".json"
    target = os.path.join(work_dir, SOURCE_BASENAME + extension)
    if os.path.abspath(str(path)) != os.path.abspath(target):
        for old in (".json", ".zip"):
            try:
                os.remove(os.path.join(work_dir, SOURCE_BASENAME + old))
            except OSError:
                pass
        shutil.copyfile(path, target)
    return target


def read_done(work_dir, fingerprint):
    try:
        with open(os.path.join(work_dir, PROGRESS_FILE), "r", encoding="utf-8") as reader:
            return set(json.load(reader).get(fingerprint, []))
    except (OSError, ValueError, AttributeError, TypeError):
        return set()


_progress_lock = threading.Lock()


def add_done(work_dir, fingerprint, numbers):
    # Several requests of the tab (its timer, Check, Start) can get here at the same moment.
    with _progress_lock:
        known = read_done(work_dir, fingerprint)
        done = known | set(numbers)
        path = os.path.join(work_dir, PROGRESS_FILE)
        if done != known or not os.path.isfile(path):
            temporary = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(temporary, "w", encoding="utf-8") as writer:
                json.dump({fingerprint: sorted(done)}, writer)  # only the latest queue is remembered
            os.replace(temporary, path)
    return done


def forget_done(work_dir):
    try:
        os.remove(os.path.join(work_dir, PROGRESS_FILE))
    except OSError:
        pass


def write_run_file(source, tasks, numbers, work_dir):
    """Writes the queue file of this run, holding only the tasks `numbers` (1-based), and returns its path."""
    selected = [tasks[number - 1] for number in numbers]
    if zipfile.is_zipfile(source):
        target = os.path.join(work_dir, "run_queue.zip")
        with zipfile.ZipFile(source) as archive, zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as output:
            for item in archive.infolist():
                if item.filename != "queue.json":
                    output.writestr(item, archive.read(item.filename))
            output.writestr("queue.json", json.dumps(selected, indent=1))
    else:
        target = os.path.join(work_dir, "run_queue.json")
        with open(target, "w", encoding="utf-8") as writer:
            json.dump(selected, writer, indent=1)
    return target


def describe(tasks, done, numbers):
    models = {}
    for task in tasks:
        model = str(task["params"].get("model_type") or task["params"].get("mode") or "?")
        models[model] = models.get(model, 0) + 1
    lines = [f"{len(tasks)} task(s) in the queue: " + ", ".join(f"{count} x {model}" for model, count in sorted(models.items())),
             f"Already completed in an earlier run: {len(done)}",
             f"To generate now: {len(numbers)}"]
    for number in numbers[:5]:
        prompt = " ".join(str(tasks[number - 1]["params"].get("prompt") or "").split())
        lines.append(f"  task {number}: {prompt[:110]}")
    if len(numbers) > 5:
        lines.append(f"  ... and {len(numbers) - 5} more")
    return "\n".join(lines)
