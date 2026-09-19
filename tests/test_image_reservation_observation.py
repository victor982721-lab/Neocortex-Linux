"""Reuse one bounded image metadata observation only for its source snapshot."""

from pathlib import Path

from PIL import Image

from neocortex.capabilities.formats.image import route as image_route
from neocortex.capabilities.formats.image.analysis import classify
from tests.test_image_admission_shutdown import _State, _images, _route


TEST_CAPABILITIES = ("image",)


def test_new_image_probes_reservation_once_and_current_replay_never_probes(tmp_path, monkeypatch):
    path, = _images(tmp_path, "one.png")
    state = _State([path])
    observations, limits = [], []
    reserve = image_route.image_worker_memory_reservation

    def observe(path, *args, **kwargs):
        value = reserve(path, *args, **kwargs)
        observations.append((Path(path), value))
        return value

    class Supervisor:
        def classify(self, path, root, **kwargs):
            limits.append(kwargs["memory_limit_bytes"])
            return classify(path, root, features=kwargs["features"],
                            document_verifier=kwargs["document_verifier"])

        def close(self):
            pass

    monkeypatch.setattr(image_route, "image_worker_memory_reservation", observe)
    monkeypatch.setattr(image_route, "ImageWorkerSupervisor", Supervisor)
    first = _route(tmp_path, state, isolate_decoders=True).run()
    assert first.classified == 1 and first.errors == 0
    assert observations == [(path, limits[0])]
    replay = _route(tmp_path, state, run_id=2, isolate_decoders=True).run()
    assert replay.cache_hits == 1 and replay.classified == 0
    assert observations == [(path, limits[0])]


def test_reservation_observation_does_not_bypass_source_revalidation(tmp_path, monkeypatch):
    path, = _images(tmp_path, "changed.png")
    state = _State([path])

    class ReplacingSupervisor:
        def __init__(self):
            # Residence is created after estimation, before worker analysis.
            with Image.new("RGB", (128, 128), "white") as replacement:
                replacement.save(path)

        def classify(self, *_args, **_kwargs):
            raise AssertionError("a changed source reached the decoder")

        def close(self):
            pass

    monkeypatch.setattr(image_route, "ImageWorkerSupervisor", ReplacingSupervisor)
    result = _route(tmp_path, state, isolate_decoders=True).run()
    assert result.classified == 0 and result.errors == 1
    assert state.review_candidates
    assert "metadata changed before classification" in str(state.review_candidates)
