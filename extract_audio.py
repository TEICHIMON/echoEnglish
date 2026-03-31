#!/usr/bin/env python3
"""
视频音频提取工具

扫描指定文件夹下的所有视频文件，用 ffmpeg 提取音频并转为 m4a 格式，
输出到同一文件夹中。如果同名 .m4a 已存在则跳过。

用法:
    python extract_audio.py /path/to/videos
    python extract_audio.py /path/to/videos --bitrate 256k
    python extract_audio.py /path/to/videos --sample-rate 48000
    python extract_audio.py /path/to/videos --overwrite
"""

import argparse
import subprocess
import sys
from pathlib import Path

# 常见视频扩展名
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv",
    ".webm", ".ts", ".m2ts", ".mpg", ".mpeg", ".3gp",
}


def find_videos(folder: Path) -> list[Path]:
    """扫描文件夹，返回所有视频文件（不递归子目录）。"""
    videos = [
        f for f in sorted(folder.iterdir())
        if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS
    ]
    return videos


def extract_audio(
    video: Path,
    bitrate: str = "192k",
    sample_rate: int = 44100,
    overwrite: bool = False,
) -> Path | None:
    """
    用 ffmpeg 从单个视频文件中提取音频，输出为同名 .m4a。

    ffmpeg 命令:
        ffmpeg -y -i input.mp4 -vn -acodec aac -b:a 192k -ar 44100 -f ipod output.m4a

    参数说明:
        -y           覆盖输出文件（不提示）
        -i           输入文件
        -vn          丢弃视频流，只保留音频
        -acodec aac  音频编码器选 AAC（m4a 容器的标准编码）
        -b:a 192k    音频比特率
        -ar 44100    采样率
        -f ipod      输出格式用 ipod muxer（即 m4a/MPEG-4 Audio）

    Returns:
        输出文件的 Path，如果跳过则返回 None。
    """
    output = video.with_suffix(".m4a")

    if output.exists() and not overwrite:
        print(f"  ⏭  跳过（已存在）: {output.name}")
        return None

    cmd = [
        "ffmpeg", "-y",
        "-i", str(video),
        "-vn",
        "-acodec", "aac",
        "-b:a", bitrate,
        "-ar", str(sample_rate),
        "-f", "ipod",
        str(output),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"  ✗  失败: {video.name}")
        # 只打印 stderr 的最后几行，避免刷屏
        err_lines = result.stderr.strip().splitlines()
        for line in err_lines[-3:]:
            print(f"      {line}")
        return None

    size_mb = output.stat().st_size / (1024 * 1024)
    print(f"  ✓  {video.name} → {output.name} ({size_mb:.1f} MB)")
    return output


def main():
    parser = argparse.ArgumentParser(
        description="从文件夹中所有视频提取音频为 m4a 格式",
    )
    parser.add_argument(
        "folder",
        help="包含视频文件的文件夹路径",
    )
    parser.add_argument(
        "--bitrate", "-b",
        default="192k",
        help="音频比特率 (默认: 192k)",
    )
    parser.add_argument(
        "--sample-rate", "-r",
        type=int,
        default=44100,
        help="采样率 Hz (默认: 44100)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="覆盖已存在的 .m4a 文件 (默认跳过)",
    )
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"❌ 不是有效的文件夹: {folder}", file=sys.stderr)
        sys.exit(1)

    videos = find_videos(folder)
    if not videos:
        print("ℹ️  未找到视频文件")
        sys.exit(0)

    print(f"📂 文件夹: {folder}")
    print(f"🎬 找到 {len(videos)} 个视频文件")
    print(f"⚙️  比特率: {args.bitrate} | 采样率: {args.sample_rate} Hz")
    print()

    succeeded = 0
    skipped = 0
    failed = 0

    for video in videos:
        result = extract_audio(
            video,
            bitrate=args.bitrate,
            sample_rate=args.sample_rate,
            overwrite=args.overwrite,
        )
        if result is not None:
            succeeded += 1
        elif not video.with_suffix(".m4a").exists() or args.overwrite:
            failed += 1
        else:
            skipped += 1

    print()
    print(f"✅ 完成: {succeeded} 成功, {skipped} 跳过, {failed} 失败")


if __name__ == "__main__":
    main()