"""AXI contract tests for the Phase 3 governed gardening commands.

Covers the classes the release requires for this surface: TOON/JSON parity for
every new schema_version, malformed and crash inputs, repeatability, privacy,
mutation/rollback links, idempotency, v1-registry compatibility, and doctor
recovery reporting. Nothing here touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import build_vault
from megamind import toon
from megamind.registry import load_registry, save_registry
from test_cli import run_json, run_toon

CAPACITY = ("--capacity-known", "--applicable-quota", "100", "--reserve-quota", "50")

PROVISION_FLAGS = (
    "--domain",
    "synthetic release operations",
    "--repeat-demand",
    "3",
    "--multi-topic",
    "2",
    "--overlap",
    "repeated synthetic queries",
    "--scope",
    "release notes and rollout steps",
    "--exclusions",
    "payroll",
    "--owner",
    "owner@example.invalid",
    "--source-policy",
    "synthetic sources only",
    "--privacy",
    "company-private",
    "--model-access",
    "local",
    "--seed-topic",
    "release",
    "--seed-topic",
    "operations",
    "--maintenance",
    "quarterly review",
)


def create_gap(capsys: pytest.CaptureFixture[str], root: Path, topic: str = "rate limits") -> str:
    _, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "gap",
        "create",
        "--wiki",
        "ProductWiki",
        "--topic",
        topic,
        "--today",
        "2026-01-01",
    )
    return str(doc["gap"]["gap_id"])


# --- TOON/JSON parity for every new document --------------------------------


def test_every_gardening_document_renders_identically_in_toon_and_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Mutating commands are replayed against two identically prepared vaults so
    # each renderer sees the same first-time document.
    json_vault = build_vault(tmp_path / "a")
    toon_vault = build_vault(tmp_path / "b")
    gap_id = create_gap(capsys, json_vault)
    assert create_gap(capsys, toon_vault) == gap_id
    nomination = {
        "wave_id": "w1",
        "correlation_id": "c1",
        "gap_id": gap_id,
        "wiki": "ProductWiki",
        "topic": "rate limits",
        "relationship": "direct",
    }
    result = {"correlation_id": "c1", "sources": [{"origin": "synthetic", "eligible": True}]}
    invocations: list[tuple[str, list[str]]] = [
        ("megamind/gaps-result/v1", ["gap", "list"]),
        (
            "megamind/gap-result/v1",
            [
                "gap",
                "create",
                "--wiki",
                "ProductWiki",
                "--topic",
                "quotas",
                "--today",
                "2026-01-01",
            ],
        ),
        (
            "megamind/gap-transition/v1",
            ["gap", "transition", gap_id, "--status", "planned", "--today", "2026-01-02"],
        ),
        (
            "megamind/gap-attempt/v1",
            ["gap", "attempt", gap_id, "--outcome", "no source", "--today", "2026-01-03"],
        ),
        ("megamind/research-wave/v1", ["research-wave", gap_id, *CAPACITY]),
        (
            "megamind/research-result/v1",
            [
                "research-result",
                "--nomination-json",
                json.dumps(nomination),
                "--result-json",
                json.dumps(result),
            ],
        ),
        (
            "megamind/provisional-wiki-result/v1",
            ["provision-wiki", "ReleaseWiki", "ReleaseWiki", *PROVISION_FLAGS],
        ),
    ]
    for schema_version, argv in invocations:
        code_j, doc, err_j = run_json(capsys, "--root", str(json_vault), *argv)
        code_t, toon_out, err_t = run_toon(capsys, "--root", str(toon_vault), *argv)
        assert code_j == code_t == 0, (schema_version, doc)
        assert err_j == err_t == ""
        assert doc["schema_version"] == schema_version
        assert doc["help"]
        assert toon.encode(doc) == toon_out


def test_gap_list_empty_state_is_definitive(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc, err = run_json(capsys, "--root", str(vault), "gap", "list")
    assert code == 0
    assert err == ""
    assert doc == {
        "schema_version": "megamind/gaps-result/v1",
        "status": "ok",
        "gaps": [],
        "count": 0,
        "help": doc["help"],
    }


# --- malformed and crash inputs ---------------------------------------------


@pytest.mark.parametrize(
    "flag_value",
    ["ProductWiki", "ProductWiki=one", "=1", "ProductWiki=", "ProductWiki=1.5"],
)
def test_malformed_active_wiki_is_a_typed_usage_error(
    vault: Path, capsys: pytest.CaptureFixture[str], flag_value: str
) -> None:
    gap_id = create_gap(capsys, vault)
    code, doc, err = run_json(
        capsys,
        "--root",
        str(vault),
        "research-wave",
        gap_id,
        *CAPACITY,
        "--active-wiki",
        flag_value,
    )
    assert code == 2
    assert err == ""
    assert doc["schema_version"] == "megamind/error/v1"
    assert doc["code"] == "usage_error"
    assert "--active-wiki" in doc["message"]


@pytest.mark.parametrize("payload", ["[]", "3", "null", '"text"', "{"])
def test_non_object_bridge_json_is_a_typed_usage_error(
    vault: Path, capsys: pytest.CaptureFixture[str], payload: str
) -> None:
    valid = json.dumps(
        {
            "wave_id": "w1",
            "correlation_id": "c1",
            "gap_id": "g1",
            "wiki": "ProductWiki",
            "topic": "t",
            "relationship": "direct",
        }
    )
    for argv in (
        ["--nomination-json", payload, "--result-json", "{}"],
        ["--nomination-json", valid, "--result-json", payload],
    ):
        code, doc, err = run_json(capsys, "--root", str(vault), "research-result", *argv)
        assert code == 2
        assert err == ""
        assert doc["code"] == "usage_error"
    assert not list((vault / ".megamind/proposals").glob("research-ingest-*.json"))


@pytest.mark.parametrize("line", ["[]", "3", "null", "{}", "not json"])
def test_malformed_gap_journal_never_ends_in_a_traceback(
    vault: Path, capsys: pytest.CaptureFixture[str], line: str
) -> None:
    (vault / ".megamind/gaps.jsonl").write_text(line + "\n", encoding="utf-8")
    code, doc, err = run_json(capsys, "--root", str(vault), "gap", "list")
    assert code == 1
    assert err == ""
    assert doc["code"] == "garden_invalid"
    assert doc["help"]


def test_doctor_reports_a_malformed_gap_journal_in_a_registry_vault(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (vault / ".megamind/gaps.jsonl").write_text("[]\n", encoding="utf-8")
    code, doc, err = run_json(capsys, "--root", str(vault), "doctor")
    assert code == 1
    assert err == ""
    assert any(
        finding["check"] == "gaps" and finding["severity"] == "error" for finding in doc["findings"]
    )


def test_doctor_reports_a_malformed_gap_journal_in_a_canonical_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "solo"
    run_json(capsys, "init", str(root), "--wiki", "SoloWiki")
    (root / ".megamind/gaps.jsonl").write_text("3\n", encoding="utf-8")
    code, doc, _ = run_json(capsys, "--root", str(root), "doctor")
    assert code == 1
    assert any(finding["check"] == "gaps" for finding in doc["findings"])


def test_doctor_reports_a_broken_registry_even_beside_a_readable_card(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "solo"
    run_json(capsys, "init", str(root), "--wiki", "SoloWiki")
    (root / ".megamind/registry.json").write_text("{ not json", encoding="utf-8")
    code, doc, _ = run_json(capsys, "--root", str(root), "doctor")
    assert code == 1
    assert any(finding["check"] == "registry" for finding in doc["findings"])


def test_gap_transition_requires_an_explicit_status(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gap_id = create_gap(capsys, vault)
    code, doc, _ = run_json(capsys, "--root", str(vault), "gap", "transition", gap_id)
    assert code == 2
    assert doc["code"] == "usage_error"
    assert "--status" in doc["message"]
    _, listed, _ = run_json(capsys, "--root", str(vault), "gap", "list")
    assert listed["gaps"][0]["status"] == "open"
    assert listed["gaps"][0]["reopened_from"] == ""


def test_unknown_lifecycle_transition_is_typed(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gap_id = create_gap(capsys, vault)
    # open -> in_progress is not an allowed hop; the record must stay untouched.
    code, doc, _ = run_json(
        capsys, "--root", str(vault), "gap", "transition", gap_id, "--status", "in_progress"
    )
    assert code == 1
    assert doc["code"] == "gap_transition_invalid"
    assert doc["help"]
    _, listed, _ = run_json(capsys, "--root", str(vault), "gap", "list")
    assert listed["gaps"][0]["status"] == "open"


def test_missing_gap_is_typed(vault: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, doc, _ = run_json(capsys, "--root", str(vault), "research-wave", "nope", *CAPACITY)
    assert code == 1
    assert doc["code"] == "gap_not_found"


# --- repeatability and idempotency ------------------------------------------


def test_research_wave_is_byte_stable_for_the_same_inputs(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gap_id = create_gap(capsys, vault)
    argv = ("--root", str(vault), "research-wave", gap_id, *CAPACITY, "--today", "2026-01-05")
    _, first, _ = run_toon(capsys, *argv)
    _, second, _ = run_toon(capsys, *argv)
    assert first == second


def test_gap_create_is_idempotent_by_semantic_identity(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = create_gap(capsys, vault, "Rate  Limits")
    _, doc, _ = run_json(
        capsys,
        "--root",
        str(vault),
        "gap",
        "create",
        "--wiki",
        "ProductWiki",
        "--topic",
        "rate limits",
        "--today",
        "2026-02-01",
    )
    assert doc["status"] == "deduplicated"
    assert doc["gap"]["gap_id"] == first
    _, listed, _ = run_json(capsys, "--root", str(vault), "gap", "list")
    assert listed["count"] == 1


def test_research_result_replay_reuses_one_proposal(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    nomination = json.dumps(
        {
            "wave_id": "w1",
            "correlation_id": "c1",
            "gap_id": "g1",
            "wiki": "ProductWiki",
            "topic": "rate limits",
            "relationship": "direct",
        }
    )
    result = json.dumps(
        {"correlation_id": "c1", "sources": [{"origin": "synthetic", "eligible": True}]}
    )
    argv = (
        "--root",
        str(vault),
        "research-result",
        "--nomination-json",
        nomination,
        "--result-json",
        result,
    )
    _, first, _ = run_json(capsys, *argv)
    _, second, _ = run_json(capsys, *argv)
    assert first == second
    assert len(list((vault / ".megamind/proposals").glob("research-ingest-*.json"))) == 1


# --- privacy ----------------------------------------------------------------


def test_research_result_never_retains_ineligible_content_or_credentials(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    nomination = json.dumps(
        {
            "wave_id": "w1",
            "correlation_id": "c9",
            "gap_id": "g9",
            "wiki": "ProductWiki",
            "topic": "rate limits",
            "relationship": "direct",
        }
    )
    result = json.dumps(
        {
            "correlation_id": "c9",
            "sources": [
                {"origin": "synthetic", "summary": "api_key=CANARY_TOKEN", "eligible": True},
                {"origin": "private", "summary": "CANARY_INELIGIBLE", "eligible": False},
            ],
        }
    )
    _, doc, _ = run_json(
        capsys,
        "--root",
        str(vault),
        "research-result",
        "--nomination-json",
        nomination,
        "--result-json",
        result,
    )
    proposal = (vault / str(doc["ingest_proposal"])).read_text(encoding="utf-8")
    assert "CANARY_TOKEN" not in proposal
    assert "CANARY_INELIGIBLE" not in proposal
    assert "[redacted]" in proposal


# --- mutation, rollback, and audit links ------------------------------------


def test_provision_wiki_leaves_a_registry_backup_and_audit_link(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = (vault / ".megamind/registry.json").read_text(encoding="utf-8")
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(vault),
        "provision-wiki",
        "ReleaseWiki",
        "ReleaseWiki",
        *PROVISION_FLAGS,
        "--today",
        "2026-01-01",
    )
    assert code == 0
    assert doc["trusted"] is False
    backups = list((vault / ".megamind/audit/backups").glob("registry.json.*.bak"))
    assert [path.read_text(encoding="utf-8") for path in backups] == [before]
    audit = [
        json.loads(line)
        for line in (vault / ".megamind/audit/log.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    created = [entry for entry in audit if entry["action"] == "provisional-wiki-create"]
    assert created[0]["wiki"] == "ReleaseWiki"
    assert created[0]["provisional"] is True
    assert created[0]["registry_backup"] == backups[0].name


def test_provision_wiki_refuses_a_canonical_root_without_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "solo"
    run_json(capsys, "init", str(root), "--wiki", "SoloWiki")
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "provision-wiki",
        "ReleaseWiki",
        "ReleaseWiki",
        *PROVISION_FLAGS,
    )
    assert code == 1
    assert doc["code"] == "garden_invalid"
    assert not (root / ".megamind/registry.json").exists()
    assert not (root / "ReleaseWiki").exists()


def test_provision_wiki_refuses_an_absolute_path_without_writing(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(vault),
        "provision-wiki",
        "ReleaseWiki",
        str(vault / "ReleaseWiki"),
        *PROVISION_FLAGS,
    )
    assert code == 2
    assert doc["code"] == "registry_invalid"
    assert not (vault / "ReleaseWiki").exists()
    # The failed attempt left nothing behind, so the correct retry succeeds.
    retry, _, _ = run_json(
        capsys,
        "--root",
        str(vault),
        "provision-wiki",
        "ReleaseWiki",
        "ReleaseWiki",
        *PROVISION_FLAGS,
    )
    assert retry == 0


# --- v1 registry compatibility ----------------------------------------------


def downgrade_to_v1(root: Path) -> None:
    registry = load_registry(root)
    registry.version = 1
    for wiki in registry.wikis:
        wiki.sensitivity = ""
        wiki.model_access.local = ""
        wiki.model_access.cloud = ""
        wiki.routing_mode = ""
    save_registry(root, registry)


def test_v1_registry_keeps_working_and_provisioning_demands_explicit_migrate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    downgrade_to_v1(vault)
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(vault),
        "gap",
        "create",
        "--wiki",
        "ProductWiki",
        "--topic",
        "rate limits",
        "--today",
        "2026-01-01",
    )
    assert code == 0
    assert doc["gap"]["gap_id"]

    code, doc, _ = run_json(
        capsys,
        "--root",
        str(vault),
        "provision-wiki",
        "ReleaseWiki",
        "ReleaseWiki",
        *PROVISION_FLAGS,
    )
    assert code == 1
    assert doc["code"] == "garden_invalid"
    assert "migrate" in doc["message"]
    assert load_registry(vault).version == 1
    assert not (vault / "ReleaseWiki").exists()

    # Doctor still names the v1 registry, and migrate still surfaces its notes.
    _, report, _ = run_json(capsys, "--root", str(vault), "doctor")
    assert any("schema v1" in finding["message"] for finding in report["findings"])
    _, migrated, _ = run_json(capsys, "--root", str(vault), "migrate")
    assert migrated["notes"]
    assert (
        run_json(
            capsys,
            "--root",
            str(vault),
            "provision-wiki",
            "ReleaseWiki",
            "ReleaseWiki",
            *PROVISION_FLAGS,
        )[0]
        == 0
    )


# --- provisional is consumed, not just stored -------------------------------


def provisioned_vault(capsys: pytest.CaptureFixture[str], vault: Path) -> Path:
    run_json(
        capsys,
        "--root",
        str(vault),
        "provision-wiki",
        "ReleaseWiki",
        "ReleaseWiki",
        *PROVISION_FLAGS,
        "--today",
        "2026-01-01",
    )
    return vault


def test_route_offers_a_provisional_wiki_but_never_loads_it(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provisioned_vault(capsys, vault)
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(vault),
        "route",
        "operations",
        "--fields",
        "path,wiki,provisional,confidence",
    )
    assert code == 0
    candidates: list[dict[str, Any]] = doc["candidates"]
    # Confident enough to load, yet offered instead: the packet still names it.
    assert [item["wiki"] for item in candidates] == ["ReleaseWiki"]
    assert candidates[0]["provisional"] is True
    assert candidates[0]["confidence"] >= doc["thresholds"]["reliance_floor"]
    assert doc["decision"] == "offer"
    assert any("provisional" in note for note in doc["notes"])


def test_route_still_loads_a_trusted_candidate_beside_a_provisional_one(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provisioned_vault(capsys, vault)
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(vault),
        "route",
        "release",
        "operations",
        "--fields",
        "path,wiki,provisional",
    )
    assert code == 0
    assert doc["decision"] == "load"
    assert all(item["provisional"] is False for item in doc["candidates"])
    assert any("provisional" in note for note in doc["notes"])
    # The provisional candidate is named once, and never as a weak-evidence drop.
    floor_notes = [note for note in doc["notes"] if "reliance floor" in note]
    assert all("ReleaseWiki" not in note for note in floor_notes)


def test_catalog_and_preflight_surface_the_provisional_marker(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provisioned_vault(capsys, vault)
    _, catalog, _ = run_json(capsys, "catalog", "--estate", str(vault))
    rows = [row for row in catalog["wikis"] if row.get("name") == "ReleaseWiki"]
    assert rows and rows[0]["provisional"] is True
    assert all(
        row["provisional"] is False for row in catalog["wikis"] if row.get("name") == "ProductWiki"
    )

    _, preflight, _ = run_json(
        capsys,
        "preflight",
        "release",
        "operations",
        "notes",
        "--estate",
        str(vault),
        "--model-class",
        "local",
    )
    entries = [
        entry
        for entry in [*preflight["matches"], *preflight["offers"]]
        if entry["name"] == "ReleaseWiki"
    ]
    assert entries and entries[0]["provisional"] is True
    assert all(match["name"] != "ReleaseWiki" for match in preflight["matches"])


# --- the route governance sidecar -------------------------------------------


def governance_by_path(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row["path"]): row for row in doc["governance"]}


def test_governance_sidecar_is_emitted_on_the_default_field_set(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provisioned_vault(capsys, vault)
    code, doc, _ = run_json(capsys, "--root", str(vault), "route", "operations")
    assert code == 0
    # The default candidate columns stay exactly what route-result/v2 promised.
    assert list(doc["candidates"][0]) == ["path", "kind", "score", "reason"]
    rows = governance_by_path(doc)
    assert set(rows) == {str(item["path"]) for item in doc["candidates"]}
    entry = rows["ReleaseWiki/INDEX.md"]
    assert entry == {"path": "ReleaseWiki/INDEX.md", "provisional": True, "trusted": False}


def test_governance_sidecar_marks_a_trusted_load_packet(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc, _ = run_json(capsys, "--root", str(vault), "route", "pricing", "model")
    assert code == 0
    assert doc["decision"] == "load"
    assert doc["governance"]
    assert all(row["trusted"] is True and row["provisional"] is False for row in doc["governance"])
    assert not any("governance" in note for note in doc["notes"])


def test_governance_sidecar_covers_mixed_trusted_and_provisional_offers(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provisioned_vault(capsys, vault)
    code, doc, _ = run_json(capsys, "--root", str(vault), "route", "release", "operations")
    assert code == 0
    assert doc["decision"] == "load"
    rows = governance_by_path(doc)
    # The load packet itself is entirely trusted; the withheld provisional wiki
    # is still named in the notes as a governance decision, not a weak one.
    assert rows and all(row["trusted"] is True for row in rows.values())
    governance_notes = [note for note in doc["notes"] if note.startswith("governance gate")]
    assert len(governance_notes) == 1
    assert "ReleaseWiki/INDEX.md" in governance_notes[0]
    assert "reliance floor" not in governance_notes[0]


def test_route_notes_separate_governance_from_confidence_downgrades(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provisioned_vault(capsys, vault)
    _, gated, _ = run_json(capsys, "--root", str(vault), "route", "operations")
    assert gated["decision"] == "offer"
    assert gated["confidence"] >= gated["thresholds"]["reliance_floor"]
    downgrade = [note for note in gated["notes"] if "governance downgrade" in note]
    assert len(downgrade) == 1
    assert "not a confidence downgrade" in downgrade[0]
    # No confidence-derived wording may claim this offer.
    assert not any("ambiguity band" in note for note in gated["notes"])
    assert not any("is below the reliance floor" in note for note in gated["notes"])
    assert any("Provisional wikis stay offers" in entry for entry in gated["help"])
    assert not any("reliance floor" in entry for entry in gated["help"])

    _, banded, _ = run_json(capsys, "--root", str(vault), "route", "brand", "voice", "palette")
    assert banded["decision"] == "offer"
    assert not any("governance" in note for note in banded["notes"])
    assert any("reliance floor" in entry for entry in banded["help"])


def test_below_floor_offers_still_report_a_provisional_candidate(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A provisional wiki offered by the thresholds alone is still marked."""
    provisioned_vault(capsys, vault)
    code, doc, _ = run_json(
        capsys, "--root", str(vault), "route", "operations", "and", "unrelated", "words"
    )
    assert code == 0
    assert doc["decision"] == "offer"
    assert doc["confidence"] < doc["thresholds"]["reliance_floor"]
    rows = governance_by_path(doc)
    assert rows["ReleaseWiki/INDEX.md"]["provisional"] is True
    assert any(note.startswith("governance gate") for note in doc["notes"])
    assert any("is below the reliance floor" in note for note in doc["notes"])


def test_preflight_names_the_governance_downgrade_distinctly(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provisioned_vault(capsys, vault)
    _, doc, _ = run_json(
        capsys, "--root", str(vault), "preflight", "operations", "--model-class", "local"
    )
    assert doc["status"] == "ambiguous"
    assert doc["confidence"] >= doc["thresholds"]["reliance_floor"]
    assert doc["matches"] == []
    assert [offer["name"] for offer in doc["offers"]] == ["ReleaseWiki"]
    assert doc["offers"][0]["provisional"] is True
    assert any("governance gate" in note and "ReleaseWiki" in note for note in doc["notes"])
    assert any("governance downgrade" in note for note in doc["notes"])
    assert not any("ambiguity band" in note for note in doc["notes"])
    assert not any("below the reliance floor" in note for note in doc["notes"])


def test_preflight_names_a_provisional_offer_the_thresholds_produced(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An ambiguity-band offer still says which of its wikis are provisional."""
    provisioned_vault(capsys, vault)
    _, doc, _ = run_json(
        capsys, "preflight", "operations", "--estate", str(vault), "--model-class", "local"
    )
    assert doc["status"] == "ambiguous"
    assert doc["matches"] == []
    # The estate projects the provisional wiki twice: from the registry entry
    # and from its own canonical card root. Both are offers, neither a match.
    assert {offer["name"] for offer in doc["offers"]} == {"ReleaseWiki"}
    assert all(offer["provisional"] is True for offer in doc["offers"])
    governance = [note for note in doc["notes"] if note.startswith("governance gate")]
    assert len(governance) == 1
    assert governance[0].endswith("evaluation pass: ReleaseWiki")
    assert any("ambiguity band" in note for note in doc["notes"])
    assert not any("governance downgrade" in note for note in doc["notes"])


def test_route_governance_sidecar_renders_identically_in_toon_and_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    json_vault = provisioned_vault(capsys, build_vault(tmp_path / "a"))
    toon_vault = provisioned_vault(capsys, build_vault(tmp_path / "b"))
    for query in (["operations"], ["release", "operations"], ["pricing", "model"]):
        _, doc, _ = run_json(capsys, "--root", str(json_vault), "route", *query)
        _, toon_out, err = run_toon(capsys, "--root", str(toon_vault), "route", *query)
        assert err == ""
        assert "governance[" in toon_out
        assert toon.encode(doc) == toon_out


def test_governance_sidecar_leaks_no_unauthorized_path_or_content(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The sidecar only ever restates an identity the packet already carries."""
    provisioned_vault(capsys, vault)
    for query in (["operations"], ["release"], ["archive", "history"], ["research", "interview"]):
        _, doc, _ = run_json(capsys, "--root", str(vault), "route", *query)
        emitted = {str(item["path"]) for item in doc["candidates"]}
        assert {str(row["path"]) for row in doc["governance"]} == emitted
        assert all(set(row) == {"path", "provisional", "trusted"} for row in doc["governance"])


def test_doctor_reports_an_escaping_gap_journal_instead_of_losing_the_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("", encoding="utf-8")
    journal = vault / ".megamind/gaps.jsonl"
    journal.unlink(missing_ok=True)
    journal.symlink_to(outside)
    code, doc, err = run_json(capsys, "--root", str(vault), "doctor")
    assert code == 1
    assert err == ""
    assert doc["schema_version"] == "megamind/doctor-report/v1"
    checks = {finding["check"] for finding in doc["findings"]}
    assert "gaps" in checks
    assert "symlinks" in checks


def test_doctor_reports_an_unreadable_gap_journal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    (vault / ".megamind/gaps.jsonl").write_bytes(b"\xff\xfe not utf 8\n")
    code, doc, _ = run_json(capsys, "--root", str(vault), "doctor")
    assert code == 1
    assert doc["schema_version"] == "megamind/doctor-report/v1"
    assert any(finding["check"] == "gaps" for finding in doc["findings"])


def test_route_document_stays_backward_compatible(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every v1/v2 field keeps its name, meaning, and position-independent shape."""
    code, doc, _ = run_json(capsys, "--root", str(vault), "route", "pricing", "model")
    assert code == 0
    assert doc["schema_version"] == "megamind/route-result/v2"
    assert set(doc) >= {
        "schema_version",
        "query",
        "matched",
        "decision",
        "confidence",
        "thresholds",
        "semantic",
        "candidates",
        "governance",
        "context_chars",
        "max_context_chars",
        "max_candidates",
        "notes",
        "help",
    }
    assert list(doc["candidates"][0]) == ["path", "kind", "score", "reason"]
    assert doc["thresholds"] == {
        "reliance_floor": 0.75,
        "offer_floor": 0.25,
        "ambiguity_band": 0.05,
    }
