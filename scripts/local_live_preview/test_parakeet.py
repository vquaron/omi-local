"""Wire/lifecycle tests with synthetic text; Core ML is exercised by local replay."""
import asyncio
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from .serve_parakeet import Worker, create_app


async def wait_event(event):
    await asyncio.wait_for(event.wait(), 2)


class FakeWorker:
    def __init__(self):
        self.process = SimpleNamespace(returncode=None)
        self.messages = asyncio.Queue()
        self.starts = 0
        self.loads = 0
        self.closes = 0
        self.closed = False
        self.text = ''
        self.reloaded = asyncio.Event()

    async def start(self):
        self.loads += 1
        self.process = SimpleNamespace(returncode=None)
        self.messages = asyncio.Queue()
        if self.loads > 1:
            self.reloaded.set()

    async def send(self, kind, pcm=b''):
        if kind == 1:
            self.starts += 1
            self.text = ''
            await self.messages.put({'type': 'started'})
        elif kind == 2:
            self.text += ' phrase'
            await self.messages.put({'type': 'snapshot', 'text': self.text})
        elif kind == 3:
            await self.messages.put({'type': 'finished'})

    async def read(self):
        return await self.messages.get()

    async def close(self):
        self.closes += 1
        self.closed = True
        self.process.returncode = -9


class ParakeetWireTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.lock = Path(self.directory.name) / 'lock'
        self.lock.touch()
        self.worker = FakeWorker()
        self.events = []
        self.client = TestClient(create_app(self.worker, self.lock,
                                           lambda event, **fields: self.events.append({'event': event, **fields})))

    def test_updates_stop_and_fresh_session_reuse_loaded_worker(self):
        with self.client as client:
            for _ in range(2):
                with client.websocket_connect('/asr') as ws:
                    config = ws.receive_json()
                    self.assertEqual(config['type'], 'config')
                    self.assertIs(config['diarization'], False)
                    self.assertEqual(config['stt_provider'], 'parakeet-local')
                    ws.send_bytes(b'\0\0')
                    first = ws.receive_json()['lines'][0]['text']
                    self.assertEqual(first, 'phrase')
                    ws.send_bytes(b'\0\0')
                    self.assertEqual(ws.receive_json()['lines'][0]['text'], first + ' phrase')
                    ws.send_bytes(b'')
                    self.assertEqual(ws.receive_json()['type'], 'ready_to_stop')
                health = client.get('/health').json()
                self.assertFalse(health['active'])
                self.assertFalse(health['diarization'])
                self.assertEqual(health['last']['outcome'], 'passed')
            self.assertEqual(self.worker.loads, 1)
            self.assertEqual(self.worker.starts, 2)

    def test_busy_and_disconnect_drain(self):
        with self.client as client:
            with client.websocket_connect('/asr') as first:
                first.receive_json()
                with client.websocket_connect('/asr') as second:
                    with self.assertRaises(WebSocketDisconnect) as error:
                        second.receive_json()
                    self.assertEqual(error.exception.code, 1013)
                first.send_bytes(b'\0\0')
                first.receive_json()
            # Session context waits for handler cleanup after disconnect.
            self.assertFalse(client.get('/health').json()['active'])
            self.assertFalse(self.worker.closed)

    def test_health_observes_final_lock_without_releasing_live_session_lock(self):
        with self.client as client:
            self.assertFalse(client.get('/health').json()['busy'])
            with self.lock.open('r+') as final_lock:
                fcntl.flock(final_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                health = client.get('/health').json()
                self.assertTrue(health['ready'])
                self.assertTrue(health['busy'])
                self.assertFalse(health['active'])
                with client.websocket_connect('/asr') as rejected:
                    with self.assertRaises(WebSocketDisconnect) as error:
                        rejected.receive_json()
                    self.assertEqual(error.exception.reason, 'final_transcription_busy')
            self.assertFalse(client.get('/health').json()['busy'])
            with client.websocket_connect('/asr') as ws:
                self.assertEqual(ws.receive_json()['type'], 'config')
                self.assertTrue(client.get('/health').json()['busy'])
                with self.lock.open('r+') as observer:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(observer, fcntl.LOCK_EX | fcntl.LOCK_NB)
                ws.send_bytes(b'')
                self.assertEqual(ws.receive_json()['type'], 'ready_to_stop')
            self.assertFalse(client.get('/health').json()['busy'])

    def test_invalid_pcm_reaps_worker_and_next_session_uses_fresh_model_state(self):
        with self.client as client:
            with client.websocket_connect('/asr') as ws:
                ws.receive_json()
                ws.send_bytes(b'\0')
                self.assertEqual(ws.receive_json()['type'], 'error')
            self.assertTrue(self.worker.closed)
            client.portal.call(wait_event, self.worker.reloaded)
            health = client.get('/health').json()
            self.assertTrue(health['ready'])
            self.assertFalse(health['active'])
            self.assertFalse(health['recovering'])
            with client.websocket_connect('/asr') as ws:
                self.assertEqual(ws.receive_json()['type'], 'config')
                ws.send_bytes(b'\0\0')
                self.assertEqual(ws.receive_json()['lines'][0]['text'], 'phrase')
                ws.send_bytes(b'')
                self.assertEqual(ws.receive_json()['type'], 'ready_to_stop')
            self.assertEqual(self.worker.loads, 2)
            self.assertEqual(self.worker.starts, 2)
            self.assertEqual(client.get('/health').json()['last']['outcome'], 'passed')

    def test_recovery_excludes_concurrent_sessions_and_final_inference(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original_start = self.worker.start

        async def delayed_start():
            if self.worker.loads:
                entered.set()
                await release.wait()
            await original_start()

        self.worker.start = delayed_start
        with self.client as client:
            with client.websocket_connect('/asr') as ws:
                ws.receive_json()
                ws.send_bytes(b'\0')
                self.assertEqual(ws.receive_json()['type'], 'error')
            client.portal.call(wait_event, entered)
            try:
                self.assertTrue(client.get('/health').json()['recovering'])
                with self.lock.open('r+') as observer:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(observer, fcntl.LOCK_EX | fcntl.LOCK_NB)
                for _ in range(2):
                    with client.websocket_connect('/asr') as ws:
                        with self.assertRaises(WebSocketDisconnect) as error:
                            ws.receive_json()
                        self.assertEqual(error.exception.code, 1013)
                        self.assertEqual(error.exception.reason, 'live_preview_recovering')
                self.assertEqual(self.worker.loads, 1)
            finally:
                client.portal.call(release.set)
            client.portal.call(wait_event, self.worker.reloaded)
            self.assertTrue(client.get('/health').json()['ready'])
            self.assertEqual(self.worker.loads, 2)

    def test_failed_reload_reports_unavailable_then_bounded_request_can_retry(self):
        failed = asyncio.Event()
        original_start = self.worker.start

        async def fail_reload():
            if self.worker.loads:
                failed.set()
                raise RuntimeError('private synthetic model path must not escape')
            await original_start()

        self.worker.start = fail_reload
        with self.client as client:
            with client.websocket_connect('/asr') as ws:
                ws.receive_json()
                ws.send_bytes(b'\0')
                self.assertEqual(ws.receive_json()['type'], 'error')
            client.portal.call(wait_event, failed)
            health = client.get('/health').json()
            self.assertFalse(health['ready'])
            self.assertFalse(health['recovering'])
            self.assertEqual(health['last_error'], 'model_start_failed')
            with client.websocket_connect('/asr') as ws:
                with self.assertRaises(WebSocketDisconnect) as error:
                    ws.receive_json()
                self.assertEqual(error.exception.reason, 'live_preview_unavailable')
            self.assertEqual(len([event for event in self.events if event['event'] == 'recovery_failed']), 1)
            self.assertNotIn('private synthetic', json.dumps(self.events))
            # No delay/sleep or repeated load loop: an elapsed cooldown permits
            # one future request to schedule recovery from an idle-death state.
            self.worker.start = original_start
            with patch('local_live_preview.serve_parakeet.time') as clock:
                clock.monotonic.return_value = float('inf')
                with client.websocket_connect('/asr') as ws:
                    with self.assertRaises(WebSocketDisconnect) as error:
                        ws.receive_json()
                    self.assertEqual(error.exception.reason, 'live_preview_recovering')
                client.portal.call(wait_event, self.worker.reloaded)
            with client.websocket_connect('/asr') as ws:
                self.assertEqual(ws.receive_json()['type'], 'config')
                ws.send_bytes(b'')
                self.assertEqual(ws.receive_json()['type'], 'ready_to_stop')

    def test_dead_idle_worker_recovery_respects_final_inference_lock(self):
        with self.client as client:
            self.worker.process.returncode = 1
            with self.lock.open('r+') as final_lock:
                fcntl.flock(final_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with client.websocket_connect('/asr') as ws:
                    with self.assertRaises(WebSocketDisconnect):
                        ws.receive_json()
                self.assertFalse(client.get('/health').json()['ready'])
                self.assertEqual(client.get('/health').json()['last_error'], 'inference_busy')
                self.assertEqual(self.worker.loads, 1)
                self.assertEqual(self.worker.closes, 0)
                with self.lock.open('r+') as observer:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(observer, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_shutdown_cancels_recovery_and_reaps_child_before_unlocking(self):
        entered = asyncio.Event()
        child = None
        reaped_under_lock = []
        original_start, original_close = self.worker.start, self.worker.close

        async def loading():
            nonlocal child
            if not self.worker.loads:
                return await original_start()
            child = await asyncio.create_subprocess_exec(
                sys.executable, '-c', 'import sys; sys.stdin.buffer.read()',
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self.worker.process = child
            entered.set()
            await asyncio.Event().wait()

        async def checked_close():
            if child is None:
                return await original_close()
            if child.returncode is not None:
                return
            with self.lock.open('r+') as observer:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(observer, fcntl.LOCK_EX | fcntl.LOCK_NB)
                await Worker.close(self.worker)
                self.assertIsNotNone(child.returncode)
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(observer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            reaped_under_lock.append(True)

        self.worker.start, self.worker.close = loading, checked_close
        with self.client as client:
            with client.websocket_connect('/asr') as ws:
                ws.receive_json()
                ws.send_bytes(b'\0')
                self.assertEqual(ws.receive_json()['type'], 'error')
            client.portal.call(wait_event, entered)
            self.assertTrue(client.get('/health').json()['recovering'])
        self.assertEqual(reaped_under_lock, [True])
        self.assertIsNotNone(child.returncode)
        with self.lock.open('r+') as observer:
            fcntl.flock(observer, fcntl.LOCK_EX | fcntl.LOCK_NB)


class StartupDiagnosticTests(unittest.TestCase):
    def test_real_main_uvicorn_lifespan_failure_is_reported_without_sensitive_details(self):
        script = '''
import sys
sys.path.insert(0, sys.argv[1])
from local_live_preview import serve_parakeet as module
async def fail(self):
    raise RuntimeError("private synthetic model path must not escape")
module.Worker.start = fail
sys.argv = ["serve_parakeet", "--worker", sys.executable, "--model-dir", sys.argv[2],
            "--inference-lock", sys.argv[3]]
raise SystemExit(module.main())
'''
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / 'lock'
            lock.touch()
            result = subprocess.run([sys.executable, '-c', script, str(Path(__file__).resolve().parents[1]),
                                     directory, str(lock)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stderr, b'')
        self.assertEqual([json.loads(line) for line in result.stdout.splitlines()], [{
            'event': 'startup_failed', 'reason': 'model_start_failed', 'outcome': 'failed',
            'action': 'verify_live_model_and_worker',
        }])

    def test_uvicorn_system_exit_without_lifespan_diagnostic_is_also_visible(self):
        script = '''
import sys
sys.path.insert(0, sys.argv[1])
import uvicorn
from local_live_preview import serve_parakeet as module
def fail(*args, **kwargs):
    raise SystemExit(1)
uvicorn.run = fail
sys.argv = ["serve_parakeet", "--worker", sys.executable, "--model-dir", sys.argv[2],
            "--inference-lock", sys.argv[3]]
raise SystemExit(module.main())
'''
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / 'lock'
            lock.touch()
            result = subprocess.run([sys.executable, '-c', script, str(Path(__file__).resolve().parents[1]),
                                     directory, str(lock)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, b'')
        self.assertEqual([json.loads(line) for line in result.stdout.splitlines()], [{
            'event': 'startup_failed', 'reason': 'server_start_failed', 'outcome': 'failed',
            'action': 'check_live_port_and_runtime',
        }])


class WorkerProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_snapshots_preserve_text_and_following_messages(self):
        worker = Worker(None, None)
        worker.process = await asyncio.create_subprocess_exec(
            sys.executable, '-c',
            'import json; '
            'print(json.dumps({"type":"snapshot","text":"synthetic речь "*12000}, ensure_ascii=False)); '
            'print(json.dumps({"type":"finished"})); '
            'print(json.dumps({"type":"snapshot","text":"fresh session"}))',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            message = await asyncio.wait_for(worker.read(), 5)
            self.assertEqual(message, {'type': 'snapshot', 'text': 'synthetic речь ' * 12000})
            self.assertEqual(await worker.read(), {'type': 'finished'})
            self.assertEqual(await worker.read(), {'type': 'snapshot', 'text': 'fresh session'})
            await worker.process.wait()
        finally:
            await worker.close()

    async def test_truncated_message_is_not_accepted_at_eof(self):
        worker = Worker(None, None)
        reader = asyncio.StreamReader()
        reader.feed_data(json.dumps({'type': 'snapshot', 'text': 'synthetic'}).encode())
        reader.feed_eof()
        worker.process = SimpleNamespace(stdout=reader)
        with self.assertRaisesRegex(RuntimeError, 'parakeet_worker_closed'):
            await worker.read()

    async def test_startup_failure_reaps_child_before_releasing_inference_lock(self):
        for error in (asyncio.TimeoutError, RuntimeError, asyncio.CancelledError):
            with self.subTest(error=error.__name__), tempfile.TemporaryDirectory() as directory:
                lock_path = Path(directory) / 'lock'
                lock_path.touch()
                checked = []
                case = self

                class FailingWorker(Worker):
                    async def start(self):
                        self.process = await asyncio.create_subprocess_exec(
                            sys.executable, '-c', 'import sys; sys.stdin.buffer.read()',
                            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        raise error('synthetic startup failure')

                    async def close(self):
                        with lock_path.open('r+') as observer:
                            with case.assertRaises(BlockingIOError):
                                fcntl.flock(observer, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            await super().close()
                            case.assertIsNotNone(self.process.returncode)
                            with case.assertRaises(BlockingIOError):
                                fcntl.flock(observer, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        checked.append(True)

                worker = FailingWorker(None, None)
                app = create_app(worker, lock_path, lambda *args, **kwargs: None)
                try:
                    with self.assertRaises(error):
                        async with app.router.lifespan_context(app):
                            self.fail('Startup must not succeed')
                    self.assertEqual(checked, [True])
                    with lock_path.open('r+') as observer:
                        fcntl.flock(observer, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    # Also reap the synthetic child when testing the broken code.
                    await Worker.close(worker)


if __name__ == '__main__':
    unittest.main()
