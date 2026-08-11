import pytest

from app.models import Project
from app.store import ProjectNotFound, delete_project, list_projects, load_project, save_project


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_save_and_load_roundtrip():
    project = Project(name="My Project")
    save_project(project)

    loaded = load_project(project.id)
    assert loaded.id == project.id
    assert loaded.name == "My Project"
    assert loaded.schema_version == 1


def test_load_missing_project_raises():
    with pytest.raises(ProjectNotFound):
        load_project("does-not-exist")


def test_list_projects_sorted_by_updated_at_desc():
    p1 = Project(name="First")
    save_project(p1)
    p2 = Project(name="Second")
    p2.updated_at = p1.updated_at + 10
    save_project(p2)

    projects = list_projects()
    assert [p.name for p in projects] == ["Second", "First"]


def test_delete_project():
    project = Project(name="Temp")
    save_project(project)
    delete_project(project.id)
    with pytest.raises(ProjectNotFound):
        load_project(project.id)


def test_save_is_atomic_no_tmp_leftover(tmp_path):
    project = Project(name="Atomic")
    save_project(project)
    from app.utils.paths import project_dir

    files = list(project_dir(project.id).iterdir())
    assert not any(f.suffix == ".tmp" for f in files)
