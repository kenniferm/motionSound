"""Versioned question and answer IDs for listening reviews."""
VERSION = 1
QUESTIONS = {
    'motion_clarity': ('動きはどれくらい追えましたか？', {
        'clear': 'はっきり追えた', 'partial': '一部だけ追えた',
        'unclear': '追えなかった', 'no_motion': '動きは感じなかった', 'unsure': '判断できない',
    }),
    'location_clarity': ('音が鳴った位置は分かりましたか？', {
        'clear': 'はっきり分かった', 'partial': '一部だけ分かった',
        'unclear': '分からなかった', 'unsure': '判断できない',
    }),
    'shape_change': ('音の形の変化はどう聴こえましたか？', {
        'same_object': '同じ音が変形した', 'different_object': '別の音に切り替わった',
        'no_change': '変化は感じなかった', 'unsure': '判断できない',
    }),
    'externalization': ('音はどこにあるように聴こえましたか？', {
        'inside_head': '頭の中', 'near_head': '頭の近く', 'outside_head': '頭の外',
        'mixed': '途中で変わった', 'unsure': '判断できない',
    }),
    'preference': ('この音は好みですか？', {
        'liked': '好き', 'neutral': 'どちらでもない', 'disliked': '好みではない', 'unsure': '判断できない',
    }),
}
PROFILES = {
    'production': ('motion_clarity', 'shape_change', 'preference'),
    'spatial_motion': ('motion_clarity', 'externalization', 'preference'),
    'spatial_static': ('location_clarity', 'externalization', 'preference'),
}


def validate_questionnaire(value):
    if not isinstance(value, dict) or set(value) != {'version', 'profile', 'answers'}:
        raise ValueError('質問票の形式が不正です')
    profile = value['profile']
    answers = value['answers']
    if (type(value['version']) is not int or value['version'] != VERSION
            or not isinstance(profile, str) or profile not in PROFILES
            or not isinstance(answers, dict) or not answers
            or any(key not in PROFILES[profile] or not isinstance(answer, str)
                   or answer not in QUESTIONS[key][1] for key, answer in answers.items())):
        raise ValueError('質問票の回答が不正です')
