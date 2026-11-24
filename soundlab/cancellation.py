"""Cooperative cancellation for work owned by one background job."""
from contextvars import ContextVar


current = ContextVar('generation_cancellation', default=None)


class GenerationCancelled(Exception):
    def __init__(self):
        super().__init__('生成を中止しました')


def check():
    event = current.get()
    if event is not None and event.is_set():
        raise GenerationCancelled()
