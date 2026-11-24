"""Logged structured model requests shared by the production workflows."""

import asyncio
import inspect
import json
import os
import time
from urllib.parse import quote
from typing import TypeVar

import httpx

from openai.lib._pydantic import to_strict_json_schema
from openai.types.responses import ResponseInputParam
from pydantic import BaseModel, ValidationError

from .cancellation import GenerationCancelled, check
from .store import write_json

Output = TypeVar('Output', bound=BaseModel)


def resolve_client(client):
    model = os.getenv('OPENAI_MODEL', '').strip()
    if not model:
        raise ValueError('OPENAI_MODELを.env.localで設定してください')
    if client is None:
        if not os.getenv('OPENAI_API_KEY'):
            raise ValueError('OPENAI_API_KEYを.env.localで設定してください')
    return model, client


def claude_model():
    if not os.getenv('ANTHROPIC_API_KEY', '').strip():
        raise ValueError('Claudeで生成するには.env.localにANTHROPIC_API_KEYを設定してください。')
    return os.getenv('ANTHROPIC_MODEL', '').strip() or 'claude-sonnet-5-5'


def resolve_provider(provider, client, saved_model=None):
    if provider == 'claude':
        configured = claude_model()
        return saved_model or configured, None
    return resolve_client(client)


def redact(message):
    for name in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY'):
        if key := os.getenv(name, '').strip():
            message = message.replace(key, '[REDACTED]')
    return message


async def await_reply(reply):
    task = asyncio.ensure_future(reply)
    try:
        while not task.done():
            check()
            await asyncio.wait({task}, timeout=.1)
        return task.result()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class IncompleteResponse(ValueError):
    pass


async def openai_reply(kwargs):
    from openai import AsyncOpenAI
    async with AsyncOpenAI(timeout=420 if kwargs['max_output_tokens'] > 10000 else 180, max_retries=0) as client:
        return await await_reply(client.responses.parse(**kwargs))


def claude_headers():
    return {'x-api-key': os.getenv('ANTHROPIC_API_KEY', '').strip(),
            'anthropic-version': '2023-06-01'}


async def claude_output_limit(model):
    async with httpx.AsyncClient(timeout=30) as client:
        response = await await_reply(client.get(
            'https://api.anthropic.com/v1/models/' + quote(model, safe=''),
            headers={**claude_headers(), 'anthropic-version': '2026-10-21'},
        ))
        response.raise_for_status()
        limit = response.json().get('max_tokens')
        if type(limit) is not int or limit <= 0:
            raise ValueError('Claudeモデルの出力上限を取得できませんでした')
        return limit


async def claude_reply(model, messages, schema, tokens, *, effort=None):
    body = {'model': model, 'system': messages[0]['content'],
            'messages': messages[1:], 'max_tokens': tokens, 'stream': True,
            'output_config': {'format': {'type': 'json_schema', 'schema': schema}}}
    if effort:
        body['output_config']['effort'] = effort
    chunks = []
    message = {}
    reason = None
    async with httpx.AsyncClient(timeout=90) as client:
        async with client.stream('POST', 'https://api.anthropic.com/v1/messages',
                                 headers=claude_headers(), json=body) as response:
            if response.is_error:
                await response.aread()
                raise ValueError(f'Claude HTTP {response.status_code}：{response.text[:1000]}')
            async for line in response.aiter_lines():
                if not line.startswith('data:'):
                    continue
                event = json.loads(line[5:])
                match event['type']:
                    case 'message_start':
                        message = event['message']
                    case 'content_block_start':
                        block = event['content_block']
                        if block['type'] == 'text':
                            chunks.append(block['text'])
                    case 'content_block_delta':
                        delta = event['delta']
                        if delta['type'] == 'text_delta':
                            chunks.append(delta['text'])
                    case 'message_delta':
                        reason = event['delta'].get('stop_reason')
                        message.setdefault('usage', {}).update(event.get('usage', {}))
                    case 'error':
                        raise ValueError(f"Claude：{event['error']['message']}")
                    case 'message_stop':
                        return message, reason, ''.join(chunks)
    raise IncompleteResponse('Claudeの応答が途中で終了しました。保存地点から再開できます。')


def request(client, model, version, folder, stage, system, payload,
            schema: type[Output], tokens, *, provider='openai') -> tuple[Output, dict]:
    messages: ResponseInputParam = [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False, separators=(',', ':'))},
    ]
    output_schema = to_strict_json_schema(schema)
    started = time.perf_counter()
    usage = None
    effort = 'medium' if provider == 'claude' and model == 'claude-sonnet-5-5' else None
    try:
        check()
        if provider == 'claude' and tokens is None:
            tokens = asyncio.run(claude_output_limit(model))
        write_json(folder / f'{stage}_request.json', {
            'version': version, 'provider': provider, 'model': model, 'input': messages,
            'max_output_tokens': tokens, 'output_schema': output_schema, 'strict': True,
            **({'store': False} if provider == 'openai' else {'stream': True}),
            **({'effort': effort} if effort else {}),
        })
        if provider == 'claude':
            async def read_claude():
                return await asyncio.wait_for(
                    await_reply(claude_reply(model, messages, output_schema, tokens, effort=effort)), timeout=600)

            message, reason, content = asyncio.run(read_claude())
            usage = message.get('usage')
            if reason == 'max_tokens':
                raise IncompleteResponse(f'{stage}の応答が出力上限に達しました。保存地点から再開できます。')
            if reason != 'end_turn':
                raise ValueError(f'Claudeの応答が完了しませんでした（{reason}）')
            parsed = schema.model_validate_json(content)
            reply_id = message['id']
            reply_model = message['model']
            incomplete = None
        else:
            kwargs = dict(model=model, store=False, input=messages, text_format=schema, max_output_tokens=tokens)
            if client is None:
                reply = asyncio.run(openai_reply(kwargs))
            else:
                reply = client.responses.parse(**kwargs)
                if inspect.isawaitable(reply):
                    reply = asyncio.run(await_reply(reply))
            parsed = reply.output_parsed
            reply_id = reply.id
            reply_model = getattr(reply, 'model', model)
            usage = reply.usage.model_dump() if reply.usage else None
            incomplete = getattr(getattr(reply, 'incomplete_details', None), 'reason', None)
    except Exception as exc:
        error = redact(str(exc))
        write_json(folder / f'{stage}_error.json', {
            'version': version, 'stage': stage, 'provider': provider, 'model': model,
            'elapsed_seconds': time.perf_counter()-started, 'error': error, 'usage': usage,
        })
        if isinstance(exc, (GenerationCancelled, IncompleteResponse)):
            raise
        if isinstance(exc, ValidationError) and any(
            item['type'] == 'json_invalid' for item in exc.errors(include_input=False)
        ):
            label = '制作設計' if stage == 'director' else stage
            raise IncompleteResponse(f'{label}の応答が不完全でした。保存地点から再開できます。') from exc
        raise ValueError(f'{stage}のAPI呼び出しに失敗しました：{error}') from exc
    result = {
        'version': version, 'provider': provider, 'id': reply_id, 'model': reply_model,
        'usage': usage,
        'output': parsed.model_dump() if parsed else None,
        'elapsed_seconds': time.perf_counter()-started,
    }
    write_json(folder / f'{stage}_response.json', result)
    if parsed is None:
        if incomplete == 'max_output_tokens':
            raise IncompleteResponse(f'{stage}の応答が出力上限に達しました。保存地点から再開できます。')
        raise ValueError(f'{stage}のAPI応答がありません')
    return parsed, result
