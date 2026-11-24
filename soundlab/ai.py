from pydantic import BaseModel, ConfigDict, Field


class Parameter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    value: float


class Proposal(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str
    approach: str
    code: str
    parameters: list[Parameter] = Field(default_factory=list)
    used_review_ids: list[str]
    hypothesis: str = ''
    used_research_ids: list[str] = Field(default_factory=list)


PROMPT_POLISH = """# Task
Polish the user's draft for an AI spatial-audio studio. Preserve its language, creative intent, explicit requirements, and protected elements. Clarify audible goals or requested changes only where supported by the draft.

# Output
Use clear everyday language from the listener's perspective. Leave room for interpretation; do not invent mandatory techniques, sounds, timings, or constraints. Return only the revised prompt in the supplied schema."""


class PolishedPrompt(BaseModel):
    model_config = ConfigDict(extra='forbid')
    prompt: str


IMPROVEMENT_PROMPT = """# Task
Read the final Python code and original brief. Suggest three distinct improvements the user could submit as production instructions, grounded in the code but described from the listener's perspective.

# Output
Use one or two short everyday sentences per proposal, with one audible change each. Mention what to retain only when useful. Refer to moments in plain language; avoid code identifiers, technical jargon, and exact timestamps unless required by the brief. Do not claim to have heard the audio. Use the brief's language and return only the three prompts in the supplied schema."""


class ImprovementSuggestions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    prompts: list[str]


class Batch(BaseModel):
    candidates: list[Proposal]
    project_title: str = ''


def repair_top_level_indent(proposal):
    """Repair one stray indent after a top-level line, only if the result compiles."""
    try:
        compile(proposal.code, '<generated>', 'exec')
    except IndentationError as error:
        lines = proposal.code.splitlines(keepends=True)
        line = error.lineno
        if error.msg != 'unexpected indent' or line is None or not 1 <= line <= len(lines):
            return
        previous = next((s for s in reversed(lines[:line-1]) if s.strip()), '')
        if previous != previous.lstrip(' \t'):
            return
        lines[line-1] = lines[line-1].lstrip(' \t')
        repaired = ''.join(lines)
        try:
            compile(repaired, '<generated>', 'exec')
        except SyntaxError:
            return
        proposal.code = repaired
        return line
    except SyntaxError:
        return


def validate_proposal(proposal, review_ids, number):
    try:
        compile(proposal.code, '<generated>', 'exec')
    except SyntaxError as error:
        raise ValueError(
            f'AIが生成した候補{number}のPythonコードに構文エラーがあります'
            f'（{error.lineno}行目：{error.msg}）。保存地点から再開できます。'
        ) from error
    if not proposal.title.strip() or not proposal.approach.strip():
        raise ValueError('候補の説明がありません')
    if set(proposal.used_review_ids) - set(review_ids):
        raise ValueError('送信していない評価IDが引用されました')
    if proposal.used_research_ids:
        raise ValueError('送信していない研究ノートIDが引用されました')
    if not proposal.hypothesis.strip():
        proposal.hypothesis = proposal.approach


