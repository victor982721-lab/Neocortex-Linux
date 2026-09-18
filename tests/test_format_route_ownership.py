"""Format lookup lifetimes have exclusive, independently scoped route owners."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.capabilities.formats.archive.route import ArchiveRoute
from neocortex.capabilities.formats.audio.route import AudioRoute
from neocortex.capabilities.formats.docx.route import DocxRoute
from neocortex.capabilities.formats.office.route import OfficeRoute
from neocortex.capabilities.formats.video.route import VideoRoute
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.runtime.control.locking import FrameworkRunLock


@pytest.mark.parametrize("route_class", (AudioRoute, VideoRoute, OfficeRoute, DocxRoute, ArchiveRoute))
def test_route_owner_lock_is_scoped_released_and_retained(tmp_path: Path, route_class) -> None:
    expected = object()

    def route(path: Path):
        value = object.__new__(route_class)
        value.config = SimpleNamespace(state_path=path)
        value.cancellation = CancellationToken()
        value._validate = lambda: None
        value._run_locked = lambda: expected
        return value

    owner = route(tmp_path / "one.sqlite3")
    peer = route(tmp_path / "two.sqlite3")
    lock_path = tmp_path / "one.sqlite3.route.lock"
    with FrameworkRunLock(lock_path):
        with pytest.raises(RuntimeError, match="another framework execution"):
            owner.run()
        assert peer.run() is expected
    assert owner.run() is expected
    assert lock_path.is_file()
    with FrameworkRunLock(lock_path):
        pass


@pytest.mark.parametrize("route_class", (AudioRoute, VideoRoute, OfficeRoute, DocxRoute, ArchiveRoute))
def test_cancelled_route_does_not_create_owner_directory(tmp_path: Path, route_class) -> None:
    route = object.__new__(route_class)
    route.config = SimpleNamespace(state_path=tmp_path / "absent" / "state.sqlite3")
    route.cancellation = CancellationToken()
    route.cancellation.cancel()
    with pytest.raises(CancellationRequested):
        route.run()
    assert not route.config.state_path.parent.exists()
