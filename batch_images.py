#!/usr/bin/env python3
"""Run one WanGP generation per image of a folder, all with the same settings.

Each image is used as the start frame and/or the first reference image, as the
settings file asks for, and the same audio file is used for every generation.
The jobs are handed to WanGP's headless mode in a single run, so the model is
loaded only once.

    python3 batch_images.py --settings my_settings.json --images /workspace/in \\
        --audio /workspace/song.mp3 --output-dir /workspace/out

The settings file is the one saved with WanGP's "Export Settings" button.
Stop the WanGP web UI first: both cannot use the GPU at the same time.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
HERE = Path(__file__).resolve().parent


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--settings", required=True, type=Path, help="settings .json exported from WanGP")
    parser.add_argument("--images", required=True, type=Path, help="folder of input images")
    parser.add_argument("--audio", type=Path, help="audio file used for every generation")
    parser.add_argument("--output-dir", type=Path, default=Path("/workspace/batch_outputs"), help="where the results are saved (default: %(default)s)")
    parser.add_argument("--prompt", help="replace the prompt of the settings file")
    parser.add_argument("--use-as", choices=["auto", "start", "reference", "both"], default="auto",
                        help="how each image is used; auto follows the settings file (default)")
    parser.add_argument("--limit", type=int, help="only process the first N images")
    parser.add_argument("--redo", action="store_true", help="also process images that already have a result in the output folder")
    parser.add_argument("--check", action="store_true", help="let WanGP validate the jobs without generating anything")
    parser.add_argument("--build-only", action="store_true", help="write the job file and stop")
    parser.add_argument("--job-file", type=Path, help="where to write the job file (default: <output-dir>/batch_jobs.json)")
    return parser.parse_args()


def natural_key(path):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


def output_stem(image):
    """A safe file name prefix that identifies the image's results."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", image.stem).strip("._") or "image"


def assign_stems(images):
    """One distinct prefix per image, even when two names sanitize alike (a.png / a.jpg)."""
    stems, used = {}, set()
    for image in images:
        stem, n = output_stem(image), 2
        while stem.lower() in used:
            stem = f"{output_stem(image)}-{n}"
            n += 1
        used.add(stem.lower())
        stems[image] = stem
    return stems


def has_result(output_dir, stem):
    if not output_dir.is_dir():
        return False
    pattern = re.compile(re.escape(stem) + r"_seed\d+")
    return any(p.is_file() and pattern.match(p.name) for p in output_dir.iterdir())


def build_job(settings, image, stem, audio, args):
    job = dict(settings)
    image_prompt_type = job.get("image_prompt_type") or ""
    video_prompt_type = job.get("video_prompt_type") or ""
    audio_prompt_type = job.get("audio_prompt_type") or ""

    as_start = "S" in image_prompt_type
    as_reference = "I" in video_prompt_type
    if args.use_as in ("start", "both") and not as_start:
        job["image_prompt_type"] = image_prompt_type + "S"
    if args.use_as in ("reference", "both") and not as_reference:
        job["video_prompt_type"] = video_prompt_type + "I"
    if args.use_as != "auto":
        as_start = args.use_as in ("start", "both")
        as_reference = args.use_as in ("reference", "both")

    if as_start:
        job["image_start"] = [str(image)]
    if as_reference:
        job["image_refs"] = [str(image)]
    if audio is not None and "A" in audio_prompt_type:
        job["audio_guide"] = str(audio)
    if args.prompt:
        job["prompt"] = args.prompt
    # Results are named after their input image, e.g. "photo12_seed123456789.mp4".
    job["output_filename"] = stem + "_seed{seed}"
    return job, as_start, as_reference


def main():
    args = parse_args()

    settings_path = args.settings.expanduser().resolve()
    images_dir = args.images.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    audio = args.audio.expanduser().resolve() if args.audio else None

    if not settings_path.is_file():
        sys.exit(f"Settings file not found: {settings_path}")
    if not images_dir.is_dir():
        sys.exit(f"Image folder not found: {images_dir}")
    if audio is not None and not audio.is_file():
        sys.exit(f"Audio file not found: {audio}")

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    if not isinstance(settings, dict):
        sys.exit("The settings file must be a single settings export (a JSON object).")

    wants_audio = "A" in (settings.get("audio_prompt_type") or "")
    if wants_audio and audio is None:
        sys.exit("These settings use an audio file: pass it with --audio.")
    if audio is not None and not wants_audio:
        print("Note: these settings do not use an audio file, --audio is ignored.")

    images = sorted((p for p in images_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS), key=natural_key)
    if not images:
        sys.exit(f"No images ({', '.join(sorted(IMAGE_EXTENSIONS))}) in {images_dir}")
    stems = assign_stems(images)

    if not args.redo:
        done = [p for p in images if has_result(output_dir, stems[p])]
        if done:
            print(f"Skipping {len(done)} image(s) that already have a result (use --redo to generate them again).")
        images = [p for p in images if p not in done]
    if args.limit is not None:
        images = images[:args.limit]
    if not images:
        print("Nothing to do.")
        return

    jobs = []
    for image in images:
        job, as_start, as_reference = build_job(settings, image, stems[image], audio, args)
        jobs.append(job)
    if not (as_start or as_reference):
        sys.exit("These settings use neither a start image nor reference images. Pass --use-as start, reference or both.")

    usage = " and ".join(name for name, on in (("start frame", as_start), ("reference image", as_reference)) if on)
    print(f"{len(jobs)} image(s) to process, each used as {usage}" + (f", with audio {audio.name}" if wants_audio else "") + ".")

    output_dir.mkdir(parents=True, exist_ok=True)
    job_file = (args.job_file.expanduser().resolve() if args.job_file else output_dir / "batch_jobs.json")
    job_file.write_text(json.dumps(jobs, indent=2), encoding="utf-8")
    print(f"Job file: {job_file}")
    if args.build_only:
        return

    if not args.check and subprocess.run(["pgrep", "-f", "wgp.py"], capture_output=True).returncode == 0:
        print("WARNING: WanGP seems to be running already. Stop it first (pkill -f setup.sh; pkill -f wgp.py),\n"
              "         otherwise both will compete for the GPU memory.")

    command = ["bash", str(HERE / "setup.sh"), "run", "--process", str(job_file), "--output-dir", str(output_dir)]
    if args.check:
        command.append("--dry-run")
    print("Running:", " ".join(command), flush=True)
    code = subprocess.call(command)

    if not args.check:
        missing = [p.name for p in images if not has_result(output_dir, stems[p])]
        print(f"\n{len(images) - len(missing)}/{len(images)} image(s) have a result in {output_dir}")
        if missing:
            print("No result for: " + ", ".join(missing))
            print("Run the same command again to retry only these.")
            code = code or 1
    sys.exit(code)


if __name__ == "__main__":
    main()
