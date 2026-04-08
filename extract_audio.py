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
    python extract_audio.py /path/to/videos --max-duration 600
"""

import argparse
import math
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


def get_video_duration(video: Path) -> float:
    """用 ffprobe 获取视频/音频时长（秒）。失败时返回 0.0。"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1",
        str(video),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return 0.0
        return float(result.stdout.strip())
    except (ValueError, FileNotFoundError):
        return 0.0


def _print_ffmpeg_error(label: str, stderr: str) -> None:
    print(f"  ✗  失败: {label}")
    err_lines = stderr.strip().splitlines()
    for line in err_lines[-3:]:
        print(f"      {line}")


def _run_ffmpeg_extract(
    video: Path,
    output: Path,
    bitrate: str,
    sample_rate: int,
    start: float | None = None,
    duration: float | None = None,
) -> bool:
    """运行单条 ffmpeg 提取命令。返回是否成功。"""
    cmd = ["ffmpeg", "-y"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(video)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += [
        "-vn",
        "-acodec", "aac",
        "-b:a", bitrate,
        "-ar", str(sample_rate),
        "-f", "ipod",
        str(output),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        _print_ffmpeg_error(output.name, result.stderr)
        return False
    return True


def extract_audio(
    video: Path,
    bitrate: str = "192k",
    sample_rate: int = 44100,
    overwrite: bool = False,
    max_duration: float = 1200.0,
) -> tuple[str, list[Path]]:
    """
    用 ffmpeg 从单个视频文件中提取音频，输出为 .m4a。
    若视频时长超过 max_duration 秒（默认 20 分钟），则按该上限切分为多个片段，
    文件名添加数字后缀（如 video_01.m4a, video_02.m4a）。
    传 max_duration <= 0 可关闭切分。

    ffmpeg 命令（单文件）:
        ffmpeg -y -i input.mp4 -vn -acodec aac -b:a 192k -ar 44100 -f ipod output.m4a
    ffmpeg 命令（切分片段）:
        ffmpeg -y -ss <start> -i input.mp4 -t <chunk> -vn -acodec aac ... output_NN.m4a

    Returns:
        (status, outputs) 其中 status 为 "ok" | "skipped" | "failed"。
    """
    duration = get_video_duration(video) if max_duration > 0 else 0.0

    # —— 单文件路径（不切分）——
    if max_duration <= 0 or duration <= 0 or duration <= max_duration:
        output = video.with_suffix(".m4a")
        if output.exists() and not overwrite:
            print(f"  ⏭  跳过（已存在）: {output.name}")
            return "skipped", []

        if not _run_ffmpeg_extract(video, output, bitrate, sample_rate):
            return "failed", []

        size_mb = output.stat().st_size / (1024 * 1024)
        print(f"  ✓  {video.name} → {output.name} ({size_mb:.1f} MB)")
        return "ok", [output]

    # —— 切分路径 ——
    n_chunks = math.ceil(duration / max_duration)
    width = max(2, len(str(n_chunks)))
    chunk_paths = [
        video.parent / f"{video.stem}_{i:0{width}d}.m4a"
        for i in range(1, n_chunks + 1)
    ]

    if all(p.exists() for p in chunk_paths) and not overwrite:
        print(f"  ⏭  跳过（已存在 {n_chunks} 个片段）: {video.name}")
        return "skipped", []

    print(f"  ✂  {video.name} → {n_chunks} 段 ({duration / 60:.1f} min)")

    succeeded: list[Path] = []
    for idx, chunk_path in enumerate(chunk_paths):
        start = idx * max_duration
        # 最后一段不限制时长，让 ffmpeg 跑到 EOF
        chunk_dur: float | None = max_duration if idx < n_chunks - 1 else None

        ok = _run_ffmpeg_extract(
            video, chunk_path, bitrate, sample_rate,
            start=start, duration=chunk_dur,
        )
        if not ok:
            return "failed", succeeded

        size_mb = chunk_path.stat().st_size / (1024 * 1024)
        print(f"    ✓  {chunk_path.name} ({size_mb:.1f} MB)")
        succeeded.append(chunk_path)

    return "ok", succeeded


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
    parser.add_argument(
        "--max-duration", "-d",
        type=float,
        default=1200.0,
        help="单个音频片段最大时长（秒），超过则切分并加数字后缀；传 0 关闭切分 (默认: 1200 = 20 分钟)",
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
    if args.max_duration > 0:
        print(f"✂  切分阈值: {args.max_duration:.0f} 秒 ({args.max_duration / 60:.1f} 分钟)")
    else:
        print("✂  切分: 已关闭")
    print()

    succeeded = 0
    skipped = 0
    failed = 0

    for video in videos:
        status, _ = extract_audio(
            video,
            bitrate=args.bitrate,
            sample_rate=args.sample_rate,
            overwrite=args.overwrite,
            max_duration=args.max_duration,
        )
        if status == "ok":
            succeeded += 1
        elif status == "skipped":
            skipped += 1
        else:
            failed += 1

    print()
    print(f"✅ 完成: {succeeded} 成功, {skipped} 跳过, {failed} 失败")


if __name__ == "__main__":
    main()