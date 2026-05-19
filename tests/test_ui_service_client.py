import time
from concurrent.futures import ThreadPoolExecutor

from projdash.ui import service_client


def test_create_project_service_reuses_single_service_per_database_path(
    monkeypatch,
    tmp_path,
):
    class FakeRepository:
        constructed = 0

        def __init__(self, db_path: str) -> None:
            type(self).constructed += 1
            self.db_path = db_path
            time.sleep(0.02)

        def load_command_replay_cache(self):
            return {}

    service_client._clear_project_service_cache()
    monkeypatch.setattr(service_client, "LadybugProjectRepository", FakeRepository)
    db_path = tmp_path / "shared.lbug"
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            services = list(
                executor.map(
                    lambda _index: service_client.create_project_service(str(db_path)),
                    range(4),
                )
            )

        assert len({id(service) for service in services}) == 1
        assert FakeRepository.constructed == 1
        assert services[0]._repository.db_path == str(db_path.resolve())
    finally:
        service_client._clear_project_service_cache()
