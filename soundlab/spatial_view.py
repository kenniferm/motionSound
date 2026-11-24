"""Browser payload for the spatial view of a saved render."""

import json
import math


def trajectory_data(request, descriptions):
    rate = request['sample_rate']
    if type(rate) is not int or rate <= 0:
        raise ValueError('Invalid sample rate')
    sources = []
    for event in request['events']:
        source_id = event['id']
        start, frames = event['start_frame'], event['frames']
        if type(start) is not int or type(frames) is not int or start < 0 or frames < 1:
            raise ValueError('Invalid event interval')
        keys = []
        previous = -1
        for key in event['keys']:
            frame, direction, distance = key['frame'], key['vector'], key['distance']
            if (type(frame) is not int or not previous < frame < frames
                    or len(direction) != 3 or not all(map(math.isfinite, direction))
                    or not math.isclose(sum(x*x for x in direction), 1, abs_tol=1e-5)
                    or not math.isfinite(distance) or distance <= 0):
                raise ValueError('Invalid trajectory key')
            keys.append([frame, *direction, distance])
            previous = frame
        if not keys:
            raise ValueError('Missing trajectory keys')
        label = descriptions.get(source_id) or source_id
        source = {'id': source_id, 'label': label, 'start': start,
                  'frames': frames, 'keys': keys}
        if 'formation' in event:
            source['formation'] = event['formation']
        sources.append(source)
    return {'rate': rate, 'sources': sources}


def source_descriptions(path):
    try:
        plan = json.loads(path.read_text()).get('candidate_plan') or {}
        return {**{event['id']: event.get('role') or event.get('audible_change')
                   for event in plan.get('events', []) if 'id' in event},
                **plan.get('source_descriptions', {})}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}
