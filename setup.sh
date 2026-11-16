#!/usr/bin/env bash
# WanGP (https://github.com/deepbeepmeep/WanGP) installer + launcher for RunPod pods.
#
#   bash setup.sh            install (first run only) and launch the web UI
#   bash setup.sh install    install / repair only
#   bash setup.sh start      launch only
#   bash setup.sh update     pull the latest WanGP and refresh its dependencies
#   bash setup.sh run ARGS   run "python wgp.py ARGS" in the WanGP environment (no web UI flags added)
#   bash setup.sh drive-setup  connect Google Drive (once), so that the plugins can upload results there
#   bash setup.sh get-lora LINK [--name FILE] [--folder ltx2]
#                          download a LoRA from Civitai (or any direct link) into WanGP's loras folder
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
# WANGP_MAX_RAM         RAM budget for WanGP in GB; defaults to the pod's memory limit minus a
#                       margin. 0 lets WanGP plan with all the RAM of the host machine.
# WANGP_AUTH_PASSWORD   the UI password; if unset WanGP generates one and prints it at startup
# WANGP_PUBLIC_URL      the URL the UI is opened at; defaults to the RunPod proxy URL of this pod.
#                       Set it to "none" when connecting some other way (e.g. a direct TCP port).
# CIVITAI_TOKEN         a Civitai API key, for "get-lora" (most Civitai downloads need one)

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
# rclone (Google Drive uploads) and its configuration live on the volume, see "drive-setup".
RCLONE_DIR="$WORKSPACE/.rclone"
export RCLONE_CONFIG="${RCLONE_CONFIG:-$WORKSPACE/rclone.conf}"
export WORKSPACE
export PATH="$UV_HOME/bin:$RCLONE_DIR:$PATH"

# RunPod images put their own CUDA toolkit on LD_LIBRARY_PATH. Its cuBLAS would be
# mixed with the (different) one bundled with PyTorch and every matrix product then
# fails with CUBLAS_STATUS_* errors, so those entries are dropped. The driver
# libraries (/usr/local/nvidia) stay.
if [ -n "${LD_LIBRARY_PATH:-}" ]; then
    LD_LIBRARY_PATH="$(printf '%s' "$LD_LIBRARY_PATH" | tr ':' '\n' | grep -v -i 'cuda' | paste -sd: - || true)"
    export LD_LIBRARY_PATH
fi

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
    a = torch.randn(64, 64, device="cuda")
    (a @ a).sum().item()
    print("GPU matrix product: OK")
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
    install_plugins
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

# ----------------------------- Memory limit ---------------------------------
# A pod may only use part of its host's RAM, but WanGP sees all of it, plans its
# model caching accordingly and gets killed by the kernel ("Killed") when it goes
# over the pod's limit. Prints the RAM budget (GB) to give WanGP, or nothing.
ram_budget_gb() {
    if [ -n "${WANGP_MAX_RAM:-}" ]; then
        [ "$WANGP_MAX_RAM" = 0 ] || printf '%s' "$WANGP_MAX_RAM"
        return 0
    fi
    local limit="" file host_kb limit_gb
    for file in /sys/fs/cgroup/memory.max /sys/fs/cgroup/memory/memory.limit_in_bytes; do
        [ -r "$file" ] && { limit="$(cat "$file")"; break; }
    done
    [[ "$limit" =~ ^[0-9]+$ ]] || return 0
    host_kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
    [ "$limit" -lt $(( ${host_kb:-0} * 1024 )) ] || return 0   # no limit below the host's RAM
    limit_gb=$(( limit / 1024 / 1024 / 1024 ))
    # Keep 10% (at least 4 GB) for everything WanGP does not count.
    local margin=$(( limit_gb / 10 ))
    [ "$margin" -ge 4 ] || margin=4
    [ $(( limit_gb - margin )) -ge 8 ] && printf '%s' $(( limit_gb - margin ))
    return 0
}

# ----------------------------- Google Drive --------------------------------
install_rclone() {
    [ -x "$RCLONE_DIR/rclone" ] && return 0
    log "Installing rclone into $RCLONE_DIR"
    mkdir -p "$RCLONE_DIR"
    local zip="$RCLONE_DIR/rclone.zip" tag
    if ! curl -fsSL -o "$zip" https://downloads.rclone.org/rclone-current-linux-amd64.zip; then
        tag="$(curl -fsSLI -o /dev/null -w '%{url_effective}' https://github.com/rclone/rclone/releases/latest | sed 's#.*/tag/##')"
        curl -fsSL -o "$zip" "https://github.com/rclone/rclone/releases/download/$tag/rclone-$tag-linux-amd64.zip" \
            || die "Could not download rclone"
    fi
    python3 - "$zip" "$RCLONE_DIR/rclone" <<'PY' || die "Could not unpack rclone"
import sys, zipfile
archive = zipfile.ZipFile(sys.argv[1])
member = next(name for name in archive.namelist() if name.endswith("/rclone"))
with open(sys.argv[2], "wb") as writer:
    writer.write(archive.read(member))
PY
    chmod +x "$RCLONE_DIR/rclone"
    rm -f "$zip"
}

# Connects a Google Drive account to rclone using only this terminal and any web
# browser (a tablet is fine). rclone normally wants the browser on the same machine:
# it waits for Google to send the browser back to http://127.0.0.1:53682/. Here that
# final address is copied from the browser and replayed on the pod instead.
do_drive_setup() {
    local remote="${1:-gdrive}" out pid link google answer query i
    install_system_packages
    install_rclone
    out="$(mktemp)"
    log "Connecting Google Drive as the rclone remote '$remote' (configuration: $RCLONE_CONFIG)"
    "$RCLONE_DIR/rclone" config create "$remote" drive scope=drive > "$out" 2>&1 &
    pid=$!
    link=""
    for i in $(seq 1 30); do
        link="$(grep -o 'http://127\.0\.0\.1:53682/auth?state=[A-Za-z0-9_-]*' "$out" | head -n1 || true)"
        [ -n "$link" ] && break
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    google=""
    [ -n "$link" ] && google="$(curl -s -o /dev/null -w '%{redirect_url}' "$link" || true)"
    if [ -z "$google" ]; then
        kill "$pid" 2>/dev/null || true
        cat "$out" >&2; rm -f "$out"
        die "rclone did not offer a sign-in link. Set the remote up by hand instead: $RCLONE_DIR/rclone config"
    fi
    cat <<EOF

1. Open this address in your web browser and allow access to your Google Drive:

$google

2. The browser then lands on a page that CANNOT be displayed ("This site can't be
   reached", address starting with http://127.0.0.1:53682/). That is expected.
   Copy the complete address of that page from the address bar.

EOF
    read -r -p "3. Paste that address here and press Enter: " answer
    query="${answer#*\?}"
    if [ -z "$answer" ] || [ "$query" = "$answer" ] || [[ "$query" != *code=* ]]; then
        kill "$pid" 2>/dev/null || true; rm -f "$out"
        die "That is not the address of the sign-in result (it should contain '?state=...&code=...'). Run the command again."
    fi
    curl -fsS -o /dev/null "http://127.0.0.1:53682/?$query" || warn "rclone did not accept the address."
    for i in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill "$pid" 2>/dev/null || true
    rm -f "$out"
    if "$RCLONE_DIR/rclone" lsd "$remote:" --max-depth 1 >/dev/null 2>&1; then
        log "Google Drive is connected. Destination to use in the plugins: $remote:WanGP-outputs"
        log "Restart WanGP (bash setup.sh start) so that the plugins see it."
    else
        die "The connection to Google Drive could not be verified. Run the command again, or set it up by hand: $RCLONE_DIR/rclone config"
    fi
}

# ------------------------------- Plugins ------------------------------------
# Copy the WanGP plugins shipped with this repository into WanGP (refreshed at every
# launch) and enable each of them the first time it is installed.
install_plugins() {
    local source_dir plugin name config="$WANGP_DIR/wgp_config.json"
    source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/plugins"
    [ -d "$source_dir" ] && [ -d "$WANGP_DIR/plugins" ] || return 0
    for plugin in "$source_dir"/*/; do
        [ -f "$plugin/plugin.py" ] || continue
        name="$(basename "$plugin")"
        if [ ! -d "$WANGP_DIR/plugins/$name" ]; then
            log "Installing the WanGP plugin '$name'"
            # Without a config file, or without a plugin list in it, WanGP enables every plugin.
            if [ -f "$config" ] && command -v python3 >/dev/null 2>&1; then
                python3 - "$config" "$name" <<'PY' || warn "Enable '$name' yourself in WanGP's Apps & PlugIn Manager."
import json, sys
path, name = sys.argv[1:3]
with open(path, encoding="utf-8") as reader:
    config = json.load(reader)
enabled = config.get("enabled_plugins")
if isinstance(enabled, list) and name not in enabled:
    enabled.append(name)
    with open(path, "w", encoding="utf-8") as writer:
        json.dump(config, writer, indent=4)
PY
            fi
        fi
        mkdir -p "$WANGP_DIR/plugins/$name"
        cp -r "$plugin". "$WANGP_DIR/plugins/$name/"
        # Modules common to the plugins of this repository.
        [ -d "$source_dir/_shared" ] && cp "$source_dir"/_shared/*.py "$WANGP_DIR/plugins/$name/"
    done
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
    local max_ram
    max_ram="$(ram_budget_gb)"
    if [ -n "$max_ram" ] && [[ " $WANGP_ARGS " != *" --max-ram "* ]]; then
        args+=(--max-ram "$max_ram")
        log "This pod's memory limit is below the host's RAM: WanGP gets a RAM budget of ${max_ram} GB."
    fi

    install_plugins
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

# Run wgp.py with the given arguments only, e.g. headless queue processing.
do_run() {
    install_system_packages
    restore_python_env
    [ -x "$VENV_DIR/bin/python" ] && [ -f "$STAMP_FILE" ] || die "WanGP is not installed yet, run: bash setup.sh install"
    install_plugins
    mkdir -p "$HF_HOME"
    cd "$WANGP_DIR"
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    exec python wgp.py "$@"
}

# ------------------------------- LoRAs --------------------------------------
# Download a LoRA straight onto the pod, into WanGP's loras folder, so that it
# never has to go through a slow home connection.
#
#   bash setup.sh get-lora https://civitai.com/models/2557755/some-name?modelVersionId=2874411
#   bash setup.sh get-lora 2874411                        (a Civitai model version number)
#   bash setup.sh get-lora https://civitai.com/models/2557755      (its latest version)
#   bash setup.sh get-lora https://civitai.com/api/download/models/2874411
#   bash setup.sh get-lora https://huggingface.co/.../file.safetensors   (any direct link)
#
# Options: --name FILE saves it under that file name (default: Civitai's name, or the
# link's); --folder DIR is the subfolder of loras/ (default ltx2, shared by every LTX-2
# model) or an absolute path; --force replaces a file that is already there.
CIVITAI_BASE="${CIVITAI_BASE:-https://civitai.com}"

do_get_lora() {
    local source="" name="" folder="ltx2" force=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --name)   [ $# -ge 2 ] || die "--name needs a file name"; name="$2"; shift 2 ;;
            --folder) [ $# -ge 2 ] || die "--folder needs a folder"; folder="$2"; shift 2 ;;
            --force)  force=1; shift ;;
            -h|--help)
                sed -n '/^# ------------------------------- LoRAs/,/^CIVITAI_BASE=/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
                return 0 ;;
            -*) die "Unknown option '$1' (use --name, --folder or --force)" ;;
            *)  [ -z "$source" ] || die "Give one link at a time."; source="$1"; shift ;;
        esac
    done
    [ -n "$source" ] || die "Usage: bash setup.sh get-lora LINK [--name FILE] [--folder ltx2] [--force]"
    install_system_packages
    command -v python3 >/dev/null 2>&1 || die "python3 is needed to read Civitai's answer"

    local dest_dir
    case "$folder" in
        /*) dest_dir="$folder" ;;
        *)  dest_dir="$WANGP_DIR/loras/$folder" ;;
    esac
    mkdir -p "$dest_dir"

    # Work out the real download link and file name (Civitai links need its API).
    local resolved url file_name size_kb
    resolved="$(CIVITAI_BASE="$CIVITAI_BASE" python3 - "$source" <<'PY'
import json, os, re, sys, urllib.error, urllib.parse, urllib.request

source = sys.argv[1].strip()
base = os.environ["CIVITAI_BASE"].rstrip("/")
token = (os.environ.get("CIVITAI_TOKEN") or os.environ.get("CIVITAI_API_KEY") or "").strip()

def fail(message):
    print(message, file=sys.stderr)
    sys.exit(2)

def api(path):
    request = urllib.request.Request(base + path, headers={"User-Agent": "wangp-runpod"})
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=60) as reply:
            return json.load(reply)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            fail("Civitai refused the request (HTTP %d). Set CIVITAI_TOKEN to an API key "
                 "(civitai.com: Account settings -> API Keys)." % error.code)
        if error.code == 404:
            fail("Civitai has no such model or version: %s" % source)
        fail("Civitai answered HTTP %d for %s" % (error.code, path))
    except (urllib.error.URLError, OSError) as error:
        fail("Could not reach Civitai: %s" % error)

version = model = None
parsed = urllib.parse.urlparse(source)
if re.fullmatch(r"\d+", source):
    version = source
elif "civitai" in parsed.netloc:
    query = urllib.parse.parse_qs(parsed.query)
    if query.get("modelVersionId"):
        version = query["modelVersionId"][0]
    else:
        m = re.search(r"/api/download/models/(\d+)", parsed.path) or re.search(r"/model-versions/(\d+)", parsed.path)
        if m:
            version = m.group(1)
        else:
            m = re.search(r"/models/(\d+)", parsed.path)
            if not m:
                fail("That Civitai link names no model or version: %s" % source)
            model = m.group(1)
elif parsed.scheme in ("http", "https"):
    name = os.path.basename(urllib.parse.unquote(parsed.path)) or "lora.safetensors"
    print("%s\t%s\t0" % (source, name))
    sys.exit(0)
else:
    fail("Give a Civitai link, a Civitai version number, or a direct http(s) link.")

if model:
    data = api("/api/v1/models/%s" % model)
    versions = data.get("modelVersions") or []
    if not versions:
        fail("Civitai lists no versions for model %s" % model)
    version = str(versions[0]["id"])
    print("Latest version of '%s': %s (%s)" % (data.get("name", model), versions[0].get("name", ""), version),
          file=sys.stderr)

data = api("/api/v1/model-versions/%s" % version)
files = data.get("files") or []
pick = (next((f for f in files if f.get("primary")), None)
        or next((f for f in files if str(f.get("name", "")).endswith(".safetensors")), None)
        or (files[0] if files else None))
if not pick:
    fail("Civitai lists no file for version %s" % version)
base_model = data.get("baseModel") or ""
if base_model:
    print("Civitai says it was trained for: %s" % base_model, file=sys.stderr)
words = data.get("trainedWords") or []
if words:
    print("Trigger words: %s" % ", ".join(words), file=sys.stderr)
url = pick.get("downloadUrl") or "%s/api/download/models/%s" % (base, version)
print("%s\t%s\t%d" % (url, pick.get("name") or ("lora-%s.safetensors" % version), int(pick.get("sizeKB") or 0)))
PY
)" || die "Could not work out what to download (see the message above)."
    IFS=$'\t' read -r url file_name size_kb <<< "$resolved"
    [ -n "$name" ] && file_name="$name"
    file_name="$(basename "$file_name")"
    [ -n "$file_name" ] || die "No file name to save as; give one with --name"

    local target="$dest_dir/$file_name"
    if [ -f "$target" ] && [ "$force" != 1 ]; then
        log "Already there: $target ($(du -h "$target" | cut -f1)). Use --force to download it again."
        return 0
    fi

    local auth=() tmp="$target.part" code
    case "$url" in
        *civitai.com*|"$CIVITAI_BASE"*) [ -n "${CIVITAI_TOKEN:-${CIVITAI_API_KEY:-}}" ] && \
            auth=(-H "Authorization: Bearer ${CIVITAI_TOKEN:-${CIVITAI_API_KEY:-}}") ;;
    esac
    local size_note=""
    if [ "${size_kb:-0}" -ge 1024 ] 2>/dev/null; then size_note=" ($(( size_kb / 1024 )) MB)"
    elif [ "${size_kb:-0}" -gt 0 ] 2>/dev/null; then size_note=" (${size_kb} KB)"; fi
    log "Downloading $file_name$size_note into $dest_dir"
    code="$(curl -L --progress-bar -A wangp-runpod ${auth[@]+"${auth[@]}"} -o "$tmp" -w '%{http_code}' "$url")" || code="${code:-000}"
    if [ "$code" != 200 ]; then
        rm -f "$tmp"
        case "$code" in
            401|403) die "The download was refused (HTTP $code). Set CIVITAI_TOKEN to a Civitai API key: CIVITAI_TOKEN=your-key bash setup.sh get-lora ..." ;;
            *)       die "The download failed (HTTP $code)." ;;
        esac
    fi

    # A refused or expired link can still answer 200 with a web page; make sure this
    # is a model file before it goes where WanGP will try to load it.
    if ! python3 - "$tmp" "$file_name" <<'PY'
import json, os, struct, sys
path, name = sys.argv[1:3]
size = os.path.getsize(path)
with open(path, "rb") as reader:
    head = reader.read(8)
    if name.endswith(".safetensors"):
        if len(head) < 8:
            sys.exit("The file is empty.")
        length = struct.unpack("<Q", head)[0]
        if not 2 <= length < size or length > 100_000_000:
            sys.exit("This is not a safetensors file (%d bytes); the link may have returned a web page." % size)
        try:
            json.loads(reader.read(length))
        except ValueError:
            sys.exit("This is not a safetensors file; its header is unreadable.")
    elif head[:1] in (b"<", b"{") or size < 100_000:
        sys.exit("This looks like a web page or an error, not a model file (%d bytes)." % size)
PY
    then
        rm -f "$tmp"
        die "Nothing was saved. Check the link, and set CIVITAI_TOKEN if the model needs a login."
    fi
    mv -f "$tmp" "$target"
    log "Saved $target ($(du -h "$target" | cut -f1))"
    log "In WanGP, or in the prompt app's LoRA box, use the file name: $file_name"
}

case "${1:-all}" in
    all)     do_install; do_start ;;
    install) do_install; log "Done. Launch with: bash setup.sh start" ;;
    start)   do_start ;;
    update)  do_update; log "Done. Launch with: bash setup.sh start" ;;
    run)     shift; do_run "$@" ;;
    drive-setup) shift; do_drive_setup "$@" ;;
    get-lora) shift; do_get_lora "$@" ;;
    -h|--help|help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//' ;;
    *) die "Unknown command '$1' (use: install, start, update, run, drive-setup, get-lora)" ;;
esac
