import json

import pytest

from sfdata import state
from sfdata.config import ObjectMapping
from sfdata.snapshot import SnapshotSource


def test_state_is_separated_per_target(tmp_path):
    state.set_id_map("devA", "acteur", {"s1": "tA"}, tmp_path)
    state.set_id_map("devB", "acteur", {"s1": "tB"}, tmp_path)
    assert state.load_id_map("devA", "acteur", tmp_path) == {"s1": "tA"}
    assert state.load_id_map("devB", "acteur", tmp_path) == {"s1": "tB"}


def test_forget_purged_clears_idmap_and_watermark(tmp_path):
    state.set_id_map("dev", "acteur", {"s1": "t1", "s2": "t2"}, tmp_path)
    state.set_watermark("prod", "acteur", "2026-01-01T00:00:00.000+0000", tmp_path)
    state.forget_purged("dev", "acteur", {"t1"}, set(), "prod", tmp_path)
    assert state.load_id_map("dev", "acteur", tmp_path) == {"s2": "t2"}
    assert state.get_watermark("prod", "acteur", tmp_path) is None


def test_snapshot_source_reads_and_refuses_delta(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "manifest.json").write_text("{}")
    (tmp_path / "data" / "acteur.jsonl").write_text(json.dumps({"Id": "1"}) + "\n" + json.dumps({"Id": "2"}) + "\n")
    src = SnapshotSource(tmp_path)
    m = ObjectMapping(key="acteur", source_object="Acteur__c", target_object="Acteur__c", level="1", external_id_field="ExtId__c")
    assert [r["Id"] for r in src.records(m)] == ["1", "2"] and len(src.records(m, limit=1)) == 1
    with pytest.raises(ValueError):
        src.records(m, watermark="x")
