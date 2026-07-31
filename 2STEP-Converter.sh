#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OS="$(uname -s)"
if [ "$OS" = "Darwin" ]; then
    LOCAL_ROOT="$HOME/Library/Application Support/2STEP-Converter"
elif [ "$OS" = "Linux" ]; then
    LOCAL_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/2STEP-Converter"
else
    echo "[ERROR] Unsupported operating system: $OS"
    exit 1
fi

if [ -d "$SCRIPT_DIR/lib" ]; then
    MM_ROOT="$SCRIPT_DIR/lib"
elif [ -d "$LOCAL_ROOT" ]; then
    MM_ROOT="$LOCAL_ROOT"
else
    echo "No existing environment found. Where should the environment be installed?"
    echo ""
    echo "  [1] Next to this script  (portable)"
    echo "  [2] $LOCAL_ROOT"
    echo ""
    read -rp "Your choice (1/2): " _choice
    if [ "$_choice" = "2" ]; then
        MM_ROOT="$LOCAL_ROOT"
    else
        MM_ROOT="$SCRIPT_DIR/lib"
    fi
    echo ""
fi

MM="$MM_ROOT/micromamba"
ENV="$MM_ROOT/env"
PY="$ENV/bin/python"
SPEC="$SCRIPT_DIR/src/environment.yml"
export MAMBA_ROOT_PREFIX="$MM_ROOT"
export CONDA_PKGS_DIRS="$MM_ROOT"
export PYTHONNOUSERSITE=1
export PATH="$ENV/bin:$PATH"

sha256_file() {
    local path="$1"
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$path" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$path" | awk '{print $1}'
    elif command -v openssl >/dev/null 2>&1; then
        openssl dgst -sha256 "$path" | awk '{print $NF}'
    else
        return 1
    fi
}

if [ ! -f "$MM" ]; then
    ARCH="$(uname -m)"
    if [ "$OS" = "Darwin" ]; then
        if [ "$ARCH" = "arm64" ]; then
            MM_URL="https://github.com/mamba-org/micromamba-releases/releases/download/2.8.1-1/micromamba-osx-arm64"
            MM_SHA256="9618a2866a2ffb3d36b55e9520f64d63dcd6dc2e622a351ca3cbe8e2cc90c757"
        elif [ "$ARCH" = "x86_64" ]; then
            MM_URL="https://github.com/mamba-org/micromamba-releases/releases/download/2.8.1-1/micromamba-osx-64"
            MM_SHA256="d6fce18e56d7c6bf2331b0ee1b372a581c70f09b509cc9e924cdd131e053b58a"
        else
            echo "[ERROR] Unsupported macOS architecture: $ARCH"
            exit 1
        fi
    else
        if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
            MM_URL="https://github.com/mamba-org/micromamba-releases/releases/download/2.8.1-1/micromamba-linux-aarch64"
            MM_SHA256="49aa29008cafa6cd6027ebd643fc43ca32c20740e60b0a05378c4e5bb837c217"
        elif [ "$ARCH" = "x86_64" ] || [ "$ARCH" = "amd64" ]; then
            MM_URL="https://github.com/mamba-org/micromamba-releases/releases/download/2.8.1-1/micromamba-linux-64"
            MM_SHA256="77b7790ec97f64581118f103585b175df4306f95829b0fa6bfe4a19cc88a1182"
        else
            echo "[ERROR] Unsupported Linux architecture: $ARCH"
            exit 1
        fi
    fi

    mkdir -p "$MM_ROOT"
    echo "Downloading portable Python manager (one-time, ~10 MB) ..."
    curl --fail --location --proto '=https' --tlsv1.2 --progress-bar -o "$MM" "$MM_URL" || {
        echo "[ERROR] Download failed. Check your internet connection."
        rm -f "$MM"
        exit 1
    }
    if ! MM_ACTUAL="$(sha256_file "$MM")"; then
        echo "[ERROR] No SHA-256 utility is available."
        rm -f "$MM"
        exit 1
    fi
    if [ "$MM_ACTUAL" != "$MM_SHA256" ]; then
        echo "[ERROR] micromamba checksum verification failed."
        rm -f "$MM"
        exit 1
    fi
    chmod +x "$MM"
fi

if [ ! -f "$SPEC" ]; then
    echo "[ERROR] Missing environment specification: $SPEC"
    exit 1
fi

if ! SPEC_HASH="$(sha256_file "$SPEC")"; then
    echo "[ERROR] No SHA-256 utility is available."
    exit 1
fi
SPEC_MARKER="$ENV/.2step-environment.sha256"

if [ ! -f "$PY" ]; then
    echo "Setting up Python environment (one-time download, ~500 MB) ..."
    "$MM" create --prefix "$ENV" --file "$SPEC" --yes || {
        echo "[ERROR] Failed to create Python environment."
        exit 1
    }
elif [ ! -f "$SPEC_MARKER" ] || [ "$(cat "$SPEC_MARKER")" != "$SPEC_HASH" ]; then
    echo "Environment specification changed -- updating dependencies ..."
    "$MM" install --prefix "$ENV" --file "$SPEC" --yes || {
        echo "[ERROR] Failed to update the Python environment."
        exit 1
    }
fi

if ! "$PY" -c "from OCC.Core.StlAPI import StlAPI_Reader; import numpy, trimesh, networkx, fast_simplification, matplotlib, open3d, PIL" >/dev/null 2>&1; then
    echo "Environment is incomplete or broken -- repairing ..."
    "$MM" install --prefix "$ENV" --file "$SPEC" --force-reinstall --yes || {
        echo "[ERROR] Failed to repair the Python environment."
        exit 1
    }
    if ! "$PY" -c "from OCC.Core.StlAPI import StlAPI_Reader; import numpy, trimesh, networkx, fast_simplification, matplotlib, open3d, PIL" >/dev/null 2>&1; then
        echo "[ERROR] Python environment is still broken after repair."
        exit 1
    fi
fi

printf '%s\n' "$SPEC_HASH" > "$SPEC_MARKER.tmp"
mv -f "$SPEC_MARKER.tmp" "$SPEC_MARKER"

"$PY" "$SCRIPT_DIR/src/converter.py" "$@"
