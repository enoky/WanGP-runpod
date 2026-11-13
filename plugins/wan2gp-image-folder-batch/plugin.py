"""Image Folder Batch: one WanGP generation per image of a folder.

Every image becomes the start frame and/or the reference image of its own
generation. All generations share the same settings (those of the Media
Generator, or an exported settings file) and the same audio file.

The batch runs in a separate WanGP process without web interface, so it keeps
going when the browser is closed or the tablet sleeps. This tab only starts
it, shows its progress and can stop it.
"""
import os
import sys
import threading
import time

import gradio as gr

from shared.utils.plugins import WAN2GPPlugin
from shared.utils.process_locks import acquire_GPU_ressources, any_GPU_process_running, release_GPU_ressources

from . import batch_status, pod_control
from .batch_plan import USE_AS_CHOICES, PlanError, build_plan, load_settings_file

PlugIn_Name = "Image Folder Batch"
PlugIn_Id = "ImageFolderBatch"
FROM_FORM = "The current settings of the Media Generator"
FROM_FILE = "An exported settings file (.json)"
SHUTDOWN_DELAY = 60  # seconds left to press Stop before the pod is shut down
WORK_FOLDER = "image_folder_batch"  # inside the WanGP folder: job list, status and log of the batch

INTRO = """**One generation per image.** Each image of the folder becomes the start frame and / or the reference image
of its own generation. Everything else (model, prompt, resolution, length...) and the audio file are the same for all of them.

1. In the **Media Generator**, set up one generation the way you want it (you can test it there first).
2. Come back here, give the images and the audio file, press **Check**, then **Start**.

The batch runs on its own on the server: **you can close this page or let your tablet sleep**, and come back to this tab
later to see how far it is. While it runs, the Media Generator has to wait for the GPU.
Results go to the usual outputs folder, named after their image, e.g. `photo12_seed123456.mp4`."""


class ImageFolderBatchPlugin(WAN2GPPlugin):
    def __init__(self):
        super().__init__()
        self.name = PlugIn_Name
        self.version = "2.0.0"
        self.description = "Runs one generation per image of a folder, all with the same settings and the same audio file."

    def setup_ui(self):
        self.request_global("get_current_model_settings")
        self.request_global("collect_current_model_settings")
        self.request_global("release_model")
        self.request_global("server_config")
        self.request_component("state")
        self.add_tab(tab_id=PlugIn_Id, label=PlugIn_Name, component_constructor=self.create_ui)

    # ------------------------------------------------------------------ helpers
    @property
    def work_dir(self):
        return os.path.abspath(WORK_FOLDER)

    def _output_dir(self):
        config = self.server_config if isinstance(getattr(self, "server_config", None), dict) else {}
        return os.path.abspath(config.get("save_path") or "outputs")

    def _form_settings(self, state):
        """The Media Generator's settings as "Export Settings" writes them, plus the audio file loaded there."""
        settings = dict(self.collect_current_model_settings(state) or {})
        settings.pop("state", None)
        raw = self.get_current_model_settings(state) or {}
        raw.pop("state", None)  # added by the export, not a setting
        if isinstance(raw.get("audio_guide"), str):
            settings.setdefault("audio_guide", raw["audio_guide"])
        return settings

    def _describe_form(self, state):
        try:
            settings = self.get_current_model_settings(state) or {}
            model_type = state.get("model_type")
        except Exception:
            return "Media Generator settings: not available yet."
        prompt = " ".join(str(settings.get("prompt") or "").split())
        if len(prompt) > 160:
            prompt = prompt[:160] + "..."
        return (f"**Media Generator settings** - model `{model_type}`, resolution {settings.get('resolution')}, "
                f"{settings.get('video_length')} frames, {settings.get('num_inference_steps')} steps  \nPrompt: {prompt}")

    def _plan(self, state, source, settings_file, folder, uploads, audio_upload, audio_path, use_as, prompt, skip_done, limit):
        if source == FROM_FILE:
            if not settings_file:
                raise PlanError("Upload the settings file, or use the settings of the Media Generator.")
            settings = load_settings_file(getattr(settings_file, "name", settings_file))
        else:
            settings = self._form_settings(state)
        return build_plan(settings, folder=folder, uploads=uploads, audio_upload=audio_upload, audio_path=audio_path, use_as=use_as,
                          prompt=prompt, skip_done=skip_done, limit=limit, output_dirs=[self._output_dir()])

    def _report(self):
        text, files, active = batch_status.report(self.work_dir)
        return text, (files or None), active

    def on_tab_select(self, state: dict):
        text, files, _active = self._report()
        return self._describe_form(state), text, files

    def _hold_gpu_while_running(self, state, process):
        """Keeps WanGP's own generations waiting until the batch process has ended."""
        def wait():
            try:
                process.wait()
                while batch_status.is_active(self.work_dir):  # the pod shutdown countdown
                    time.sleep(2)
            finally:
                release_GPU_ressources(state, PlugIn_Id)

        threading.Thread(target=wait, daemon=True, name="image-folder-batch-gpu").start()

    # ----------------------------------------------------------------------- UI
    def create_ui(self):
        def check(state, *inputs):
            try:
                jobs, _images, _stems, _skipped, description = self._plan(state, *inputs)
            except PlanError as error:
                raise gr.Error(str(error))
            return description if jobs else description + "\n\nNothing to do."

        def start(state, source, settings_file, folder, uploads, audio_upload, audio_path, use_as, prompt, skip_done, limit, when_done):
            if batch_status.is_active(self.work_dir):
                raise gr.Error("A batch is already running. Stop it first, or wait for it to finish.")
            try:
                jobs, images, stems, _skipped, description = self._plan(state, source, settings_file, folder, uploads, audio_upload,
                                                                         audio_path, use_as, prompt, skip_done, limit)
            except PlanError as error:
                raise gr.Error(str(error))
            if not jobs:
                return description + "\n\nNothing to do.", gr.update()
            if any_GPU_process_running(state, PlugIn_Id):
                raise gr.Error("WanGP is generating right now. Wait for its queue to finish (or abort it), then press Start again.")

            # The batch is another process: take the GPU away from this one and free its model first.
            acquire_GPU_ressources(state, PlugIn_Id, PlugIn_Name, gr=gr)
            try:
                self.release_model()
                process = batch_status.launch(self.work_dir, jobs, images, stems, self._output_dir(), when_done, SHUTDOWN_DELAY, sys.argv[1:])
            except Exception as error:
                release_GPU_ressources(state, PlugIn_Id)
                raise gr.Error(f"The batch could not be started: {error}")
            self._hold_gpu_while_running(state, process)
            gr.Info(f"Batch started: {len(jobs)} generation(s). You can close this page.")
            return description + "\n\nBatch started. This report refreshes by itself.", gr.update()

        def stop():
            gr.Info(batch_status.stop(self.work_dir))

        def refresh():
            text, files, _active = self._report()
            return text, files

        def refresh_if_active(current_text):
            text, files, active = self._report()
            current_text = str(current_text or "")
            # A report showing a live state still needs its final update once the batch has ended.
            live = current_text.startswith("RUNNING") or "Batch started" in current_text or "press Stop to keep it running" in current_text
            if active or live:
                return text, files
            return gr.update(), gr.update()

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
                refresh_btn = gr.Button("Refresh")
            initial_text, initial_files, _ = self._report()
            report = gr.Textbox(label="Report", lines=16, interactive=False, value=initial_text)
            results = gr.File(label="Results", file_count="multiple", interactive=False, value=initial_files)
            timer = gr.Timer(5)

        self.on_tab_outputs = [form_info, report, results]

        plan_inputs = [self.state, source, settings_file, folder, uploads, audio_upload, audio_path, use_as, prompt, skip_done, limit]
        check_btn.click(fn=check, inputs=plan_inputs, outputs=[report])
        start_btn.click(fn=start, inputs=[*plan_inputs, when_done], outputs=[report, results])
        stop_btn.click(fn=stop, queue=False)
        refresh_btn.click(fn=refresh, outputs=[report, results])
        timer.tick(fn=refresh_if_active, inputs=[report], outputs=[report, results], show_progress="hidden")
