"""
Echo Loop assembler module.

Builds Echo Loop patterns from individual audio components.
Supports three variants:
  full:        T-S-N-S-T-S
  progressive: T-S-N-S-T-S + T-S-silence-S-T-S
  shadow:      T-S-silence-S-T-S

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


def build_echo_loop(
    target_audio: AudioSegment,
    native_audio: AudioSegment,
    timing: EchoTiming,
    variant: str = "full",
) -> AudioSegment:
    """
    Build Echo Loop unit(s) for a single segment.

    Args:
        target_audio: The target language audio segment
        native_audio: The native language TTS audio
        timing: EchoTiming configuration
        variant: "full", "progressive", or "shadow"

    Returns:
        AudioSegment containing the loop(s)
    """
    s1 = timing.silence_after_first_target()
    s2 = timing.silence_after_native()
    s3 = timing.silence_after_second_target()
    native_silence = AudioSegment.silent(duration=len(native_audio))

    if variant == "shadow":
        # T-S-silence(len(N))-S-T-S
        return target_audio + s1 + native_silence + s2 + target_audio + s3

    # full pass: T-S-N-S-T-S
    full_pass = target_audio + s1 + native_audio + s2 + target_audio + s3

    if variant == "progressive":
        # shadow pass: T-S-silence(len(N))-S-T-S
        shadow_pass = target_audio + s1 + native_silence + s2 + target_audio + s3
        return full_pass + shadow_pass

    # variant == "full" (default)
    return full_pass


def assemble_all_loops(
    target_audios: list[AudioSegment],
    native_audios: list[AudioSegment],
    timing: EchoTiming,
    progress_callback=None,
    variant: str = "full",
) -> AudioSegment:
    """
    Assemble all Echo Loops into a single continuous audio track.

    Args:
        target_audios: List of target language audio segments
        native_audios: List of native language TTS audio segments
        timing: EchoTiming configuration
        progress_callback: Optional callback(current, total) for progress reporting
        variant: "full", "progressive", or "shadow"

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

    result = build_echo_loop(target_audios[0], native_audios[0], timing, variant)
    if progress_callback:
        progress_callback(1, total)

    for i in range(1, total):
        loop = build_echo_loop(target_audios[i], native_audios[i], timing, variant)
        result = result + loop
        if progress_callback:
            progress_callback(i + 1, total)

    return result