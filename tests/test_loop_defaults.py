from pathlib import Path
import tempfile
import unittest

from audio.assembler import resolve_loop_pattern
from main import load_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class LoopDefaultTests(unittest.TestCase):
    def assert_split_one_plus_one(self, config: dict) -> None:
        loop = config["loop"]
        pattern = resolve_loop_pattern(
            loop["variant"], loop["tnt_repeats"], loop["tst_repeats"]
        )
        self.assertEqual(pattern.tnt_repeats, 1)
        self.assertEqual(pattern.tst_repeats, 1)
        self.assertIs(loop["split_outputs"], True)

    def test_builtin_loop_default_is_configurable_split_one_plus_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing-config.yaml"
            self.assert_split_one_plus_one(load_config(missing))

    def test_shipped_config_uses_split_one_plus_one(self) -> None:
        self.assert_split_one_plus_one(load_config(PROJECT_ROOT / "config.yaml"))

    def test_variant_and_repeat_overrides_still_work(self) -> None:
        pattern = resolve_loop_pattern("progressive", tnt_repeats=2, tst_repeats=3)
        self.assertEqual(pattern.tnt_repeats, 2)
        self.assertEqual(pattern.tst_repeats, 3)


if __name__ == "__main__":
    unittest.main()
