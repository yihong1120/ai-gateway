from __future__ import annotations

import unittest

from official_document_workflow.domain.calendar.presentation import clean_ocr_presentation


class DisplayTests(unittest.TestCase):
    def test_known_nested_wrappers_keep_all_words_and_numbers(self):
        raw = r'二、於 $ \underline{\text{115年9月9日前提送本段續辦}} $。'
        self.assertEqual(clean_ocr_presentation(raw), '二、於 115年9月9日前提送本段續辦。')
        self.assertEqual(clean_ocr_presentation(r'\textbf{A版}，\underline{2份}'), 'A版，2份')
        self.assertEqual(clean_ocr_presentation(r'$$\underline{費用100元}$$'), '費用100元')

    def test_money_math_unknown_commands_and_malformed_text_are_not_deleted(self):
        for text in (
            r'$100 + $200', r'$f(x)=\frac{1}{2}$', r'$x + \text{m}$', r'\unknown{重要內容}',
            r'$ \underline{2} + 3 $', r'\underline{未閉合', r'C:\drawings\file.dwg',
            '第一段\n第二段 0K+100 115/9/9',
        ):
            self.assertEqual(clean_ocr_presentation(text), text)

    def test_cleaning_is_idempotent_and_rejects_unbounded_nesting(self):
        text = r'$\underline{\text{提報期限}}$'
        once = clean_ocr_presentation(text)
        self.assertEqual(clean_ocr_presentation(once), once)
        with self.assertRaises(ValueError):
            clean_ocr_presentation('\\text{'*30+'內容'+'}'*30)
