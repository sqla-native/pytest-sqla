from __future__ import annotations

import pytest
from fastapi_app.app import Base as ApiBase
from sync_app.services import Base as ServicesBase


@pytest.fixture(scope="session")
def sqla_metadata() -> list[type]:
    """Two applications, two declarative bases, one test schema.

    An ini key works too, for one import path: sqla_metadata = "app.models:Base".
    """
    return [ApiBase, ServicesBase]
