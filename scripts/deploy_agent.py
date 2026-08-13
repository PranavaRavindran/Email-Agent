"""Deploys (or redeploys) the email agent to Google Cloud Agent Runtime.

Uses the Vertex AI SDK's client.agent_engines.create()/update() rather than
the `adk deploy agent_engine` CLI: every useful CLI flag is deprecated on
ADK 2.4.0 in favour of an unverifiable JSON config schema, while the SDK
config surface is documented and inspectable (vertexai 1.163.0,
vertexai._genai.types.common.AgentEngineConfig).

Redeployment is the supported way to change env vars or config on an
already-deployed agent, so this script is the update mechanism too: it
finds an existing engine by --resource-name (or by display-name lookup)
and calls update() on it; only when none exists does it create() one.

Sessions: the agent is wrapped in AdkApp with NO session_service_builder.
On the deployed instance, AdkApp.set_up() (vertexai/agent_engines/templates/
adk.py) sees the runtime-provided GOOGLE_CLOUD_AGENT_ENGINE_ID env var and
defaults to VertexAiSessionService — the managed session backend — so
tool_context.state survives across turns and instances, which the tracker
two-turn confirm flow depends on. InMemorySessionService is only the
fallback when that env var is absent (i.e. running locally). Passing a
session_service_builder here would OVERRIDE that default; never add one.

Tracing: GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=true is the SDK's
replacement for the deprecated --trace_to_cloud/--otel_to_cloud flags.
With AdkApp's enable_tracing left unset (it is deprecated), the template's
truth table enables OpenTelemetry trace + log export when this env var is
true and google-adk >= 1.17 (this project pins 2.4.0).

The deploy never runs gcloud and never prints a credential value; the IAM
grants the identities need are printed as commands for a human to run.

Usage (from anywhere; the script chdirs to the repo root):
    python scripts/deploy_agent.py \
        --service-account email-agent-runtime@email-agent-498702.iam.gserviceaccount.com \
        --staging-bucket gs://email-agent-498702-agent-staging
"""

import argparse
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

PROJECT = "email-agent-498702"
# The project *number* for email-agent-498702, visible in both the MCP
# service URL and the Agent Platform Service Agent's address.
PROJECT_NUMBER = "279648370419"
LOCATION = "us-central1"
DISPLAY_NAME = "email-agent"
DESCRIPTION = "Email intelligence agent: inbox triage, drafting, job-application tracking."

# Secret Manager secret holding the token.json payload auth.py consumes via
# GOOGLE_TOKEN_JSON. Only the NAME and VERSION appear here or in any output.
TOKEN_SECRET_NAME = "agent-token-json"
TOKEN_SECRET_VERSION = "1"

MCP_SERVER_URL = "https://workspace-mcp-279648370419.us-central1.run.app"
MCP_SERVICE_NAME = "workspace-mcp"
USER_GOOGLE_EMAIL = "pranava.ravindran19@gmail.com"

# Deploy-time principal: the Agent Platform Service Agent fetches the secret
# while building the deployment. Distinct from the runtime service account.
PLATFORM_SERVICE_AGENT = f"service-{PROJECT_NUMBER}@gcp-sa-aiplatform.iam.gserviceaccount.com"

# Env vars Agent Runtime sets itself; setting any of these in the deploy
# config conflicts with the runtime. tools/genai_client.py READS
# GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION to select Vertex mode — the
# runtime provides both, so they must not (and need not) be set here.
RESERVED_ENV_VARS = frozenset(
    {
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_QUOTA_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "PORT",
        "K_SERVICE",
        "K_REVISION",
        "K_CONFIGURATION",
        "GOOGLE_APPLICATION_CREDENTIALS",
    }
)
RESERVED_ENV_PREFIX = "GOOGLE_CLOUD_AGENT_ENGINE"
# The one sanctioned exception to the reserved prefix: the SDK's own
# telemetry toggle. create() injects it as "unspecified" when absent, and
# the AdkApp template's deprecation warning instructs setting it through
# env_vars — it is a deploy-config input, not a runtime-owned value.
TELEMETRY_ENV_VAR = "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY"

# What ships: exactly agent.py's import graph, nothing else. The SDK tars
# each listed path (tarfile.add, recursive for directories) relative to the
# CWD, so the container sees the same layout the repo root has.
EXTRA_PACKAGES = ("agent.py", "agents", "tools", "auth.py")

# Paths that must never ship. venv/evals/tests/scripts are bloat; the
# credential and log files are a security failure if bundled.
EXCLUDED_BUNDLE_PATHS = frozenset(
    {
        "venv",
        "evals",
        "eval_agent",
        "tests",
        "scripts",
        "credentials.json",
        "token.json",
        "token.pickle",
        "run_log.jsonl",
        ".git",
    }
)
# File names that indicate a credential or log leaked INSIDE a bundled
# directory (e.g. a stray token.json dropped into tools/).
FORBIDDEN_BUNDLE_BASENAMES = frozenset(
    {"credentials.json", "token.json", "token.pickle", "run_log.jsonl", ".env"}
)

# PyPI dependencies the container installs. Pinned to the exact versions in
# requirements.txt (assert_pins_match_lockfile guards against drift) so the
# container runs what local testing ran. The [agent_engines] extra brings
# the runtime template's own needs: cloudpickle, the OpenTelemetry Cloud
# Trace/Logging exporters, google-cloud-iam. Dev/eval-only packages
# (litellm, openai, gepa, scikit-learn, pytest, ...) deliberately do not
# ship. The SDK additionally auto-appends cloudpickle==<local version> and
# pydantic==<local version> if unpinned, matching the environment that
# serialized the agent.
CONTAINER_REQUIREMENTS = (
    "google-cloud-aiplatform[agent_engines]==1.163.0",
    "google-adk==2.4.0",
    "google-genai==2.11.0",
    "google-api-python-client==2.198.0",
    "google-auth==2.56.0",
    "google-auth-httplib2==0.4.0",
    "google-auth-oauthlib==1.4.0",
    "httpx2==2.10.0",
    "mcp==2.0.0",
    "pydantic==2.13.4",
)


class DeployConfigError(RuntimeError):
    """A deploy precondition failed; the message says how to fix it."""


def build_env_vars() -> dict[str, Any]:
    """The full env var dict for the deployed agent.

    String values become plain env vars; the dict value becomes a Secret
    Manager reference (the SDK maps {"secret", "version"} dicts to
    deployment_spec.secret_env entries — verified against
    vertexai/_genai/agent_engines.py, _update_deployment_spec_with_env_vars_
    dict_or_raise). The token payload itself never appears anywhere.
    """
    return {
        # auth.py: never open a browser; fail loudly without a credential.
        "HEADLESS": "1",
        # tools/genai_client.py: Vertex mode. Project/location come from the
        # runtime's own reserved env vars. (AdkApp.set_up() also forces this
        # flag to "1" at startup; both spellings parse as true.)
        "GOOGLE_GENAI_USE_ENTERPRISE": "true",
        "MCP_SERVER_URL": MCP_SERVER_URL,
        "USER_GOOGLE_EMAIL": USER_GOOGLE_EMAIL,
        # Both default on ("1") when unset, but this project has already
        # flip-flopped the recommended value once (DEPLOYMENT.md used to say
        # 0); pinning them makes the deployed config self-documenting and
        # immune to a future default change.
        "USE_MCP_GMAIL": "1",
        "USE_MCP_SHEETS": "1",
        # OpenTelemetry trace + log export to Cloud Trace/Logging (step 11).
        TELEMETRY_ENV_VAR: "true",
        # auth.py's read-only credential source, resolved by the runtime
        # from Secret Manager at instance start.
        "GOOGLE_TOKEN_JSON": {"secret": TOKEN_SECRET_NAME, "version": TOKEN_SECRET_VERSION},
    }


def validate_env_vars(env_vars: dict[str, Any]) -> None:
    """Rejects any env var the runtime reserves for itself."""
    for name in env_vars:
        if name in RESERVED_ENV_VARS:
            raise DeployConfigError(
                f"Env var {name!r} is reserved by Agent Runtime (the runtime sets "
                f"it itself); remove it from the deploy config."
            )
        if name.startswith(RESERVED_ENV_PREFIX) and name != TELEMETRY_ENV_VAR:
            raise DeployConfigError(
                f"Env var {name!r} uses the reserved prefix {RESERVED_ENV_PREFIX!r}; "
                f"only {TELEMETRY_ENV_VAR} (the SDK's own telemetry toggle) may be set."
            )


def validate_bundle(repo_root: Path) -> None:
    """Asserts the bundle is exactly the intended import graph and carries
    no credential or log file."""
    for entry in EXTRA_PACKAGES:
        if entry in EXCLUDED_BUNDLE_PATHS:
            raise DeployConfigError(f"Bundle entry {entry!r} is on the exclusion list.")
        path = repo_root / entry
        if not path.exists():
            raise DeployConfigError(
                f"Bundle entry {entry!r} does not exist under {repo_root}; "
                f"the agent cannot import without it."
            )
        if path.is_dir():
            for child in path.rglob("*"):
                if child.name in FORBIDDEN_BUNDLE_BASENAMES:
                    raise DeployConfigError(
                        f"Refusing to deploy: {child} would ship a credential or "
                        f"log file inside the bundle. Remove it before deploying."
                    )
        elif path.name in FORBIDDEN_BUNDLE_BASENAMES:
            raise DeployConfigError(f"Refusing to deploy: {entry!r} is a credential/log file.")


def assert_pins_match_lockfile(container_requirements: tuple[str, ...], lockfile_text: str) -> None:
    """Asserts every container pin matches requirements.txt exactly, so the
    container can never silently run a different version than local tests."""
    locked: dict[str, str] = {}
    for line in lockfile_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, _, version = line.partition("==")
        locked[name.strip().lower()] = version.strip()
    for requirement in container_requirements:
        name, _, version = requirement.partition("==")
        name = name.split("[", 1)[0].strip().lower()
        if name not in locked:
            raise DeployConfigError(
                f"Container requirement {requirement!r} is not in requirements.txt; "
                f"add it there (and to the local venv) or drop it here."
            )
        if locked[name] != version:
            raise DeployConfigError(
                f"Container requirement {requirement!r} disagrees with "
                f"requirements.txt ({name}=={locked[name]}); align them before deploying."
            )


def iam_grant_commands(service_account: str) -> list[str]:
    """The gcloud grants both principals need, printed for a human to run.

    The first three are for the RUNTIME identity (--service-account); the
    last is for the DEPLOY-TIME Agent Platform Service Agent, which fetches
    the secret while building the deployment. Different principals — both
    need secretAccessor.
    """
    return [
        # Runtime: read the Gmail token secret at instance start.
        f"gcloud secrets add-iam-policy-binding {TOKEN_SECRET_NAME} "
        f"--project={PROJECT} "
        f'--member="serviceAccount:{service_account}" '
        f'--role="roles/secretmanager.secretAccessor"',
        # Runtime: invoke the MCP Cloud Run service (deployed
        # --no-allow-unauthenticated; IAM is its only boundary).
        f"gcloud run services add-iam-policy-binding {MCP_SERVICE_NAME} "
        f"--project={PROJECT} --region={LOCATION} "
        f'--member="serviceAccount:{service_account}" '
        f'--role="roles/run.invoker"',
        # Runtime: call Vertex AI models (gemini-2.5-* via the Vertex API).
        f"gcloud projects add-iam-policy-binding {PROJECT} "
        f'--member="serviceAccount:{service_account}" '
        f'--role="roles/aiplatform.user"',
        # Deploy time: the Agent Platform Service Agent resolves the secret
        # reference while building the deployment.
        f"gcloud secrets add-iam-policy-binding {TOKEN_SECRET_NAME} "
        f"--project={PROJECT} "
        f'--member="serviceAccount:{PLATFORM_SERVICE_AGENT}" '
        f'--role="roles/secretmanager.secretAccessor"',
    ]


def require_service_account(service_account: str | None) -> str:
    """The deployed agent must run as an explicit identity; the SDK would
    otherwise silently fall back to the default Reasoning Engine service
    agent, whose IAM grants nobody audited for this agent."""
    if service_account:
        return service_account
    suggested = f"email-agent-runtime@{PROJECT}.iam.gserviceaccount.com"
    grants = "\n".join(iam_grant_commands(suggested))
    raise DeployConfigError(
        "No --service-account was supplied, and this script never deploys on "
        "the default Reasoning Engine identity. Create a dedicated runtime "
        "identity and grant it what the agent needs:\n\n"
        f"gcloud iam service-accounts create email-agent-runtime "
        f'--project={PROJECT} --display-name="Email agent runtime"\n'
        f"{grants}\n\n"
        f"then re-run with --service-account {suggested}"
    )


def build_config(service_account: str, staging_bucket: str) -> dict[str, Any]:
    """The AgentEngineConfig dict passed to create()/update()."""
    if not staging_bucket.startswith("gs://"):
        raise DeployConfigError(
            f"--staging-bucket must be a gs:// URL, got {staging_bucket!r}. If the "
            f"bucket does not exist yet:\n"
            f"gcloud storage buckets create gs://{PROJECT}-agent-staging "
            f"--project={PROJECT} --location={LOCATION} --uniform-bucket-level-access"
        )
    env_vars = build_env_vars()
    validate_env_vars(env_vars)
    return {
        "display_name": DISPLAY_NAME,
        "description": DESCRIPTION,
        "agent_framework": "google-adk",
        "staging_bucket": staging_bucket,
        "requirements": list(CONTAINER_REQUIREMENTS),
        "extra_packages": list(EXTRA_PACKAGES),
        "env_vars": env_vars,
        "service_account": service_account,
        # min_instances defaults to 1 on this SDK, which bills continuously;
        # 0 scales to zero between conversations. max_instances=1 because
        # this is a single-user agent: managed sessions make the confirm
        # flow correct across instances, but one instance is the cheapest
        # correct configuration and also keeps the in-process body cache
        # warm within a conversation.
        "min_instances": 0,
        "max_instances": 1,
    }


def describe_env_vars(env_vars: dict[str, Any]) -> str:
    """Human-readable env var summary. Secret refs print as name/version
    only — never a payload."""
    lines = []
    for name, value in sorted(env_vars.items()):
        if isinstance(value, dict):
            lines.append(f"  {name} = <Secret Manager: {value['secret']} v{value['version']}>")
        else:
            lines.append(f"  {name} = {value}")
    return "\n".join(lines)


def _resolve_existing(client: Any, resource_name: str | None) -> str | None:
    """Create-vs-update is explicit: an existing engine (by resource name or
    unique display-name match) is updated in place; re-running create() on a
    taken display_name would silently make a duplicate."""
    if resource_name:
        return resource_name
    matches = [
        engine.api_resource.name
        for engine in client.agent_engines.list(config={"filter": f'display_name="{DISPLAY_NAME}"'})
    ]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    listing = "\n".join(f"  {name}" for name in matches)
    raise DeployConfigError(
        f"{len(matches)} agent engines already share display_name="
        f"{DISPLAY_NAME!r}:\n{listing}\n"
        f"Pass --resource-name to say which one to update."
    )


def deploy(service_account: str, staging_bucket: str, resource_name: str | None) -> str:
    """Runs the deploy. Returns the deployed engine's resource name."""
    if importlib.util.find_spec("cloudpickle") is None:
        raise DeployConfigError(
            "cloudpickle is not installed locally, and the SDK needs it to "
            "serialize the agent. Install the SDK's agent-engines extra into "
            "the venv first:\n"
            '  ./venv/bin/pip install "google-cloud-aiplatform[agent_engines]==1.163.0"'
        )

    import vertexai
    from vertexai.agent_engines import AdkApp

    # extra_packages are tarred relative to the CWD; agent.py imports
    # resolve from the repo root.
    os.chdir(REPO_ROOT)
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    validate_bundle(REPO_ROOT)
    assert_pins_match_lockfile(CONTAINER_REQUIREMENTS, (REPO_ROOT / "requirements.txt").read_text())
    config = build_config(service_account, staging_bucket)

    from agent import root_agent

    # No session_service_builder: the deployed default is the managed
    # VertexAiSessionService (see module docstring). enable_tracing stays
    # unset; the telemetry env var controls tracing.
    app = AdkApp(agent=root_agent)

    client = vertexai.Client(project=PROJECT, location=LOCATION)
    existing = _resolve_existing(client, resource_name)

    print(f"Bundle: {', '.join(EXTRA_PACKAGES)}")
    print("Env vars:")
    print(describe_env_vars(config["env_vars"]))
    print(f"Runtime identity: {service_account}")
    print("IAM grants both principals need (run these first if not already granted):")
    for command in iam_grant_commands(service_account):
        print(f"  {command}")

    if existing:
        print(f"Updating existing agent engine: {existing}")
        engine = client.agent_engines.update(name=existing, agent=app, config=config)
    else:
        print(f"Creating new agent engine (display_name={DISPLAY_NAME!r})")
        engine = client.agent_engines.create(agent=app, config=config)

    name = str(engine.api_resource.name)
    print(f"Deployed: {name}")
    return name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--service-account",
        help="Runtime identity for the deployed agent (required; never defaults).",
    )
    parser.add_argument(
        "--staging-bucket",
        default=f"gs://{PROJECT}-agent-staging",
        help="GCS bucket for staging the serialized agent (default: %(default)s).",
    )
    parser.add_argument(
        "--resource-name",
        help="Existing reasoningEngines resource name to update. Without it, an "
        "engine whose display_name matches is updated; otherwise one is created.",
    )
    args = parser.parse_args(argv)
    try:
        deploy(
            require_service_account(args.service_account),
            args.staging_bucket,
            args.resource_name,
        )
    except DeployConfigError as e:
        print(f"deploy_agent: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
