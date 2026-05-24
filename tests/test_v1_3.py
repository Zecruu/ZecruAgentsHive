"""v1.3 feature coverage: tool-catalog drift detection via get_server_info + refresh_tool_catalog.

Run against a fresh local server:
    AGENTSHIVE_API_KEY=test-key PORT=8001 TOOL_BLOCK_TIMEOUT_SECONDS=8 \
        python -m agentshive.main &
    python tests/test_v1_3.py
"""

import asyncio
import os
import re
import sys

from fastmcp import Client

KEY = os.environ.get("AGENTSHIVE_API_KEY", "test-key")
URL = os.environ.get("AGENTSHIVE_URL", "http://localhost:8001/mcp")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _c(r):
    return r.structured_content if r.structured_content is not None else (r.content[0].text if r.content else None)


async def test_get_server_info_shape():
    print("--- T1: get_server_info returns the three required fields ---")
    async with Client(URL, auth=KEY) as cli:
        r = _c(await cli.call_tool("get_server_info", {}))
        for k in ("server_version", "tools_catalog_hash", "started_at"):
            assert k in r, f"missing key {k} in {r}"
            assert r[k], f"empty value for {k}: {r}"
        assert ISO_RE.match(r["started_at"]), f"started_at not ISO-shaped: {r['started_at']}"
        assert len(r["tools_catalog_hash"]) == 16, f"hash should be 16 hex chars: {r['tools_catalog_hash']}"
        print(f"  [OK] version={r['server_version']} hash={r['tools_catalog_hash']} started_at={r['started_at'][:19]}")


async def test_get_server_info_hash_stable():
    print("--- T2: tools_catalog_hash is stable across consecutive calls ---")
    async with Client(URL, auth=KEY) as cli:
        a = _c(await cli.call_tool("get_server_info", {}))
        b = _c(await cli.call_tool("get_server_info", {}))
        assert a["tools_catalog_hash"] == b["tools_catalog_hash"], f"hash drifted between calls: {a} vs {b}"
        assert a["started_at"] == b["started_at"], f"started_at drifted: {a} vs {b}"
        print(f"  [OK] hash and started_at stable")


async def test_hash_changes_when_extra_tool_registered():
    """Prove the hash is actually a function of the tool surface, not a constant.

    We compute it directly via the same helper used by the tool, with a manually
    augmented name list. This avoids the complexity of spinning up a second
    server with a different tool set just to compare.
    """
    print("--- T3: hash differs from a variant with an extra tool name ---")
    import sys as _sys
    _sys.path.insert(0, "src")
    from agentshive.tools import _compute_tools_catalog_hash

    async with Client(URL, auth=KEY) as cli:
        live = _c(await cli.call_tool("get_server_info", {}))["tools_catalog_hash"]

    # Pull the tool list a different way (via Client.list_tools) and compute what
    # the hash *would* be if we added a phantom tool. If the hash function is
    # actually hashing the catalog, the live value should match the live name set,
    # and adding any name should produce a different hash.
    async with Client(URL, auth=KEY) as cli:
        tools = await cli.list_tools()
        names = [t.name for t in tools]
    computed = _compute_tools_catalog_hash(names)
    augmented = _compute_tools_catalog_hash(names + ["phantom_extra_tool"])
    assert computed == live, f"recomputed hash {computed} doesn't match live {live}"
    assert augmented != live, f"augmented hash should differ from live: both {live}"
    print(f"  [OK] live=computed={live}, augmented(+phantom)={augmented}")


async def test_refresh_tool_catalog_ok():
    print("--- T4: refresh_tool_catalog returns ok=true + matching hash ---")
    async with Client(URL, auth=KEY) as cli:
        info = _c(await cli.call_tool("get_server_info", {}))
        refresh = _c(await cli.call_tool("refresh_tool_catalog", {}))
        assert refresh.get("ok") is True, f"expected ok=True, got {refresh}"
        assert refresh["tools_catalog_hash"] == info["tools_catalog_hash"], f"hash mismatch: {info} vs {refresh}"
        assert "tools/list_changed" in refresh.get("message", "") or "compliant" in refresh.get("message", "").lower(), (
            f"message should mention the notification or compliant clients: {refresh}"
        )
        print(f"  [OK] ok={refresh['ok']}, hash matches, message mentions notification")


async def test_server_version_matches_package():
    print("--- T5: server_version matches package __version__ ---")
    import sys as _sys
    _sys.path.insert(0, "src")
    from agentshive import __version__ as pkg_version

    async with Client(URL, auth=KEY) as cli:
        info = _c(await cli.call_tool("get_server_info", {}))
    assert info["server_version"] == pkg_version, f"server_version {info['server_version']} != package {pkg_version}"
    print(f"  [OK] both report {pkg_version}")


async def main():
    await test_get_server_info_shape()
    await test_get_server_info_hash_stable()
    await test_hash_changes_when_extra_tool_registered()
    await test_refresh_tool_catalog_ok()
    await test_server_version_matches_package()
    print("\nALL v1.3 TESTS PASS")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)
