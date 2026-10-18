"""Install the built wheel into a clean virtualenv and use it as a consumer would.

Catches packaging mistakes the test suite structurally cannot: a module left
out of the wheel, a missing ``py.typed``, a ``pytest11`` entry point that does
not resolve once installed, or an import that only works because the dev
environment happens to have greenlet or pytest-asyncio.

Two phases in one throwaway virtualenv:

1. ``pip install pytest-sqla``: no extras, no dev tooling. On SQLAlchemy 2.1
   that means no greenlet. A small sync project must pass, and an async
   fixture must fail with pytest-sqla's own message rather than an ImportError.
2. ``pip install "pytest-sqla[asyncio]" aiosqlite``: an async project must pass.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MODELS = """
from sqlalchemy import String, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "user_account"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(100), unique=True)


def count(session):
    return session.scalar(select(func.count()).select_from(User))
"""

SYNC_TESTS = """
from models import User, count


def test_commits(sqla_session):
    sqla_session.add(User(email="ada@example.com"))
    sqla_session.commit()
    assert count(sqla_session) == 1


def test_rolled_back(sqla_session):
    assert count(sqla_session) == 0
"""

ASYNC_TESTS = """
import pytest
from sqlalchemy import func, select
from models import User


@pytest.mark.asyncio
async def test_commits(sqla_async_session):
    sqla_async_session.add(User(email="ada@example.com"))
    await sqla_async_session.commit()
    assert await sqla_async_session.scalar(select(func.count()).select_from(User)) == 1


@pytest.mark.asyncio(loop_scope="module")
async def test_rolled_back(sqla_async_session):
    assert await sqla_async_session.scalar(select(func.count()).select_from(User)) == 0
"""

WANTS_ASYNC = """
def test_wants_async(sqla_async_session):
    pass
"""

INSTALLED = """
import importlib.metadata as md
import importlib.util
import os

import pytest_sqla

entry_points = {ep.name: ep.value for ep in md.entry_points(group="pytest11")}
assert entry_points.get("sqla") == "pytest_sqla.plugin", entry_points
root = importlib.util.find_spec("pytest_sqla").submodule_search_locations[0]
assert os.path.exists(os.path.join(root, "py.typed")), "py.typed missing from the wheel"
assert importlib.util.find_spec("greenlet") is None, "a plain install must not need greenlet"
assert importlib.util.find_spec("pytest_asyncio") is None
print("installed:", pytest_sqla.__version__)
"""


def run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    print(f"$ {' '.join(args)}", flush=True)
    return subprocess.run(args, check=False, cwd=cwd, capture_output=True, text=True)


def checked(*args: str, cwd: Path | None = None) -> str:
    result = run(*args, cwd=cwd)
    if result.returncode != 0:
        print(result.stdout, result.stderr, sep="\n", file=sys.stderr)
        raise SystemExit(f"failed: {' '.join(args)}")
    return result.stdout


def project(path: Path, ini: str, files: dict[str, str]) -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir()
    (path / "pytest.ini").write_text(textwrap.dedent(ini))
    (path / "models.py").write_text(MODELS)
    for name, source in files.items():
        (path / name).write_text(source)
    return path


def main() -> int:
    dist = ROOT / "dist"
    if dist.exists():
        shutil.rmtree(dist)
    checked(sys.executable, "-m", "build", "--wheel", str(ROOT))
    wheels = sorted(dist.glob("*.whl"))
    if len(wheels) != 1:
        print(f"expected exactly one wheel, found {len(wheels)}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        venv = Path(tmp) / "venv"
        checked(sys.executable, "-m", "venv", str(venv))
        python = str(venv / "bin" / "python")
        checked(python, "-m", "pip", "install", "--quiet", "--upgrade", "pip")

        # Phase 1: the plain install.
        checked(python, "-m", "pip", "install", "--quiet", str(wheels[0]))
        print(checked(python, "-c", INSTALLED), end="")
        sync = project(
            Path(tmp) / "sync",
            """
            [pytest]
            filterwarnings = error
            addopts = -p no:cacheprovider
            sqla_url = sqlite://
            sqla_metadata = models:Base
            """,
            {"test_sync.py": SYNC_TESTS, "test_wants_async.py": WANTS_ASYNC},
        )
        print(checked(python, "-m", "pytest", "test_sync.py", cwd=sync).splitlines()[-1])
        refused = run(python, "-m", "pytest", "test_wants_async.py", cwd=sync)
        expected = "sqla_async_session needs an async test, but no async plugin runs"
        if refused.returncode == 0 or expected not in refused.stdout:
            print(refused.stdout, refused.stderr, sep="\n", file=sys.stderr)
            raise SystemExit("an async fixture on a plain install must fail with F5")
        print("an async fixture on a plain install fails with pytest-sqla's message")

        # Phase 2: the asyncio extra.
        checked(python, "-m", "pip", "install", "--quiet", f"{wheels[0]}[asyncio]", "aiosqlite")
        asynchronous = project(
            Path(tmp) / "async",
            """
            [pytest]
            filterwarnings = error
            addopts = -p no:cacheprovider
            asyncio_mode = strict
            asyncio_default_fixture_loop_scope = function
            sqla_url = sqlite+aiosqlite://
            sqla_metadata = models:Base
            """,
            {"test_async.py": ASYNC_TESTS},
        )
        print(checked(python, "-m", "pytest", cwd=asynchronous).splitlines()[-1])

    print("packed consumer OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
