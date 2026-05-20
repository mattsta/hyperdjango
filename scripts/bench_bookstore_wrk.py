"""
Bookstore API wrk benchmark — wire-speed throughput via the real Zig
HTTP server.

Counterpart to ``scripts/profile_list_cprofile.py`` and parallel
sibling to ``scripts/bench_hypernews_wrk.py``. All shared machinery
lives in ``scripts/_wrk_bench.py`` — this file only declares the
target list, the app spec, and the `hyper setup` arguments.

Run: uv run python scripts/bench_bookstore_wrk.py
"""

from _wrk_bench import WrkBenchmark, WrkTarget, run_wrk_benchmark

_TARGETS: list[WrkTarget] = [
    WrkTarget(
        "books_list",
        "/api/v1/books/",
        "GET /api/v1/books/ (List + serializer)",
    ),
    WrkTarget(
        "books_detail",
        "/api/v1/books/1",
        "GET /api/v1/books/1 (Detail + select_related)",
    ),
    WrkTarget(
        "books_stats",
        "/api/v1/books/stats",
        "GET /api/v1/books/stats (Aggregate)",
    ),
    WrkTarget(
        "reviews_list",
        "/api/v1/reviews/",
        "GET /api/v1/reviews/ (CursorPagination)",
    ),
    WrkTarget(
        "books_search",
        "/api/v1/books/?search=python",
        "GET books?search=python (FTS)",
    ),
    WrkTarget(
        "health",
        "/health",
        "GET /health (no DB baseline)",
    ),
]


def main() -> None:
    run_wrk_benchmark(
        WrkBenchmark(
            name="Bookstore API",
            app_spec="services.bookstore_api.app:app",
            port=18802,
            targets=_TARGETS,
            setup_args=[
                "--drop",
                "--seed",
                "services.bookstore_api.seed:run",
            ],
            output_slug="bookstore",
        )
    )


if __name__ == "__main__":
    main()
