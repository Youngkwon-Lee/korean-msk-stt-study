"""Exploratory literal term preservation, not a validated clinical safety score."""
from collections import Counter

from metrics import normalize


# Frozen before either model's outputs are inspected. Occurrences, not unique types.
# No synonyms, semantic equivalence or clinical severity is inferred.
LEXICON = {
    '마비': 'symptom', '감전': 'injury', '얼굴': 'body_part', '목': 'body_part',
    '피': 'symptom', '다리': 'body_part', '화상': 'injury', '배': 'body_part',
    '어깨': 'body_part', '고혈압': 'disease', '수술 이력': 'history',
    '알레르기': 'disease', '당 수치': 'measurement', '통증': 'symptom',
    '암 검사': 'test', '가슴': 'body_part', '혈당': 'measurement',
    '현기증': 'symptom', '당뇨': 'disease', '가족력': 'history',
    '종양': 'disease', '치료': 'treatment', '열 기운': 'symptom',
    '가래': 'symptom', '기침': 'symptom',
}


def annotate(reference):
    reference = normalize(reference)
    spans = []
    for term, category in LEXICON.items():
        term = normalize(term)
        start = reference.find(term)
        while start >= 0:
            spans.append({'start': start, 'end': start + len(term), 'category': category})
            start = reference.find(term, start + len(term))
    spans.sort(key=lambda x: (x['start'], x['end']))
    if any(a['end'] > b['start'] for a, b in zip(spans, spans[1:])):
        raise ValueError('Overlapping annotations require adjudication')
    return spans


def alignment(reference, hypothesis):
    """Global character edit alignment, deterministic match/substitution > delete > insert ties."""
    a, b = normalize(reference), normalize(hypothesis)
    matrix = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        matrix[i][0] = i
    for j in range(len(b) + 1):
        matrix[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            matrix[i][j] = min(matrix[i-1][j-1] + (a[i-1] != b[j-1]),
                               matrix[i-1][j] + 1, matrix[i][j-1] + 1)
    mapped, insertions = {}, []
    i, j = len(a), len(b)
    while i or j:
        if i and j and matrix[i][j] == matrix[i-1][j-1] + (a[i-1] != b[j-1]):
            mapped[i-1] = (j-1, a[i-1] == b[j-1]); i -= 1; j -= 1
        elif i and matrix[i][j] == matrix[i-1][j] + 1:
            mapped[i-1] = (None, False); i -= 1
        else:
            insertions.append(i); j -= 1
    return mapped, insertions


def score_terms(reference, hypothesis, spans):
    mapped, insertions = alignment(reference, hypothesis)
    outcomes = []
    for span in spans:
        start, end = span['start'], span['end']
        retained = all(mapped[i][1] for i in range(start, end))
        retained = retained and not any(start < boundary < end for boundary in insertions)
        outcomes.append({**span, 'preserved': bool(retained)})
    return outcomes


def summarize_terms(outcomes):
    totals = Counter(x['category'] for row in outcomes for x in row)
    errors = Counter(x['category'] for row in outcomes for x in row if not x['preserved'])
    total, erroneous = sum(totals.values()), sum(errors.values())
    return {'definition': 'aligned exact literal term preservation; no semantic or severity adjudication',
            'term_occurrences': total, 'term_errors': erroneous,
            'term_error_rate': erroneous / total if total else None,
            'sentences_with_term_error': sum(any(not x['preserved'] for x in row) for row in outcomes),
            'by_category': {c: {'occurrences': totals[c], 'errors': errors[c],
                               'error_rate': errors[c] / totals[c]} for c in sorted(totals)}}
