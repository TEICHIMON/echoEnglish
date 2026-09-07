"""The trilingual-script splitter behind the web UI's 「三语稿」 toggle.

Four columns ``EN|||JA|||ZH-for-EN|||ZH-for-JA`` give each language its own
Chinese line; the older three-column form shares one Chinese line.
"""

import unittest

from webapp.jobs import split_multilingual_script


class SplitMultilingualScriptTests(unittest.TestCase):
    def test_four_columns_give_each_language_its_own_chinese(self) -> None:
        raw = (
            "# comment\n"
            "\n"
            "I'll call you when I get home.|||家に着いたら電話します。"
            "|||我会给你打电话，等我到家。|||到家了就打电话。\n"
        )
        en, ja, found = split_multilingual_script(raw)
        self.assertTrue(found)
        self.assertEqual(en, ["# comment", "", "I'll call you when I get home.|||我会给你打电话，等我到家。"])
        self.assertEqual(ja, ["# comment", "", "家に着いたら電話します。|||到家了就打电话。"])

    def test_three_columns_share_the_chinese_line(self) -> None:
        en, ja, found = split_multilingual_script("Hello.|||こんにちは。|||你好。\n")
        self.assertTrue(found)
        self.assertEqual(en, ["Hello.|||你好。"])
        self.assertEqual(ja, ["こんにちは。|||你好。"])

    def test_empty_fourth_column_falls_back_to_shared_chinese(self) -> None:
        en, ja, _ = split_multilingual_script("Hello.|||こんにちは。|||你好。|||\n")
        self.assertEqual(en, ["Hello.|||你好。"])
        self.assertEqual(ja, ["こんにちは。|||你好。"])

    def test_interview_role_marker_is_copied_to_the_japanese_line(self) -> None:
        raw = (
            "Q:Tell me about yourself.|||自己紹介をお願いします。"
            "|||请介绍一下你自己。|||请做一下自我介绍。\n"
            "A:I am a backend engineer.|||バックエンドエンジニアです。"
            "|||我是一名后端工程师。|||我是后端工程师。\n"
        )
        en, ja, found = split_multilingual_script(raw, interview=True)
        self.assertTrue(found)
        self.assertEqual(en[0], "Q:Tell me about yourself.|||请介绍一下你自己。")
        self.assertEqual(ja[0], "Q:自己紹介をお願いします。|||请做一下自我介绍。")
        self.assertEqual(ja[1], "A:バックエンドエンジニアです。|||我是后端工程师。")

    def test_bilingual_lines_pass_through_and_do_not_count_as_found(self) -> None:
        raw = "こんにちは|||你好\n"
        en, ja, found = split_multilingual_script(raw)
        self.assertFalse(found)
        self.assertEqual(en, [raw.rstrip("\n")])
        self.assertEqual(ja, [raw.rstrip("\n")])


if __name__ == "__main__":
    unittest.main()
