from dev_agents.workflows.degodify import DegodifyFile, select_candidate


def test_select_candidate_skips_active_and_catalog_files() -> None:
    files = [
        DegodifyFile("src/catalog.ts", 900, 20, "Data Catalog", "CRITICAL", True),
        DegodifyFile("src/active-service.ts", 800, 700, "Service", "CRITICAL"),
        DegodifyFile("src/eligible-service.ts", 700, 600, "Service", "WATCH"),
    ]
    selection = select_candidate(files, ["curator/active-service-refactor"])
    assert selection.candidate == files[2]
    assert selection.skipped[0].file == files[1]


def test_select_candidate_returns_none_when_all_are_ineligible() -> None:
    files = [DegodifyFile("src/stable.ts", 100, 80, "Utility", "STABLE")]
    assert select_candidate(files, []).candidate is None
