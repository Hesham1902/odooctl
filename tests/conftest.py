import textwrap

import pytest
import yaml


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    home = tmp_path / "odoocfg"
    monkeypatch.setenv("ODOOCTL_HOME", str(home))
    yield home


def write_compose(
    project_dir,
    version="18",
    host_prefix="/Users/dev",
    web_ports=(("8056", "8069"), ("8072", "8072"), ("8777", "8888")),
    db_ports=(("5456", "5432"),),
    containers=("acme_web", "acme_db"),
    include_enterprise=True,
    filename="docker-compose.yml",
):
    volumes = [
        "./home/odoo/.local/share/Odoo:/var/lib/odoo",
        "./config:/etc/odoo",
        "./custom_addons:/mnt/extra-addons",
    ]
    if include_enterprise:
        volumes.append(f"{host_prefix}/_odoo_addons/odoo-{version}.0/odoo/addons:/mnt/enterprise")

    data = {
        "services": {
            "web": {
                "container_name": containers[0],
                "build": {"context": ".", "dockerfile": "odoo.Dockerfile"},
                "depends_on": ["db"],
                "ports": [f"{h}:{c}" for h, c in web_ports],
                "volumes": volumes,
                "environment": ["HOST=db", "USER=odoo", "PASSWORD=odoo"],
            },
            "db": {
                "container_name": containers[1],
                "build": {"context": ".", "dockerfile": "postgres.Dockerfile"},
                "ports": [f"{h}:{c}" for h, c in db_ports],
                "volumes": ["./data/:/var/lib/postgresql/data"],
                "environment": [
                    "POSTGRES_PASSWORD=odoo",
                    "POSTGRES_USER=odoo",
                    "POSTGRES_DB=postgres",
                ],
            },
        }
    }
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / filename).write_text(yaml.safe_dump(data))
    return project_dir


@pytest.fixture
def make_project(tmp_path):
    def _make(name, **kwargs):
        d = tmp_path / "work" / name
        write_compose(d, **kwargs)
        (d / "custom_addons").mkdir(exist_ok=True)
        return d

    return _make


SAMPLE_SUCCESS_LOG = textwrap.dedent("""\
    2026-08-21 10:00:00,001 1 INFO ? odoo.modules.loading: loading 379 modules...
    2026-08-21 10:00:02,000 1 INFO mod odoo.addons.base.tests.test_base: test something
    2026-08-21 10:00:05,000 1 INFO ? odoo.tests.runner: Ran 12 tests in 3.400s
    2026-08-21 10:00:05,100 1 INFO ? odoo.modules.loading: Modules loaded.
""")

SAMPLE_FAILURE_LOG = textwrap.dedent("""\
    2026-08-21 10:00:02,000 1 INFO mod odoo.addons.mod.tests.test_x: running
    FAIL: test_compute_net (odoo.addons.hr_payslip_fix.tests.test_net.TestNet)
    Traceback:
    AssertionError: 1000 != 900
    2026-08-21 10:00:05,000 1 INFO ? odoo.tests.result: 1 failed, 0 error(s) of 43 tests
""")
