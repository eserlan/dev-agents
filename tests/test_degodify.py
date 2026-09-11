from dev_agents.workflows.degodify import DegodifyFile, analyze_repository, select_candidate


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


def test_analyze_repository_ignores_build_outputs_and_ranks_large_sources(tmp_path) -> None:
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "generated.ts").write_text("x\n" * 2000)
    (tmp_path / "source.ts").write_text("const value = 1;\n" * 600)
    files = analyze_repository(tmp_path)
    assert files[0].relative_path == "source.ts"
    assert files[0].status == "WATCH"
