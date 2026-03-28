"""
Echo Loop assembler module.

Builds the T-S-N-S-T-S (Target → Silence → Native → Silence → Target → Silence)
Echo Loop pattern from individual audio components.

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
) -> AudioSegment:
    """
    Build a single Echo Loop unit.
    
    Structure: T → S(0.8s) → N → S(0.5s) → T → S(1.2s)
    
    Args:
        target_audio: The target language audio segment
        native_audio: The native language (Chinese) TTS audio
        timing: EchoTiming configuration
        
    Returns:
        A single AudioSegment containing the complete Echo Loop
    """
    loop = (
        target_audio
        + timing.silence_after_first_target()
        + native_audio
        + timing.silence_after_native()
        + target_audio
        + timing.silence_after_second_target()
    )
    return loop


def assemble_all_loops(
    target_audios: list[AudioSegment],
    native_audios: list[AudioSegment],
    timing: EchoTiming,
    progress_callback=None,
) -> AudioSegment:
    """
    Assemble all Echo Loops into a single continuous audio track.
    
    Args:
        target_audios: List of target language audio segments
        native_audios: List of native language TTS audio segments
        timing: EchoTiming configuration
        progress_callback: Optional callback(current, total) for progress reporting
        
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

    # Start with the first loop
    result = build_echo_loop(target_audios[0], native_audios[0], timing)
    if progress_callback:
        progress_callback(1, total)

    # Append remaining loops
    for i in range(1, total):
        loop = build_echo_loop(target_audios[i], native_audios[i], timing)
        result = result + loop
        if progress_callback:
            progress_callback(i + 1, total)

    return result
