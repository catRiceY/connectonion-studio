"""Isolated browser acceptance for Prompt CRUD. No models, Agents or real files.

PYTHONPATH=. .venv/bin/python tests/manual/prompt_qa_app.py
The temporary registry is deleted on exit; all non-Prompt writes are forbidden.
"""
import os
from pathlib import Path
import socket
import signal
import tempfile
from unittest.mock import patch


def forbidden(*args, **kwargs):
    raise RuntimeError('Browser QA forbids outbound connections')


def terminate(*args):
    # Uvicorn replays SIGTERM after shutdown; unwind TemporaryDirectory as well.
    raise SystemExit(0)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, terminate)
    with tempfile.TemporaryDirectory(prefix='studio-prompt-qa-') as temporary:
        root = Path(temporary)
        with patch.object(Path, 'home', return_value=root), patch.dict(os.environ, {'AGENT_CONFIG_PATH': str(root / '.co')}), patch.object(socket.socket, 'connect', forbidden), patch.object(socket.socket, 'connect_ex', forbidden):
            from co_studio import config, creator, registry, setup_check, update_check
            from co_studio.app import create_app
            from fastapi.responses import JSONResponse
            import uvicorn

            config.STUDIO_PORT = 9903
            config.save_appearance('lavender')
            setup_check.status = lambda: {
                'co_auth_ok': True, 'key_ok': True, 'framework_ok': True,
                'studio_version': 'isolated-qa', 'agents_dir': str(config.AGENTS_DIR),
                'manager_url': 'http://127.0.0.1:9903', 'doctor': [],
            }
            update_check.check = lambda: {'update_available': False, 'current': 'isolated-qa'}
            directory = config.AGENTS_DIR / 'prompt-qa'
            directory.mkdir(parents=True)
            registry.save(registry.AgentMeta(slug='prompt-qa', name='Prompt QA · isolated', address='0xsynthetic',
                port=8099, model='offline', capabilities=[], created_at='2026-10-28T00:00:00Z'))
            (directory / 'agent.py').write_text(creator.render('Prompt QA', 'offline', 8099, [], 'open'), encoding='utf-8')
            app = create_app()

            @app.middleware('http')
            async def writes_only_to_prompts(request, call_next):
                if request.method not in {'GET', 'HEAD'} and request.url.path not in {
                    '/api/agents/prompt-qa/prompts', '/api/agents/prompt-qa/prompts/restore',
                }:
                    return JSONResponse({'detail': 'Only isolated Prompt writes are permitted.'}, status_code=403)
                return await call_next(request)

            print(f'Isolated Prompt QA: {root}', flush=True)
            uvicorn.run(app, host='127.0.0.1', port=9903, lifespan='off', log_level='warning')
