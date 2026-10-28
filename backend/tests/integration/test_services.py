"""Smoke checks against the Compose stack (`make up`). Run with `make test-integration`."""

import psycopg
import pytest
import redis
from pyarrow.fs import S3FileSystem

from sluice.settings import get_settings

pytestmark = pytest.mark.integration


def test_postgres_up() -> None:
    with psycopg.connect(get_settings().postgres_dsn, connect_timeout=5) as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)


def test_redis_up() -> None:
    client = redis.Redis.from_url(get_settings().redis_url, socket_timeout=5)
    try:
        assert client.ping() is True
    finally:
        client.close()


def test_rustfs_bucket_exists() -> None:
    s = get_settings()
    fs = S3FileSystem(
        access_key=s.rustfs_access_key.get_secret_value(),
        secret_key=s.rustfs_secret_key.get_secret_value(),
        endpoint_override=s.s3_endpoint,
        region="us-east-1",
        connect_timeout=5,
        request_timeout=5,
    )
    info = fs.get_file_info(s.warehouse_bucket)
    assert info.type.name == "Directory"
