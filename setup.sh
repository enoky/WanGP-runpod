#!/usr/bin/env bash
# WanGP (https://github.com/deepbeepmeep/WanGP) installer + launcher for RunPod pods.
#
#   bash setup.sh            install (first run only) and launch the web UI
#   bash setup.sh install    install / repair only
#   bash setup.sh start      launch only
#   bash setup.sh update     pull the latest WanGP and refresh its dependencies
#
# Everything that must survive a pod restart lives under /workspace (the network
# volume): the WanGP checkout, models, LoRAs, outputs and an archive of the
# Python environment (which itself runs from the faster container disk).
# Settings are environment variables, see the "Settings" block below.

set -euo pipefail

# ------------------------------- Settings -----------------------------------
WORKSPACE="${WORKSPACE:-/workspace}"
WANGP_DIR="${WANGP_DIR:-$WORKSPACE/WanGP}"
WANGP_REPO="${WANGP_REPO:-https://github.com/deepbeepmeep/WanGP.git}"
WANGP_REF="${WANGP_REF:-}"                 # branch, tag or commit; empty = default branch
WANGP_PORT="${WANGP_PORT:-7860}"           # expose this as an HTTP port on the pod
WANGP_AUTH="${WANGP_AUTH:-1}"              # 1 = password-protect the UI, 0 = open
WANGP_ARGS="${WANGP_ARGS:-}"               # extra wgp.py arguments, e.g. "--profile 3 --compile"
WANGP_STACK="${WANGP_STACK:-auto}"         # auto | cu130 | cu128
WANGP_EXTRA_KERNELS="${WANGP_EXTRA_KERNELS:-1}"  # 1 = also install Nunchaku / GGUF kernels
# WANGP_AUTH_PASSWORD   the UI password; if unset WanGP generates one and prints it at startup
# WANGP_PUBLIC_URL      the URL the UI is opened at; defaults to the RunPod proxy URL of this pod.
#                       Set it to "none" when connecting some other way (e.g. a direct TCP port).

# The Python environment is ~100k small files, which network volumes handle
# very slowly. It lives on the fast container disk and is saved to / restored
# from a single archive on the volume.
LOCAL_DIR="${WANGP_LOCAL_DIR:-/opt/wangp}"
VENV_DIR="$LOCAL_DIR/venv"
ENV_ARCHIVE="${WANGP_ENV_ARCHIVE:-$WORKSPACE/.wangp-env.tar}"
PYTHON_DIR="$LOCAL_DIR/python"
ENV_ON_VOLUME=0
# Installations made by the first version of this script keep their
# environment on the volume; they are used as they are.
if [ -z "${WANGP_LOCAL_DIR:-}" ] && [ -f "$WANGP_DIR/venv/.runpod_setup_stamp" ]; then
    ENV_ON_VOLUME=1
    VENV_DIR="$WANGP_DIR/venv"
    PYTHON_DIR="$WORKSPACE/.uv/python"
fi
UV_HOME="${UV_HOME:-$WORKSPACE/.uv}"
STAMP_FILE="$VENV_DIR/.runpod_setup_stamp"
SCRIPT_VERSION=1

export HF_HOME="${HF_HOME:-$WORKSPACE/.cache/huggingface}"
export UV_PYTHON_INSTALL_DIR="$PYTHON_DIR"
export UV_LINK_MODE=copy
export UV_NO_CACHE=1                       # keep the (small) container disk free
export UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-300}"
export DEBIAN_FRONTEND=noninteractive
export PYTHONUNBUFFERED=1
export PATH="$UV_HOME/bin:$PATH"

log()  { printf '\n\033[1;36m[wangp-setup]\033[0m %s\n' "$*"; }
warn() { printf '\n\033[1;33m[wangp-setup] WARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\n\033[1;31m[wangp-setup] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null 2>&1 && SUDO="sudo"
fi

# --------------------------- System packages --------------------------------
# The container disk is wiped whenever the pod restarts, so this runs on every
# launch. It is a no-op when the packages are already there.
install_system_packages() {
    # (ffmpeg also pulls in libglib2.0, which OpenCV needs.)
    local wanted=(git curl ca-certificates ffmpeg build-essential libgl1 libportaudio2)
    local missing=() pkg
    for pkg in "${wanted[@]}"; do
        dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
    done
    [ "${#missing[@]}" -eq 0 ] && return 0
    command -v apt-get >/dev/null 2>&1 || die "apt-get not found; install manually: ${missing[*]}"
    log "Installing system packages: ${missing[*]}"
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq --no-install-recommends "${missing[@]}"
}

install_uv() {
    [ -x "$UV_HOME/bin/uv" ] && return 0
    log "Installing uv into $UV_HOME/bin"
    mkdir -p "$UV_HOME/bin"
    curl -fsSL "https://github.com/astral-sh/uv/releases/latest/download/uv-$(uname -m)-unknown-linux-gnu.tar.gz" \
        | tar -xz --no-same-owner --strip-components=1 -C "$UV_HOME/bin" \
        || die "Could not download uv"
    [ -x "$UV_HOME/bin/uv" ] || die "uv installation failed"
}

# ------------------------------ GPU checks ----------------------------------
DRIVER_MAJOR=0
detect_gpu() {
    if ! command -v nvidia-smi >/dev/null 2>&1; then
        warn "nvidia-smi not found. WanGP needs an NVIDIA GPU pod."
        return 0
    fi
    local driver
    driver="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n1 || true)"
    DRIVER_MAJOR="${driver%%.*}"
    [[ "$DRIVER_MAJOR" =~ ^[0-9]+$ ]] || DRIVER_MAJOR=0
    log "GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | head -n1) | driver ${driver:-unknown}"
}

# CUDA 13 wheels need NVIDIA driver R580+; CUDA 12.8 wheels need R570+.
choose_stack() {
    case "$WANGP_STACK" in
        cu130|cu128) STACK="$WANGP_STACK" ;;
        auto)
            if [ "$DRIVER_MAJOR" -ge 580 ] || [ "$DRIVER_MAJOR" -eq 0 ]; then
                STACK=cu130
            else
                STACK=cu128
                warn "Driver $DRIVER_MAJOR is older than R580: using the legacy PyTorch 2.7.1 / CUDA 12.8 stack."
            fi ;;
        *) die "WANGP_STACK must be auto, cu130 or cu128 (got '$WANGP_STACK')" ;;
    esac

    local kernels="https://github.com/deepbeepmeep/kernels/releases/download"
    local sage="https://github.com/deepbeepmeep/SageAttention/releases/download/v2.2.0-linux.cg1"
    local wgpk="https://github.com/deepbeepmeep/WGPKernels/releases/download/v1.4.3"
    if [ "$STACK" = cu130 ]; then
        PY_VERSION=3.11; PY_TAG=cp311
        TORCH_SPEC=(torch==2.10.0 torchvision==0.25.0 torchaudio==2.10.0)
        SAGE_WHEEL="$sage/sageattention-2.2.0+cu130torch2.10.0.cg1-$PY_TAG-$PY_TAG-linux_x86_64.whl"
        WGPKERNELS_WHEEL="$wgpk/wgpkernels-1.4.3%2Bcu130-py3-none-linux_x86_64.whl"
        GGUF_WHEEL="$kernels/gguf-v1.0.30/llamacpp_gguf_cuda-1.0.30%2Btorch210cu130py311-cp311-cp311-linux_x86_64.whl"
        NUNCHAKU_WHEEL="https://github.com/nunchaku-ai/nunchaku/releases/download/v1.2.1/nunchaku-1.2.1+cu13.0torch2.10-cp311-cp311-linux_x86_64.whl"
        LIGHTX2V_WHEEL="$kernels/Light2xv/lightx2v_kernel-0.0.2+torch2.10.0-cp311-abi3-linux_x86_64.whl"
    else
        PY_VERSION=3.10; PY_TAG=cp310
        TORCH_SPEC=(torch==2.7.1 torchvision==0.22.1 torchaudio==2.7.1)
        SAGE_WHEEL="$sage/sageattention-2.2.0+cu128torch2.7.1.cg1-$PY_TAG-$PY_TAG-linux_x86_64.whl"
        WGPKERNELS_WHEEL="$wgpk/wgpkernels-1.4.3%2Bcu128-py3-none-linux_x86_64.whl"
        GGUF_WHEEL="$kernels/gguf-v1.0.30/llamacpp_gguf_cuda-1.0.30%2Btorch271cu128py310-cp310-cp310-linux_x86_64.whl"
        NUNCHAKU_WHEEL="$kernels/v1.2.0_Nunchaku/nunchaku-1.2.0+torch2.7-cp310-cp310-linux_x86_64.whl"
        LIGHTX2V_WHEEL=""
    fi
    TORCH_INDEX="https://download.pytorch.org/whl/$STACK"
}

# ------------------------------ Install -------------------------------------
fetch_wangp() {
    if [ ! -d "$WANGP_DIR/.git" ]; then
        log "Cloning WanGP into $WANGP_DIR"
        mkdir -p "$(dirname "$WANGP_DIR")"
        git clone "$WANGP_REPO" "$WANGP_DIR"
    fi
    if [ -n "$WANGP_REF" ]; then
        git -C "$WANGP_DIR" fetch --tags origin
        git -C "$WANGP_DIR" checkout "$WANGP_REF"
    fi
}

stamp_value() {
    printf '%s|%s|%s|%s' "$SCRIPT_VERSION" "$STACK" "$WANGP_EXTRA_KERNELS" \
        "$(sha256sum "$WANGP_DIR/requirements.txt" | cut -d' ' -f1)"
}

pip_install() { uv pip install --python "$VENV_DIR/bin/python" --index-strategy unsafe-best-match "$@"; }

# Optional accelerators must never break the installation.
pip_install_optional() {
    local label="$1"; shift
    log "Installing $label"
    pip_install "$@" || warn "$label could not be installed; WanGP will run without it."
}

# Bring the environment back from the volume after a pod restart.
restore_python_env() {
    [ "$ENV_ON_VOLUME" = 1 ] && return 0
    [ -x "$VENV_DIR/bin/python" ] && return 0
    [ -f "$ENV_ARCHIVE" ] || return 0
    log "Restoring the Python environment from $ENV_ARCHIVE (a few minutes)"
    rm -rf "$LOCAL_DIR"
    mkdir -p "$LOCAL_DIR"
    if ! tar -xf "$ENV_ARCHIVE" --no-same-owner -C "$LOCAL_DIR"; then
        warn "The saved environment is unusable; it will be reinstalled."
        rm -rf "$LOCAL_DIR" "$ENV_ARCHIVE"
    fi
}

save_python_env() {
    [ "$ENV_ON_VOLUME" = 1 ] && return 0
    log "Saving the Python environment to $ENV_ARCHIVE (a few minutes)"
    if tar -cf "$ENV_ARCHIVE.tmp" -C "$LOCAL_DIR" . && mv -f "$ENV_ARCHIVE.tmp" "$ENV_ARCHIVE"; then
        return 0
    fi
    rm -f "$ENV_ARCHIVE.tmp"
    warn "Could not save the environment; it will be reinstalled after the next pod restart."
}

install_python_env() {
    restore_python_env
    if [ -f "$STAMP_FILE" ] && [ "$(cat "$STAMP_FILE")" = "$(stamp_value)" ]; then
        log "Python environment is up to date."
        return 0
    fi

    local free_gb=99
    if [ "$ENV_ON_VOLUME" = 0 ]; then
        mkdir -p "$LOCAL_DIR"
        free_gb="$(df -BG --output=avail "$LOCAL_DIR" | tail -n1 | tr -dc '0-9')"
    fi
    if [ "${free_gb:-0}" -lt 20 ]; then
        die "Only ${free_gb}G free on the container disk ($LOCAL_DIR); the environment needs about 20G. Raise the pod's container disk to 40 GB."
    fi

    if [ ! -x "$VENV_DIR/bin/python" ] || ! "$VENV_DIR/bin/python" -c "import sys; sys.exit(sys.version_info[:2] != tuple(map(int, '$PY_VERSION'.split('.'))))"; then
        log "Creating Python $PY_VERSION environment in $VENV_DIR"
        uv venv --clear --python-preference only-managed --python "$PY_VERSION" "$VENV_DIR"
    fi

    log "Installing PyTorch (${TORCH_SPEC[0]}, $STACK)"
    uv pip install --python "$VENV_DIR/bin/python" "${TORCH_SPEC[@]}" --index-url "$TORCH_INDEX"

    # Pin the exact torch builds so that requirements.txt cannot swap them.
    local constraints="$VENV_DIR/.torch-constraints.txt"
    uv pip freeze --python "$VENV_DIR/bin/python" | grep -E '^(torch|torchvision|torchaudio)==' > "$constraints"

    log "Installing WanGP requirements (this takes several minutes)"
    (cd "$WANGP_DIR" && pip_install -r requirements.txt -c "$constraints" --extra-index-url "$TORCH_INDEX")

    pip_install_optional "SageAttention 2.2.0" "$SAGE_WHEEL"
    pip_install_optional "WGPKernels (INT8 kernels)" --no-deps "$WGPKERNELS_WHEEL"
    if [ "$WANGP_EXTRA_KERNELS" = 1 ]; then
        pip_install_optional "GGUF CUDA kernels" --no-deps "$GGUF_WHEEL"
        pip_install_optional "Nunchaku INT4/FP4 kernels" -c "$constraints" "$NUNCHAKU_WHEEL"
        # NVFP4 kernels only make sense on RTX 50xx (Blackwell, compute capability 12.x).
        if [ -n "$LIGHTX2V_WHEEL" ] && "$VENV_DIR/bin/python" -c "import sys, torch; sys.exit(not (torch.cuda.is_available() and torch.cuda.get_device_capability()[0] >= 12))" 2>/dev/null; then
            pip_install_optional "LightX2V NVFP4 kernels" "$LIGHTX2V_WHEEL"
        fi
    fi

    stamp_value > "$STAMP_FILE"
    save_python_env
}

verify_install() {
    log "Checking the installation"
    "$VENV_DIR/bin/python" - <<'PY' || warn "PyTorch check failed, see the message above."
import torch
print(f"torch {torch.__version__} | CUDA build {torch.version.cuda} | CUDA available: {torch.cuda.is_available()}")
if torch.cuda.is_available():
    p = torch.cuda.get_device_properties(0)
    print(f"GPU 0: {p.name}, {p.total_memory // 2**20} MiB, compute capability {p.major}.{p.minor}")
    torch.zeros(1, device="cuda")
else:
    print("PyTorch cannot see a GPU: the NVIDIA driver on this pod is probably too old for this CUDA build.")
    print("Try another pod, or reinstall with WANGP_STACK=cu128.")
try:
    import sageattention
    print("SageAttention: installed")
except Exception as exc:
    print(f"SageAttention: not available ({exc})")
PY
}

do_install() {
    install_system_packages
    install_uv
    detect_gpu
    choose_stack
    fetch_wangp
    install_python_env
    verify_install
}

do_update() {
    install_system_packages
    [ -d "$WANGP_DIR/.git" ] || die "WanGP is not installed yet, run: bash setup.sh install"
    log "Updating WanGP"
    if [ -n "$WANGP_REF" ]; then
        git -C "$WANGP_DIR" fetch --tags origin
        git -C "$WANGP_DIR" checkout "$WANGP_REF"
    else
        git -C "$WANGP_DIR" pull --ff-only
    fi
    do_install   # reinstalls only if requirements.txt changed
}

# ------------------------------- Launch -------------------------------------
do_start() {
    install_system_packages
    restore_python_env
    [ -x "$VENV_DIR/bin/python" ] && [ -f "$STAMP_FILE" ] || die "WanGP is not installed yet, run: bash setup.sh install"

    local args=(--listen --server-port "$WANGP_PORT")
    if [ "$WANGP_AUTH" = 1 ]; then
        args+=(--auth)
    else
        warn "Authentication is disabled: anyone who knows the pod URL can use this UI."
    fi
    local extra=()
    # shellcheck disable=SC2206  # intentional word splitting of user-supplied flags
    [ -n "$WANGP_ARGS" ] && extra=($WANGP_ARGS)

    mkdir -p "$HF_HOME"
    cd "$WANGP_DIR"
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"

    # WanGP rejects logins ("Cross-origin request rejected") unless it knows the
    # public address it is served at, because the RunPod proxy rewrites the Host header.
    local public_url="${WANGP_PUBLIC_URL:-}"
    if [ -z "$public_url" ] && [ -n "${RUNPOD_POD_ID:-}" ]; then
        public_url="https://${RUNPOD_POD_ID}-${WANGP_PORT}.proxy.runpod.net"
    fi
    if [ -n "$public_url" ] && [ "$public_url" != none ]; then
        args+=(--public-url "$public_url")
        log "WanGP will be reachable at $public_url (port $WANGP_PORT must be an exposed HTTP port)"
    fi
    if [ "$WANGP_AUTH" = 1 ] && [ -z "${WANGP_AUTH_PASSWORD:-}" ]; then
        log "No WANGP_AUTH_PASSWORD set: look for the generated 'WanGP web password' line below."
    fi

    # WanGP exits with code 42 when it wants to be restarted (e.g. after an in-app update).
    local code
    while true; do
        log "Launching WanGP: python wgp.py ${args[*]} ${extra[*]:-}"
        code=0
        python wgp.py "${args[@]}" ${extra[@]+"${extra[@]}"} || code=$?
        [ "$code" -eq 42 ] || exit "$code"
        log "Restarting WanGP"
    done
}

case "${1:-all}" in
    all)     do_install; do_start ;;
    install) do_install; log "Done. Launch with: bash setup.sh start" ;;
    start)   do_start ;;
    update)  do_update; log "Done. Launch with: bash setup.sh start" ;;
    -h|--help|help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//' ;;
    *) die "Unknown command '$1' (use: install, start, update)" ;;
esac
