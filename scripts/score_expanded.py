#!/usr/bin/env python3
"""Private ordered score vectors and aggregate-only expanded comparison.

No vectors, references or hypotheses may be published. Phrase resampling is
conditional sensitivity analysis and does not resolve unknown speaker identity.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import random

from analyze_manifest import percentile, read_run
from clinical_terms import score_terms, summarize_terms
from compare_api import latency_summary
from metrics import edit_distance, normalize, summarize
from prepare_corpus import digest


def score(manifest_path, run_root, annotations_path):
    manifest_path=Path(manifest_path)
    manifest=json.loads(manifest_path.read_text());rows=manifest['samples']
    fingerprint=digest(manifest_path)
    annotation_sha=digest(annotations_path)
    annotations=json.loads(Path(annotations_path).read_text())
    if set(annotations)!={r['sample_id'] for r in rows}:raise ValueError('Annotation set mismatch')
    config,records=read_run(Path(run_root)/'accuracy',fingerprint,rows)
    if config['annotation_sha256']!=annotation_sha:raise ValueError('Unfrozen annotations')
    latency_rows=[r for r in rows if r['latency_substudy']]
    latency_config,latency=read_run(Path(run_root)/'latency',fingerprint,latency_rows)
    if latency_config['annotation_sha256']!=annotation_sha:raise ValueError('Latency annotation mismatch')
    vectors=[];pairs=[];term_outcomes=[];failures=0
    for row in rows:
        result=records[row['sample_id']];spans=annotations[row['sample_id']]
        if result['status']=='failed':
            failures+=1;vectors.append([None,None,len(spans)]);continue
        ref,hyp=row['reference'],result['hypothesis']
        outcomes=score_terms(ref,hyp,spans)
        vectors.append([edit_distance(normalize(ref),normalize(hyp)),sum(not o['preserved'] for o in outcomes),len(spans)])
        pairs.append((row['group'],ref,hyp));term_outcomes.append(outcomes)
    aggregate={**(summarize(pairs) if pairs else {}),'planned_samples':len(rows),
        'technical_failures':failures,'literal_terms':summarize_terms(term_outcomes),
        'scored_successful_samples':len(pairs),'audio_seconds':sum(r['audio_seconds'] for r in rows)}
    serial_values=[];serial_pairs=[];normalized_changes=raw_changes=0
    for row in latency_rows:
        item=latency[row['sample_id']];original=records[row['sample_id']]
        if item['status']!='success':continue
        key='http_seconds' if config['provider']=='gpt-transcribe' else 'processing_seconds'
        value=item.get(key)
        if not isinstance(value,(int,float)) or value<0:raise ValueError('Invalid serial timing')
        serial_values.append(value)
        serial_pairs.append((row['group'],row['reference'],item['hypothesis']))
        if original['status']=='success':
            normalized_changes+=normalize(original['hypothesis'])!=normalize(item['hypothesis'])
            raw_changes+=original['hypothesis']!=item['hypothesis']
    aggregate['serial_latency']={**(latency_summary(serial_values) if serial_values else {}),
        'planned_samples':len(latency_rows),'audio_seconds':sum(r['audio_seconds'] for r in latency_rows),
        'scope':'HTTP roundtrip' if config['provider']=='gpt-transcribe' else 'local decode plus inference; not HTTP'}
    aggregate['serial_repeat']={'normalized_output_changes':normalized_changes,'raw_output_changes':raw_changes,
        'scored_repeat':summarize(serial_pairs) if serial_pairs else None,
        'definition':'Repeat does not replace full accuracy outputs; batch/concurrency differs between phases'}
    batch_times={r['batch_id']:r['batch_seconds'] for r in records.values() if r.get('batch_id') and r['status']=='success'}
    if batch_times:aggregate['accuracy_distinct_batch_seconds']=sum(batch_times.values())
    allowed=('provider','condition_id','annotation_sha256','api_settings','accuracy_workers','accuracy_batch_size',
        'precision','attention','max_new_tokens','language','chunk_seconds','package_versions','model_file_sha256',
        'gpu_device','decode_source_sha256','source_sha256')
    aggregate['execution']={k:config[k] for k in allowed if k in config}
    aggregate['returned_model_labels']=sorted({r.get('response_model') for r in records.values() if r.get('response_model')})
    starts=[datetime.fromisoformat(r['started_at']) for r in records.values() if r.get('started_at')]
    aggregate['accuracy_first_request_at']=min(starts).isoformat() if starts else None
    return {'schema':'private-expanded-score/v1','manifest_sha256':fingerprint,
        'annotation_sha256':annotation_sha,'aggregate':aggregate,
        'vector_order':'manifest.samples order','private_vectors':vectors}


def compare(manifest_path,a,b,repetitions=2000,seed=20261117):
    manifest=json.loads(Path(manifest_path).read_text());rows=manifest['samples']
    if a['aggregate'].get('execution',{}).get('provider')!='gpt-transcribe' or b['aggregate'].get('execution',{}).get('provider')!='qwen-local':
        raise ValueError('Model identity/order mismatch')
    if a['manifest_sha256']!=b['manifest_sha256'] or a['manifest_sha256']!=digest(manifest_path):raise ValueError('Manifest mismatch')
    if a['annotation_sha256']!=b['annotation_sha256']:raise ValueError('Annotation fingerprint mismatch')
    va,vb=a['private_vectors'],b['private_vectors']
    if len(va)!=len(rows) or len(vb)!=len(rows):raise ValueError('Missing paired score')
    blocks=defaultdict(lambda:[0,0,0,0,0,0]);fa=fb=common=0
    for row,ra,rb in zip(rows,va,vb):
        if ra[2]!=rb[2]:raise ValueError('Term denominator mismatch')
        fa+=ra[0] is None;fb+=rb[0] is None
        if ra[0] is None or rb[0] is None:continue
        common+=1
        for i,value in enumerate((ra[0],rb[0],len(normalize(row['reference'])),ra[1],rb[1],ra[2])):
            if not isinstance(value,int) or value<0:raise ValueError('Invalid numeric score')
            blocks[row['text_group']][i]+=value
    totals=[sum(v[i] for v in blocks.values()) for i in range(6)]
    rng=random.Random(seed);cer_draws=[];term_draws=[];values=list(blocks.values())
    if len(values)>=2:
        for _ in range(repetitions):
            picked=[values[rng.randrange(len(values))] for _ in values]
            chars=sum(v[2] for v in picked);terms=sum(v[5] for v in picked)
            cer_draws.append(sum(v[0]-v[1] for v in picked)/chars)
            if terms:term_draws.append(sum(v[3]-v[4] for v in picked)/terms)
    interval=lambda draws:[percentile(draws,.025),percentile(draws,.975)] if draws else None
    return {'manifest_sha256':a['manifest_sha256'],'selection_manifest_sha256':manifest['selection_manifest_sha256'],
        'annotation_sha256':a['annotation_sha256'],'models':{'gpt_transcribe':a['aggregate'],'qwen_1_7b':b['aggregate']},
        'paired':{'common_success_samples':common,'technical_failures_a':fa,'technical_failures_b':fb,
            'reference_characters':totals[2],'normalized_phrase_groups':len(blocks),
            'cer_difference_a_minus_b':(totals[0]-totals[1])/totals[2] if totals[2] else None,
            'descriptive_phrase_interval_95':interval(cer_draws),
            'literal_term_error_rate_difference_a_minus_b':(totals[3]-totals[4])/totals[5] if totals[5] else None,
            'descriptive_literal_term_interval_95':interval(term_draws),'repetitions':repetitions,'seed':seed},
        'interval_scope':'Observed corpus conditional phrase resampling; unknown speaker dependence NOT adjusted; no population p values',
        'independent_speakers':None,'independent_reference_review_complete':False,
        'clinical_semantic_errors':None,'clinical_editing_burden':None,
        'publication_scope':'Reviewed aggregates only; private ordered vectors excluded'}


def main():
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('manifest','run-root','annotations','private-out'):p.add_argument('--'+name,required=True)
    p.add_argument('--compare-with-private-score');p.add_argument('--public-out')
    args=p.parse_args();result=score(args.manifest,args.run_root,args.annotations)
    with Path(args.private_out).open('x') as stream:json.dump(result,stream,ensure_ascii=False,separators=(',',':'))
    if args.compare_with_private_score:
        if not args.public_out:p.error('Comparison requires public candidate path')
        first=json.loads(Path(args.compare_with_private_score).read_text())
        combined=compare(args.manifest,first,result)
        with Path(args.public_out).open('x') as stream:json.dump(combined,stream,ensure_ascii=False,indent=2)
    print('Scoring complete. Ordered vectors remain private; aggregate requires review.')


if __name__=='__main__':main()
