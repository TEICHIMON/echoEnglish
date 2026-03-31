#!/bin/bash
# =============================================================================
# extract_audio: 从视频文件夹中批量提取音频为 m4a
#
# 调用 echoEnglish 项目下的 extract_audio.py
#
# Usage:
#   extract_audio /path/to/videos
#   extract_audio /path/to/videos --bitrate 256k
#   extract_audio /path/to/videos --overwrite
# =============================================================================
set -euo pipefail

PYTHON="/opt/homebrew/anaconda3/envs/echo_env/bin/python"
SCRIPT="/Volumes/SP/code/python/echoEnglish/extract_audio.py"

if [[ $# -eq 0 ]]; then
    echo "用法: extract_audio <视频文件夹> [选项]"
    echo ""
    echo "选项:"
    echo "  --bitrate, -b      音频比特率 (默认: 192k)"
    echo "  --sample-rate, -r  采样率 Hz (默认: 44100)"
    echo "  --overwrite        覆盖已存在的 .m4a 文件"
    exit 1
fi

"$PYTHON" "$SCRIPT" "$@"