import uuid

import pytest
from trace_hash import Config
from trace_hash.adapters import Expansion
from trace_hash.sdk import _process_path


@pytest.fixture
def text():
    return " ".join(f"word{i}" for i in range(800))


@pytest.fixture
def fp(tmp_path):
    def make(data, name="sample.txt", config=None):
        path = tmp_path / name
        if isinstance(data, str):
            data = data.encode()
        path.write_bytes(data)
        config = config or Config()
        return _process_path(path, str(uuid.uuid4()), str(uuid.uuid4()), config, Expansion(config))

    return make
