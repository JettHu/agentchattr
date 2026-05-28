"""Tests for AGENTCHATTR_* isolation overrides and project resolution.

Most tests exercise load_config() directly because wrappers also call it, and
the core guarantee is that the same env vars produce the same config regardless
of entry point. The resolver tests cover launcher --project output feeding that
same lower-level mechanism.
"""

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config_loader  # noqa: E402
from scripts import resolve_project_instance as resolver  # noqa: E402


ENV_VARS = [
    "AGENTCHATTR_DATA_DIR",
    "AGENTCHATTR_PORT",
    "AGENTCHATTR_MCP_HTTP_PORT",
    "AGENTCHATTR_MCP_SSE_PORT",
    "AGENTCHATTR_UPLOAD_DIR",
    "AGENTCHATTR_PROJECT",
    "AGENTCHATTR_PROJECT_NAME",
    "AGENTCHATTR_PROJECT_ID",
    "AGENTCHATTR_ARTIFACT_ROOT",
]


def _env_from_output(output: str) -> dict[str, str]:
    env = {}
    for line in output.strip().splitlines():
        key, value = line.split("=", 1)
        env[key] = value
    return env


class ConfigOverrideTests(unittest.TestCase):
    def setUp(self):
        # Snapshot and clear all override env vars so tests don't interfere.
        self._saved = {k: os.environ.get(k) for k in ENV_VARS}
        for k in ENV_VARS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_no_env_vars_uses_config_toml_values(self):
        config = config_loader.load_config(ROOT)
        self.assertEqual(config["server"]["port"], 8300)
        self.assertEqual(config["server"]["data_dir"], "./data")

    def test_port_env_var_overrides_config(self):
        os.environ["AGENTCHATTR_PORT"] = "8310"
        config = config_loader.load_config(ROOT)
        self.assertEqual(config["server"]["port"], 8310)

    def test_mcp_ports_env_vars_override_config(self):
        os.environ["AGENTCHATTR_MCP_HTTP_PORT"] = "8210"
        os.environ["AGENTCHATTR_MCP_SSE_PORT"] = "8211"
        config = config_loader.load_config(ROOT)
        self.assertEqual(config["mcp"]["http_port"], 8210)
        self.assertEqual(config["mcp"]["sse_port"], 8211)

    def test_data_dir_absolute_path_preserved(self):
        abs_path = str(Path("/tmp/test-agentchattr").resolve())
        os.environ["AGENTCHATTR_DATA_DIR"] = abs_path
        config = config_loader.load_config(ROOT)
        self.assertEqual(config["server"]["data_dir"], abs_path)

    def test_data_dir_relative_path_resolves_to_cwd(self):
        # Relative path should resolve against CWD, not agentchattr install
        os.environ["AGENTCHATTR_DATA_DIR"] = "./my-project-data"
        config = config_loader.load_config(ROOT)
        expected = str((Path.cwd() / "my-project-data").resolve())
        self.assertEqual(config["server"]["data_dir"], expected)

    def test_upload_dir_relative_path_resolves_to_cwd(self):
        os.environ["AGENTCHATTR_UPLOAD_DIR"] = "./my-uploads"
        config = config_loader.load_config(ROOT)
        expected = str((Path.cwd() / "my-uploads").resolve())
        self.assertEqual(config["images"]["upload_dir"], expected)

    def test_empty_env_var_does_not_override(self):
        os.environ["AGENTCHATTR_PORT"] = ""
        config = config_loader.load_config(ROOT)
        # Empty value is ignored, default stays
        self.assertEqual(config["server"]["port"], 8300)

    def test_invalid_int_env_var_warns_and_keeps_default(self):
        os.environ["AGENTCHATTR_PORT"] = "not-a-number"
        config = config_loader.load_config(ROOT)
        self.assertEqual(config["server"]["port"], 8300)

    def test_all_overrides_applied_together(self):
        abs_data = str(Path("/tmp/proj-a/.agentchattr").resolve())
        abs_uploads = str(Path("/tmp/proj-a/uploads").resolve())
        os.environ["AGENTCHATTR_DATA_DIR"] = abs_data
        os.environ["AGENTCHATTR_PORT"] = "8310"
        os.environ["AGENTCHATTR_MCP_HTTP_PORT"] = "8210"
        os.environ["AGENTCHATTR_MCP_SSE_PORT"] = "8211"
        os.environ["AGENTCHATTR_UPLOAD_DIR"] = abs_uploads
        config = config_loader.load_config(ROOT)
        self.assertEqual(config["server"]["data_dir"], abs_data)
        self.assertEqual(config["server"]["port"], 8310)
        self.assertEqual(config["mcp"]["http_port"], 8210)
        self.assertEqual(config["mcp"]["sse_port"], 8211)
        self.assertEqual(config["images"]["upload_dir"], abs_uploads)

    def test_project_env_vars_override_metadata_and_agent_cwd(self):
        project_path = str(Path("/tmp/proj-a").resolve())
        os.environ["AGENTCHATTR_PROJECT"] = project_path
        os.environ["AGENTCHATTR_PROJECT_NAME"] = "Project A"
        os.environ["AGENTCHATTR_PROJECT_ID"] = "project-a"

        config = config_loader.load_config(ROOT)

        self.assertEqual(config["project"]["path"], project_path)
        self.assertEqual(config["project"]["name"], "Project A")
        self.assertEqual(config["project"]["id"], "project-a")
        for agent_cfg in config["agents"].values():
            if "cwd" in agent_cfg:
                self.assertEqual(agent_cfg["cwd"], project_path)

    def test_project_name_and_id_are_not_resolved_as_paths(self):
        os.environ["AGENTCHATTR_PROJECT_NAME"] = "Project A"
        os.environ["AGENTCHATTR_PROJECT_ID"] = "project-a"

        config = config_loader.load_config(ROOT)

        self.assertEqual(config["project"]["name"], "Project A")
        self.assertEqual(config["project"]["id"], "project-a")

    def test_artifact_root_relative_path_resolves_to_cwd(self):
        os.environ["AGENTCHATTR_ARTIFACT_ROOT"] = "./project-artifacts"

        config = config_loader.load_config(ROOT)

        expected = str((Path.cwd() / "project-artifacts").resolve())
        self.assertEqual(config["server"]["artifact_root"], expected)

    def test_agents_section_unchanged_by_overrides(self):
        os.environ["AGENTCHATTR_PORT"] = "8310"
        config = config_loader.load_config(ROOT)
        # Agent definitions must be untouched by path/port overrides
        self.assertIn("claude", config["agents"])
        self.assertEqual(config["agents"]["claude"]["command"], "claude")


class CliOverrideExtractionTests(unittest.TestCase):
    """apply_cli_overrides() extracts CLI flags into env vars.

    This is the shared helper used by run.py, wrapper.py, and wrapper_api.py
    so the same flags produce the same config regardless of entry point.
    """

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV_VARS}
        for k in ENV_VARS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_space_separated_flags_set_env_vars(self):
        argv = ["run.py", "--port", "8310", "--data-dir", "./foo"]
        config_loader.apply_cli_overrides(argv)
        self.assertEqual(os.environ["AGENTCHATTR_PORT"], "8310")
        self.assertEqual(os.environ["AGENTCHATTR_DATA_DIR"], "./foo")

    def test_equals_form_flags_set_env_vars(self):
        argv = ["run.py", "--port=8310", "--data-dir=./foo"]
        config_loader.apply_cli_overrides(argv)
        self.assertEqual(os.environ["AGENTCHATTR_PORT"], "8310")
        self.assertEqual(os.environ["AGENTCHATTR_DATA_DIR"], "./foo")

    def test_missing_flags_do_not_touch_env(self):
        argv = ["run.py"]
        config_loader.apply_cli_overrides(argv)
        for env in ENV_VARS:
            self.assertNotIn(env, os.environ)

    def test_overrides_flow_through_to_load_config(self):
        argv = ["run.py", "--port", "8315", "--mcp-http-port", "8215"]
        config_loader.apply_cli_overrides(argv)
        config = config_loader.load_config(ROOT)
        self.assertEqual(config["server"]["port"], 8315)
        self.assertEqual(config["mcp"]["http_port"], 8215)

    def test_all_five_flags_extracted(self):
        argv = [
            "run.py",
            "--data-dir", "/tmp/proj",
            "--port", "8310",
            "--mcp-http-port", "8210",
            "--mcp-sse-port", "8211",
            "--upload-dir", "/tmp/proj-uploads",
        ]
        config_loader.apply_cli_overrides(argv)
        self.assertEqual(os.environ["AGENTCHATTR_DATA_DIR"], "/tmp/proj")
        self.assertEqual(os.environ["AGENTCHATTR_PORT"], "8310")
        self.assertEqual(os.environ["AGENTCHATTR_MCP_HTTP_PORT"], "8210")
        self.assertEqual(os.environ["AGENTCHATTR_MCP_SSE_PORT"], "8211")
        self.assertEqual(os.environ["AGENTCHATTR_UPLOAD_DIR"], "/tmp/proj-uploads")

    def test_project_flags_extracted(self):
        argv = [
            "run.py",
            "--project", "/tmp/proj",
            "--project-name", "Project A",
            "--project-id", "project-a",
            "--artifact-root", "/tmp/proj-artifacts",
        ]
        config_loader.apply_cli_overrides(argv)
        self.assertEqual(os.environ["AGENTCHATTR_PROJECT"], "/tmp/proj")
        self.assertEqual(os.environ["AGENTCHATTR_PROJECT_NAME"], "Project A")
        self.assertEqual(os.environ["AGENTCHATTR_PROJECT_ID"], "project-a")
        self.assertEqual(os.environ["AGENTCHATTR_ARTIFACT_ROOT"], "/tmp/proj-artifacts")

    def test_pass_through_separator_ignores_later_flags(self):
        # `-- --port 9999` belongs to the agent CLI, not agentchattr.
        # Flags AFTER `--` must NOT leak into the env.
        argv = [
            "wrapper.py", "claude",
            "--port", "8310",
            "--",
            "--port", "9999",
            "--data-dir", "/agent-arg",
        ]
        config_loader.apply_cli_overrides(argv)
        self.assertEqual(os.environ["AGENTCHATTR_PORT"], "8310")
        self.assertNotIn("AGENTCHATTR_DATA_DIR", os.environ)

    def test_pass_through_alone_ignores_everything(self):
        # If agentchattr flags appear ONLY after `--`, none are applied.
        argv = [
            "wrapper.py", "claude",
            "--",
            "--port", "9999",
            "--data-dir", "/agent-arg",
        ]
        config_loader.apply_cli_overrides(argv)
        self.assertNotIn("AGENTCHATTR_PORT", os.environ)
        self.assertNotIn("AGENTCHATTR_DATA_DIR", os.environ)


class ProjectInstanceResolverTests(unittest.TestCase):
    """The --project resolver feeds the lower-level AGENTCHATTR_* mechanism."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.project = self.tmp_path / "project-a"
        self.registry_path = self.tmp_path / "registry" / "project_instances.json"

        self._saved = {
            "REGISTRY_PATH": resolver.REGISTRY_PATH,
            "LOCK_PATH": resolver.LOCK_PATH,
            "_port_is_free": resolver._port_is_free,
            "_port_in_use": resolver._port_in_use,
            "_fetch_instance_info": resolver._fetch_instance_info,
            "_classify_record": resolver._classify_record,
        }

        resolver.REGISTRY_PATH = self.registry_path
        resolver.LOCK_PATH = self.registry_path.with_suffix(".json.lock")
        resolver._port_is_free = lambda port: True
        resolver._port_in_use = lambda port, timeout=0.5: False
        resolver._fetch_instance_info = lambda port, timeout=1.5: None

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(resolver, name, value)
        self.tmp.cleanup()

    def _resolve(self, **overrides) -> dict[str, str]:
        args = argparse.Namespace(
            project=str(self.project),
            project_name=None,
            port=None,
            mcp_http_port=None,
            mcp_sse_port=None,
            artifact_root=None,
        )
        for key, value in overrides.items():
            setattr(args, key, value)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            resolver.resolve(args)
        return _env_from_output(stdout.getvalue())

    def test_resolve_emits_low_level_isolation_env_and_registry(self):
        env = self._resolve(port=8310, mcp_http_port=8210, mcp_sse_port=8211)

        project_path = str(self.project.resolve())
        agentchattr_dir = self.project.resolve() / ".agentchattr"
        self.assertEqual(env["AGENTCHATTR_PROJECT"], project_path)
        self.assertEqual(env["AGENTCHATTR_PROJECT_ID"], "project-a")
        self.assertEqual(env["AGENTCHATTR_PROJECT_NAME"], "project-a")
        self.assertEqual(env["AGENTCHATTR_DATA_DIR"], str(agentchattr_dir / "data"))
        self.assertEqual(env["AGENTCHATTR_UPLOAD_DIR"], str(agentchattr_dir / "uploads"))
        self.assertEqual(env["AGENTCHATTR_ARTIFACT_ROOT"], str(agentchattr_dir / "artifacts"))
        self.assertEqual(env["AGENTCHATTR_PORT"], "8310")
        self.assertEqual(env["AGENTCHATTR_MCP_HTTP_PORT"], "8210")
        self.assertEqual(env["AGENTCHATTR_MCP_SSE_PORT"], "8211")

        self.assertTrue((agentchattr_dir / "data").is_dir())
        self.assertTrue((agentchattr_dir / "uploads").is_dir())
        self.assertTrue((agentchattr_dir / "artifacts").is_dir())

        registry = json.loads(self.registry_path.read_text("utf-8"))
        record = registry["projects"][project_path]
        self.assertEqual(record["web_port"], 8310)
        self.assertEqual(record["mcp_http_port"], 8210)
        self.assertEqual(record["mcp_sse_port"], 8211)

    def test_existing_running_project_reuses_ports_without_free_probe(self):
        project_path = str(self.project.resolve())
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.registry_path.write_text(
            json.dumps({
                "projects": {
                    project_path: {
                        "project_id": "project-a",
                        "web_port": 8311,
                        "mcp_http_port": 8212,
                        "mcp_sse_port": 8213,
                        "data_dir": str(self.project / ".agentchattr" / "data"),
                        "upload_dir": str(self.project / ".agentchattr" / "uploads"),
                        "artifact_root": str(self.project / ".agentchattr" / "artifacts"),
                        "updated_at": 1,
                    }
                }
            }),
            "utf-8",
        )

        resolver._classify_record = (
            lambda abs_path, rec: "running" if abs_path == project_path else "stale"
        )

        def fail_if_called(port):
            raise AssertionError(f"_port_is_free should not probe running project port {port}")

        resolver._port_is_free = fail_if_called

        env = self._resolve()

        self.assertEqual(env["AGENTCHATTR_PORT"], "8311")
        self.assertEqual(env["AGENTCHATTR_MCP_HTTP_PORT"], "8212")
        self.assertEqual(env["AGENTCHATTR_MCP_SSE_PORT"], "8213")


if __name__ == "__main__":
    unittest.main()
