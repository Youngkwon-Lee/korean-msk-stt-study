#!/usr/bin/env python3
"""Read-only, model-blind review QA and explicit local-evaluation release gate.

No external distribution, model calls, training, or database writes. Reports contain
only counts and hashes, never original speech/text, reviewer notes or file paths.
"""
import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

from rescore_reviewed import fingerprint, load_manifest, snapshot_db, validate_snapshot
from review_app import Dataset

PURPOSES = ('local_evaluation', 'external_review', 'model_training')
APPROVAL_SCHEMA = 'review-release-approval/v1'
REPORT_SCHEMA = 'review-release-readiness/v1'


def collect_review_state(manifest, expected_sha, review_db, first, second=None, expected_count=300):
    if second is not None and first == second:
        raise ValueError('Independent reviewers must have distinct identifiers')
    rows = load_manifest(manifest, expected_sha, expected_count)
    snapshots = [snapshot_db(review_db, expected_sha, rows, first)]
    if second:
        snapshots.append(snapshot_db(review_db, expected_sha, rows, second))
    progress = [validate_snapshot(snapshot, expected_sha, rows) for snapshot in snapshots]
    matched = conflicted = both_completed = 0
    if len(snapshots) == 2:
        for a, b in zip(snapshots[0]['items'], snapshots[1]['items']):
            x, y = a['review'], b['review']
            if x and y and x['status'] == y['status'] == 'reviewed':
                both_completed += 1
                if x['transcript'] == y['transcript']:
                    matched += 1
                else:
                    conflicted += 1
    # Digest binds all review texts, versions and the frozen manifest but emits none.
    state_sha = fingerprint({'manifest_sha256': expected_sha, 'snapshots': snapshots})
    return {
        'rows': rows,
        'snapshots': snapshots,
        'report': {
            'schema': REPORT_SCHEMA,
            'total': len(rows),
            'first_review': {key: progress[0][key] for key in ('reviewed', 'unreviewed', 'draft', 'flagged')},
            'second_review': ({key: progress[1][key] for key in ('reviewed', 'unreviewed', 'draft', 'flagged')}
                              if len(progress) == 2 else None),
            'both_reviewed': both_completed,
            'exact_agreements': matched,
            'disagreements': conflicted,
            'independent_reviewers_supplied': len(snapshots) == 2,
            'review_state_sha256': state_sha,
            'manifest_sha256': expected_sha,
        },
    }


def load_approval(path, expected_sha, manifest_sha, state_sha):
    if not path or not expected_sha:
        if path or expected_sha:
            raise ValueError('Approval file and expected SHA-256 must both be specified')
        return None
    if not re.fullmatch(r'[0-9a-f]{64}', expected_sha):
        raise ValueError('Approval fingerprint invalid')
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ValueError('Approval record unavailable')
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError('Approval fingerprint mismatch')
    try:
        approval = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ValueError('Approval JSON is invalid') from None
    if not isinstance(approval, dict) or approval.get('schema') != APPROVAL_SCHEMA:
        raise ValueError('Approval schema mismatch')
    if approval.get('manifest_sha256') != manifest_sha or approval.get('review_state_sha256') != state_sha:
        raise ValueError('Approval does not match the current frozen data and reviews')
    if not (isinstance(approval.get('approval_id'), str) and approval['approval_id'].strip()
            and isinstance(approval.get('approved_by_role'), str) and approval['approved_by_role'].strip()
            and isinstance(approval.get('evidence_record_id'), str) and approval['evidence_record_id'].strip()):
        raise ValueError('Approval lacks an accountable private evidence reference')
    try:
        approved = date.fromisoformat(approval['approved_on'])
        expires = date.fromisoformat(approval['expires_on'])
    except (ValueError, TypeError, KeyError):
        raise ValueError('Approval dates are invalid') from None
    if approved > date.today() or expires < date.today() or expires < approved:
        raise ValueError('Approval is not currently valid')
    names = ('source_reuse_rights_verified', 'privacy_review_passed',
             'reviewer_independence_verified', 'expert_qa_signed_off',
             'external_processor_contract_verified', 'model_training_rights_verified')
    if any(type(approval.get(k)) is not bool for k in names):
        raise ValueError('Approval decisions must be explicit booleans')
    if not isinstance(approval.get('scopes'), dict) or any(type(approval['scopes'].get(k)) is not bool for k in PURPOSES):
        raise ValueError('Approval scopes must be explicit booleans')
    return approval


def release_decisions(state, approval):
    report = state['report']; total = report['total']
    base = []
    if report['first_review']['reviewed'] != total:
        base.append('first_review_incomplete')
    if not report['independent_reviewers_supplied']:
        base.append('independent_review_missing')
    elif report['second_review']['reviewed'] != total:
        base.append('second_review_incomplete')
    if report['disagreements']:
        base.append('consensus_adjudication_pending')
    if report['both_reviewed'] != total or report['exact_agreements'] != total:
        base.append('full_independent_consensus_missing')
    if approval is None:
        base.append('approval_missing')
    else:
        for key in ('source_reuse_rights_verified', 'privacy_review_passed',
                    'reviewer_independence_verified', 'expert_qa_signed_off'):
            if not approval[key]:
                base.append(key + '_missing')
    decisions = {}
    for purpose in PURPOSES:
        reasons = base.copy()
        if approval is not None and not approval['scopes'][purpose]:
            reasons.append(purpose + '_not_approved')
        if purpose == 'external_review':
            if approval is None or not approval['external_processor_contract_verified']:
                reasons.append('external_processor_contract_missing')
            # Local reviewer IDs are not authentication. No external release path in this tool.
            reasons.append('authenticated_external_reviewer_service_not_implemented')
        elif purpose == 'model_training':
            if approval is None or not approval['model_training_rights_verified']:
                reasons.append('model_training_rights_missing')
            # No subject/group-safe train/validation/test split and no training exporter.
            reasons.append('training_split_and_export_not_implemented')
        decisions[purpose] = {'ready': not reasons, 'blockers': reasons}
    return decisions


def local_evaluation_bundle(state, approval_sha, destination, private_root):
    """Write paired consensus text only; never copy audio or train a model."""
    root = Path(private_root).resolve(strict=True)
    out = Path(destination).absolute()
    if out.is_symlink() or out.exists():
        raise ValueError('Release destination already exists')
    try:
        if not out.parent.resolve(strict=True).is_relative_to(root):
            raise ValueError('Release destination must be under private-runs')
    except FileNotFoundError:
        raise ValueError('Create the parent directory under private-runs first') from None
    snapshot = state['snapshots'][0]
    lines = []
    for item in snapshot['items']:
        lines.append(json.dumps({
            'review_id': item['id'],
            'audio_sha256': item['audio_sha256'],
            'reference': item['review']['transcript'],
        }, ensure_ascii=False, separators=(',', ':')))
    blob = ('\n'.join(lines) + '\n').encode('utf-8')
    evidence = {
        'schema': 'private-local-evaluation-release/v1',
        'scope': 'local_evaluation',
        'count': len(lines),
        'manifest_sha256': state['report']['manifest_sha256'],
        'review_state_sha256': state['report']['review_state_sha256'],
        'approval_sha256': approval_sha,
        'references_sha256': hashlib.sha256(blob).hexdigest(),
        'contains_audio': False,
        'requires_group_safe_splits_for_training': True,
    }
    os.mkdir(out, 0o700)
    try:
        for name, data in (('references.jsonl', blob),
                           ('release-manifest.json', (json.dumps(evidence, indent=2) + '\n').encode('utf-8'))):
            path = out / name
            with path.open('xb') as target:
                os.chmod(path, 0o600)
                target.write(data)
    except Exception:
        for path in out.iterdir():
            if path.is_file():
                path.unlink()
        out.rmdir()
        raise
    return evidence


def main(argv=None, *, private_root=None):
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('manifest', 'expected-manifest-sha256', 'review-db', 'reviewer'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--second-reviewer')
    p.add_argument('--expected-count', type=int, default=300)
    p.add_argument('--approval-file')
    p.add_argument('--expected-approval-sha256')
    p.add_argument('--private-out')
    p.add_argument('--audio-root')
    p.add_argument('--purpose', choices=PURPOSES, default='local_evaluation')
    args = p.parse_args(argv)
    try:
        state = collect_review_state(args.manifest, args.expected_manifest_sha256, args.review_db,
                                     args.reviewer, args.second_reviewer, args.expected_count)
        approval = load_approval(args.approval_file, args.expected_approval_sha256,
                                 args.expected_manifest_sha256, state['report']['review_state_sha256'])
        decisions = release_decisions(state, approval)
        report = {**state['report'], 'purposes': decisions}
        if args.private_out:
            if args.purpose != 'local_evaluation':
                raise ValueError('External review and model training release are not implemented')
            if not decisions['local_evaluation']['ready']:
                raise ValueError('Release denied: ' + ','.join(decisions['local_evaluation']['blockers']))
            if not args.audio_root:
                raise ValueError('Release requires an approved audio root for hash validation')
            # Validate each original WAV and reference against the same frozen manifest.
            Dataset(args.manifest, args.expected_manifest_sha256, args.audio_root, args.expected_count)
            # Detect concurrent review edits between the decision and file creation.
            refreshed = collect_review_state(args.manifest, args.expected_manifest_sha256, args.review_db,
                                             args.reviewer, args.second_reviewer, args.expected_count)
            if refreshed['report']['review_state_sha256'] != state['report']['review_state_sha256']:
                raise ValueError('Review state changed during release')
            root = private_root if private_root is not None else Path(__file__).resolve().parents[1] / 'private-runs'
            evidence = local_evaluation_bundle(refreshed, args.expected_approval_sha256,
                                               args.private_out, root)
            report['release'] = {'written': True, 'count': evidence['count'],
                                 'references_sha256': evidence['references_sha256']}
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if not args.private_out or report['release']['written'] else 2
    except (ValueError, TypeError, OSError, KeyError, sqlite3.Error) as ex:
        # Only allow controlled messages; never print exceptions containing private paths/text.
        message = str(ex) if type(ex) is ValueError and str(ex).startswith('Release denied:') else type(ex).__name__
        print(json.dumps({'schema': REPORT_SCHEMA, 'release': 'blocked', 'reason': message}))
        return 2


if __name__ == '__main__':
    sys.exit(main())
