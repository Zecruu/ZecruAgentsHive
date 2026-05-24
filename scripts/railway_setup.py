"""One-shot Railway provisioning via GraphQL API.

Railway's CLI hangs on TUI prompts in non-TTY environments. This script uses the
GraphQL API directly with the token already in ~/.railway/config.json.

What it does:
1. Loads token + project_id + environment_id from local Railway config.
2. Creates a Postgres database service (if not already present).
3. Creates an empty app service named "agentshive" (if not already present).
4. Sets AGENTSHIVE_API_KEY and DATABASE_URL (referenced from Postgres) on the app service.
5. Generates and prints a public domain for the app service.

After this runs, `railway up -s agentshive --ci` will deploy the code.
"""

import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

import httpx

CONFIG = Path.home() / ".railway" / "config.json"
ENDPOINT = "https://backboard.railway.app/graphql/v2"
APP_SERVICE_NAME = "agentshive"


def load_ctx() -> dict[str, str]:
    raw = json.loads(CONFIG.read_text())
    token = raw["user"]["token"]
    cwd = str(Path.cwd())
    proj = raw["projects"].get(cwd)
    if not proj:
        sys.exit(f"No Railway project linked at {cwd}. Run `railway link` first.")
    return {
        "token": token,
        "project_id": proj["project"],
        "environment_id": proj["environment"],
    }


def gql(token: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    r = httpx.post(
        ENDPOINT,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"query": query, "variables": variables},
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    if "errors" in body:
        raise RuntimeError(json.dumps(body["errors"], indent=2))
    return body["data"]


def list_services(ctx) -> list[dict]:
    q = """
    query Project($id: String!) {
      project(id: $id) {
        services { edges { node { id name } } }
      }
    }
    """
    d = gql(ctx["token"], q, {"id": ctx["project_id"]})
    return [e["node"] for e in d["project"]["services"]["edges"]]


def create_postgres(ctx) -> str:
    q = """
    mutation Create($input: TemplateDeployV2Input!) {
      templateDeployV2(input: $input) { projectId workflowId }
    }
    """
    variables = {
        "input": {
            "projectId": ctx["project_id"],
            "environmentId": ctx["environment_id"],
            "templateCode": "postgres",
            "serializedConfig": {
                "services": {
                    "Postgres": {
                        "source": {"image": "ghcr.io/railwayapp-templates/postgres-ssl:17"},
                        "name": "Postgres",
                        "variables": {
                            "POSTGRES_DB": {"defaultValue": "railway"},
                            "POSTGRES_USER": {"defaultValue": "postgres"},
                            "POSTGRES_PASSWORD": {"defaultValue": "${{secret(32)}}"},
                        },
                        "volumeMounts": {"PostgresVolume": "/var/lib/postgresql/data"},
                    }
                },
                "volumes": {"PostgresVolume": {"mountPath": "/var/lib/postgresql/data"}},
            },
        }
    }
    d = gql(ctx["token"], q, variables)
    return d["templateDeployV2"]["workflowId"]


def create_empty_service(ctx, name: str) -> str:
    q = """
    mutation Create($input: ServiceCreateInput!) {
      serviceCreate(input: $input) { id name }
    }
    """
    variables = {
        "input": {
            "projectId": ctx["project_id"],
            "environmentId": ctx["environment_id"],
            "name": name,
        }
    }
    d = gql(ctx["token"], q, variables)
    return d["serviceCreate"]["id"]


def set_variables(ctx, service_id: str, vars: dict[str, str]) -> None:
    q = """
    mutation Upsert($input: VariableCollectionUpsertInput!) {
      variableCollectionUpsert(input: $input)
    }
    """
    variables = {
        "input": {
            "projectId": ctx["project_id"],
            "environmentId": ctx["environment_id"],
            "serviceId": service_id,
            "variables": vars,
            "replace": False,
        }
    }
    gql(ctx["token"], q, variables)


def create_domain(ctx, service_id: str) -> str:
    q = """
    mutation Create($input: ServiceDomainCreateInput!) {
      serviceDomainCreate(input: $input) { domain }
    }
    """
    variables = {
        "input": {
            "environmentId": ctx["environment_id"],
            "serviceId": service_id,
            "targetPort": 8000,
        }
    }
    d = gql(ctx["token"], q, variables)
    return d["serviceDomainCreate"]["domain"]


def main():
    ctx = load_ctx()
    print(f"Project: {ctx['project_id'][:8]}...  Env: {ctx['environment_id'][:8]}...")

    services = list_services(ctx)
    print(f"Existing services: {[s['name'] for s in services] or 'none'}")

    pg = next((s for s in services if s["name"].lower() in ("postgres", "postgresql")), None)
    if not pg:
        print("Creating Postgres via templateDeployV2...")
        wf = create_postgres(ctx)
        print(f"  triggered workflow {wf}")
    else:
        print(f"Postgres already exists: {pg['name']} ({pg['id'][:8]})")

    app = next((s for s in services if s["name"] == APP_SERVICE_NAME), None)
    if not app:
        print(f"Creating empty service '{APP_SERVICE_NAME}'...")
        app_id = create_empty_service(ctx, APP_SERVICE_NAME)
        print(f"  service id {app_id}")
    else:
        app_id = app["id"]
        print(f"App service already exists: {app['name']} ({app_id[:8]})")

    api_key = secrets.token_urlsafe(32)
    print("Setting variables on app service...")
    set_variables(ctx, app_id, {
        "AGENTSHIVE_API_KEY": api_key,
        "DATABASE_URL": "${{Postgres.DATABASE_URL}}",
        "TOOL_BLOCK_TIMEOUT_SECONDS": "50",
        "POLL_INTERVAL_SECONDS": "2",
    })
    print("  done.")

    print("Creating public domain...")
    try:
        domain = create_domain(ctx, app_id)
        print(f"  domain: https://{domain}")
    except Exception as e:
        print(f"  could not auto-create domain ({e!s}); generate via dashboard or `railway domain`")
        domain = None

    print("\n--- SAVE THESE — they will NOT be printed again ---")
    print(f"AGENTSHIVE_API_KEY = {api_key}")
    if domain:
        print(f"MCP endpoint       = https://{domain}/mcp")
    print(f"Service ID         = {app_id}")


if __name__ == "__main__":
    main()
