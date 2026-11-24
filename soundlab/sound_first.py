"""Plan and synthesize the source events for a finished spatial work."""

import re
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Lock

from pydantic import BaseModel, ConfigDict

from . import ai, structured_api
from .cancellation import check
from .room_delete import deletion_requested
from .store import read_json, uid, write_json

VERSION = 'sound-first-v0.3.5'
DEFAULT_COUNT = 3
MAX_COUNT = 5

DIRECT = """# Role
You are an experimental sound artist, composer, and researcher in mathematics, physics, computational science, psychoacoustics, and DSP.

# Objective
Translate the user's full creative intention directly into a compelling sound artwork through executable Python. Own the conception, synthesis, composition, and implementation.

# Creative Direction
- Treat each request as a new artistic problem. Invent or choose any processes, systems, algorithms, and interactions through which sound emerges and evolves.
- When the brief invites experimentation, take substantial creative risks. Explore unfamiliar sonic behavior and challenge established listening conventions rather than defaulting to textbook solutions or familiar effects.
- Make the intended phenomena audible, not merely mathematical or verbal. Continuous processes, discrete events, evolving systems, and other approaches are equally valid.
- Let the user's intention determine the aesthetic, complexity, sound palette, structure, and number of components. Neither simplicity nor complexity is mandatory; originality need not be loud, noisy, or chaotic.

# Quality
Create sound with the precision, sonic craftsmanship, distinctive identity, and artistic substance of a finished professional artwork, not a technical demonstration. Develop texture, spectral detail, articulation, dynamics, clarity, and temporal behavior wherever relevant. Professional quality must not domesticate originality. Computational sophistication alone is not sonic sophistication.

# Responsibilities
- Preserve the user's explicit requirements and protected elements. Reconsider your approach before finalizing if the implementation weakens any essential aspect of the brief.
- Make the dry sound artistically compelling in its own right. Leave spatial placement to the spatial composer. Export sounds meant to move independently as separate events. When a single perceptual structure needs spatial extent or deformation, use separately placeable, complementary materials for its parts. Sounds that belong at the same position may share an event.

# Output
Return complete executable sound-generation code and source metadata according to the supplied technical contract and DirectProposal schema. Do not claim to have heard or perceptually verified audio you have not heard."""

DIRECTOR = """# Role
You are an experimental composer, sound artist, and interdisciplinary researcher.

# Objective
Develop the requested number of genuinely distinct artistic interpretations of the user's full creative intention.

# Creative Direction
- Explore what the idea could become as an audible phenomenon before choosing a synthesis method or structure.
- Draw freely on mathematics, physics, computation, musical knowledge, or original inventions. When appropriate, take creative risks and challenge familiar sonic conventions.
- Do not prescribe a fixed aesthetic, complexity, sound palette, component count, or temporal structure. Avoid prematurely narrowing the brief or introducing needless complexity.

# Quality and Responsibilities
Define each concept's distinctive audible qualities, artistic logic, and intended temporal behavior. Aim for compelling dry sound with artistic substance, not a technical demonstration. Give the downstream agent enough direction to work while leaving room for invention and reinterpretation. Leave spatial placement to the spatial composer. Plan separate, complementary events where independent motion or the internal extent of a spatial structure matters. Respect explicit requirements and protected elements.

# Output
Follow the supplied technical contract and Plan schema. Write the plan in the language of the user's instruction."""

CODE = """# Role
You are an experimental sound artist, computational composer, and DSP researcher.

# Objective
Create executable Python that realizes the user's creative intention. Treat the Director's plan as a creative foundation, not the final artistic authority.

# Creative Direction
- Independently invent, choose, or refine synthesis algorithms, mathematical or physical systems, and compositional mechanisms. Improve or reinterpret the plan when this better serves the original brief, within the supplied event contract.
- When experimentation is invited, take meaningful creative risks rather than defaulting to familiar sounds or textbook methods.
- Realize the intended phenomena in the actual signal. Avoid reducing distinct phenomena to one mechanism without artistic reason, or adding complexity merely for its own sake.
- No predetermined aesthetic, synthesis method, sound palette, complexity, or musical structure is mandatory.

# Quality
Aim for the precision, texture, spectral detail, articulation, dynamics, clarity, and artistic identity of a finished sound artwork, as appropriate to the brief. Professional quality is not conventionality. Mathematical sophistication is valuable only when it serves the audible result.

# Responsibilities
Respect explicit requirements and protected elements. Generate dry sound and preserve each supplied event as a separate component for spatial placement. Before returning, reconsider whether any essential auditory qualities have been weakened or unnecessarily simplified.

# Output
Return complete executable code according to the supplied technical contract and Proposal schema. Do not claim to have heard or perceptually verified audio you have not heard."""

EVENT_CONTRACT = (
    "Give each event a unique 1–40 character ID "
    "(ASCII letters, digits, underscores or hyphens) and start/end seconds within the requested duration. "
    "Each event must span at least two samples. Reference only supplied review IDs."
)
CODE_CONTRACT = (
    "Read the JSON file at sys.argv[1] and use its seed for randomness. "
    "For each supplied event write exactly frames dry mono samples as finite float32 or float64 "
    "in [-1, 1] to output_dir/events/<id>.npy. The app mixes the dry clips for preview. "
    "Use installed packages offline without external inputs or installs. Write only within output_dir. "
    "Reference only supplied review IDs and research IDs."
)


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Event(Record):
    id: str
    time_seconds: float
    end_seconds: float
    audible_change: str
    role: str


class Direction(Record):
    generation_principle: str
    state_evolution: str
    audible_mapping: str
    sonic_identity: str
    time_structure: str
    events: list[Event]
    component_roles: list[str]
    failure_and_check: str


class DirectProposal(ai.Proposal):
    events: list[Event]


class Plan(Record):
    perceptual_goal: str
    candidate_directions: list[Direction]
    used_review_ids: list[str]


def compile_events(events, duration):
    compiled = []
    ids = set()
    for event in events:
        if not (re.fullmatch(r'[A-Za-z0-9_-]{1,40}', event.id) and event.id not in ids
                and 0 <= event.time_seconds < event.end_seconds <= duration and event.audible_change.strip() and event.role.strip()):
            raise ValueError('発音イベントのID・時刻・説明が不正です')
        ids.add(event.id)
        start = round(event.time_seconds * 48000)
        frames = round(event.end_seconds * 48000) - start
        if frames < 2:
            raise ValueError('発音イベントは2サンプル以上必要です')
        compiled.append({'id': event.id, 'start_frame': start, 'frames': frames})
    if not compiled:
        raise ValueError('発音イベントがありません')
    return compiled


def run_candidates(action, positions):
    """Run independent candidates with the current job's cancellation context."""
    if len(positions) <= 1:
        return [action(position) for position in positions]
    with ThreadPoolExecutor(max_workers=min(MAX_COUNT, len(positions))) as pool:
        futures = [pool.submit(copy_context().run, action, position) for position in positions]
        return [future.result() for future in futures]


def generation_groups(context):
    count = context.get('candidate_count') or DEFAULT_COUNT
    if context.get('compare_claude'):
        half = count // 2
        return [('openai', 0, half), ('claude', half, half)]
    return [(context.get('provider', 'openai'), 0, count)]


def group_for_position(context, position):
    return next(group for group in generation_groups(context)
                if group[1] <= position < group[1] + group[2])


def provider_for_position(context, position):
    if not context.get('compare_claude'):
        return context.get('provider', 'openai')
    return group_for_position(context, position)[0]


def candidate_positions(context, saved_positions):
    return set(range(context.get('candidate_count') or 0)) | set(saved_positions)


def position_within_provider(context, position):
    return position - group_for_position(context, position)[1]


def director_stage(provider, context):
    return 'director_claude' if provider == 'claude' and context.get('compare_claude') else 'director'


def plan_filename(provider, context):
    return 'production_plan_claude.json' if director_stage(provider, context) == 'director_claude' else 'production_plan.json'


def candidate_attempts(folder):
    retries = sorted((folder/'retries').glob('*'), key=lambda path: path.stat().st_mtime_ns)
    return [folder, *retries]


def saved_code_response(folder, number):
    for attempt in reversed(candidate_attempts(folder)):
        if (attempt/f'candidate_{number}_error.json').is_file():
            break
        response = attempt/f'code_{number}_response.json'
        if response.is_file():
            return read_json(response)


def generate(prompt, context, folder, client=None, *, save_candidate, positions=None, retry=False):
    target = folder
    if retry:
        target = folder / 'retries' / uid()
        target.mkdir(parents=True)
    if context.get('selective_refinement'):
        from . import refinement
        selected = range(context['candidate_count']) if positions is None else sorted(positions)
        return refinement.generate(prompt, context, folder, target, client, save_candidate, selected)
    groups = generation_groups(context)

    def generate_group(group):
        provider, start, count = group
        selected = [index for index in range(start, start + count) if positions is None or index in positions]
        if not selected:
            return ai.Batch(candidates=[]), {}
        try:
            return _generate_group(prompt, context, folder, target, client, save_candidate, selected,
                                   provider=provider, start=start, count=count, retry=retry)
        except ValueError as exc:
            if len(groups) == 1:
                raise
            for index in selected:
                path = target/f'candidate_{index+1}_error.json'
                if not path.exists():
                    write_json(path, {'error': structured_api.redact(str(exc))})
            return ai.Batch(candidates=[]), {'provider': provider, 'error': structured_api.redact(str(exc))}

    results = run_candidates(generate_group, groups)
    if len(groups) == 1:
        return results[0]
    return ai.Batch(candidates=[p for batch, _ in results for p in batch.candidates],
                    project_title=next((b.project_title for b, _ in results if b.project_title), '')), {
        'prompt_version': VERSION, 'providers': {group[0]: metadata
                                                for group, (_, metadata) in zip(groups, results)},
    }


def _generate_group(prompt, context, folder, target, client, save_candidate, selected, *,
                    provider, start, count, retry):
    resolved = None
    lock = Lock()
    root = folder.parent.parent
    owner = context.get('inline_room_id', folder.name)

    def cancelled():
        check()
        return deletion_requested(root, folder.name) or deletion_requested(root, owner)

    def call(stage, system, payload, schema, tokens):
        if cancelled():
            raise ValueError('生成が削除されました')
        nonlocal resolved
        with lock:
            if resolved is None:
                resolved = structured_api.resolve_provider(provider, client, context.get('claude_model'))
            model, transport = resolved
        return structured_api.request(transport, model, VERSION, target, stage,
                                      system, payload, schema, tokens, provider=provider)

    duration = context['render_context']['duration_seconds']
    reviews = context['reviews'][:3]
    review_ids = [review['id'] for review in reviews]
    parent_design = context.get('parent_design') or {}
    common = {'prompt': prompt, 'duration_seconds': duration, 'candidate_count': count,
              'sample_rate': 48000, 'reviews': reviews,
              'parent_design': {key: parent_design[key] for key in Direction.model_fields if key in parent_design}}
    # Earlier projects embedded trajectories in sound events. Strip them at the new stage boundary.
    if 'events' in common['parent_design']:
        common['parent_design']['events'] = [
            {key: event[key] for key in Event.model_fields if key in event}
            for event in parent_design['events']
        ]
    direct = context.get('sound_generation_mode') == 'direct'
    director = {'model': None, 'usage': None}
    if not direct:
        plan_path = folder / plan_filename(provider, context)
        if retry and plan_path.is_file():
            plan = Plan.model_validate_json(plan_path.read_text())
        else:
            plan, director = call(director_stage(provider, context), DIRECTOR, {**common, 'technical_contract': 'Return exactly candidate_count directions. ' + EVENT_CONTRACT}, Plan, None if provider == 'claude' else 10000)
        if (len(plan.candidate_directions) != count or not plan.perceptual_goal.strip()
                or not set(plan.used_review_ids) <= set(review_ids)):
            raise ValueError(f'制作計画には{count}候補と有効な参照IDが必要です')
        summaries = []
        compiled = []
        for direction in plan.candidate_directions:
            fields = direction.model_dump(exclude={'events', 'component_roles'})
            if any(not value.strip() for value in fields.values()) or any(not role.strip() for role in direction.component_roles):
                raise ValueError('原音の制作計画に空欄があります')
            compiled.append(compile_events(direction.events, duration))
            summaries.append({k: fields[k] for k in ('generation_principle', 'sonic_identity', 'time_structure')})
        if not plan_path.is_file():
            write_json(plan_path, plan.model_dump())
    responses = [None] * count
    seen_codes = set()
    def generate_candidate(index):
        if cancelled():
            return None
        try:
            number = start + index
            code_stage = f'code_{number}'
            schema = DirectProposal if direct else ai.Proposal
            response = saved_code_response(folder, number) if retry else None
            if response is not None:
                proposal = schema.model_validate(response['output'])
            elif direct:
                proposal, response = call(code_stage, DIRECT, {
                    **{key: value for key, value in common.items() if key != 'parent_design'},
                    'candidate_number': index, 'parent_code': context.get('parent_code'),
                    'parent_instructions': context.get('ancestry_prompts', []),
                    'sent_review_ids': review_ids,
                    'technical_contract': EVENT_CONTRACT + " As this candidate, define your own event list alongside the code. "
                    "The app converts start/end seconds into event frames before execution. " + CODE_CONTRACT,
                }, DirectProposal, None if provider == 'claude' else 24000)
            else:
                direction = plan.candidate_directions[index-1]
                proposal, response = call(code_stage, CODE, {
                    **common, 'technical_contract': CODE_CONTRACT, 'candidate_number': index, 'candidate_plan': direction.model_dump(),
                    'other_candidates': [item for i, item in enumerate(summaries, 1) if i != index],
                    'perceptual_goal': plan.perceptual_goal, 'parent_code': context.get('parent_code'),
                    'sent_review_ids': review_ids, 'sent_research_ids': [], 'events': compiled[index-1],
                }, ai.Proposal, None if provider == 'claude' else 10000)
            if direct:
                metadata = {'sonic_identity': proposal.approach,
                            'events': [event.model_dump() for event in proposal.events],
                            'sound_events': compile_events(proposal.events, duration)}
            else:
                metadata = {**plan.candidate_directions[index-1].model_dump(), 'sound_events': compiled[index-1]}
            responses[index-1] = response
            if line := ai.repair_top_level_indent(proposal):
                write_json(target / f'{code_stage}_repair.json', {'line': line, 'candidate': number})
            ai.validate_proposal(proposal, review_ids, index)
            with lock:
                if proposal.code in seen_codes:
                    raise ValueError('他の候補とPythonコードが重複しています')
                seen_codes.add(proposal.code)
        except ValueError as exc:
            error = structured_api.redact(str(exc))
            write_json(target / f'candidate_{start + index}_error.json', {'error': error})
            return None
        if cancelled():
            return None
        save_candidate(start+index-1, proposal, metadata)
        return start+index, proposal

    local_indices = [position - start + 1 for position in selected]
    complete = [result for result in run_candidates(generate_candidate, local_indices) if result is not None]
    candidate_numbers = [index for index, _ in complete]
    proposals = [proposal for _, proposal in complete]
    first_response = next((response for response in responses if response), director)
    return ai.Batch(candidates=proposals, project_title=proposals[0].title if proposals else ''), {
        'provider': provider, 'model': first_response['model'], 'requested_model': resolved[0] if resolved else None,
        'director_model': director['model'], 'prompt_version': VERSION, 'codegen_mode': 'direct' if direct else 'separate',
        'candidate_numbers': candidate_numbers,
        'code_models': [result['model'] if result else None for result in responses],
        'stage_usage': [director['usage'], *[result['usage'] if result else None for result in responses]],
        'sent_review_ids': review_ids, 'sent_research_ids': [],
    }
