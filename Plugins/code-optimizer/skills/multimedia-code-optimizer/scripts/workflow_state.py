import argparse
import copy
import json
import os
from pathlib import Path
import re
import tempfile

RETRY_STATUSES = ('blocked', 'needs_environment', 'pending_validation')
RECOVERY_FIELDS = ('stage_results', 'stage_retries', 'stage_notes')
WORKFLOWS = ('media-preparation', 'media-optimization-loop', 'media-validation')


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def save(path, value, exclusive=False):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w',
            encoding='utf-8',
            dir=path.parent,
            prefix='.state-',
            suffix='.tmp',
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)
        else:
            temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def script_parts(path):
    script = path.read_bytes().decode('utf-8')
    end = script.find('\n}\n')
    if end < 0:
        raise ValueError('frozen workflow meta must end with a standalone } followed by a newline')
    return script[: end + 3], script[end + 3 :]


def workflow_name(script):
    meta, _ = script_parts(script)
    match = re.search(r"\bname\s*:\s*(['\"])([^'\"]+)\1", meta)
    if not match or match[2] not in WORKFLOWS:
        raise ValueError('unrecognized media workflow meta.name')
    return match[2]


def absolute(value, name):
    if (
        not isinstance(value, str)
        or not value.startswith('/')
        or '\0' in value
        or '//' in value
        or any(part in ('.', '..') for part in value.split('/'))
        or not value.rstrip('/')
    ):
        raise ValueError(f'{name} must be a normalized absolute path')
    return value.rstrip('/')


def workflow_schema(script):
    name = workflow_name(script)
    _, body = script_parts(script)
    declared = re.search(r'return\s*\{\s*schema_version:\s*(\d+)', body)
    schema = int(declared[1]) if declared else None
    if schema not in dict(zip(WORKFLOWS, ((1,), (4, 5), (2, 3))))[name]:
        raise ValueError('unrecognized frozen workflow result schema')
    return schema


def result_path(state, result):
    name = workflow_name(Path(state['scriptPath']))
    args = state['args']
    schema = workflow_schema(Path(state['scriptPath']))
    if (
        not isinstance(result, dict)
        or type(result.get('schema_version')) is not int
        or result['schema_version'] != schema
    ):
        raise ValueError(f'{name} result requires schema_version={schema}')
    statuses = (
        ('converged', 'budget_exhausted')
        if name == WORKFLOWS[1]
        else (('ready',) if name == WORKFLOWS[0] else ('completed',))
    )
    if result.get('status') not in (*statuses, 'blocked', 'interrupted'):
        raise ValueError('invalid workflow result status')
    if not isinstance(result.get('reason'), str) or 'stopped_stage' not in result:
        raise ValueError('incomplete workflow result')
    if name == WORKFLOWS[0]:
        revision = args.get('check_revision', 1)
        if type(revision) is not int or not 1 <= revision <= 9007199254740991:
            raise ValueError('check_revision must be a positive safe integer')
        expected = {key: absolute(args.get(key), key) for key in ('repo_path', 'preparation_dir')}
        expected.update(
            check_revision=revision,
            build_scope=args.get('build_scope', '项目默认媒体库构建'),
            instructions=args.get('instructions') or '',
            profile_paths=args.get('profile_paths', []),
        )
        required = ('requirements', 'environment')
        target = Path(expected['preparation_dir']) / f'preparation-{revision}.json'
    elif name == WORKFLOWS[1]:
        expected = {
            key: absolute(args.get(key), key)
            for key in ('repo_path', 'run_dir', 'preparation_path')
        }
        expected.update(operator=args.get('operator'), instructions=args.get('instructions') or '')
        required = (
            'context_path',
            'environment',
            'source',
            'baseline',
            'baseline_mca',
            'current',
            'current_build_status',
            'selected',
            'selected_mca',
            'correctness',
            'environment_request',
            'candidates',
        )
        if schema == 5:
            expected['writeback'] = args.get('writeback', True)
            required += ('workspace',)
        target = Path(expected['run_dir']) / 'optimization.json'
    else:
        optimization = args.get('optimization')
        if not isinstance(optimization, dict):
            raise ValueError('validation args lack optimization identity')
        expected = {
            key: absolute(args.get(key), key) for key in ('optimization_path', 'validation_dir')
        }
        expected.update(
            {key: optimization.get(key) for key in ('selected', 'baseline', 'selected_mca')}
        )
        required = ('correctness', 'validation_setup', 'context_path')
        if schema == 3:
            expected.update(
                workspace=optimization.get('workspace'),
                writeback_enabled=args.get('writeback', optimization.get('writeback', True)),
            )
            required += ('writeback',)
        target = Path(expected['validation_dir']) / 'validation.json'
    if any(key not in result for key in required):
        raise ValueError('incomplete workflow result')
    for key, value in expected.items():
        if (
            key not in result
            or result[key] != value
            or (key == 'check_revision' and type(result[key]) is not int)
        ):
            raise ValueError(f'result does not belong to this task: {key}')
    repo = absolute(
        (
            args.get('optimization', {}).get('repo_path')
            if name == WORKFLOWS[2]
            else args.get('repo_path')
        ),
        'repo_path',
    )
    if target.parent == Path(repo) or Path(repo) in target.resolve().parents:
        raise ValueError('result directory must be outside the target repository')
    return target


def can_advance(name, result):
    if name == WORKFLOWS[0]:
        return result['status'] == 'ready' and all(
            isinstance(result.get(key), dict) for key in ('requirements', 'environment')
        )
    if name == WORKFLOWS[1]:
        return (
            result.get('schema_version') == 5
            and result['status'] in ('converged', 'budget_exhausted')
            and all(
                isinstance(result.get(key), dict) and result[key]
                for key in ('workspace', 'baseline', 'selected', 'environment', 'source')
            )
            and isinstance(result.get('context_path'), str)
            and bool(result['context_path'])
        )
    return False


def collect(directory, state, output, run_id):
    if state.get('needs_record', False) or not run_id or run_id != state.get('runId'):
        raise ValueError('collect requires the latest recorded workflow run ID')
    if not re.fullmatch(r'wf_[A-Za-z0-9_-]+', run_id):
        raise ValueError('invalid workflow run ID')
    wrapper = load(output)
    if not isinstance(wrapper, dict) or 'result' not in wrapper:
        raise ValueError('output must be the original runtime JSON object containing result')
    if 'runId' in wrapper and wrapper['runId'] != run_id:
        raise ValueError('runtime output runId does not match the recorded run')
    result = wrapper['result']
    target = result_path(state, result)
    snapshot = directory / f'result-{run_id}.json'
    previous = state.get('collected')
    if snapshot.exists() and load(snapshot) != result:
        raise ValueError('this run already has a different collected result')
    exists = target.exists() or target.is_symlink()
    if exists:
        if (
            target.is_symlink()
            or not (
                (snapshot.exists() and load(target) == result)
                or (
                    previous
                    and previous.get('path') == str(target)
                    and load(target) == load(Path(previous['snapshotPath']))
                )
            )
        ):
            raise ValueError(f'refusing to overwrite a result not owned by this state: {target}')
    target.parent.mkdir(parents=True, exist_ok=True)
    if not snapshot.exists():
        save(snapshot, result, exclusive=True)
    if not exists or load(target) != result:
        save(target, result, exclusive=not exists)
    state['collected'] = {'runId': run_id, 'path': str(target), 'snapshotPath': str(snapshot)}
    save(directory / 'state.json', state)
    print(
        json.dumps(
            {
                'resultPath': str(target),
                'runId': run_id,
                'canAdvance': can_advance(workflow_name(Path(state['scriptPath'])), result),
            },
            ensure_ascii=False,
        )
    )


def inherit(directory, script, overrides):
    state = load(directory / 'state.json')
    collected = state.get('collected')
    if (
        state.get('needs_record', False)
        or not state.get('runId')
        or not collected
        or collected.get('runId') != state['runId']
    ):
        raise ValueError('--from-state requires a collected result for the latest recorded run')
    result = load(Path(collected['path']))
    if (
        result != load(Path(collected['snapshotPath']))
        or str(result_path(state, result)) != collected['path']
    ):
        raise ValueError('collected result changed or no longer belongs to this task')
    previous = workflow_name(Path(state['scriptPath']))
    following = workflow_name(script)
    if (
        not can_advance(previous, result)
        or previous == WORKFLOWS[2]
        or following != WORKFLOWS[WORKFLOWS.index(previous) + 1]
    ):
        raise ValueError(
            'only ready preparation -> optimization or converged/budget_exhausted optimization -> validation may advance'
        )
    original = copy.deepcopy(state['original_args'])
    for key in (
        *RECOVERY_FIELDS,
        'stage_reference_path',
        'preparation',
        'preparation_path',
        'optimization',
        'optimization_path',
    ):
        original.pop(key, None)
    if any(key in overrides for key in RECOVERY_FIELDS):
        raise ValueError('--from-state cannot supply recovery fields')
    original.update(overrides)
    identity = (
        ('repo_path', 'preparation_dir')
        if previous == WORKFLOWS[0]
        else ('repo_path', 'operator', 'run_dir')
    )
    for key in identity:
        if key in overrides and overrides[key] != result[key]:
            raise ValueError(f'override conflicts with collected task identity: {key}')
        original[key] = result[key]
    if previous == WORKFLOWS[0]:
        if (
            'check_revision' in overrides
            and overrides['check_revision'] != result['check_revision']
        ):
            raise ValueError('override conflicts with collected check_revision')
        original['check_revision'] = result['check_revision']
    field = 'preparation' if previous == WORKFLOWS[0] else 'optimization'
    for key, value in ((field, result), (field + '_path', collected['path'])):
        if key in overrides and overrides[key] != value:
            raise ValueError(f'override conflicts with collected {key}')
        original[key] = value
    other = 'optimization' if field == 'preparation' else 'preparation'
    if any(key in overrides for key in (other, other + '_path')):
        raise ValueError('cannot override unrelated stage results')
    for key in ('skill_dir', 'run_dir' if previous == WORKFLOWS[0] else 'validation_dir'):
        absolute(original.get(key), key)
    if previous == WORKFLOWS[0] and (
        not isinstance(original.get('operator'), str) or not original['operator'].strip()
    ):
        raise ValueError('operator is required for optimization')
    return original


def launch(directory, state):
    meta, body = script_parts(Path(state['scriptPath']))
    embedded = json.dumps(json.dumps(state['args'], ensure_ascii=True, allow_nan=False))
    script = meta + '\nreturn await (async function(args) {\n' + body
    script += '\n})(JSON.parse(' + embedded + '));\n'
    number = state.get('launch_count', 0) + 1
    while True:
        path = directory / f'launch-{number}.js'
        try:
            with path.open('x', encoding='utf-8') as stream:
                stream.write(script)
                stream.flush()
                os.fsync(stream.fileno())
            break
        except FileExistsError:
            number += 1
    state['launch_count'] = number
    state['launchPath'] = str(path)
    state['needs_record'] = True
    save(directory / 'state.json', state)
    print(json.dumps({'scriptPath': str(path)}, ensure_ascii=False))


def journal_path(state):
    remembered = state.get('journalPath')
    if remembered:
        path = Path(remembered)
        if not path.is_file():
            raise ValueError(f'recorded journal is absent: {path}')
        return path
    run_id = state['runId']
    matches = list(
        (Path.home() / '.claude' / 'projects').glob(
            f'*/*/subagents/workflows/{run_id}/journal.jsonl'
        )
    )
    matches = [path for path in matches if path.is_file()]
    if len(matches) != 1:
        raise ValueError(
            f'expected one journal for {run_id}, found {len(matches)}; '
            'record the run ID with --journal to select its journal'
        )
    return matches[0].resolve()


def reusable(result):
    return isinstance(result, dict) and result.get('status') not in RETRY_STATUSES


def recover(path, checkpoints, stage):
    if not isinstance(checkpoints, dict):
        raise ValueError('stage_results must be a JSON object')
    sequence = [{'label': label, 'result': result} for label, result in checkpoints.items()]
    seen = set(checkpoints)
    with path.open(encoding='utf-8') as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError as error:
                raise ValueError(f'invalid journal JSON at {path}:{number}') from error
            if not isinstance(event, dict):
                raise ValueError(f'invalid journal event at {path}:{number}')
            kind = event.get('type')
            if kind not in {'started', 'result'}:
                continue
            if any(
                not isinstance(event.get(key), str) or not event[key] for key in ('key', 'agentId')
            ):
                raise ValueError(f'journal event lacks key/agentId at {path}:{number}')
            identity = (event['key'], event['agentId'])
            if kind == 'started':
                label = event.get('label')
                if not isinstance(label, str) or not label:
                    raise ValueError(f'journal stage lacks a label at {path}:{number}')
                seen.add(label)
                for index, item in enumerate(sequence):
                    if item['label'] == label:
                        sequence = sequence[:index]
                        break
                sequence.append({'label': label, 'identity': identity})
            else:
                for item in reversed(sequence):
                    if item.get('identity') == identity:
                        item['result'] = event.get('result')
                        break
    if stage and stage not in seen:
        raise ValueError(f'unknown stage: {stage}; not present in checkpoints or journal starts')
    results = {}
    for item in sequence:
        if item['label'] == stage or not reusable(item.get('result')):
            break
        results[item['label']] = item['result']
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start', 'record', 'resume', 'collect'])
    parser.add_argument('--state-dir', required=True, type=Path)
    parser.add_argument('--script', type=Path)
    parser.add_argument('--args', type=Path)
    parser.add_argument('--from-state', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--run-id')
    parser.add_argument('--journal', type=Path)
    parser.add_argument('--stage')
    parser.add_argument('--note')
    parser.add_argument('--check-revision', type=int)
    options = parser.parse_args()
    directory = options.state_dir.resolve()
    path = directory / 'state.json'
    try:
        if options.action not in ('record', 'collect') and options.run_id is not None:
            parser.error('--run-id is only valid for record or collect')
        if options.action != 'record' and options.journal is not None:
            parser.error('--journal is only valid for record')
        if options.action != 'start' and options.from_state is not None:
            parser.error('--from-state is only valid for start')
        if options.action != 'collect' and options.output is not None:
            parser.error('--output is only valid for collect')
        if options.action != 'resume' and any(
            value is not None for value in (options.stage, options.note, options.check_revision)
        ):
            parser.error('--stage, --note, and --check-revision are only valid for resume')
        if options.action == 'start':
            if not options.script or (not options.args and not options.from_state):
                parser.error('start requires --script and either --args or --from-state')
            original = load(options.args) if options.args else {}
            if not isinstance(original, dict):
                parser.error('args must be a JSON object')
            if options.from_state:
                original = inherit(options.from_state.resolve(), options.script, original)
            else:
                field = {
                    'media-optimization-loop': 'preparation',
                    'media-validation': 'optimization',
                }.get(workflow_name(options.script))
                if field and field + '_path' in original:
                    result = load(Path(absolute(original[field + '_path'], field + '_path')))
                    if field in original and original[field] != result:
                        raise ValueError(f'{field} differs from {field}_path')
                    original[field] = result
            if not isinstance(original.get('stage_results', {}), dict):
                parser.error('stage_results must be a JSON object')
            if original.get('stage_results'):
                parser.error(
                    'start cannot supply stage_results; checkpoints are recovered from journals'
                )
            script_parts(options.script)
            script = options.script.read_bytes()
            reference = (
                Path(absolute(original.get('skill_dir'), 'skill_dir')) / 'references' / 'stages.md'
            )
            stage_rules = reference.read_bytes()
            directory.mkdir(parents=True, exist_ok=False)
            frozen = directory / 'workflow.js'
            frozen.write_bytes(script)
            frozen_rules = directory / 'stages.md'
            frozen_rules.write_bytes(stage_rules)
            original['stage_reference_path'] = str(frozen_rules)
            state = {
                'scriptPath': str(frozen),
                'original_args': original,
                'args': copy.deepcopy(original),
                'runId': None,
            }
        else:
            state = load(path)
            if options.action == 'collect':
                if not options.output or not options.run_id:
                    parser.error('collect requires --output and --run-id')
                collect(directory, state, options.output, options.run_id)
                return
            if options.action == 'record':
                if not options.run_id or not re.fullmatch(r'wf_[A-Za-z0-9_-]+', options.run_id):
                    parser.error('record requires a workflow run ID')
                if (state.get('needs_record') and options.run_id == state.get('runId')) or (
                    options.run_id != state.get('runId')
                    and (directory / f'result-{options.run_id}.json').exists()
                ):
                    parser.error('record a new workflow run ID for the latest launch')
                journal = (
                    options.journal.expanduser().resolve() if options.journal is not None else None
                )
                if journal is not None and (
                    journal.name != 'journal.jsonl' or journal.parent.name != options.run_id
                ):
                    parser.error(
                        '--journal must be the journal.jsonl inside the recorded run ID directory'
                    )
                if options.run_id != state.get('runId'):
                    state.pop('journalPath', None)
                state['runId'] = options.run_id
                if journal is not None:
                    state['journalPath'] = str(journal)
                state['needs_record'] = False
                save(path, state)
                return
            if not state.get('runId') or state.get('needs_record', False):
                parser.error('record the latest launch workflow run ID before resuming')
            if not options.stage:
                parser.error('resume requires --stage to identify the stage to retry')
            frozen = Path(state['scriptPath'])
            if (workflow_name(frozen), workflow_schema(frozen)) in (
                (WORKFLOWS[1], 4),
                (WORKFLOWS[2], 2),
            ):
                parser.error('旧源码快照任务不能按 worktree 契约恢复；保留旧产物并新建任务')
            if options.check_revision is not None:
                if options.stage != 'media:prepare:Environment' or options.check_revision <= state[
                    'args'
                ].get('check_revision', 1):
                    parser.error('increase check_revision only for media:prepare:Environment')
            journal = journal_path(state)
            results = recover(journal, state['args'].get('stage_results', {}), options.stage)
            state['journalPath'] = str(journal)
            state['args']['stage_results'] = results
            if options.check_revision is not None:
                state['args']['check_revision'] = options.check_revision
            if options.stage:
                retries = state['args'].setdefault('stage_retries', {})
                retries[options.stage] = retries.get(options.stage, 0) + 1
                if options.note is not None:
                    state['args'].setdefault('stage_notes', {})[options.stage] = options.note
        launch(directory, state)
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
