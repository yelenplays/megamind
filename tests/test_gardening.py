from __future__ import annotations

import json
from pathlib import Path

import pytest

from megamind.card import load_wiki_card
from megamind.gardening import (
    CapacityInput,
    GapRecord,
    GapStore,
    GardenError,
    InvalidTransition,
    PriorityInputs,
    ProvisionCriteria,
    _short,
    apply_provision_plan,
    ingest_research_result,
    make_nomination,
    plan_provision_wiki,
    plan_research_wave,
    provision_local_wiki,
    resume_provision,
    rollback_provision,
    validate_gap_journal,
)
from megamind.registry import RegistryError, load_registry, save_registry
from megamind.scaffold import init_vault, init_wiki_root


def make_criteria(**overrides: object) -> ProvisionCriteria:
    """The fully-qualified synthetic criteria set; override one field per case."""
    fields: dict[str, object] = {
        "accepted_domain": "synthetic release operations",
        "repeat_demand": 3,
        "multi_topic": 2,
        "overlap": "overlap with repeated queries",
        "scope": "release notes",
        "exclusions": "payroll",
        "owner": "owner@example.invalid",
        "source_policy": "synthetic sources",
        "privacy": "company-private",
        "model_access": "local",
        "seed_topics": ("release", "operations"),
        "maintenance": "quarterly review",
    }
    fields.update(overrides)
    return ProvisionCriteria(**fields)  # type: ignore[arg-type]


def test_gap_identity_lifecycle_and_replay(tmp_path: Path) -> None:
    store = GapStore(tmp_path)
    first = store.create(
        GapRecord.new(
            "ProductWiki",
            "Rate limits",
            "weak",
            priority=PriorityInputs(4, 3, 2, 1, 4),
            today="2026-01-01",
        )
    )
    assert store.create(GapRecord.new("ProductWiki", "rate  limits", "weak")) == first
    store.transition(first.gap_id, "rejected", reason="needs owner review", today="2026-01-02")
    reopened = store.transition(first.gap_id, "open", today="2026-01-03")
    assert reopened.reopened_from == "rejected"
    attempted = store.attempt(first.gap_id, "no eligible source", correlation_id="c1")
    assert attempted.attempts[0]["correlation_id"] == "c1"
    assert len((tmp_path / ".megamind/gaps.jsonl").read_text().splitlines()) == 4


def test_cooldown_survives_a_later_transition_that_omits_it(tmp_path: Path) -> None:
    store = GapStore(tmp_path)
    gap = store.create(GapRecord.new("ProductWiki", "Rate limits", today="2026-01-01"))
    store.transition(gap.gap_id, "nominated", today="2026-01-02")
    store.attempt(gap.gap_id, "no source", cooldown_until="2026-02-01", today="2026-01-03")
    paused = store.transition(gap.gap_id, "paused", today="2026-01-04")
    assert paused.cooldown_until == "2026-02-01"
    assert store.get(gap.gap_id).cooldown_until == "2026-02-01"
    cleared = store.transition(gap.gap_id, "open", cooldown_until="", today="2026-01-05")
    assert cleared.cooldown_until == ""


def test_gap_audit_records_carry_the_mutation_details(tmp_path: Path) -> None:
    store = GapStore(tmp_path)
    gap = store.create(GapRecord.new("ProductWiki", "Rate limits", today="2026-01-01"))
    store.transition(gap.gap_id, "planned", today="2026-01-02")
    store.attempt(gap.gap_id, "no source", correlation_id="c1", today="2026-01-03")
    events = [
        json.loads(line)
        for line in (tmp_path / ".megamind/audit/log.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert [entry["event"] for entry in events] == ["created", "transition", "attempt"]
    assert events[1]["status"] == "planned"
    assert events[2]["attempt_id"] == store.get(gap.gap_id).attempts[0]["attempt_id"]
    assert all(entry["gap_id"] == gap.gap_id for entry in events)
    assert all(entry["audit_ref"] == f"gaps.jsonl:{gap.gap_id}" for entry in events)


@pytest.mark.parametrize("line", ["[]", "3", "null", '"text"', "{}", '{"schema": "other"}'])
def test_malformed_journal_lines_are_typed_errors_not_crashes(tmp_path: Path, line: str) -> None:
    (tmp_path / ".megamind").mkdir(parents=True)
    (tmp_path / ".megamind/gaps.jsonl").write_text(line + "\n", encoding="utf-8")
    with pytest.raises(GardenError, match="invalid gap journal entry"):
        GapStore(tmp_path).records()
    assert validate_gap_journal(tmp_path)


def test_research_result_rejects_a_source_without_an_origin(tmp_path: Path) -> None:
    gap = GapRecord.new("A", "topic")
    wave = plan_research_wave([gap], gap.gap_id, CapacityInput(True, 0, {}, 100, 25))
    nomination = make_nomination(wave.wave_id, wave.nominations[0])
    for sources in ([{"summary": "s", "eligible": True}], [{"origin": 7, "eligible": True}]):
        with pytest.raises(GardenError, match="origin"):
            ingest_research_result(
                tmp_path,
                nomination,
                {"correlation_id": nomination.correlation_id, "sources": sources},
            )
    assert not list((tmp_path / ".megamind/proposals").glob("*.json"))


@pytest.mark.parametrize("payload", [[], "text", 3])
def test_research_bridge_rejects_non_object_payloads(tmp_path: Path, payload: object) -> None:
    gap = GapRecord.new("A", "topic")
    wave = plan_research_wave([gap], gap.gap_id, CapacityInput(True, 0, {}, 100, 25))
    with pytest.raises(GardenError, match="JSON object"):
        make_nomination(wave.wave_id, payload)  # type: ignore[arg-type]
    nomination = make_nomination(wave.wave_id, wave.nominations[0])
    with pytest.raises(GardenError, match="JSON object"):
        ingest_research_result(tmp_path, nomination, payload)  # type: ignore[arg-type]


def test_wave_is_one_hop_and_capacity_is_typed() -> None:
    direct = GapRecord.new("A", "one", related_topics=["two", "three", "four"])
    wave = plan_research_wave(
        [direct],
        direct.gap_id,
        CapacityInput(True, 0, {}, 100, 25),
        today="2026-01-01",
    )
    assert wave.status == "planned"
    assert len(wave.nominations) == 3
    assert len(wave.deferred_nominations) == 1
    paused = plan_research_wave([direct], direct.gap_id, CapacityInput(False, 0, {}, None, None))
    assert paused.status == "paused"
    assert paused.reason == "capacity_unknown"
    refused = plan_research_wave([direct], direct.gap_id, CapacityInput(True, 0, {}, 100, 24))
    assert refused.status == "refused"
    assert refused.reason == "quota_reserve_below_25_percent"


def test_research_bridge_is_eligible_bounded_and_idempotent(tmp_path: Path) -> None:
    gap = GapRecord.new("A", "topic")
    wave = plan_research_wave([gap], gap.gap_id, CapacityInput(True, 0, {}, 100, 25))
    nomination = make_nomination(wave.wave_id, wave.nominations[0])
    result = ingest_research_result(
        tmp_path,
        nomination,
        {
            "correlation_id": nomination.correlation_id,
            "sources": [
                {"origin": "synthetic-source", "summary": "safe summary", "eligible": True},
                {"origin": "private-source", "summary": "CANARY_SECRET", "eligible": False},
            ],
        },
    )
    assert result.status == "proposed"
    proposal = json.loads((tmp_path / result.ingest_proposal).read_text())
    assert "CANARY_SECRET" not in json.dumps(proposal)
    again = ingest_research_result(
        tmp_path,
        nomination,
        {
            "correlation_id": nomination.correlation_id,
            "sources": [
                {"origin": "synthetic-source", "summary": "safe summary", "eligible": True}
            ],
        },
    )
    assert again.ingest_proposal == result.ingest_proposal
    assert len(list((tmp_path / ".megamind/proposals").glob("research-ingest-*.json"))) == 1


def test_research_result_without_an_eligible_source_writes_nothing(tmp_path: Path) -> None:
    """A proposal keyed by correlation can never be repaired, so an empty
    result must not create one, audit one, or log one."""
    gap = GapRecord.new("A", "topic")
    wave = plan_research_wave([gap], gap.gap_id, CapacityInput(True, 0, {}, 100, 25))
    nomination = make_nomination(wave.wave_id, wave.nominations[0])
    for sources in ([], [{"origin": "private", "summary": "s", "eligible": False}]):
        rejected = ingest_research_result(
            tmp_path,
            nomination,
            {"correlation_id": nomination.correlation_id, "sources": sources},
        )
        assert rejected.status == "rejected"
        assert rejected.ingest_proposal == ""
        assert rejected.reason == "no eligible sources"
        assert not (tmp_path / ".megamind/proposals").exists()
        assert not (tmp_path / ".megamind/audit/log.jsonl").exists()
    # The replay that finally carries a real source is still the first write.
    accepted = ingest_research_result(
        tmp_path,
        nomination,
        {
            "correlation_id": nomination.correlation_id,
            "sources": [{"origin": "synthetic-source", "eligible": True}],
        },
    )
    assert accepted.status == "proposed"
    proposal = json.loads((tmp_path / accepted.ingest_proposal).read_text(encoding="utf-8"))
    assert [item["origin"] for item in proposal["sources"]] == ["synthetic-source"]


def test_research_result_refuses_to_diverge_from_its_stored_proposal(tmp_path: Path) -> None:
    gap = GapRecord.new("A", "topic")
    wave = plan_research_wave([gap], gap.gap_id, CapacityInput(True, 0, {}, 100, 25))
    nomination = make_nomination(wave.wave_id, wave.nominations[0])
    base = {"correlation_id": nomination.correlation_id}
    ingest_research_result(
        tmp_path, nomination, {**base, "sources": [{"origin": "one", "eligible": True}]}
    )
    with pytest.raises(GardenError, match="already ingested with different"):
        ingest_research_result(
            tmp_path,
            nomination,
            {
                **base,
                "sources": [
                    {"origin": "one", "eligible": True},
                    {"origin": "two", "eligible": True},
                ],
            },
        )
    assert len(list((tmp_path / ".megamind/proposals").glob("research-ingest-*.json"))) == 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://docs.example.com/home/getting-started", "kept"),
        ("https://docs.example.com/Users/guide", "kept"),
        ("capital: raised", "kept"),
        ("the topic is skirmish", "kept"),
        ("see /home/someone/notes.md", "redacted"),
        ("see /Users/someone/notes.md", "redacted"),
        ("api_key=CANARY", "redacted"),
        ("token: CANARY", "redacted"),
    ],
)
def test_redaction_only_fires_on_structurally_identified_values(text: str, expected: str) -> None:
    """An origin has to survive as a citable identifier, so redaction may never
    fire on a keyword or path prefix buried inside a longer token."""
    short = _short(text)
    if expected == "kept":
        assert short == text
    else:
        assert "CANARY" not in short and "someone" not in short
        assert "[redacted]" in short or "[path]" in short


@pytest.mark.parametrize("bad", ["yesterday", "2026-13-01", "01/01/2026", "  "])
def test_non_iso_dates_never_reach_a_durable_record(tmp_path: Path, bad: str) -> None:
    with pytest.raises(GardenError, match="ISO"):
        GapRecord.new("ProductWiki", "rate limits", today=bad)
    assert not (tmp_path / ".megamind/gaps.jsonl").exists()
    store = GapStore(tmp_path)
    gap = store.create(GapRecord.new("ProductWiki", "rate limits", today="2026-01-01"))
    with pytest.raises(GardenError, match="ISO"):
        store.transition(gap.gap_id, "planned", today=bad)
    assert store.get(gap.gap_id).status == "open"


def test_an_exact_self_transition_is_idempotent_and_keeps_terminal_meaning(
    tmp_path: Path,
) -> None:
    store = GapStore(tmp_path)
    first = store.create(GapRecord.new("A", "one", today="2026-01-01"))
    other = store.create(GapRecord.new("A", "two", today="2026-01-01"))
    store.transition(first.gap_id, "rejected", reason="needs owner review", today="2026-01-02")
    lines = len((tmp_path / ".megamind/gaps.jsonl").read_text(encoding="utf-8").splitlines())

    # Repeating the rejection may not blank the recorded reason or restamp it.
    repeated = store.transition(first.gap_id, "rejected", today="2026-03-01")
    assert repeated.rejection == {"reason": "needs owner review", "date": "2026-01-02"}
    assert repeated.updated == "2026-01-02"
    assert len((tmp_path / ".megamind/gaps.jsonl").read_text().splitlines()) == lines
    # A different reason is not an exact repeat, so it refuses rather than wins.
    with pytest.raises(InvalidTransition, match="cannot change reason"):
        store.transition(first.gap_id, "rejected", reason="something else")
    assert store.get(first.gap_id).rejection == {
        "reason": "needs owner review",
        "date": "2026-01-02",
    }

    store.transition(first.gap_id, "superseded", superseded_by=other.gap_id, today="2026-01-03")
    assert store.transition(first.gap_id, "superseded").superseded_by == other.gap_id
    with pytest.raises(InvalidTransition, match="cannot change superseded_by"):
        store.transition(first.gap_id, "superseded", superseded_by="somewhere-else")
    assert store.get(first.gap_id).superseded_by == other.gap_id


def test_a_self_transition_never_silently_rewrites_a_cooldown(tmp_path: Path) -> None:
    store = GapStore(tmp_path)
    gap = store.create(GapRecord.new("A", "one", today="2026-01-01"))
    store.attempt(gap.gap_id, "no source", cooldown_until="2026-02-01", today="2026-01-02")
    assert store.transition(gap.gap_id, "open").cooldown_until == "2026-02-01"
    with pytest.raises(InvalidTransition, match="cannot change cooldown_until"):
        store.transition(gap.gap_id, "open", cooldown_until="")
    assert store.get(gap.gap_id).cooldown_until == "2026-02-01"


@pytest.mark.parametrize("bad", ["tomorrow", "2026-13-01", "01/02/2026", "next week"])
def test_a_malformed_cooldown_never_reaches_a_durable_record(tmp_path: Path, bad: str) -> None:
    """`cooldown_until` is a date on the same record as `today`, so the same
    typed owner decides whether it may be written."""
    store = GapStore(tmp_path)
    gap = store.create(GapRecord.new("A", "one", today="2026-01-01"))
    lines = len((tmp_path / ".megamind/gaps.jsonl").read_text(encoding="utf-8").splitlines())
    with pytest.raises(GardenError, match="ISO"):
        store.attempt(gap.gap_id, "no source", cooldown_until=bad, today="2026-01-02")
    with pytest.raises(GardenError, match="ISO"):
        store.transition(gap.gap_id, "planned", cooldown_until=bad, today="2026-01-02")
    assert store.get(gap.gap_id).cooldown_until == ""
    assert store.get(gap.gap_id).attempts == []
    assert len((tmp_path / ".megamind/gaps.jsonl").read_text().splitlines()) == lines


@pytest.mark.parametrize(
    "field", ["cooldown_until", "created", "updated", "attempt_date", "rejection_date"]
)
def test_journal_replay_revalidates_every_recorded_date(tmp_path: Path, field: str) -> None:
    """A hand-edited journal fails typed at read, not at the next mutation."""
    store = GapStore(tmp_path)
    gap = store.create(GapRecord.new("A", "one", today="2026-01-01"))
    store.attempt(gap.gap_id, "no source", cooldown_until="2026-02-01", today="2026-01-02")
    store.transition(gap.gap_id, "rejected", reason="no owner", today="2026-01-03")
    journal = tmp_path / ".megamind/gaps.jsonl"
    last = json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])
    if field == "attempt_date":
        last["attempts"][0]["date"] = "tomorrow"
    elif field == "rejection_date":
        last["rejection"]["date"] = "tomorrow"
    else:
        last[field] = "tomorrow"
    journal.write_text(json.dumps(last) + "\n", encoding="utf-8")
    with pytest.raises(GardenError, match="invalid gap journal entry"):
        GapStore(tmp_path).records()
    assert validate_gap_journal(tmp_path)


def test_provision_requires_all_criteria_and_registers_restrictively(tmp_path: Path) -> None:
    init_vault(tmp_path, starter=False)
    files = provision_local_wiki(
        tmp_path, "ReleaseWiki", "ReleaseWiki", make_criteria(), today="2026-01-01"
    )
    assert ".megamind/registry.json" in files
    registry = load_registry(tmp_path)
    assert registry.wiki_by_name("ReleaseWiki").provisional is True  # type: ignore[union-attr]
    card = json.loads((tmp_path / "ReleaseWiki/.megamind/wiki-card.json").read_text())
    assert card["provisional"] is True
    with pytest.raises(GardenError):
        make_criteria(accepted_domain="", repeat_demand=1, seed_topics=("one",)).validate()


def test_provision_plan_apply_is_idempotent_and_rollback_verifies_content(tmp_path: Path) -> None:
    init_vault(tmp_path, starter=False)
    plan = plan_provision_wiki(
        tmp_path, "PlanWiki", "PlanWiki", make_criteria(), today="2026-01-01"
    )
    assert not (tmp_path / "PlanWiki").exists()
    with pytest.raises(GardenError, match="plan id mismatch"):
        apply_provision_plan(tmp_path, plan, "tampered")
    apply_provision_plan(tmp_path, plan, plan.plan_id)
    assert apply_provision_plan(tmp_path, plan, plan.plan_id) == []
    (tmp_path / "PlanWiki/wiki/index.md").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(GardenError, match="rollback refused"):
        rollback_provision(tmp_path, plan.plan_id)
    (tmp_path / "PlanWiki/wiki/index.md").write_text(
        "# PlanWiki compiled index\n", encoding="utf-8"
    )
    assert rollback_provision(tmp_path, plan.plan_id).removed
    assert not (tmp_path / "PlanWiki").exists()


def test_provision_apply_crash_recovers_without_an_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_vault(tmp_path, starter=False)
    plan = plan_provision_wiki(tmp_path, "CrashWiki", "CrashWiki", make_criteria())
    import megamind.gardening as gardening

    original = gardening.atomic_write
    calls = 0

    def crash(root: Path, target: str | Path, content: str, **kwargs: object) -> Path:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("synthetic crash")
        return original(root, target, content, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(gardening, "atomic_write", crash)
    with pytest.raises(OSError, match="synthetic crash"):
        apply_provision_plan(tmp_path, plan, plan.plan_id)
    assert not (tmp_path / "CrashWiki").exists()
    assert load_registry(tmp_path).wiki_by_name("CrashWiki") is None
    # An in-process recovery undoes the transaction, so no record is left to
    # resume and a fresh apply is not blocked by it.
    manifest = tmp_path / f".megamind/audit/provisional-wiki-{plan.plan_id}.json"
    assert not manifest.exists()
    monkeypatch.setattr(gardening, "atomic_write", original)
    assert apply_provision_plan(tmp_path, plan, plan.plan_id)


def kill_after(monkeypatch: pytest.MonkeyPatch, writes: int) -> None:
    """Stop mid-apply the way a signal or power loss does: the in-process
    recovery never gets to run, so only the durable record survives."""
    import megamind.gardening as gardening

    original = gardening.atomic_write
    done = 0

    def killed(root: Path, target: str | Path, content: str, **kwargs: object) -> Path:
        nonlocal done
        if done >= writes:
            raise KeyboardInterrupt("synthetic power loss")
        done += 1
        return original(root, target, content, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(gardening, "atomic_write", killed)
    monkeypatch.setattr(gardening, "_undo", lambda *args, **kwargs: None)


def test_an_interrupted_apply_leaves_a_recoverable_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write-ahead manifest lands before the first target mutation, so a
    kill in the middle of the writes is resumable without a second plan."""
    init_vault(tmp_path, starter=False)
    plan = plan_provision_wiki(
        tmp_path, "HaltWiki", "HaltWiki", make_criteria(), today="2026-01-01"
    )
    manifest = tmp_path / f".megamind/audit/provisional-wiki-{plan.plan_id}.json"

    with monkeypatch.context() as patched:
        # Manifest, then the registry, then killed part-way through the tree.
        kill_after(patched, 3)
        with pytest.raises(KeyboardInterrupt):
            apply_provision_plan(tmp_path, plan, plan.plan_id)
    assert manifest.is_file()
    assert json.loads(manifest.read_text(encoding="utf-8"))["state"] == "pending"
    assert load_registry(tmp_path).wiki_by_name("HaltWiki") is not None
    assert not (tmp_path / "HaltWiki/wiki/log.md").exists()

    status, files = resume_provision(tmp_path, plan.plan_id, "HaltWiki", "HaltWiki")  # type: ignore[misc]
    assert status == "applied"
    assert files == [str(change["path"]) for change in plan.changes]
    assert json.loads(manifest.read_text(encoding="utf-8"))["state"] == "applied"
    assert (tmp_path / "HaltWiki/wiki/log.md").read_text(encoding="utf-8").count(
        "megamind:event:"
    ) == 1
    assert load_wiki_card(tmp_path / "HaltWiki").name == "HaltWiki"
    # A resumed transaction verifies as a no-op and rolls back completely.
    assert apply_provision_plan(tmp_path, plan, plan.plan_id) == []
    assert rollback_provision(tmp_path, plan.plan_id).removed
    assert not (tmp_path / "HaltWiki").exists()
    assert load_registry(tmp_path).wiki_by_name("HaltWiki") is None


def test_an_interrupted_apply_rolls_back_without_resuming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_vault(tmp_path, starter=False)
    before = (tmp_path / ".megamind/registry.json").read_text(encoding="utf-8")
    plan = plan_provision_wiki(tmp_path, "HaltWiki", "HaltWiki", make_criteria())
    with monkeypatch.context() as patched:
        kill_after(patched, 4)
        with pytest.raises(KeyboardInterrupt):
            apply_provision_plan(tmp_path, plan, plan.plan_id)
    assert rollback_provision(tmp_path, plan.plan_id).removed
    assert not (tmp_path / "HaltWiki").exists()
    assert (tmp_path / ".megamind/registry.json").read_text(encoding="utf-8") == before
    with pytest.raises(GardenError, match="already rolled back"):
        rollback_provision(tmp_path, plan.plan_id)
    # Rollback leaves the vault re-plannable, and the plan id is unchanged.
    replanned = plan_provision_wiki(tmp_path, "HaltWiki", "HaltWiki", make_criteria())
    assert replanned.plan_id == plan.plan_id
    assert apply_provision_plan(tmp_path, replanned, replanned.plan_id)


def test_a_resume_refuses_content_the_transaction_did_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_vault(tmp_path, starter=False)
    plan = plan_provision_wiki(tmp_path, "HaltWiki", "HaltWiki", make_criteria())
    with monkeypatch.context() as patched:
        kill_after(patched, 3)
        with pytest.raises(KeyboardInterrupt):
            apply_provision_plan(tmp_path, plan, plan.plan_id)
    (tmp_path / "HaltWiki/wiki").mkdir(parents=True, exist_ok=True)
    (tmp_path / "HaltWiki/wiki/index.md").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(GardenError, match="changed outside the transaction"):
        apply_provision_plan(tmp_path, plan, plan.plan_id)
    with pytest.raises(GardenError, match="rollback refused"):
        rollback_provision(tmp_path, plan.plan_id)


def provision_write_count(tmp_path: Path, name: str) -> int:
    """How many gardening writes one whole apply performs, so a power-loss cut
    can be placed on either side of every durability boundary."""
    probe = tmp_path / f"probe-{name}"
    init_vault(probe, starter=False)
    plan = plan_provision_wiki(probe, name, name, make_criteria(), today="2026-01-01")
    import megamind.gardening as gardening

    original = gardening.atomic_write
    seen = 0

    def counted(root: Path, target: str | Path, content: str, **kwargs: object) -> Path:
        nonlocal seen
        seen += 1
        return original(root, target, content, **kwargs)  # type: ignore[arg-type]

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(gardening, "atomic_write", counted)
    try:
        apply_provision_plan(probe, plan, plan.plan_id)
    finally:
        monkeypatch.undo()
    return seen


def test_a_power_loss_at_every_durability_boundary_stays_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cut the process at each write in turn. Whatever survives, recovery must
    either finish the transaction or undo it, and never claim a false no-op."""
    total = provision_write_count(tmp_path, "CountWiki")
    # Every cut point: before the write-ahead record, between each target, and
    # on both sides of the commit record that closes the transaction.
    for cut in range(total):
        root = tmp_path / f"cut-{cut}"
        init_vault(root, starter=False)
        before = (root / ".megamind/registry.json").read_text(encoding="utf-8")
        plan = plan_provision_wiki(
            root, "HaltWiki", "HaltWiki", make_criteria(), today="2026-01-01"
        )
        with monkeypatch.context() as patched:
            kill_after(patched, cut)
            with pytest.raises(KeyboardInterrupt):
                apply_provision_plan(root, plan, plan.plan_id)

        manifest = root / f".megamind/audit/provisional-wiki-{plan.plan_id}.json"
        if not manifest.is_file():
            # Cut before the write-ahead record: nothing was promised.
            assert load_registry(root).wiki_by_name("HaltWiki") is None
            continue
        resumed = resume_provision(root, plan.plan_id, "HaltWiki", "HaltWiki")
        assert resumed is not None
        status, _files = resumed
        assert status in {"applied", "noop"}
        # However the cut fell, the wiki is now whole and verifies as a no-op.
        assert json.loads(manifest.read_text(encoding="utf-8"))["state"] == "applied"
        assert load_registry(root).wiki_by_name("HaltWiki") is not None
        assert load_wiki_card(root / "HaltWiki").name == "HaltWiki"
        assert (root / "HaltWiki/wiki/log.md").read_text(encoding="utf-8").count(
            "megamind:event:"
        ) == 1
        assert resume_provision(root, plan.plan_id, "HaltWiki", "HaltWiki") == ("noop", [])
        assert rollback_provision(root, plan.plan_id).removed
        assert not (root / "HaltWiki").exists()
        assert (root / ".megamind/registry.json").read_text(encoding="utf-8") == before


def test_an_applied_marker_over_a_lost_target_is_never_a_noop(tmp_path: Path) -> None:
    """A rename that did not reach disk leaves an applied record over content
    that is not there. That is an incomplete commit, not a success."""
    init_vault(tmp_path, starter=False)
    before = (tmp_path / ".megamind/registry.json").read_text(encoding="utf-8")
    plan = plan_provision_wiki(tmp_path, "LostWiki", "LostWiki", make_criteria())
    apply_provision_plan(tmp_path, plan, plan.plan_id)

    # The backed-up target reverts to its prior version; the generated one is
    # simply gone. Neither may be reported as an applied no-op.
    (tmp_path / ".megamind/registry.json").write_text(before, encoding="utf-8")
    (tmp_path / "LostWiki/CARD.md").unlink()
    status, files = resume_provision(tmp_path, plan.plan_id, "LostWiki", "LostWiki")  # type: ignore[misc]
    assert status == "applied"
    assert files
    assert load_registry(tmp_path).wiki_by_name("LostWiki") is not None
    assert (tmp_path / "LostWiki/CARD.md").is_file()
    assert resume_provision(tmp_path, plan.plan_id, "LostWiki", "LostWiki") == ("noop", [])


def test_an_applied_marker_over_a_lost_target_can_also_roll_back(tmp_path: Path) -> None:
    init_vault(tmp_path, starter=False)
    before = (tmp_path / ".megamind/registry.json").read_text(encoding="utf-8")
    plan = plan_provision_wiki(tmp_path, "LostWiki", "LostWiki", make_criteria())
    apply_provision_plan(tmp_path, plan, plan.plan_id)
    (tmp_path / ".megamind/registry.json").write_text(before, encoding="utf-8")
    assert rollback_provision(tmp_path, plan.plan_id).removed
    assert not (tmp_path / "LostWiki").exists()
    assert (tmp_path / ".megamind/registry.json").read_text(encoding="utf-8") == before


def derive_plan_id(plan: object, today: str) -> str:
    from megamind.fsops import content_hash
    from megamind.gardening import _stable

    return content_hash(
        _stable(
            {
                "name": plan.name,  # type: ignore[attr-defined]
                "path": plan.path,  # type: ignore[attr-defined]
                "today": today,
                "changes": plan.changes,  # type: ignore[attr-defined]
            }
        )
    )


def test_apply_never_mutates_the_reviewed_plan(tmp_path: Path) -> None:
    """`plan_id` is the approval token, so the plan it hashes must still hash to
    it after the apply that token authorized."""
    init_vault(tmp_path, starter=False)
    plan = plan_provision_wiki(
        tmp_path, "PlanWiki", "PlanWiki", make_criteria(), today="2026-01-01"
    )
    snapshot = [dict(change) for change in plan.changes]
    assert derive_plan_id(plan, "2026-01-01") == plan.plan_id
    apply_provision_plan(tmp_path, plan, plan.plan_id)
    assert [dict(change) for change in plan.changes] == snapshot
    assert derive_plan_id(plan, "2026-01-01") == plan.plan_id
    # The manifest recorded the bytes that landed without reaching back into it.
    manifest = json.loads(
        (tmp_path / f".megamind/audit/provisional-wiki-{plan.plan_id}.json").read_text(
            encoding="utf-8"
        )
    )
    logged = [c for c in manifest["plan"]["changes"] if c["path"] == "PlanWiki/wiki/log.md"]
    assert "megamind:event:" in logged[0]["new"]
    assert apply_provision_plan(tmp_path, plan, plan.plan_id) == []


def test_a_transaction_manifest_is_refused_for_other_arguments(tmp_path: Path) -> None:
    init_vault(tmp_path, starter=False)
    plan = plan_provision_wiki(tmp_path, "PlanWiki", "PlanWiki", make_criteria())
    apply_provision_plan(tmp_path, plan, plan.plan_id)
    assert resume_provision(tmp_path, plan.plan_id, "PlanWiki", "PlanWiki") == ("noop", [])
    with pytest.raises(GardenError, match="was recorded for"):
        resume_provision(tmp_path, plan.plan_id, "OtherWiki", "PlanWiki")
    assert resume_provision(tmp_path, "no-such-plan", "PlanWiki", "PlanWiki") is None


def test_provision_card_paths_are_rooted_at_the_wiki(tmp_path: Path) -> None:
    """The nested card is authoritative for its own root, so its paths resolve
    from there; the registry entry keeps the vault-relative ones."""
    init_vault(tmp_path, starter=False)
    provision_local_wiki(tmp_path, "ReleaseWiki", "ReleaseWiki", make_criteria())
    wiki_root = tmp_path / "ReleaseWiki"
    card = load_wiki_card(wiki_root)
    assert card.path == "."
    assert (card.card, card.index) == ("CARD.md", "INDEX.md")
    assert (wiki_root / card.card).is_file()
    assert (wiki_root / card.index).is_file()
    entry = load_registry(tmp_path).wiki_by_name("ReleaseWiki")
    assert entry is not None
    assert (entry.card, entry.index) == ("ReleaseWiki/CARD.md", "ReleaseWiki/INDEX.md")
    assert (tmp_path / entry.card).is_file()


def test_provision_refuses_to_create_a_dual_shape_root(tmp_path: Path) -> None:
    init_wiki_root(tmp_path, "MyWiki")
    with pytest.raises(GardenError, match="canonical single-wiki root"):
        provision_local_wiki(tmp_path, "ReleaseWiki", "ReleaseWiki", make_criteria())
    assert not (tmp_path / ".megamind/registry.json").exists()
    assert not (tmp_path / "ReleaseWiki").exists()


def test_provision_refuses_to_bootstrap_an_uninitialized_root(tmp_path: Path) -> None:
    with pytest.raises(RegistryError) as error:
        provision_local_wiki(tmp_path, "ReleaseWiki", "ReleaseWiki", make_criteria())
    assert error.value.code == "not_initialized"
    assert not (tmp_path / ".megamind").exists()
    assert not (tmp_path / "ReleaseWiki").exists()


def test_provision_refuses_a_v1_registry_instead_of_migrating_it(tmp_path: Path) -> None:
    init_vault(tmp_path, starter=False)
    registry = load_registry(tmp_path)
    registry.version = 1
    save_registry(tmp_path, registry)
    with pytest.raises(GardenError, match="migrate"):
        provision_local_wiki(tmp_path, "ReleaseWiki", "ReleaseWiki", make_criteria())
    assert load_registry(tmp_path).version == 1
    assert not (tmp_path / "ReleaseWiki").exists()


def test_provision_validates_the_registry_plan_before_writing_anything(tmp_path: Path) -> None:
    init_vault(tmp_path, starter=False)
    absolute = str(tmp_path / "ReleaseWiki")
    with pytest.raises(RegistryError, match="root-relative"):
        provision_local_wiki(tmp_path, "ReleaseWiki", absolute, make_criteria())
    assert not (tmp_path / "ReleaseWiki").exists()
    assert load_registry(tmp_path).wiki_by_name("ReleaseWiki") is None
    # A retry with the correct relative path is not blocked by an orphan scaffold.
    provision_local_wiki(tmp_path, "ReleaseWiki", "ReleaseWiki", make_criteria())
    assert load_registry(tmp_path).wiki_by_name("ReleaseWiki") is not None


def test_rollback_preserves_content_authored_after_the_apply(tmp_path: Path) -> None:
    """Rollback owns exactly what the transaction wrote. A page authored inside
    the provisional wiki afterwards is not its to delete."""
    init_vault(tmp_path, starter=False)
    before = (tmp_path / ".megamind/registry.json").read_text(encoding="utf-8")
    plan = plan_provision_wiki(tmp_path, "KeepWiki", "KeepWiki", make_criteria())
    apply_provision_plan(tmp_path, plan, plan.plan_id)
    page = tmp_path / "KeepWiki/wiki/topics/rate-limits.md"
    page.parent.mkdir(parents=True)
    page.write_text("# rate limits\n", encoding="utf-8")
    notes = tmp_path / "KeepWiki/NOTES.md"
    notes.write_text("hand-written\n", encoding="utf-8")

    outcome = rollback_provision(tmp_path, plan.plan_id)
    assert outcome.status == "partial"
    assert outcome.preserved == ["KeepWiki/NOTES.md", "KeepWiki/wiki/topics"]
    assert page.read_text(encoding="utf-8") == "# rate limits\n"
    assert notes.read_text(encoding="utf-8") == "hand-written\n"
    # Every tracked file is still undone and the vault is back to its old state.
    assert not (tmp_path / "KeepWiki/CARD.md").exists()
    assert not (tmp_path / "KeepWiki/INDEX.md").exists()
    assert not (tmp_path / "KeepWiki/wiki/log.md").exists()
    assert not (tmp_path / "KeepWiki/.megamind").exists()
    assert (tmp_path / ".megamind/registry.json").read_text(encoding="utf-8") == before
    assert load_registry(tmp_path).wiki_by_name("KeepWiki") is None


def test_rollback_prunes_only_directories_the_transaction_created(tmp_path: Path) -> None:
    """An empty directory the transaction created is pruned; a directory that
    already existed above the wiki path is never the transaction's to remove."""
    init_vault(tmp_path, starter=False)
    (tmp_path / "wikis").mkdir()
    sibling = tmp_path / "wikis/README.md"
    sibling.write_text("pre-existing\n", encoding="utf-8")
    plan = plan_provision_wiki(tmp_path, "NestedWiki", "wikis/NestedWiki", make_criteria())
    apply_provision_plan(tmp_path, plan, plan.plan_id)
    assert (tmp_path / "wikis/NestedWiki/.megamind").is_dir()

    outcome = rollback_provision(tmp_path, plan.plan_id)
    assert (outcome.status, outcome.preserved) == ("rolled_back", [])
    assert not (tmp_path / "wikis/NestedWiki").exists()
    assert (tmp_path / "wikis").is_dir()
    assert sibling.read_text(encoding="utf-8") == "pre-existing\n"


def test_rollback_refuses_a_directory_standing_where_a_target_belongs(tmp_path: Path) -> None:
    """A directory in place of a generated file reads as absent to a text
    reader. It is content the transaction did not write, so it is refused
    rather than removed with everything inside it."""
    init_vault(tmp_path, starter=False)
    plan = plan_provision_wiki(tmp_path, "SwapWiki", "SwapWiki", make_criteria())
    apply_provision_plan(tmp_path, plan, plan.plan_id)
    card = tmp_path / "SwapWiki/CARD.md"
    card.unlink()
    card.mkdir()
    (card / "kept.md").write_text("inside\n", encoding="utf-8")

    with pytest.raises(GardenError, match="rollback refused"):
        rollback_provision(tmp_path, plan.plan_id)
    assert (card / "kept.md").read_text(encoding="utf-8") == "inside\n"
    with pytest.raises(GardenError, match="not idempotent"):
        resume_provision(tmp_path, plan.plan_id, "SwapWiki", "SwapWiki")


def test_an_in_process_undo_preserves_content_it_did_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The undo that runs when an apply raises follows the same rule: it removes
    only what it wrote, so a page that appeared during the transaction stays."""
    init_vault(tmp_path, starter=False)
    plan = plan_provision_wiki(tmp_path, "UndoWiki", "UndoWiki", make_criteria())
    import megamind.gardening as gardening

    original = gardening.atomic_write
    calls = 0
    concurrent = tmp_path / "UndoWiki/wiki/topics/rate-limits.md"

    def crash(root: Path, target: str | Path, content: str, **kwargs: object) -> Path:
        nonlocal calls
        calls += 1
        if calls == 6:
            concurrent.parent.mkdir(parents=True)
            concurrent.write_text("written mid-transaction\n", encoding="utf-8")
            raise OSError("synthetic crash")
        return original(root, target, content, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(gardening, "atomic_write", crash)
    with pytest.raises(OSError, match="synthetic crash"):
        apply_provision_plan(tmp_path, plan, plan.plan_id)

    assert concurrent.read_text(encoding="utf-8") == "written mid-transaction\n"
    assert load_registry(tmp_path).wiki_by_name("UndoWiki") is None
    assert not (tmp_path / "UndoWiki/CARD.md").exists()
    assert not (tmp_path / "UndoWiki/.megamind").exists()
    assert not (tmp_path / f".megamind/audit/provisional-wiki-{plan.plan_id}.json").exists()


def test_provisioning_stays_usable_without_a_directory_flush_primitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows exposes no directory handle to flush. Durable writes there fall
    back to the file flush rather than making the whole feature unusable."""
    import megamind.fsops as fsops

    monkeypatch.setattr(fsops, "DIRECTORY_FSYNC", False)
    assert fsops.sync_directory(tmp_path) is False
    init_vault(tmp_path, starter=False)
    plan = plan_provision_wiki(tmp_path, "WinWiki", "WinWiki", make_criteria())
    assert apply_provision_plan(tmp_path, plan, plan.plan_id)
    assert load_registry(tmp_path).wiki_by_name("WinWiki") is not None
    assert (tmp_path / "WinWiki/CARD.md").is_file()
    assert rollback_provision(tmp_path, plan.plan_id).removed
    assert not (tmp_path / "WinWiki").exists()


@pytest.mark.parametrize(
    ("status", "reason", "superseded_by"),
    [
        ("open", "reopened after new evidence", ""),
        ("paused", "waiting on an owner", ""),
        ("paused", "", "other-gap"),
    ],
)
def test_a_self_transition_ignores_flags_that_status_never_persists(
    tmp_path: Path, status: str, reason: str, superseded_by: str
) -> None:
    """A repeat is only inexact when a field the status actually stores would
    change; `--reason` outside a rejection changes nothing, so it is a no-op."""
    store = GapStore(tmp_path)
    gap = store.create(GapRecord.new("ProductWiki", "Rate limits", today="2026-01-01"))
    if status != "open":
        store.transition(gap.gap_id, "nominated", today="2026-01-02")
        store.transition(gap.gap_id, status, today="2026-01-03")
    lines = len((tmp_path / ".megamind/gaps.jsonl").read_text(encoding="utf-8").splitlines())
    unchanged = store.transition(
        gap.gap_id, status, today="2026-01-04", reason=reason, superseded_by=superseded_by
    )
    assert unchanged.status == status
    assert unchanged.rejection is None
    assert unchanged.superseded_by == ""
    assert (
        len((tmp_path / ".megamind/gaps.jsonl").read_text(encoding="utf-8").splitlines()) == lines
    )


def test_a_replayed_rejection_keeps_its_reason_and_refuses_a_different_one(
    tmp_path: Path,
) -> None:
    store = GapStore(tmp_path)
    gap = store.create(GapRecord.new("ProductWiki", "Rate limits", today="2026-01-01"))
    store.transition(gap.gap_id, "rejected", reason="no owner", today="2026-01-02")
    replay = store.transition(gap.gap_id, "rejected", reason="no owner", today="2026-01-03")
    assert replay.rejection == {"reason": "no owner", "date": "2026-01-02"}
    with pytest.raises(InvalidTransition, match="cannot change reason"):
        store.transition(gap.gap_id, "rejected", reason="a different reason")


def test_a_replayed_supersession_refuses_a_different_target(tmp_path: Path) -> None:
    store = GapStore(tmp_path)
    gap = store.create(GapRecord.new("ProductWiki", "Rate limits", today="2026-01-01"))
    store.transition(gap.gap_id, "planned", today="2026-01-02")
    store.transition(gap.gap_id, "in_progress", today="2026-01-03")
    store.transition(gap.gap_id, "superseded", superseded_by="newer", today="2026-01-04")
    assert (
        store.transition(gap.gap_id, "superseded", superseded_by="newer").superseded_by == "newer"
    )
    with pytest.raises(InvalidTransition, match="cannot change superseded_by"):
        store.transition(gap.gap_id, "superseded", superseded_by="other")


@pytest.mark.parametrize("field", ["wiki", "topic"])
def test_research_result_replay_is_bound_to_the_whole_nomination_identity(
    tmp_path: Path, field: str
) -> None:
    """Correlation alone keys the proposal file, so a replay that renames the
    wiki or the topic is a different nomination and may not reuse it."""
    gap = GapRecord.new("A", "topic")
    wave = plan_research_wave([gap], gap.gap_id, CapacityInput(True, 0, {}, 100, 25))
    entry = dict(wave.nominations[0])
    first = make_nomination(wave.wave_id, entry)
    result = {
        "correlation_id": first.correlation_id,
        "sources": [{"origin": "synthetic-source", "eligible": True}],
    }
    accepted = ingest_research_result(tmp_path, first, result)
    stored = json.loads((tmp_path / accepted.ingest_proposal).read_text(encoding="utf-8"))

    renamed = make_nomination(wave.wave_id, {**entry, field: "something else"})
    with pytest.raises(GardenError, match=f"already ingested with different {field}"):
        ingest_research_result(tmp_path, renamed, result)
    assert json.loads((tmp_path / accepted.ingest_proposal).read_text(encoding="utf-8")) == stored
    assert len(list((tmp_path / ".megamind/proposals").glob("research-ingest-*.json"))) == 1
