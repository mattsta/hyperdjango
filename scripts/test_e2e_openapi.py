"""
Verify REST API OpenAPI spec completeness and correctness.
"""

# hyper-test: e2e

from e2e_helper import TEST_PORTS, AppRunner, http_get

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
        return True
    FAIL += 1
    msg = f"  FAIL  {name}" + (f" — {detail}" if detail else "")
    print(msg)
    ERRORS.append(msg)
    return False


def main() -> None:
    global PASS, FAIL

    print("=" * 60)
    print("REST API OpenAPI Spec Audit")
    print("=" * 60)

    with AppRunner(
        "services.rest_api.app:app", host="127.0.0.1", port=TEST_PORTS["openapi"]
    ) as runner:
        base = runner.url()

        r = http_get(f"{base}/openapi.json")
        ok("OpenAPI spec loads", r.status == 200)

        spec = r.json
        ok("Has openapi version", "openapi" in spec)
        ok("Version is 3.1", spec.get("openapi", "").startswith("3.1"))
        ok("Has info.title", spec.get("info", {}).get("title") == "Blog API")

        paths = spec.get("paths", {})
        ok("Has paths", len(paths) > 0)

        # Check expected endpoints exist
        expected = [
            "/auth/register",
            "/auth/login",
            "/auth/logout",
            "/api/posts",
            "/api/posts/{pid}",
            "/api/admin/stats",
            "/api/admin/users",
            "/health",
        ]
        for ep in expected:
            found = any(ep in p for p in paths)
            ok(f"Endpoint {ep} documented", found, f"not found in {list(paths.keys())}")

        # Check methods
        posts_path = paths.get("/api/posts", {})
        ok("/api/posts has GET", "get" in posts_path)
        ok("/api/posts has POST", "post" in posts_path)

        post_id_path = paths.get("/api/posts/{pid}", {})
        ok("/api/posts/{pid} has GET", "get" in post_id_path)
        ok("/api/posts/{pid} has PUT", "put" in post_id_path)
        ok("/api/posts/{pid} has DELETE", "delete" in post_id_path)

        # Swagger UI
        r = http_get(f"{base}/docs")
        ok("Swagger UI loads", r.status == 200)
        ok(
            "Swagger UI has HTML",
            "<html" in r.body.lower() or "swagger" in r.body.lower(),
        )

    print("\n" + "=" * 60)
    total = PASS + FAIL
    print(f"Results: {PASS}/{total} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(e)
    print("=" * 60)
    raise SystemExit(1 if FAIL > 0 else 0)


if __name__ == "__main__":
    main()
