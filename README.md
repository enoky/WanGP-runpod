# WanGP on RunPod

`setup.sh` installs and launches [WanGP](https://github.com/deepbeepmeep/WanGP) on a RunPod GPU pod.

## Pod settings

| Setting | Value |
| --- | --- |
| Template | Any Ubuntu-based NVIDIA image, e.g. *RunPod PyTorch 2.x* (the script brings its own Python and PyTorch) |
| GPU | Any RTX 30xx or newer / A-series / L-series card |
| Exposed HTTP port | `7860` |
| Volume (`/workspace`) | 100 GB or more: the saved environment takes ~15 GB, each model 10-30 GB |
| Container disk | 40 GB: the Python environment (~15 GB) runs from it |

Optional pod environment variable: `WANGP_AUTH_PASSWORD` (the password for the web UI).

## Run

In the pod's web terminal or over SSH:

```bash
cd /workspace
git clone https://github.com/enoky/WanGP-runpod.git
bash WanGP-runpod/setup.sh
```

The first run takes 10-20 minutes. When `Running on local URL` appears, open the pod's
**Connect → HTTP Service [7860]** link (`https://<pod-id>-7860.proxy.runpod.net`).

The UI is password protected by default. Without `WANGP_AUTH_PASSWORD`, WanGP prints a
generated password (`WanGP web password: ...`) in the terminal at each start.

To keep WanGP running after the terminal closes:

```bash
nohup bash /workspace/WanGP-runpod/setup.sh > /workspace/wangp.log 2>&1 &
tail -f /workspace/wangp.log
```

After a pod restart, run the same command again: the Python environment is restored from its
archive on `/workspace` instead of being downloaded again, which takes a few minutes.

## Commands

```bash
bash setup.sh            # install if needed, then launch
bash setup.sh install    # install / repair only
bash setup.sh start      # launch only
bash setup.sh update     # pull the latest WanGP and refresh dependencies
```

## Settings

Set these as environment variables (in the pod template or in front of the command).

| Variable | Default | Meaning |
| --- | --- | --- |
| `WANGP_AUTH_PASSWORD` | generated | Web UI password |
| `WANGP_AUTH` | `1` | `0` disables the password. The RunPod proxy URL is reachable by anyone who has it |
| `WANGP_PORT` | `7860` | Port of the web UI |
| `WANGP_PUBLIC_URL` | the pod's RunPod proxy URL | The address the UI is opened at; logins from any other address are rejected. `none` lifts the restriction |
| `WANGP_ARGS` | empty | Extra `wgp.py` flags, e.g. `"--profile 3 --compile"` |
| `WANGP_REF` | default branch | WanGP branch, tag or commit to install |
| `WANGP_STACK` | `auto` | `cu130` (PyTorch 2.10, needs NVIDIA driver 580+) or `cu128` (PyTorch 2.7.1). `auto` picks from the pod's driver |
| `WANGP_EXTRA_KERNELS` | `1` | `0` skips the optional Nunchaku / GGUF / NVFP4 kernels |
| `WORKSPACE` | `/workspace` | Where everything is stored |

## Where things are

| Path | Content |
| --- | --- |
| `/workspace/WanGP` | WanGP checkout and config (`wgp_config.json`) |
| `/workspace/.wangp-env.tar` | Saved Python environment, unpacked to `/opt/wangp` on the container disk |
| `/workspace/WanGP/ckpts` | Downloaded models |
| `/workspace/WanGP/loras` | LoRAs |
| `/workspace/WanGP/outputs` | Generated videos and images |
| `/workspace/.cache/huggingface` | Hugging Face cache |

## What gets installed

Following WanGP's own Linux recommendations: Python 3.11, PyTorch 2.10 / CUDA 13, the
requirements, and the prebuilt SageAttention 2.2, WGPKernels, GGUF and Nunchaku wheels
(plus NVFP4 kernels on RTX 50xx). On pods whose NVIDIA driver is older than 580 it falls back
to WanGP's legacy stack: Python 3.10, PyTorch 2.7.1 / CUDA 12.8.

Flash and Sparge attention are not installed: on Linux they compile from source for a long
time, and SageAttention is the recommended mode.
