"""Image Folder Batch: one WanGP generation per image of a folder.

Every image becomes the start frame and/or the reference image of its own
generation. All generations share the same settings (those of the Media
Generator, or an exported settings file) and the same audio file, and they
are added to WanGP's normal queue.
"""
import os
import time

import gradio as gr

from shared.gradio.progress import WangpProgress
from shared.utils.plugins import WAN2GPPlugin

from . import pod_control
from .batch_plan import USE_AS_CHOICES, PlanError, build_plan, files_of, load_settings_file

PlugIn_Name = "Image Folder Batch"
PlugIn_Id = "ImageFolderBatch"
FROM_FORM = "The current settings of the Media Generator"
FROM_FILE = "An exported settings file (.json)"
SHUTDOWN_DELAY = 60  # seconds left to press Stop before the pod is shut down

INTRO = """**One generation per image.** Each image of the folder becomes the start frame and / or the reference image
of its own generation. Everything else (model, prompt, resolution, length...) and the audio file are the same for all of them.

1. In the **Media Generator**, set up one generation the way you want it (you can test it there first).
2. Come back here, give the images and the audio file, press **Check**, then **Start**.

The generations are added to WanGP's queue and their results are named after their image, e.g. `photo12_seed123456.mp4`."""


class ImageFolderBatchPlugin(WAN2GPPlugin):
    def __init__(self):
        super().__init__()
        self.name = PlugIn_Name
        self.version = "1.0.0"
        self.description = "Queues one generation per image of a folder, all with the same settings and the same audio file."

    def setup_ui(self):
        self.request_global("get_current_model_settings")
        self.request_global("server_config")
        self.request_component("state")
        self.add_tab(tab_id=PlugIn_Id, label=PlugIn_Name, component_constructor=self.create_ui)

    # ------------------------------------------------------------------ helpers
    def _form_settings(self, state):
        settings = dict(self.get_current_model_settings(state) or {})
        settings["model_type"] = state.get("model_type") or settings.get("model_type")
        return settings

    def _output_dirs(self):
        config = self.server_config if isinstance(getattr(self, "server_config", None), dict) else {}
        folders = {config.get(key) or "outputs" for key in ("save_path", "image_save_path")}
        return [os.path.abspath(folder) for folder in folders]

    def _describe_form(self, state):
        try:
            settings = self._form_settings(state)
        except Exception:
            return "Media Generator settings: not available yet."
        prompt = " ".join(str(settings.get("prompt") or "").split())
        if len(prompt) > 160:
            prompt = prompt[:160] + "..."
        return (f"**Media Generator settings** - model `{settings.get('model_type')}`, resolution {settings.get('resolution')}, "
                f"{settings.get('video_length')} frames, {settings.get('num_inference_steps')} steps  \nPrompt: {prompt}")

    def on_tab_select(self, state: dict):
        return self._describe_form(state)

    def _plan(self, state, source, settings_file, folder, uploads, audio_upload, audio_path, use_as, prompt, skip_done, limit):
        if source == FROM_FILE:
            if not settings_file:
                raise PlanError("Upload the settings file, or use the settings of the Media Generator.")
            settings = load_settings_file(getattr(settings_file, "name", settings_file))
        else:
            settings = self._form_settings(state)
        return build_plan(settings, folder=folder, uploads=uploads, audio_upload=audio_upload, audio_path=audio_path, use_as=use_as,
                          prompt=prompt, skip_done=skip_done, limit=limit, output_dirs=self._output_dirs())

    # ----------------------------------------------------------------------- UI
    def create_ui(self, api_session):
        active_job = {"job": None, "shutdown": None}

        def check(state, *inputs):
            try:
                jobs, _images, _stems, _skipped, description = self._plan(state, *inputs)
            except PlanError as error:
                raise gr.Error(str(error))
            return description if jobs else description + "\n\nNothing to do."

        def shutdown_pod(action, progress):
            """Counts down, then stops or terminates the pod. Returns the lines to add to the report."""
            verb = "terminated" if action == pod_control.TERMINATE else "stopped"
            reason = pod_control.unavailable_reason()
            if reason:
                return [f"The pod was left running: {reason}."]
            active_job["shutdown"] = "pending"
            try:
                for remaining in range(SHUTDOWN_DELAY, 0, -1):
                    if active_job.get("shutdown") == "cancelled":
                        return ["Pod shutdown cancelled: the pod was left running."]
                    progress(1.0, desc=f"The pod will be {verb} in {remaining}s - press Stop to keep it running")
                    time.sleep(1)
                if active_job.get("shutdown") == "cancelled":
                    return ["Pod shutdown cancelled: the pod was left running."]
            finally:
                active_job["shutdown"] = None
            ok, message = pod_control.shutdown(action)
            print(f"[{PlugIn_Name}] pod {action}: {message}")
            return [f"The pod is being {verb}." if ok else message]

        def start(state, source, settings_file, folder, uploads, audio_upload, audio_path, use_as, prompt, skip_done, limit, when_done,
                  progress=WangpProgress()):
            previous = active_job.get("job")
            if previous is not None and not previous.done or active_job.get("shutdown"):
                raise gr.Error("A batch is already running. Stop it first, or wait for it to finish.")
            try:
                jobs, images, stems, _skipped, description = self._plan(state, source, settings_file, folder, uploads, audio_upload, audio_path,
                                                                         use_as, prompt, skip_done, limit)
            except PlanError as error:
                raise gr.Error(str(error))
            if not jobs:
                return description + "\n\nNothing to do.", None

            class Callbacks:
                ratio = 0.0

                def on_status(self, status):
                    status = str(status or "").strip()
                    if status:
                        progress(self.ratio, desc=status)

                def on_progress(self, update):
                    self.ratio = max(0.0, min(1.0, float(getattr(update, "progress", 0)) / 100.0))
                    progress(self.ratio, desc=str(getattr(update, "status", "") or "Generating..."))

            progress(0, desc=f"Adding {len(jobs)} generation(s) to the queue")
            job = api_session.submit_manifest(jobs, callbacks=Callbacks())
            active_job["job"] = job
            try:
                result = job.result()
            finally:
                if active_job.get("job") is job:
                    active_job["job"] = None

            files = [str(path) for path in (result.generated_files or [])]
            lines, missing = [], 0
            for image in images:
                outputs = files_of(stems[image], files)
                missing += not outputs
                lines.append(f"  {image.name}: " + (", ".join(os.path.basename(path) for path in outputs) if outputs else "no result"))
            status = "Stopped" if result.cancelled else "Finished"
            summary = [f"{status}: {len(images) - missing} of {len(images)} image(s) generated."] + lines
            errors = [str(error) for error in (result.errors or [])]
            if errors:
                summary += ["", "Errors:"] + [f"  {error}" for error in errors]
            if missing:
                summary += ["", "Press Start again to generate only the missing ones."]
            if when_done in (pod_control.STOP, pod_control.TERMINATE):
                # A batch stopped by hand keeps the pod: the user is there and may want to go on.
                summary += [""] + (["The batch was stopped: the pod was left running."] if result.cancelled else shutdown_pod(when_done, progress))
            return "\n".join(summary), (files or None)

        def stop():
            job = active_job.get("job")
            if active_job.get("shutdown") == "pending":
                active_job["shutdown"] = "cancelled"
                gr.Info("The pod will be left running.")
            elif job is not None and not job.done:
                job.cancel()
                gr.Info("Stopping the batch...")
            else:
                gr.Info("No batch is running.")

        with gr.Column():
            gr.Markdown(INTRO)
            form_info = gr.Markdown(self._describe_form(self.state.value) if isinstance(self.state.value, dict) else "")

            with gr.Row():
                with gr.Column():
                    gr.Markdown("### Images")
                    folder = gr.Textbox(label="Image folder on the server", placeholder="/workspace/input_images")
                    uploads = gr.File(label="...and / or upload images", file_count="multiple", file_types=["image"], type="filepath")
                    use_as = gr.Dropdown(label="Use each image as", choices=USE_AS_CHOICES, value="auto")
                with gr.Column():
                    gr.Markdown("### Audio (the same for every generation)")
                    audio_upload = gr.Audio(label="Upload the audio file", type="filepath", sources=["upload"])
                    audio_path = gr.Textbox(label="...or its path on the server", placeholder="/workspace/song.mp3",
                                            info="Leave both empty to use the audio loaded in the Media Generator.")

            with gr.Accordion("Settings and options", open=False):
                source = gr.Radio(label="Generation settings", choices=[FROM_FORM, FROM_FILE], value=FROM_FORM)
                settings_file = gr.File(label="Settings file exported from WanGP", file_types=[".json"], type="filepath")
                prompt = gr.Textbox(label="Prompt (leave empty to keep the prompt of the settings)", lines=3)
                with gr.Row():
                    skip_done = gr.Checkbox(label="Skip images that already have a result in the outputs folder", value=True)
                    limit = gr.Number(label="Only the first N images (0 = all)", value=0, precision=0, minimum=0)

            pod_reason = pod_control.unavailable_reason()
            when_done = gr.Dropdown(label="When all the generations are complete", choices=pod_control.CHOICES, value=pod_control.KEEP,
                                    interactive=not pod_reason,
                                    info=(f"Not available: {pod_reason}." if pod_reason else
                                          f"The pod is shut down {SHUTDOWN_DELAY}s after the last generation, unless you press Stop. "
                                          "A pod with a network volume cannot be stopped, only terminated."))

            with gr.Row():
                check_btn = gr.Button("Check")
                start_btn = gr.Button("Start", variant="primary")
                stop_btn = gr.Button("Stop", variant="stop")
            batch_progress = WangpProgress.component()
            report = gr.Textbox(label="Report", lines=12, interactive=False)
            results = gr.File(label="Results", file_count="multiple", interactive=False)

        self.on_tab_outputs = [form_info]

        plan_inputs = [self.state, source, settings_file, folder, uploads, audio_upload, audio_path, use_as, prompt, skip_done, limit]
        check_btn.click(fn=check, inputs=plan_inputs, outputs=[report])
        WangpProgress.bind(start_btn.click, start, inputs=[*plan_inputs, when_done], outputs=[report, results], component=batch_progress)
        stop_btn.click(fn=stop, queue=False)
