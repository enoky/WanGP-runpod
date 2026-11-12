"""Builds the list of generation jobs: one per image, all sharing settings and audio.

Kept free of Gradio and WanGP imports so that it can be tested on its own.
"""
import json
import os
import re
from pathlib import Path

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
USE_AS_CHOICES = [("As the settings say", "auto"), ("Start frame", "start"), ("Reference image", "reference"),
                  ("Start frame and reference image", "both")]


class PlanError(Exception):
    """A problem the user can fix (missing folder, missing audio...)."""


def natural_key(path):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", Path(path).name)]


def list_images(folder, uploads):
    """Images of the server folder followed by the uploaded ones."""
    images = []
    folder = (folder or "").strip()
    if folder:
        folder_path = Path(folder).expanduser()
        if not folder_path.is_dir():
            raise PlanError(f"Image folder not found: {folder_path}")
        images += sorted((p for p in folder_path.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS), key=natural_key)
    for upload in uploads or []:
        path = Path(getattr(upload, "name", upload))
        if path.suffix.lower() in IMAGE_EXTENSIONS:
            images.append(path)
    if not images:
        raise PlanError("No images: give a folder that contains images, or upload some.")
    return [p.resolve() for p in images]


def assign_stems(images):
    """A distinct, file-name-safe prefix per image (a.png and a.jpg must not collide)."""
    stems, used = {}, set()
    for image in images:
        base = re.sub(r"[^A-Za-z0-9._-]+", "_", image.stem).strip("._") or "image"
        stem, n = base, 2
        while stem.lower() in used:
            stem = f"{base}-{n}"
            n += 1
        used.add(stem.lower())
        stems[image] = stem
    return stems


def result_pattern(stem):
    return re.compile(re.escape(stem) + r"_seed\d+")


def files_of(stem, files):
    pattern = result_pattern(stem)
    return [f for f in files if pattern.match(os.path.basename(str(f)))]


def has_result(stem, output_dirs):
    pattern = result_pattern(stem)
    for folder in output_dirs:
        try:
            if any(pattern.match(name) for name in os.listdir(folder)):
                return True
        except OSError:
            continue
    return False


def load_settings_file(path):
    try:
        with open(path, "r", encoding="utf-8") as reader:
            settings = json.load(reader)
    except (OSError, ValueError) as error:
        raise PlanError(f"The settings file could not be read: {error}")
    if not isinstance(settings, dict) or "model_type" not in settings:
        raise PlanError("The settings file must be a single settings export from WanGP (Export Settings).")
    return settings


def resolve_audio(settings, audio_upload, audio_path):
    """The audio file of the batch, or None when the settings use none."""
    wants_audio = "A" in (settings.get("audio_prompt_type") or "")
    audio = (audio_path or "").strip() or audio_upload
    if not audio and isinstance(settings.get("audio_guide"), str):
        audio = settings["audio_guide"]  # the one loaded in the Media Generator
    if not wants_audio:
        return None
    if not audio:
        raise PlanError("These settings use an audio file: upload it here or give its path.")
    audio = Path(str(audio)).expanduser()
    if not audio.is_file():
        raise PlanError(f"Audio file not found: {audio}")
    return str(audio.resolve())


def build_plan(settings, *, folder="", uploads=None, audio_upload=None, audio_path="", use_as="auto", prompt="",
               skip_done=True, limit=0, output_dirs=()):
    """Returns (jobs, images, stems, skipped, description)."""
    settings = {key: value for key, value in settings.items() if key != "state"}
    if not settings.get("model_type"):
        raise PlanError("No model is selected.")

    image_prompt_type = settings.get("image_prompt_type") or ""
    video_prompt_type = settings.get("video_prompt_type") or ""
    as_start, as_reference = "S" in image_prompt_type, "I" in video_prompt_type
    if use_as != "auto":
        as_start, as_reference = use_as in ("start", "both"), use_as in ("reference", "both")
        if as_start and "S" not in image_prompt_type:
            settings["image_prompt_type"] = image_prompt_type + "S"
        if as_reference and "I" not in video_prompt_type:
            settings["video_prompt_type"] = video_prompt_type + "I"
    if not (as_start or as_reference):
        raise PlanError("These settings use neither a start image nor reference images: choose how the images are used.")

    audio = resolve_audio(settings, audio_upload, audio_path)
    images = list_images(folder, uploads)
    stems = assign_stems(images)

    skipped = [image for image in images if skip_done and has_result(stems[image], output_dirs)]
    images = [image for image in images if image not in skipped]
    if limit and int(limit) > 0:
        images = images[:int(limit)]

    jobs = []
    for image in images:
        job = dict(settings)
        for key in ("image_start", "image_refs"):
            job.pop(key, None)
        if as_start:
            job["image_start"] = [str(image)]
        if as_reference:
            job["image_refs"] = [str(image)]
        if audio is not None:
            job["audio_guide"] = audio
        if (prompt or "").strip():
            job["prompt"] = prompt.strip()
        # Results are named after their image, e.g. "photo12_seed123456789.mp4".
        job["output_filename"] = stems[image] + "_seed{seed}"
        jobs.append(job)

    usage = " and ".join(name for name, on in (("start frame", as_start), ("reference image", as_reference)) if on)
    lines = [f"Model: {settings['model_type']}",
             f"{len(jobs)} image(s) to generate, each used as {usage}.",
             f"Audio: {os.path.basename(audio)}" if audio else "Audio: none (these settings do not use an audio file)"]
    if skipped:
        lines.append(f"Skipped, already generated: {len(skipped)}")
    lines += [f"  {index}. {image.name}" for index, image in enumerate(images, 1)]
    return jobs, images, stems, skipped, "\n".join(lines)
