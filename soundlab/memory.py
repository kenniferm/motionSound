"""Literal, project-scoped evidence retrieval; no inferred global preferences."""
import json
import re
import unicodedata


def terms(text):
    text = unicodedata.normalize('NFKC', text).lower()
    tokens = set(re.findall(r'[a-z0-9]{2,}', text))
    for word in re.findall(r'[ぁ-んァ-ヶ一-龯]+', text):
        tokens.update(word[i:i+2] for i in range(len(word)-1))
    return tokens


def context_for(store, project_id, parent_id, prompt='', playback_mode='unknown'):
    ancestry, seen, prompts = [], set(), []
    current = parent_id
    parent_code = None
    while current:
        if current in seen:
            raise ValueError('親子関係が循環しています')
        seen.add(current)
        candidate = store.one('''SELECT c.*,r.project_id,r.parent_id,r.prompt FROM candidates c
            JOIN rounds r ON r.id=c.round_id WHERE c.id=?''', (current,))
        if candidate['project_id'] != project_id:
            raise ValueError('別の作品の候補は親にできません')
        ancestry.append(current)
        prompts.append(candidate['prompt'])
        if current == parent_id:
            parent_code = (store.candidate_dir(current) / 'source.py').read_text()
        current = candidate['parent_id']
    reviews = store.rows('''SELECT v.*,r.prompt AS source_prompt FROM reviews v
        JOIN rounds r ON r.id=v.round_id WHERE r.project_id=?
        ORDER BY v.created DESC,v.id DESC''', (project_id,))
    query = terms(prompt)
    ranked = []
    latest_parent = next((r['id'] for r in reviews if r['candidate_id'] == parent_id), None)
    for review in reviews:
        listening = json.loads(review['context'])
        relevance = 3 * len(query & terms(review['text'])) + len(query & terms(review['source_prompt']))
        on_branch = review['candidate_id'] in ancestry
        if review['id'] != latest_parent and not on_branch and not relevance:
            continue
        priority = (review['id'] == latest_parent, relevance,
                    listening.get('playback_mode') == playback_mode, on_branch, review['created'], review['id'])
        reason = 'current_parent' if review['id'] == latest_parent else 'ancestry' if on_branch else 'project_relevant'
        ranked.append((priority, {'id': review['id'], 'candidate_id': review['candidate_id'],
            'text': review['text'], 'created': review['created'], 'context': listening,
            'run_id': review.get('run_id'),
            'source_prompt': review['source_prompt'], 'scope': 'project', 'retrieval_reason': reason}))
    selected = [review for _, review in sorted(ranked, key=lambda pair: pair[0], reverse=True)[:10]]
    return {'ancestry_prompts': list(reversed(prompts)), 'reviews': selected,
            'retrieved_review_ids': [r['id'] for r in selected], 'parent_code': parent_code,
            'memory_policy': 'current-parent-first, lexical-project-relevance, listening-context, ancestry; max 10; no global inference'}
