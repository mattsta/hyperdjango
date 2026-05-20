"""
HyperNews wrk benchmark — wire-speed throughput via the real Zig
HTTP server.

Counterpart to ``scripts/profile_hypernews_cprofile.py``. Uses the
shared machinery in ``scripts/_wrk_bench.py`` — this file only
declares the target list, the app spec, the `hyper setup` arguments,
and a pre-hook that (a) pre-seeds extra data and (b) resolves the
HMAC-signed post external ID used by the `/post/{pid}` endpoint so
the URL is valid when wrk hits it.

Run: uv run python scripts/bench_hypernews_wrk.py
"""

from _wrk_bench import WrkBenchmark, WrkTarget, run_wrk_benchmark

# `__POST_PID__` marker is rewritten by the pre-hook to the actual
# HMAC-signed external ID of the first seeded post.
_TARGETS: list[WrkTarget] = [
    WrkTarget("index_cached", "/", "GET / (cached index)"),
    WrkTarget(
        "post_detail",
        "__POST_PID__",
        "GET /post/{pid} (uncached detail + tree)",
    ),
    WrkTarget("user_profile", "/user/alice", "GET /user/alice (multi-query)"),
    WrkTarget("forums_list", "/forums", "GET /forums (directory)"),
    WrkTarget("login_form", "/login", "GET /login (template-only baseline)"),
]


async def _pre_hook(targets: list[WrkTarget]) -> list[WrkTarget]:
    """Pre-benchmark setup: seed extra data + resolve the post_detail URL.

    Runs after `hyper setup --drop --seed` but before `AppRunner`
    starts, so the DB is fully populated before the server boots.
    """
    # Importing the app module binds HyperApp → Database so get_db()
    # returns the right pool inside _seed_extra_data().
    # The cProfile script's seeding helper is reusable here so the
    # workload matches between the two harnesses.
    from profile_hypernews_cprofile import _seed_extra_data

    from hyperdjango.database import get_db
    from services.hypernews.app import app  # noqa: F401 — import side effects
    from services.hypernews.models import Post

    db = get_db()
    if db._pool_handle is None:
        await db.connect()

    await _seed_extra_data()

    first_post = await Post.objects.order_by("id").first()
    if first_post is None:
        raise RuntimeError("No posts seeded — run `hyper setup --seed` first")
    post_pid = first_post.get_external_id()
    print(f"  post external ID: {post_pid}")

    # Rewrite the post_detail target with the resolved pid.
    rewritten: list[WrkTarget] = []
    for target in targets:
        if target.path == "__POST_PID__":
            rewritten.append(WrkTarget(target.slug, f"/post/{post_pid}", target.label))
        else:
            rewritten.append(target)
    return rewritten


def main() -> None:
    run_wrk_benchmark(
        WrkBenchmark(
            name="HyperNews",
            app_spec="services.hypernews.app:app",
            port=18801,
            targets=_TARGETS,
            setup_args=[
                "--drop",
                "--seed",
                "services.hypernews.setup:seed",
            ],
            output_slug="hypernews",
            pre_hook=_pre_hook,
        )
    )


if __name__ == "__main__":
    main()
