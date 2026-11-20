from __future__ import annotations

import pytest
from fastapi_app.app import Base as ApiBase
from sync_app.services import Base as ServicesBase

# A session of its own, with its own pytest.ini: its schema comes from Alembic.
collect_ignore = ["alembic_app"]


@pytest.fixture(scope="session")
def sqla_metadata() -> list[type]:
    """Two applications, two declarative bases, one test schema.

    An ini key works too, for one import path: sqla_metadata = "app.models:Base".
    """
    return [ApiBase, ServicesBase]
