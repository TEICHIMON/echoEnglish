"""
Echo Loop assembler module.

Builds Echo Loop patterns from individual audio components.
Supports three variants:
  full:        T-S-N-S-T-S
  progressive: T-S-N-S-T-S + T-S-silence-S-T-S
  shadow:      T-S-silence-S-T-S

Each variant can also be refined with explicit T-N-T and T-S-T repeat counts.

Based on "Echo: Rebuilding the Natural Reflex of Language" by H. Reeve.
"""

from dataclasses import dataclass

from pydub import AudioSegment


@dataclass
class EchoTiming:
    """Configurable silence durations for the Echo Loop."""
    after_first_target: float = 0.8   # seconds
    after_native: float = 0.5         # seconds
    after_second_target: float = 1.2  # seconds

    def silence_after_first_target(self) -> AudioSegment:
        return AudioSegment.silent(duration=int(self.after_first_target * 1000))

    def silence_after_native(self) -> AudioSegment:
        return AudioSegment.silent(duration=int(self.after_native * 1000))

    def silence_after_second_target(self) -> AudioSegment:
        return AudioSegment.silent(duration=int(self.after_second_target * 1000))


@dataclass(frozen=True)
class EchoLoopPattern:
    """Number of T-N-T and T-S-T passes to emit for each segment."""
    tnt_repeats: int
    tst_repeats: int


_VARIANT_PATTERNS = {
    "full": EchoLoopPattern(tnt_repeats=1, tst_repeats=0),
    "progressive": EchoLoopPattern(tnt_repeats=1, tst_repeats=1),
    "shadow": EchoLoopPattern(tnt_repeats=0, tst_repeats=1),
}


def _coerce_repeat_count(name: str, value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{name} must be a non-negative integer")
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if count < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return count


def resolve_loop_pattern(
    variant: str = "full",
    tnt_repeats=None,
    tst_repeats=None,
) -> EchoLoopPattern:
    """
    Resolve a variant plus optional repeat overrides into concrete pass counts.

    Explicit repeat counts override the matching value from the variant preset.
    """
    variant = (variant or "full").strip().lower()
    if variant not in _VARIANT_PATTERNS:
        allowed = ", ".join(sorted(_VARIANT_PATTERNS))
        raise ValueError(f"loop variant must be one of: {allowed}")

    preset = _VARIANT_PATTERNS[variant]
    tnt = _coerce_repeat_count("tnt_repeats", tnt_repeats)
    tst = _coerce_repeat_count("tst_repeats", tst_repeats)

    pattern = EchoLoopPattern(
        tnt_repeats=preset.tnt_repeats if tnt is None else tnt,
        tst_repeats=preset.tst_repeats if tst is None else tst,
    )
    if pattern.tnt_repeats + pattern.tst_repeats == 0:
        raise ValueError("tnt_repeats and tst_repeats cannot both be 0")
    return pattern


def build_echo_loop(
    target_audio: AudioSegment,
    native_audio: AudioSegment,
    timing: EchoTiming,
    variant: str = "full",
    tnt_repeats=None,
    tst_repeats=None,
) -> AudioSegment:
    """
    Build Echo Loop unit(s) for a single segment.

    Args:
        target_audio: The target language audio segment
        native_audio: The native language TTS audio
        timing: EchoTiming configuration
        variant: "full", "progressive", or "shadow"
        tnt_repeats: Optional number of T-N-T passes
        tst_repeats: Optional number of T-S-T passes

    Returns:
        AudioSegment containing the loop(s)
    """
    s1 = timing.silence_after_first_target()
    s2 = timing.silence_after_native()
    s3 = timing.silence_after_second_target()
    native_silence = AudioSegment.silent(duration=len(native_audio))
    pattern = resolve_loop_pattern(variant, tnt_repeats, tst_repeats)

    # full pass: T-S-N-S-T-S; shadow pass: T-S-silence(len(N))-S-T-S
    tnt_pass = target_audio + s1 + native_audio + s2 + target_audio + s3
    tst_pass = target_audio + s1 + native_silence + s2 + target_audio + s3

    passes = (
        [tnt_pass] * pattern.tnt_repeats
        + [tst_pass] * pattern.tst_repeats
    )
    result = passes[0]
    for next_pass in passes[1:]:
        result += next_pass
    return result


def assemble_all_loops(
    target_audios: list[AudioSegment],
    native_audios: list[AudioSegment],
    timing: EchoTiming,
    progress_callback=None,
    variant: str = "full",
    tnt_repeats=None,
    tst_repeats=None,
) -> AudioSegment:
    """
    Assemble all Echo Loops into a single continuous audio track.

    Args:
        target_audios: List of target language audio segments
        native_audios: List of native language TTS audio segments
        timing: EchoTiming configuration
        progress_callback: Optional callback(current, total) for progress reporting
        variant: "full", "progressive", or "shadow"
        tnt_repeats: Optional number of T-N-T passes per segment
        tst_repeats: Optional number of T-S-T passes per segment

    Returns:
        Complete AudioSegment with all loops concatenated
    """
    if len(target_audios) != len(native_audios):
        raise ValueError(
            f"Mismatch: {len(target_audios)} target segments "
            f"vs {len(native_audios)} native segments"
        )

    total = len(target_audios)
    if total == 0:
        return AudioSegment.empty()

    result = build_echo_loop(
        target_audios[0],
        native_audios[0],
        timing,
        variant,
        tnt_repeats=tnt_repeats,
        tst_repeats=tst_repeats,
    )
    if progress_callback:
        progress_callback(1, total)

    for i in range(1, total):
        loop = build_echo_loop(
            target_audios[i],
            native_audios[i],
            timing,
            variant,
            tnt_repeats=tnt_repeats,
            tst_repeats=tst_repeats,
        )
        result = result + loop
        if progress_callback:
            progress_callback(i + 1, total)

    return result
