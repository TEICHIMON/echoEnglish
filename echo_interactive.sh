#!/usr/bin/env bash
set -euo pipefail

ENV_NAME="echo_env"
PROJECT_DIR="/Users/sudami/PycharmProjects/echoEnglish"

load_conda() {
    if command -v conda >/dev/null 2>&1; then
        local conda_base
        conda_base="$(conda info --base 2>/dev/null)"
        # shellcheck disable=SC1090
        source "${conda_base}/etc/profile.d/conda.sh"
        return
    fi

    local candidates=(
        "${HOME}/anaconda3/etc/profile.d/conda.sh"
        "${HOME}/miniconda3/etc/profile.d/conda.sh"
        "/opt/homebrew/anaconda3/etc/profile.d/conda.sh"
        "/opt/homebrew/miniconda3/etc/profile.d/conda.sh"
    )

    for conda_sh in "${candidates[@]}"; do
        if [[ -f "${conda_sh}" ]]; then
            # shellcheck disable=SC1090
            source "${conda_sh}"
            return
        fi
    done

    echo "Could not find conda. Please install Anaconda/Miniconda or initialize conda in your shell." >&2
    exit 1
}

load_conda
conda activate "${ENV_NAME}"
cd "${PROJECT_DIR}"

if [[ $# -eq 0 ]]; then
    python main.py -i
else
    python main.py "$@"
fi
