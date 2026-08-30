"""target clip 结束时间的契约测试。

背景见 audio/splitter.py 的模块 docstring：LRC 只有开始时间，`end_ms` 是推出来的
「下一行开始时间」。直接照切会一头切掉句尾、一头带进大段空转。

这里用合成音频验三件事：
  A. 结尾干净的句子 —— 多余的空转被剪掉，clip 变短
  B. 结尾被切的句子 —— clip 往后延到这句话说完
  C. 连着说的一长串 —— 延长有上限，不会把下一句吞进来
另外：start_ms 任何情况下都不许被改动。
"""

import unittest

from pydub import AudioSegment
from pydub.generators import Sine

from audio.splitter import (
    MAX_TAIL_EXTEND_MS,
    TAIL_PAD_MS,
    extract_segment,
    resolve_end_ms,
    speech_mask,
)
from parser.lrc_parser import Segment


FRAME_SLACK = 20   # 帧量化误差


def tone(ms: int) -> AudioSegment:
    return Sine(440).to_audio_segment(duration=ms).apply_gain(-6)


def hush(ms: int) -> AudioSegment:
    return AudioSegment.silent(duration=ms, frame_rate=44100)


def seg(start_ms: int, end_ms: int) -> Segment:
    return Segment(
        index=0, start_ms=start_ms, end_ms=end_ms,
        target_text="t", native_text="n",
    )


class TailResolutionTests(unittest.TestCase):
    def test_trailing_silence_is_trimmed(self):
        """A：说完之后跟着 3 秒静音，clip 不该把这 3 秒全带上。"""
        audio = hush(200) + tone(1000) + hush(3000)
        mask = speech_mask(audio)
        end = resolve_end_ms(mask, start_ms=0, boundary_ms=4200)
        self.assertLess(end, 4200, "尾部空转没有被剪掉")
        self.assertAlmostEqual(end, 1200 + TAIL_PAD_MS, delta=60)

    def test_clip_extends_when_speech_crosses_the_boundary(self):
        """B：边界处还在说话，clip 要延到这句话说完（延长量在上限之内）。"""
        audio = hush(200) + tone(1500) + hush(1000)   # 声音 200~1700ms
        mask = speech_mask(audio)
        boundary = 1500                                # 砍在声音中间，离结尾 200ms
        end = resolve_end_ms(mask, start_ms=0, boundary_ms=boundary)
        self.assertGreater(end, boundary, "边界落在语音里却没有延长")
        self.assertGreaterEqual(
            end, 1700 + TAIL_PAD_MS - FRAME_SLACK, "没有延到这段声音结束"
        )

    def test_extension_is_capped(self):
        """C：一长串连续语音里，延长必须有上限，否则会吞掉下一句。"""
        audio = hush(200) + tone(20000)
        mask = speech_mask(audio)
        boundary = 3000
        end = resolve_end_ms(mask, start_ms=0, boundary_ms=boundary)
        self.assertLessEqual(
            end, boundary + MAX_TAIL_EXTEND_MS + TAIL_PAD_MS + FRAME_SLACK,
            "延长没有被上限挡住",
        )

    def test_start_is_never_modified(self):
        audio = hush(200) + tone(2000) + hush(1000)
        mask = speech_mask(audio)
        s = seg(300, 1500)
        clip_with = extract_segment(audio, s, mask=mask)
        clip_without = extract_segment(audio, s, mask=None)
        # 两者都从 300ms 开始：前 100ms 内容必须一模一样
        self.assertEqual(clip_with[:100].raw_data, clip_without[:100].raw_data)

    def test_without_mask_behaviour_is_unchanged(self):
        """不传 mask 时必须完全等价于旧实现，别的调用方不受影响。"""
        audio = hush(200) + tone(2000) + hush(1000)
        s = seg(300, 1500)
        self.assertEqual(len(extract_segment(audio, s, mask=None)), 1200)

    def test_never_produces_empty_or_reversed_clip(self):
        audio = hush(5000)                  # 整段无声
        mask = speech_mask(audio)
        s = seg(1000, 1200)
        clip = extract_segment(audio, s, mask=mask)
        self.assertGreater(len(clip), 0)

    def test_end_never_exceeds_source(self):
        audio = hush(100) + tone(400)
        mask = speech_mask(audio)
        s = seg(0, 500)
        self.assertLessEqual(len(extract_segment(audio, s, mask=mask)), len(audio))



if __name__ == "__main__":
    unittest.main()
