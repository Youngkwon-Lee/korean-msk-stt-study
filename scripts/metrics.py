"""Historical normalized CER; this is not a clinical meaning/safety metric."""
import re


def normalize(text):
    text = re.sub(r"\([^)]*\)/\(([^)]*)\)", r"\1", text)
    return re.sub(r"[^0-9A-Za-z가-힣]", "", text).lower()


def strip_nemotron_metadata(text):
    return re.sub(r"\s*<ko-KR>\s*$", "", text)


def edit_distance(a, b):
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + (char_a != char_b)))
        previous = current
    return previous[-1]


def summarize(pairs):
    """pairs: (group, reference, hypothesis); outputs aggregates without utterances."""
    errors = chars = exact = count = 0
    groups = {}
    for group, reference, hypothesis in pairs:
        reference, hypothesis = normalize(reference), normalize(hypothesis)
        if not reference:
            raise ValueError("Reference is empty after normalization")
        distance = edit_distance(reference, hypothesis)
        errors += distance
        chars += len(reference)
        exact += distance == 0
        count += 1
        bucket = groups.setdefault(group, [0, 0])
        bucket[0] += distance
        bucket[1] += len(reference)
    if not count:
        raise ValueError("No paired utterances")
    return {"cer": errors / chars,
            "cer_by_group": {key: value[0] / value[1] for key, value in groups.items()},
            "exact_sentences": exact, "sentences": count,
            "edit_errors": errors, "reference_characters": chars}
