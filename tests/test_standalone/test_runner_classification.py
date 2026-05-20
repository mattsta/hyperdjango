"""Contract tests for the test runner's marker classification.

`classify_test` is the single authority that turns `# hyper-test*` markers
into scheduling decisions (kind lane, timeout budget, concurrency cap, flaky
retry). Every rule here is load-bearing for the whole suite: a silently
misparsed marker reclassifies a file into the wrong lane, which surfaces as
nondeterministic CI failures far from the real cause — so the invalid shapes
must fail loudly at classification time, not at run time.
"""

from pathlib import Path

import pytest

from hyperdjango.test_runner import (
    E2E_TIMEOUT_SECONDS,
    TEST_TIMEOUT_SECONDS,
    classify_test,
)


def _classify(tmp_path: Path, body: str):
    f = tmp_path / "test_sample.py"
    f.write_text(body)
    return classify_test(f)


def test_unit_is_canonical_and_pure_is_an_alias(tmp_path):
    assert _classify(tmp_path, "# hyper-test: unit\n").kind == "unit"
    # `pure` is still honored as an alias for `unit`: the marker gate rejects it
    # in scripts/, but its --fix path resolves `pure` → `unit` THROUGH
    # classify_test, so the alias must stay reachable here.
    assert _classify(tmp_path, "# hyper-test: pure\n").kind == "unit"


def test_unknown_kind_is_rejected_loudly(tmp_path):
    with pytest.raises(ValueError, match="invalid '# hyper-test: db'"):
        _classify(tmp_path, "# hyper-test: db\n")


def test_unmarked_file_is_rejected_loudly(tmp_path):
    # No content heuristics: a file with no `# hyper-test:` marker cannot be
    # classified into a resource lane and must fail loudly, pointing at the
    # marker and the check_test_markers --fix tool. Content that would once have
    # been sniffed as a DB/e2e test (Database(...), AppRunner(...)) no longer
    # rescues an unmarked file.
    with pytest.raises(ValueError, match="no '# hyper-test: <kind>' marker"):
        _classify(tmp_path, "import os\n\nDatabase('x')\nAppRunner('a:b', port=1)\n")
    with pytest.raises(ValueError, match="check_test_markers.py --fix"):
        _classify(tmp_path, "x = 1\n")


def test_timeout_marker_overrides_default(tmp_path):
    meta = _classify(tmp_path, "# hyper-test: unit\n# hyper-test-timeout: 500\n")
    assert meta.timeout == 500


def test_no_timeout_marker_leaves_category_default(tmp_path):
    meta = _classify(tmp_path, "# hyper-test: unit\n")
    assert meta.timeout == 0  # scheduler substitutes the category default
    assert TEST_TIMEOUT_SECONDS > 0 and E2E_TIMEOUT_SECONDS > 0


def test_concurrency_low_is_recorded(tmp_path):
    meta = _classify(tmp_path, "# hyper-test: unit\n# hyper-test-concurrency: low\n")
    assert meta.concurrency == "low"


def test_concurrency_rejects_unknown_values(tmp_path):
    with pytest.raises(ValueError, match="hyper-test-concurrency"):
        _classify(tmp_path, "# hyper-test: unit\n# hyper-test-concurrency: solo\n")


def test_concurrency_low_conflicts_with_db_shared(tmp_path):
    # The shared-DB lane is serial (width 1); `low` substituting a width-2
    # semaphore would WIDEN it and race shared-DB tests against each other.
    with pytest.raises(ValueError, match="shared-DB lane"):
        _classify(tmp_path, "# hyper-test: db_shared\n# hyper-test-concurrency: low\n")


def test_flaky_requires_a_reason(tmp_path):
    meta = _classify(
        tmp_path, "# hyper-test: unit\n# hyper-test-flaky: shared gauge race\n"
    )
    assert meta.flaky_reason == "shared gauge race"


def test_flaky_empty_reason_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="non-empty reason"):
        _classify(tmp_path, "# hyper-test: unit\n# hyper-test-flaky:\n")


def test_flaky_reason_never_swallows_the_next_line(tmp_path):
    # A reason is captured on the marker's own line only: an empty reason
    # followed by source code must still be rejected, never adopt `x = 1`.
    with pytest.raises(ValueError, match="non-empty reason"):
        _classify(tmp_path, "# hyper-test: unit\n# hyper-test-flaky:\nx = 1\n")


def test_e2e_extracts_literal_port(tmp_path):
    # The extractor matches a lowercase literal `port = NNNN` assignment; files
    # using the TEST_PORTS registry resolve their port at runtime instead and
    # classify with port 0.
    meta = _classify(tmp_path, "# hyper-test: e2e\nport = 18099\n")
    assert meta.kind == "e2e"
    assert meta.port == 18099


def test_e2e_honors_per_file_timeout_override(tmp_path):
    meta = _classify(tmp_path, "# hyper-test: e2e\n# hyper-test-timeout: 600\n")
    assert meta.kind == "e2e" and meta.timeout == 600
