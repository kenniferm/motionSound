"""Selective revisions of the actual materials used in a finished render."""
import re

from . import ai, runner, structured_api
from .cancellation import check
from .spatial.work import validate_clips
from .store import read_json, write_json
from .room_delete import deletion_requested
from .sound_first import CODE_CONTRACT, Record, VERSION, provider_for_position, run_candidates, saved_code_response

FORMAT = 'selective-v1'
NO_EDITS = '変更対象がありません。指示を編集して、変えたい音を指定してください。'
EDIT_CODE_CONTRACT = CODE_CONTRACT.replace(
    'without external inputs or installs', 'with only the supplied clips as input and without installs')


class Edit(Record):
    id: str
    audio: bool
    motion: bool
    instruction: str


class EditPlan(Record):
    edits: list[Edit]


def source(root, run_id, duration, *, verify_audio=True):
    folder = root / 'runs' / run_id
    try:
        path = runner.safe_artifact(folder/'request.json', folder)
        request = read_json(path)
        result = read_json(runner.safe_artifact(folder/'result.json', folder))
        if request['duration_seconds'] != duration or request['sample_rate'] != 48000:
            raise ValueError('部分改善では元の作品と同じ長さを指定してください')
        if result['status'] != 'succeeded':
            raise ValueError('元の完成版が見つかりません')
        events = request['events']
        ids = {event['id'] for event in events}
        if (not events or len(ids) != len(events) or any(
            not re.fullmatch(r'[A-Za-z0-9_-]+', event['id'])
            or not 0 <= event['start_frame'] < event['start_frame'] + event['frames'] <= duration*48000
            or not event['keys'] or not 0 <= event['reverb'] <= 1 for event in events
        )):
            raise ValueError('元の素材の設定が不正です')
        hashes = result['event_hashes']
        if verify_audio:
            validate_clips(folder, events, hashes)
            return folder, events, hashes, runner.file_hash(path)
        clips = folder/'events'
        if clips.is_symlink() or hashes.keys() != ids:
            raise ValueError('元の完成版の個別素材または設定が見つかりません')
        for event_id in ids:
            runner.safe_artifact(clips/(event_id + '.npy'), clips)
        return None
    except (OSError, KeyError) as exc:
        raise ValueError('元の完成版の個別素材または設定が見つかりません') from exc


def materials(folder, events, hashes, ids):
    return folder, [event for event in events if event['id'] in ids], {
        key: value for key, value in hashes.items() if key in ids}


def generate(prompt, context, folder, target, client, save_candidate, positions):
    provider = provider_for_position(context, 0)
    resolved = None
    root = folder.parent.parent

    def check_active():
        check()
        if any(deletion_requested(root, rid) for rid in (folder.name, context['inline_room_id'])):
            raise ValueError('生成が削除されました')

    def call(stage, system, payload, schema, tokens):
        check_active()
        model, transport = resolved
        return structured_api.request(transport, model, VERSION + '-' + FORMAT,
                                      target, stage, system, payload, schema,
                                      tokens if provider == 'openai' else None, provider=provider)

    run_id = context['refinement_run_id']
    duration = context['render_context']['duration_seconds']
    check_active()
    _, events, hashes, digest = source(root, run_id, duration)
    parent_design = context.get('parent_design') or {}
    roles = {**parent_design.get('source_descriptions', {}), **{
        event['id']: event.get('audible_change') or event.get('role') or event['id']
        for event in parent_design.get('events', [])}}
    sources = [{**{key: event[key] for key in ('id', 'start_frame', 'frames')},
                'description': roles.get(event['id'], event['id'])} for event in events]
    plan_path = folder/'refinement_plan.json'
    identity = {'version': FORMAT, 'source_run_id': run_id,
                'request_sha256': digest, 'event_hashes': hashes}
    if plan_path.is_file():
        plan = read_json(plan_path)
        if any(plan.get(key) != value for key, value in identity.items()):
            raise ValueError('改善元の保存素材または設定が変更されています')
        edits = EditPlan.model_validate({'edits': plan['edits']}).edits
    else:
        resolved = structured_api.resolve_provider(provider, client, context.get('claude_model'))
        reply, _ = call('refinement', '''Resolve the user's selective edit against the supplied
finished work. List only clearly targeted source IDs. Set audio for waveform changes
and motion for placement, trajectory or reverb changes. Respect all explicit retention
requests. If target and protected sounds cannot be separated in one source, leave it
untouched. Omitted or ambiguous sources are retained. Use the user's language.''',
                        {'prompt': prompt, 'sources': sources, 'duration_seconds': duration,
                         'original_instruction': (context.get('ancestry_prompts') or [])[-1:]},
                        EditPlan, 10000)
        edits = reply.edits
    ids = {edit.id for edit in edits}
    if (len(ids) != len(edits) or not ids <= hashes.keys()
            or any(not (edit.audio or edit.motion) or not edit.instruction.strip() for edit in edits)):
        raise ValueError('編集計画の素材IDまたは変更内容が不正です')
    if not plan_path.is_file():
        write_json(plan_path, {**identity, 'edits': [edit.model_dump() for edit in edits]})
    if not edits:
        raise ValueError(NO_EDITS)

    audio = {edit.id for edit in edits if edit.audio}
    if audio and resolved is None:
        resolved = structured_api.resolve_provider(provider, client, context.get('claude_model'))

    def candidate(position):
        number = position + 1
        stage = f'code_{number}'
        try:
            if audio:
                saved = saved_code_response(folder, number)
                if saved is not None:
                    proposal = ai.Proposal.model_validate(saved['output'])
                else:
                    proposal, _ = call(stage, '''Create Python that creatively transforms or synthesizes
the requested editable dry sources. Input clips for those IDs are in input_dir/events.
Write exactly the editable IDs, for their specified frame counts. Preserve their timing.
Keep all spatial processing for the next stage.''',
                                       {'prompt': prompt, 'candidate_number': number,
                                        'edits': [e.model_dump() for e in edits if e.audio],
                                        'sources': [s for s in sources if s['id'] in audio],
                                        'duration_seconds': duration, 'sample_rate': 48000,
                                        'technical_contract': EDIT_CODE_CONTRACT},
                                       ai.Proposal, 24000)
                if not proposal.code.strip() or proposal.used_review_ids or proposal.used_research_ids:
                    raise ValueError('部分改善の生成コードまたは参照IDが不正です')
                compile(proposal.code, '<refinement>', 'exec')
            else:
                proposal = ai.Proposal(title=f'空間の改善 {number}', approach=prompt,
                                       code='', used_review_ids=[])
        except (ValueError, SyntaxError) as exc:
            write_json(target/f'candidate_{number}_error.json',
                       {'error': structured_api.redact(str(exc))})
            return None
        check_active()
        save_candidate(position, proposal, {
            'sonic_identity': proposal.approach,
            'source_descriptions': {s['id']: s['description'] for s in sources},
            'sound_events': [{k: e[k] for k in ('id', 'frames', 'start_frame')} for e in events],
            'retained': [s['description'] for s in sources if s['id'] not in ids],
            'edits': [{**edit.model_dump(), 'label': roles.get(edit.id, edit.id)} for edit in edits],
            'refinement': identity,
        })
        return proposal

    proposals = [p for p in run_candidates(candidate, positions) if p is not None]
    return ai.Batch(candidates=proposals), {'provider': provider, 'prompt_version': VERSION + '-' + FORMAT}
