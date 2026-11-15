"""Queue Runner: processes a WanGP queue file without the browser, then can shut the pod down.

The queue (a queue.json or a queue .zip saved by WanGP) is handed to a separate
WanGP process without web interface, so it keeps going when the browser is closed.
The tab starts it, shows its progress and can stop it. Completed tasks are
remembered: starting the same queue again continues where it stopped.
"""
import json
import os
import sys
import threading
import time

import gradio as gr

from shared.utils.plugins import WAN2GPPlugin
from shared.utils.process_locks import acquire_GPU_ressources, any_GPU_process_running, release_GPU_ressources

from . import batch_runner, drive_upload, headless, pod_control, queue_file
from .queue_file import QueueError

PlugIn_Name = "Queue Runner"
PlugIn_Id = "QueueRunner"
SHUTDOWN_DELAY = 60  # seconds left to press Stop before the pod is shut down
WORK_FOLDER = "queue_runner"  # inside the WanGP folder: queue copy, progress, status and log
RUN_FILE = "run.json"

INTRO = """**Process a whole WanGP queue file, then shut the pod down.** Give a `queue.json`, or a queue `.zip` saved with
WanGP's *Save Queue*, and press **Start**.

The queue runs on its own on the server: **you can close this page or let your tablet sleep**, and come back to this tab
later to see how far it is. While it runs, the Media Generator has to wait for the GPU.

Completed tasks are remembered. If the run is interrupted (or the pod restarted), press **Start** again with the same
queue, or with no file at all, and it continues with the tasks that are left."""


class QueueRunnerPlugin(WAN2GPPlugin):
    def __init__(self):
        super().__init__()
        self.name = PlugIn_Name
        self.version = "1.2.0"
        self.description = "Processes a WanGP queue file without the browser, and can shut the RunPod pod down when it is finished."

    def setup_ui(self):
        self.request_global("release_model")
        self.request_global("server_config")
        self.request_component("state")
        self.add_tab(tab_id=PlugIn_Id, label=PlugIn_Name, component_constructor=self.create_ui)

    # ------------------------------------------------------------------ helpers
    @property
    def work_dir(self):
        return os.path.abspath(WORK_FOLDER)

    def _default_output_dir(self):
        config = self.server_config if isinstance(getattr(self, "server_config", None), dict) else {}
        return os.path.abspath(config.get("save_path") or "outputs")

    def _read_run(self):
        try:
            with open(os.path.join(self.work_dir, RUN_FILE), "r", encoding="utf-8") as reader:
                return json.load(reader)
        except (OSError, ValueError):
            return {}

    def _sync_progress(self):
        """Adds the tasks completed by the latest run to the remembered ones. Returns (run, status, done)."""
        run, status = self._read_run(), batch_runner.read_status(self.work_dir)
        if not run:
            return run, status, set()
        numbers = run.get("numbers", [])
        finished = [numbers[local - 1] for local in status.get("completed", []) if 0 < local <= len(numbers)]
        return run, status, queue_file.add_done(self.work_dir, run["fingerprint"], finished)

    def _plan(self, upload, path, skip_done, limit):
        """Returns (source file, tasks, fingerprint, done, numbers to run)."""
        self._sync_progress()
        source = (path or "").strip() or (getattr(upload, "name", upload) if upload else None) or queue_file.stored_source(self.work_dir)
        if not source:
            raise QueueError("Give a queue file: upload it, or type its path on the server.")
        tasks, fingerprint = queue_file.load(os.path.expanduser(str(source)))
        done = queue_file.read_done(self.work_dir, fingerprint) if skip_done else set()
        numbers = [number for number in range(1, len(tasks) + 1) if number not in done]
        if limit and int(limit) > 0:
            numbers = numbers[:int(limit)]
        return os.path.expanduser(str(source)), tasks, fingerprint, done, numbers

    def _report(self):
        """Returns (text, active)."""
        run, status, done = self._sync_progress()
        if not run or not status:
            stored = queue_file.stored_source(self.work_dir)
            text = "No queue has been run yet." + (" A queue file from an earlier session is still here: press Start to use it." if stored else "")
            return "\n".join([text, *drive_upload.manual_upload_lines(self.work_dir)]), False
        active = headless.is_active(self.work_dir)
        phase, total_run, total = status.get("phase"), len(run.get("numbers", [])), run.get("total", 0)
        completed, failed, skipped = (len(status.get(key, [])) for key in ("completed", "failed", "skipped"))
        counts = f"{completed} of {total_run} task(s) of this run completed"
        if failed or skipped:
            counts += f" ({failed} failed, {skipped} skipped)"
        if phase == "running" and active:
            elapsed = time.time() - (status.get("started") or time.time())
            head = f"RUNNING for {headless.duration(elapsed)} - {counts}."
            current = status.get("current") or 0
            if 0 < current <= total_run:
                head += f" Now on task {run['numbers'][current - 1]}."
            if completed:
                head += f" About {headless.duration(elapsed / completed * (total_run - completed))} left."
        elif phase == "uploading" and active:
            head = f"FINISHED: {counts}. Uploading the results to Google Drive (progress in the log lines below)..."
        elif phase == "countdown" and active:
            head = f"FINISHED: {counts}. " + headless.shutdown_countdown_text(status)
        elif phase == "stopped":
            head = f"STOPPED: {counts}."
        elif phase == "done":
            head = f"FINISHED: {counts}."
            if status.get("returncode"):
                head += f" WanGP reported a problem (exit code {status['returncode']}): see the log below."
        else:
            head = f"INTERRUPTED (the queue process is gone): {counts}."
        text = [head, f"Whole queue: {len(done)} of {total} task(s) completed. Results are in {run.get('output_dir')}"]
        if status.get("live_uploads_ok") or status.get("live_uploads_failed"):
            line = f"Uploads to Google Drive during this run: {status.get('live_uploads_ok', 0)} done"
            if status.get("live_uploads_failed"):
                line += f", {status['live_uploads_failed']} failed (retried by the following ones and by the final upload)"
            text.append(line + ".")
        for key in ("upload_message", "shutdown_message"):
            if status.get(key):
                text.append(status[key])
        if len(done) < total and not active:
            text.append("Press Start again to continue with the tasks that are left.")
        text += drive_upload.manual_upload_lines(self.work_dir)
        text += ["", "Latest log lines:"] + [f"  {row}" for row in headless.log_tail(self.work_dir)]
        return "\n".join(text), active

    def on_tab_select(self, state: dict):
        return self._report()[0]

    def _hold_gpu_while_running(self, state, process):
        """Keeps WanGP's own generations waiting until the queue process has ended."""
        def wait():
            try:
                process.wait()
                while headless.is_active(self.work_dir):  # the pod shutdown countdown
                    time.sleep(2)
            finally:
                release_GPU_ressources(state, PlugIn_Id)

        threading.Thread(target=wait, daemon=True, name="queue-runner-gpu").start()

    # ----------------------------------------------------------------------- UI
    def create_ui(self):
        def check(upload, path, skip_done, limit):
            try:
                _source, tasks, _fingerprint, done, numbers = self._plan(upload, path, skip_done, limit)
            except QueueError as error:
                raise gr.Error(str(error))
            return queue_file.describe(tasks, done, numbers) + ("" if numbers else "\n\nNothing to do.")

        def resolve_output_dir(output_dir):
            output_dir = (output_dir or "").strip()
            return os.path.abspath(os.path.expanduser(output_dir)) if output_dir else self._default_output_dir()

        def upload_now(output_dir, destination):
            source = resolve_output_dir(output_dir) if (output_dir or "").strip() else (self._read_run().get("output_dir") or self._default_output_dir())
            gr.Info(drive_upload.start_manual_upload(source, destination or "", self.work_dir))
            return self._report()[0]

        def start(state, upload, path, skip_done, limit, output_dir, when_done, upload_when_done, destination, upload_each):
            if headless.is_active(self.work_dir):
                raise gr.Error("A queue is already running. Stop it first, or wait for it to finish.")
            if headless.other_active_run(self.work_dir):
                raise gr.Error("Another headless run (Image Folder Batch) is still going. Stop it first, or wait for it to finish.")
            try:
                source, tasks, fingerprint, done, numbers = self._plan(upload, path, skip_done, limit)
            except QueueError as error:
                raise gr.Error(str(error))
            description = queue_file.describe(tasks, done, numbers)
            if not numbers:
                return description + "\n\nNothing to do."
            upload_to = (destination or "").strip() if upload_when_done else ""
            if upload_when_done:
                reason = drive_upload.unavailable_reason(upload_to)
                if reason:
                    raise gr.Error(f"The results cannot be uploaded: {reason}. Fix this, or untick the upload.")
            if any_GPU_process_running(state, PlugIn_Id):
                raise gr.Error("WanGP is generating right now. Wait for its queue to finish (or abort it), then press Start again.")
            output_dir = resolve_output_dir(output_dir)

            # The queue runs in another process: take the GPU away from this one and free its model first.
            acquire_GPU_ressources(state, PlugIn_Id, PlugIn_Name, gr=gr)
            try:
                self.release_model()
                source = queue_file.keep_source(source, self.work_dir)
                # Reset the previous run's task list before describing the new run, so that the two are never mixed up.
                batch_runner.write_status(self.work_dir, phase="starting", pid=None, current=0, total=0, completed=[], failed=[], skipped=[])
                run_queue = queue_file.write_run_file(source, tasks, numbers, self.work_dir)
                with open(os.path.join(self.work_dir, RUN_FILE), "w", encoding="utf-8") as writer:
                    json.dump({"fingerprint": fingerprint, "total": len(tasks), "numbers": numbers, "output_dir": output_dir}, writer)
                process = headless.launch(self.work_dir, run_queue, output_dir, when_done, SHUTDOWN_DELAY, sys.argv[1:], upload_to=upload_to,
                                          upload_each=bool(upload_each))
            except Exception as error:
                release_GPU_ressources(state, PlugIn_Id)
                raise gr.Error(f"The queue could not be started: {error}")
            self._hold_gpu_while_running(state, process)
            gr.Info(f"Queue started: {len(numbers)} task(s). You can close this page.")
            return description + "\n\nQueue started. This report refreshes by itself."

        def stop():
            gr.Info(headless.stop(self.work_dir))

        def refresh():
            return self._report()[0]

        def refresh_if_active(current_text):
            text, active = self._report()
            current_text = str(current_text or "")
            # A report showing a live state still needs its final update once the run has ended.
            live = (current_text.startswith("RUNNING") or "Queue started" in current_text or "press Stop to keep it running" in current_text
                    or "Uploading the results" in current_text or "upload (RUNNING)" in current_text)
            return text if active or live or drive_upload.manual_upload_running(self.work_dir) else gr.update()

        def forget():
            if headless.is_active(self.work_dir):
                raise gr.Error("Stop the running queue first.")
            queue_file.forget_done(self.work_dir)
            try:
                os.remove(os.path.join(self.work_dir, RUN_FILE))
            except OSError:
                pass
            gr.Info("Progress forgotten: the next Start processes the whole queue again.")
            return self._report()[0]

        with gr.Column():
            gr.Markdown(INTRO)
            with gr.Row():
                upload = gr.File(label="Queue file (queue.json or queue .zip)", file_types=[".json", ".zip"], type="filepath")
                with gr.Column():
                    path = gr.Textbox(label="...or its path on the server", placeholder="/workspace/queue.json",
                                      info="Leave both empty to continue with the queue file of the previous run.")
                    output_dir = gr.Textbox(label="Output folder (empty = WanGP's usual outputs folder)", placeholder="/workspace/queue_outputs")

            pod_reason = pod_control.unavailable_reason()
            when_done = gr.Dropdown(label="When the queue is finished", choices=pod_control.CHOICES, value=pod_control.KEEP,
                                    interactive=not pod_reason,
                                    info=(f"Not available: {pod_reason}." if pod_reason else
                                          f"The pod is shut down {SHUTDOWN_DELAY}s after the last task, unless you press Stop. "
                                          "A pod with a network volume cannot be stopped, only terminated."))
            drive_reason = drive_upload.unavailable_reason()
            with gr.Row():
                upload_when_done = gr.Checkbox(label="Upload the results to Google Drive when the queue is finished, before any shutdown",
                                               value=False, interactive=not drive_reason,
                                               info=(f"Not available: {drive_reason}." if drive_reason else
                                                     "The whole output folder is copied; files already in Drive are skipped. "
                                                     "If the upload fails, the pod is left running."))
                destination = gr.Textbox(label="Google Drive folder (rclone remote:folder)", value=drive_upload.DEFAULT_DESTINATION)
                upload_btn = gr.Button("Upload now", size="sm", interactive=not drive_reason)
            upload_each = gr.Checkbox(label="...and also upload each result as soon as it is generated", value=True, interactive=not drive_reason,
                                      info="Runs beside the generation without slowing it. The final upload is then short, so the pod "
                                           "can be shut down right after the last task. Only applies when the upload above is ticked.")
            with gr.Accordion("Options", open=False):
                with gr.Row():
                    skip_done = gr.Checkbox(label="Skip the tasks already completed in an earlier run", value=True)
                    limit = gr.Number(label="Only the next N tasks (0 = all)", value=0, precision=0, minimum=0)
                forget_btn = gr.Button("Forget the completed tasks (start the queue over)", size="sm")

            with gr.Row():
                check_btn = gr.Button("Check")
                start_btn = gr.Button("Start", variant="primary")
                stop_btn = gr.Button("Stop", variant="stop")
                refresh_btn = gr.Button("Refresh")
            report = gr.Textbox(label="Report", lines=18, interactive=False, value=self._report()[0])
            timer = gr.Timer(5)

        self.on_tab_outputs = [report]

        check_btn.click(fn=check, inputs=[upload, path, skip_done, limit], outputs=[report])
        start_btn.click(fn=start, inputs=[self.state, upload, path, skip_done, limit, output_dir, when_done, upload_when_done, destination, upload_each],
                        outputs=[report])
        upload_btn.click(fn=upload_now, inputs=[output_dir, destination], outputs=[report])
        stop_btn.click(fn=stop, queue=False)
        refresh_btn.click(fn=refresh, outputs=[report])
        forget_btn.click(fn=forget, outputs=[report])
        timer.tick(fn=refresh_if_active, inputs=[report], outputs=[report], show_progress="hidden")
