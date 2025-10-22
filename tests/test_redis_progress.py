import uuid
import importlib

from app import set_progress, get_progress_data


class FakeRedis:
    """Minimal fake Redis to support hset, pipeline, and hgetall used by set_progress/get_progress_data."""
    def __init__(self):
        self.store = {}
        self._pipeline_buffer = None

    def pipeline(self):
        self._pipeline_buffer = {}
        return self

    def hset(self, key, mapping=None):
        if self._pipeline_buffer is not None:
            # store into pipeline buffer
            self._pipeline_buffer[key] = {k: str(v) for k, v in mapping.items()}
        else:
            # direct write
            self.store[key] = {k: str(v) for k, v in mapping.items()}

    def execute(self):
        # commit pipeline buffer
        for key, mapping in (self._pipeline_buffer or {}).items():
            self.store[key] = mapping
        self._pipeline_buffer = None

    def hgetall(self, key):
        m = self.store.get(key)
        if not m:
            return {}
        # return bytes as real redis client would
        return {k.encode(): v.encode() for k, v in m.items()}


class FailingPipelineRedis(FakeRedis):
    def execute(self):
        # Simulate a pipeline failure: clear buffer to mimic pipeline reset, then raise
        self._pipeline_buffer = None
        raise Exception('pipeline failed')

    def hgetall(self, key):
        m = self.store.get(key)
        if not m:
            return {}
        # return bytes as real redis client would
        return {k.encode(): v.encode() for k, v in m.items()}


def test_set_progress_redis_pipeline(monkeypatch):
    app_mod = importlib.import_module('app.app')
    fake = FakeRedis()
    monkeypatch.setattr(app_mod, 'redis_conn', fake)

    key = str(uuid.uuid4())
    set_progress(key, percent=55, csv_filename='a.csv', done=True)
    data = get_progress_data(key)
    assert data['percent'] == 55
    assert data['done'] is True
    assert data['csv_filename'] == 'a.csv'
    assert data.get('error') is None


def test_set_progress_redis_error_struct(monkeypatch):
    app_mod = importlib.import_module('app.app')
    fake = FakeRedis()
    monkeypatch.setattr(app_mod, 'redis_conn', fake)

    key = str(uuid.uuid4())
    set_progress(key, percent=100, csv_filename='err.csv', done=True, error={'code': 'E1', 'message': 'fail'})
    data = get_progress_data(key)
    assert data['percent'] == 100
    assert data['done'] is True
    assert data['csv_filename'] == 'err.csv'
    assert isinstance(data.get('error'), dict)
    assert data['error'].get('code') == 'E1'


def test_set_progress_redis_pipeline_failure_fallback(monkeypatch):
    # Ensure that if pipeline.execute() fails, set_progress falls back to hset
    app_mod = importlib.import_module('app.app')
    fake = FailingPipelineRedis()
    # Monkeypatch redis_conn to the fake that fails on execute
    monkeypatch.setattr(app_mod, 'redis_conn', fake)

    key = str(uuid.uuid4())
    # This should attempt pipeline and then fallback; no exception should propagate
    set_progress(key, percent=42, csv_filename='b.csv', done=True)
    data = get_progress_data(key)
    assert data['percent'] == 42
    assert data['csv_filename'] == 'b.csv'
