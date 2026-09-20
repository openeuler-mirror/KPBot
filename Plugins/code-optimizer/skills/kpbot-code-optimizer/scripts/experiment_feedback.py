#!/usr/bin/env python3
"""Check experiment provenance and route an agent's explicit verdict, not prove causality."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from experiment_record import ID_FIELDS, validate_record


def choose_next(feedback: dict, remaining: int) -> str:
    """Route checked feedback; callers must independently compute the checked flags."""
    if type(remaining) is not int or remaining < 0:
        raise ValueError('remaining must be a non-negative integer')
    if not isinstance(feedback, dict):
        raise ValueError('feedback must be an object')
    for field, choices in (
        ('execution_status', ('completed', 'failed', 'awaiting_target_evidence')),
        ('hypothesis_verdict', ('supported', 'refuted', 'inconclusive', 'invalid')),
    ):
        if feedback.get(field) not in choices:
            raise ValueError(f'invalid or missing {field}')
    for field in ('evidence_checked', 'target_checked'):
        if type(feedback.get(field)) is not bool:
            raise ValueError(f'{field} must be a boolean')
    if feedback['execution_status'] == 'awaiting_target_evidence':
        if feedback['hypothesis_verdict'] != 'inconclusive':
            raise ValueError('awaiting_target_evidence requires inconclusive verdict')
        return 'awaiting_target_evidence'
    if feedback['execution_status'] == 'failed':
        return 'blocked_execution'
    if not feedback['evidence_checked'] or not feedback['target_checked']:
        return 'blocked_evidence'
    verdict = feedback['hypothesis_verdict']
    if verdict == 'invalid':
        return 'blocked_evidence'
    if verdict == 'supported':
        return 'DecideOptimization'
    if remaining == 0:
        return 'stop_budget'
    return 'AnalyzeHotspot' if verdict == 'refuted' else 'DesignExperiment'


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def checked_feedback(record: dict, feedback: dict, record_path: Path, run_dir: Path) -> dict:
    """Check independent active identity and observation metadata against the record."""
    validate_record(record, run_dir)
    for field in ('explanation', 'record_path'):
        if not _text(feedback.get(field)):
            raise ValueError(f'{field} must be a non-empty string')
    root = run_dir.resolve()
    actual = record_path.resolve()
    claimed = (root / feedback['record_path']).resolve()
    if root not in actual.parents or claimed != actual:
        raise ValueError('record_path does not identify the input record inside run-dir')
    for field in ID_FIELDS:
        if feedback.get(field) != record[field]:
            raise ValueError(f'feedback {field} differs from record')
    status = feedback['execution_status']
    if status == 'awaiting_target_evidence':
        if feedback['hypothesis_verdict'] != 'inconclusive':
            raise ValueError('awaiting_target_evidence requires inconclusive verdict')
        if (root / 'experiments').resolve() in actual.parents:
            raise ValueError('awaiting_target_evidence requires a separate draft location')
        if 'exit_code' in record['experiment'] or record['evidence']:
            raise ValueError('awaiting_target_evidence requires an unexecuted draft without evidence')
        return dict(feedback, evidence_checked=False, target_checked=False)
    else:
        expected = root / 'experiments' / record['experiment_id'] / 'record.json'
        if actual != expected.resolve() or not record['evidence']:
            raise ValueError('executed attempt requires a finalized record with evidence')
        code = record['experiment'].get('exit_code')
        if type(code) is not int:
            raise ValueError('executed attempt requires integer experiment.exit_code')
        if code != 0:
            status = 'failed'

    checked = dict(feedback, execution_status=status, evidence_checked=False, target_checked=False)
    try:
        context = json.loads((root / 'current_context.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return checked
    if not isinstance(context, dict):
        raise ValueError('current_context must be an object')
    # Non-empty fields are already validated in record; equality also excludes missing context fields.
    checked['target_checked'] = all(context.get(k) == record[k] for k in
                                    ('run_id', 'point_id', 'parent_state', 'execution_context'))
    if not checked['target_checked']:
        return checked
    for item in record['evidence']:
        if item['kind'] != 'observation' or item['source'] == 'fixture':
            continue
        provenance = item.get('provenance')
        if not isinstance(provenance, dict):
            continue
        if any(provenance.get(k) != record[k] for k in ('parent_state', 'execution_context')):
            continue
        if provenance.get('scope') != item['scope']:
            continue
        if item['source'] == 'historical' and not _text(item.get('reuse_explanation')):
            continue
        checked['evidence_checked'] = True
        break
    return checked


def route_verification(result: dict) -> str:
    """Validate supplied candidate verdicts, then route; missing evidence never delivers."""
    if not isinstance(result, dict):
        raise ValueError('verification result must be an object')
    verdicts = ('passed', 'failed', 'inconclusive', 'not_run')
    for field in ('correctness_verdict', 'local_performance_verdict', 'system_acceptance'):
        if field in result and result[field] not in verdicts:
            raise ValueError(f'invalid {field}')
    for field, flag in (('compilation', 'ok'), ('functional_test', 'passed'),
                        ('performance', 'regression')):
        if field in result:
            value = result[field]
            if not isinstance(value, dict):
                raise ValueError(f'{field} must be an object')
            if flag in value and type(value[flag]) is not bool:
                raise ValueError(f'{field}.{flag} must be a boolean')
    status = result.get('status')
    if 'status' in result and status not in ('verified', 'regression', 'failed', 'unverified'):
        raise ValueError('invalid or obsolete verification status')
    compilation = result.get('compilation', {}).get('ok')
    functional = result.get('functional_test', {}).get('passed')
    regression = result.get('performance', {}).get('regression')
    correctness = result.get('correctness_verdict')
    local = result.get('local_performance_verdict')
    system = result.get('system_acceptance')
    failed = compilation is False or functional is False or correctness == 'failed'
    accepted = correctness == 'passed' and system == 'passed'
    if correctness == 'passed' and (compilation is False or functional is False):
        raise ValueError('correctness passed conflicts with compilation/functional failure')
    if system == 'passed' and (failed or correctness not in (None, 'passed') or
                               local == 'failed' or regression is True):
        raise ValueError('system acceptance conflicts with failed or unresolved verdicts')
    if status == 'verified' and not accepted:
        raise ValueError('verified requires passed correctness and system acceptance')
    if accepted and status not in (None, 'verified'):
        raise ValueError('accepted verdicts conflict with legacy status')
    if status == 'failed' and system == 'passed':
        raise ValueError('failed status conflicts with system acceptance')
    if status == 'failed' and correctness in ('passed', 'inconclusive', 'not_run') and not failed:
        raise ValueError('failed status conflicts with correctness verdict')
    if status in ('regression', 'unverified') and failed:
        raise ValueError('correctness failure requires failed status')
    if status == 'regression' and (system in ('passed', 'not_run') or regression is False):
        raise ValueError('regression status conflicts with system performance')
    if status == 'unverified' and regression is True:
        raise ValueError('observed regression requires regression status')
    if failed:
        return 'FixCode'
    if accepted:
        return 'Deliver'
    return 'ValidateExperiment'


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--record', type=Path)
    modes.add_argument('--verify-result', type=Path)
    parser.add_argument('--feedback', type=Path)
    parser.add_argument('--run-dir', type=Path)
    parser.add_argument('--remaining', type=int)
    args = parser.parse_args(argv)
    diagnostic_args = (args.feedback, args.run_dir, args.remaining)
    if args.verify_result is not None and any(value is not None for value in diagnostic_args):
        parser.error('--verify-result cannot be combined with diagnostic arguments')
    if args.record is not None and any(value is None for value in diagnostic_args):
        parser.error('--record requires --feedback, --run-dir and --remaining')
    try:
        if args.verify_result is not None:
            result = json.loads(args.verify_result.read_text(encoding='utf-8'))
            next_stage = route_verification(result)
            print(json.dumps(dict(result, next_stage=next_stage), ensure_ascii=False))
            return 0
        record = json.loads(args.record.read_text(encoding='utf-8'))
        feedback = json.loads(args.feedback.read_text(encoding='utf-8'))
        choose_next(feedback, args.remaining)  # Validate caller types, never trust caller flags.
        checked = checked_feedback(record, feedback, args.record, args.run_dir)
        next_stage = choose_next(checked, args.remaining)
    except (OSError, ValueError, TypeError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(dict(checked, next_stage=next_stage,
                         reason=f"{next_stage}: execution={checked['execution_status']}, "
                                f"verdict={checked['hypothesis_verdict']}, "
                                f"evidence_checked={checked['evidence_checked']}, "
                                f"target_checked={checked['target_checked']}, remaining={args.remaining}"),
                     ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
