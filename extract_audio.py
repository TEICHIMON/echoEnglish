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
    python extract_audio.py /path/to/videos --split-threshold 900 --chunk-duration 600
"""

import argparse
import math
import subprocess
import sys
from pathlib import Path

# 常见视频和音频扩展名
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv",
    ".webm", ".ts", ".m2ts", ".mpg", ".mpeg", ".3gp",
}
AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma", ".m4r",
}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS


def find_media(folder: Path) -> list[Path]:
    """扫描文件夹，返回所有音视频文件（不递归子目录）。"""
    media = [
        f for f in sorted(folder.iterdir())
        if f.is_file() and f.suffix.lower() in MEDIA_EXTENSIONS
    ]
    return media


def get_video_duration(video: Path) -> float:
    """用 ffprobe 获取视频/音频时长（秒）。失败时返回 0.0。"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1",
        str(video),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
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

    result = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if result.returncode != 0:
        _print_ffmpeg_error(output.name, result.stderr)
        return False
    return True


def extract_audio(
    video: Path,
    bitrate: str = "192k",
    sample_rate: int = 44100,
    overwrite: bool = False,
    split_threshold: float = 900.0,
    chunk_duration: float = 600.0,
) -> tuple[str, list[Path]]:
    """
    用 ffmpeg 从单个视频文件中提取音频，输出为 .m4a。
    若视频时长超过 split_threshold 秒（默认 15 分钟），则按 chunk_duration（默认 10 分钟）切分为多个片段，
    文件名添加数字后缀（如 video_01.m4a, video_02.m4a）。
    传 split_threshold <= 0 可关闭切分。

    ffmpeg 命令（单文件）:
        ffmpeg -y -i input.mp4 -vn -acodec aac -b:a 192k -ar 44100 -f ipod output.m4a
    ffmpeg 命令（切分片段）:
        ffmpeg -y -ss <start> -i input.mp4 -t <chunk> -vn -acodec aac ... output_NN.m4a

    Returns:
        (status, outputs) 其中 status 为 "ok" | "skipped" | "failed"。
    """
    duration = get_video_duration(video) if split_threshold > 0 else 0.0

    # —— 单文件路径（不切分）——
    if split_threshold <= 0 or duration <= 0 or duration <= split_threshold:
        output = video.with_suffix(".m4a")
        
        # 避免自己覆盖自己：如果输入已经是 m4a 并且不需要切分，直接跳过
        if video.resolve() == output.resolve():
            print(f"  ⏭  跳过（文件已是 m4a 格式且无需切分）: {output.name}")
            return "skipped", []

        if output.exists() and not overwrite:
            print(f"  ⏭  跳过（已存在）: {output.name}")
            return "skipped", []

        if not _run_ffmpeg_extract(video, output, bitrate, sample_rate):
            return "failed", []

        size_mb = output.stat().st_size / (1024 * 1024)
        print(f"  ✓  {video.name} → {output.name} ({size_mb:.1f} MB)")
        return "ok", [output]

    # —— 切分路径 ——
    full_chunks = int(duration // chunk_duration)
    remainder = duration % chunk_duration
    
    # 如果最后一段剩下的时间太短（少于单段时长的 30%），则合并到前一段
    if remainder > 0 and remainder < (chunk_duration * 0.3) and full_chunks > 0:
        n_chunks = full_chunks
    else:
        n_chunks = math.ceil(duration / chunk_duration)

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
        start = idx * chunk_duration
        # 最后一段不限制时长，让 ffmpeg 跑到 EOF
        chunk_dur: float | None = chunk_duration if idx < n_chunks - 1 else None

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
        description="从文件夹中所有音视频文件提取/转换为 m4a 格式，并支持长文件切分",
    )
    parser.add_argument(
        "folder",
        help="包含音视频文件的文件夹路径",
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
        "--split-threshold", "-t",
        type=float,
        default=900.0,
        help="触发切分的时长阈值（秒）；传 0 关闭切分 (默认: 900 = 15 分钟)",
    )
    parser.add_argument(
        "--chunk-duration", "-c",
        type=float,
        default=600.0,
        help="切分时的单段音频时长（秒） (默认: 600 = 10 分钟)",
    )
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"❌ 不是有效的文件夹: {folder}", file=sys.stderr)
        sys.exit(1)

    media_files = find_media(folder)
    if not media_files:
        print("ℹ️  未找到音视频文件")
        sys.exit(0)

    print(f"📂 文件夹: {folder}")
    print(f"🎬 找到 {len(media_files)} 个音视频文件")
    print(f"⚙️  比特率: {args.bitrate} | 采样率: {args.sample_rate} Hz")
    if args.split_threshold > 0:
        print(f"✂  切分阈值: {args.split_threshold:.0f} 秒 ({args.split_threshold / 60:.1f} 分钟)")
        print(f"🔪 切分段长: {args.chunk_duration:.0f} 秒 ({args.chunk_duration / 60:.1f} 分钟)")
    else:
        print("✂  切分: 已关闭")
    print()

    succeeded = 0
    skipped = 0
    failed = 0

    for media_file in media_files:
        status, _ = extract_audio(
            media_file,
            bitrate=args.bitrate,
            sample_rate=args.sample_rate,
            overwrite=args.overwrite,
            split_threshold=args.split_threshold,
            chunk_duration=args.chunk_duration,
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