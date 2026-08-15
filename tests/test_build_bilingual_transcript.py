import json
import tempfile
import unittest
from pathlib import Path

from tools.build_bilingual_transcript import (
    BilingualBuildError,
    build,
    clean_english_for_tts,
)


class BuildBilingualTranscriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "source.jsonl"
        records = [
            {"id": "sec-01-b0001-s01", "section_id": "sec-01", "en": "First."},
            {"id": "sec-01-b0002-s01", "section_id": "sec-01", "en": "Second."},
        ]
        self.source.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_builds_in_source_order_and_uses_echo_delimiter(self) -> None:
        translations = self.root / "translations.tsv"
        translations.write_text(
            "id\tzh\n"
            "sec-01-b0002-s01\t第二句。\n"
            "sec-01-b0001-s01\t第一句。\n",
            encoding="utf-8",
        )
        review = self.root / "review.jsonl"
        website = self.root / "website.txt"

        count = build(self.source, translations, "sec-01", review, website)

        self.assertEqual(count, 2)
        self.assertEqual(
            website.read_text(encoding="utf-8"),
            "First.|||第一句。\nSecond.|||第二句。\n",
        )
        review_records = [
            json.loads(line) for line in review.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual([record["zh"] for record in review_records], ["第一句。", "第二句。"])

    def test_rejects_missing_translation_id(self) -> None:
        translations = self.root / "translations.tsv"
        translations.write_text(
            "id\tzh\nsec-01-b0001-s01\t第一句。\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(BilingualBuildError, "missing"):
            build(
                self.source,
                translations,
                "sec-01",
                self.root / "review.jsonl",
                self.root / "website.txt",
            )

    def test_repairs_known_epub_artifacts_for_tts(self) -> None:
        self.assertEqual(
            clean_english_for_tts("His body moved firSt near the sawduSt."),
            "His body moved first near the sawdust.",
        )
        self.assertEqual(
            clean_english_for_tts("EchoLangs and MacNeilage stay unchanged."),
            "EchoLangs and MacNeilage stay unchanged.",
        )


if __name__ == "__main__":
    unittest.main()
