import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from clinical_terms import annotate, score_terms, summarize_terms


class ClinicalTermTests(unittest.TestCase):
    def test_spacing_is_ignored_but_body_part_substitution_is_not(self):
        ref = '왼쪽 어깨 통증'
        spans = annotate(ref)
        self.assertTrue(all(x['preserved'] for x in score_terms(ref, '왼쪽어깨 통증.', spans)))
        scored = score_terms(ref, '왼쪽 무릎 통증', spans)
        self.assertEqual([x['preserved'] for x in scored], [False, True])

    def test_repeated_terms_are_scored_at_their_own_aligned_occurrence(self):
        ref = '어깨와 어깨'
        scored = score_terms(ref, '어깨와 무릎', annotate(ref))
        self.assertEqual([x['preserved'] for x in scored], [True, False])

    def test_internal_insertion_counts_but_neighboring_addition_does_not(self):
        ref = '고혈압'
        self.assertFalse(score_terms(ref, '고저혈압', annotate(ref))[0]['preserved'])
        self.assertTrue(score_terms(ref, '고혈압이', annotate(ref))[0]['preserved'])

    def test_negation_is_not_falsely_claimed_as_a_terminology_error(self):
        ref = '통증이 없어요'
        outcome = score_terms(ref, '통증이 있어요', annotate(ref))
        self.assertTrue(outcome[0]['preserved'])  # Meaning needs separate clinical assessment.
        summary = summarize_terms([outcome, []])
        self.assertEqual(summary['term_occurrences'], 1)
        self.assertEqual(summary['term_errors'], 0)


if __name__ == '__main__':
    unittest.main()
