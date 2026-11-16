"""Illustrative paired-binary sample-size approximation; no observed/pilot data used.
Final clustered confirmatory N must be chosen by simulation after pilot estimation.
Python standard library only. Run: python3 sample_size_plan.py
"""
import csv
import math
from pathlib import Path
from statistics import NormalDist


def approximate_n(p10, p01, power=0.80, alpha=0.05):
    if not (0 <= p01 < p10 <= 1 and p10 + p01 <= 1):
        raise ValueError('Require p10 > p01 >= 0 and p10+p01 <= 1')
    q, delta = p10 + p01, p10 - p01
    z_alpha = NormalDist().inv_cdf(1 - alpha / 2)
    z_power = NormalDist().inv_cdf(power)
    return math.ceil((z_alpha * math.sqrt(q) + z_power * math.sqrt(q - delta**2))**2 / delta**2)


def scenarios():
    for label, p10, p01 in [('large_difference', .20, .05), ('moderate_difference', .20, .10), ('small_difference', .15, .10)]:
        for power in (.80, .90):
            n = approximate_n(p10, p01, power)
            for rho in (0.0, 0.05, 0.10):
                m, missing = 5, .15
                de = 1 + (m-1)*rho
                yield dict(scenario=label, p10=p10, p01=p01, absolute_difference=round(p10-p01, 6),
                    power=power, alpha=.05, independent_evaluable_pairs=n,
                    mean_encounters_per_therapist=m, assumed_icc_of_paired_difference=rho,
                    screening_design_effect=de, non_evaluable_rate=missing,
                    illustrative_recruitment=math.ceil(n*de/(1-missing)))


if __name__ == '__main__':
    rows = list(scenarios())
    output = Path(__file__).resolve().parent.parent / 'results' / 'sample_size_scenarios.csv'
    with output.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]), lineterminator="\n");writer.writeheader();writer.writerows(rows)
    print('HYPOTHETICAL assumptions; approximate screening estimates, not final targets.')
    for r in rows:
        if r['assumed_icc_of_paired_difference']==.05:
            print(r['scenario'],r['power'],'independent pairs',r['independent_evaluable_pairs'],'illustrative recruitment',r['illustrative_recruitment'])
