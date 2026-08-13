"""Unit tests for the pure parts of scripts/deploy_agent.py.

The deploy call itself talks to Google Cloud and is never exercised here;
these tests cover the config construction and the guards around it."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import deploy_agent  # noqa: E402
from deploy_agent import (  # noqa: E402
    CONTAINER_REQUIREMENTS,
    EXCLUDED_BUNDLE_PATHS,
    EXTRA_PACKAGES,
    RESERVED_ENV_VARS,
    TELEMETRY_ENV_VAR,
    DeployConfigError,
    assert_pins_match_lockfile,
    build_config,
    build_env_vars,
    describe_env_vars,
    iam_grant_commands,
    require_service_account,
    validate_bundle,
    validate_env_vars,
)

_SA = "email-agent-runtime@email-agent-498702.iam.gserviceaccount.com"


class TestEnvVars:
    def test_secret_reference_shape(self):
        env = build_env_vars()
        assert env["GOOGLE_TOKEN_JSON"] == {"secret": "agent-token-json", "version": "1"}

    def test_expected_plain_env_vars(self):
        env = build_env_vars()
        assert env["HEADLESS"] == "1"
        assert env["GOOGLE_GENAI_USE_ENTERPRISE"] == "true"
        assert env["MCP_SERVER_URL"] == "https://workspace-mcp-279648370419.us-central1.run.app"
        assert env["USER_GOOGLE_EMAIL"] == "pranava.ravindran19@gmail.com"
        assert env["USE_MCP_GMAIL"] == "1"
        assert env["USE_MCP_SHEETS"] == "1"
        assert env[TELEMETRY_ENV_VAR] == "true"

    def test_secret_ref_is_the_only_dict_value(self):
        env = build_env_vars()
        dict_valued = [k for k, v in env.items() if isinstance(v, dict)]
        assert dict_valued == ["GOOGLE_TOKEN_JSON"]

    def test_built_env_vars_pass_validation(self):
        validate_env_vars(build_env_vars())

    @pytest.mark.parametrize("reserved", sorted(RESERVED_ENV_VARS))
    def test_every_reserved_name_rejected(self, reserved):
        env = build_env_vars()
        env[reserved] = "x"
        with pytest.raises(DeployConfigError, match="reserved"):
            validate_env_vars(env)

    def test_reserved_prefix_rejected(self):
        env = build_env_vars()
        env["GOOGLE_CLOUD_AGENT_ENGINE_ID"] = "x"
        with pytest.raises(DeployConfigError, match="reserved prefix"):
            validate_env_vars(env)

    def test_telemetry_toggle_is_the_prefix_exception(self):
        assert TELEMETRY_ENV_VAR.startswith("GOOGLE_CLOUD_AGENT_ENGINE")
        validate_env_vars({TELEMETRY_ENV_VAR: "true"})

    def test_no_reserved_name_in_built_config(self):
        config = build_config(_SA, "gs://bucket")
        for name in config["env_vars"]:
            assert name not in RESERVED_ENV_VARS
            assert not name.startswith("GOOGLE_CLOUD_AGENT_ENGINE") or name == TELEMETRY_ENV_VAR

    def test_describe_never_prints_a_secret_payload(self):
        described = describe_env_vars(build_env_vars())
        assert "agent-token-json v1" in described
        # The description carries the reference, not any credential field.
        assert "refresh_token" not in described
        assert "client_secret" not in described


class TestBundle:
    def test_extra_packages_disjoint_from_exclusions(self):
        for excluded in EXCLUDED_BUNDLE_PATHS:
            assert excluded not in EXTRA_PACKAGES
            # Nor may an excluded path ride inside a bundle entry's path.
            for entry in EXTRA_PACKAGES:
                assert not entry.startswith(f"{excluded}/")

    def test_bundle_is_exactly_the_import_graph(self):
        assert set(EXTRA_PACKAGES) == {"agent.py", "agents", "tools", "auth.py"}

    def test_real_repo_bundle_validates(self):
        validate_bundle(deploy_agent.REPO_ROOT)

    def _fake_repo(self, tmp_path):
        (tmp_path / "agent.py").write_text("")
        (tmp_path / "auth.py").write_text("")
        for directory in ("agents", "tools"):
            (tmp_path / directory).mkdir()
            (tmp_path / directory / "__init__.py").write_text("")
        return tmp_path

    def test_missing_entry_rejected(self, tmp_path):
        self._fake_repo(tmp_path)
        (tmp_path / "auth.py").unlink()
        with pytest.raises(DeployConfigError, match="does not exist"):
            validate_bundle(tmp_path)

    @pytest.mark.parametrize(
        "leaked", ["credentials.json", "token.json", "token.pickle", "run_log.jsonl", ".env"]
    )
    def test_credential_inside_bundled_dir_rejected(self, tmp_path, leaked):
        repo = self._fake_repo(tmp_path)
        (repo / "tools" / leaked).write_text("secret")
        with pytest.raises(DeployConfigError, match="credential or\nlog file|credential or log"):
            validate_bundle(repo)

    def test_credential_nested_deeper_rejected(self, tmp_path):
        repo = self._fake_repo(tmp_path)
        nested = repo / "agents" / "sub"
        nested.mkdir()
        (nested / "token.json").write_text("secret")
        with pytest.raises(DeployConfigError):
            validate_bundle(repo)


class TestRequirementsPins:
    def test_pins_match_actual_lockfile(self):
        lockfile = (deploy_agent.REPO_ROOT / "requirements.txt").read_text()
        assert_pins_match_lockfile(CONTAINER_REQUIREMENTS, lockfile)

    def test_version_disagreement_rejected(self):
        with pytest.raises(DeployConfigError, match="disagrees"):
            assert_pins_match_lockfile(("mcp==1.0.0",), "mcp==2.0.0\n")

    def test_unlocked_requirement_rejected(self):
        with pytest.raises(DeployConfigError, match="not in requirements.txt"):
            assert_pins_match_lockfile(("nonexistent-package==1.0",), "mcp==2.0.0\n")

    def test_extras_are_ignored_when_matching_names(self):
        assert_pins_match_lockfile(
            ("google-cloud-aiplatform[agent_engines]==1.163.0",),
            "google-cloud-aiplatform==1.163.0\n",
        )


class TestServiceAccount:
    def test_explicit_value_passes_through(self):
        assert require_service_account(_SA) == _SA

    def test_missing_raises_with_creation_command(self):
        with pytest.raises(DeployConfigError) as excinfo:
            require_service_account(None)
        message = str(excinfo.value)
        assert "gcloud iam service-accounts create" in message
        assert "roles/secretmanager.secretAccessor" in message
        assert "roles/run.invoker" in message
        assert "roles/aiplatform.user" in message

    def test_empty_string_raises(self):
        with pytest.raises(DeployConfigError):
            require_service_account("")

    def test_grants_cover_both_principals(self):
        commands = "\n".join(iam_grant_commands(_SA))
        assert _SA in commands
        # The deploy-time Agent Platform Service Agent is a different
        # principal and needs its own secretAccessor grant.
        assert "service-279648370419@gcp-sa-aiplatform.iam.gserviceaccount.com" in commands
        assert commands.count("roles/secretmanager.secretAccessor") == 2


class TestBuildConfig:
    def test_full_config_shape(self):
        config = build_config(_SA, "gs://bucket")
        assert config["service_account"] == _SA
        assert config["staging_bucket"] == "gs://bucket"
        assert config["agent_framework"] == "google-adk"
        assert config["extra_packages"] == list(EXTRA_PACKAGES)
        assert config["requirements"] == list(CONTAINER_REQUIREMENTS)
        assert config["min_instances"] == 0
        assert config["max_instances"] == 1

    def test_non_gs_staging_bucket_rejected(self):
        with pytest.raises(DeployConfigError, match="gs://"):
            build_config(_SA, "email-agent-498702-agent-staging")
