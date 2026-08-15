import unittest

from tools.extract_epub_transcript import normalize_text, split_sentences


class NormalizeTextTests(unittest.TestCase):
    def test_repairs_missing_space_before_new_sentence(self) -> None:
        self.assertEqual(
            normalize_text("learning…These ideas matter."),
            "learning… These ideas matter.",
        )
        self.assertEqual(
            normalize_text("(2006).Toward a theory."),
            "(2006). Toward a theory.",
        )

    def test_does_not_change_uppercase_file_extension(self) -> None:
        self.assertEqual(normalize_text("ECHO_LOOP_DAY_1.WAV"), "ECHO_LOOP_DAY_1.WAV")


class SplitSentenceTests(unittest.TestCase):
    def test_splits_period_ellipsis_and_quoted_boundary(self) -> None:
        self.assertEqual(
            split_sentences('First sentence. Another idea… Then she said “Go.” Next step.'),
            [
                "First sentence.",
                "Another idea…",
                "Then she said “Go.”",
                "Next step.",
            ],
        )

    def test_preserves_common_abbreviations_and_decimals(self) -> None:
        self.assertEqual(
            split_sentences("Dr. Reeve used e.g. 0.8 seconds. It worked."),
            ["Dr. Reeve used e.g. 0.8 seconds.", "It worked."],
        )


if __name__ == "__main__":
    unittest.main()
