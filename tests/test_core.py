import pytest

from sfdata.config import FieldMapping, LookupMapping, ObjectMapping
from sfdata.extract import build_soql, normalize_watermark
from sfdata.graph import load_order
from sfdata.ledger import entries_from_results, live_entries
from sfdata.load import LoadResult, chunks
from sfdata.transform import (Context, TransformError, apply_transcode, check_condition, relationship_name,
                              transform_record)


def mapping(**kw):
    base = dict(key="enq", source_object="Enquete__c", target_object="Enquete__c", level="1",
                external_id_field="ExtId__c", external_id_compute={"source": "Id", "prefer": "ExtId__c", "prefix": "ENQ"})
    base.update(kw)
    return ObjectMapping(**base)


def test_load_order_parents_first_and_cycles_deferred():
    order, d = load_order({"E": {"S": {"S"}}, "S": {"Self": {"S"}}, "A": {"E": {"E"}}})
    assert order.index("S") < order.index("E") < order.index("A")
    assert d == {"S": {"Self"}}


def test_relationship_name():
    assert relationship_name("Enquete__c") == "Enquete__r"
    assert relationship_name("AccountId") == "Account"


def test_condition_forms():
    r = {"A": "x", "O": {"T": "P"}}
    assert check_condition(r, "A") and check_condition(r, "O.T=P") and not check_condition(r, "O.T=Q")


def test_transcode_unknown_value_blocks():
    assert apply_transcode("a", {"a": "b"}) == "b"
    with pytest.raises(TransformError):
        apply_transcode("z", {"a": "b"})


def test_external_id_prefers_source_then_computes_from_id():
    m = mapping()
    assert transform_record({"Id": "a01", "ExtId__c": "E-1"}, m)["ExtId__c"] == "E-1"
    assert transform_record({"Id": "a01", "ExtId__c": None}, m)["ExtId__c"] == "ENQ_a01"


def test_lookup_uses_parent_external_id_or_computed_fallback():
    lk = LookupMapping(source="Sig__c", target="Sig__c", parent_external_id="ExtId__c", relationship_name="Sig__r",
                       prefer="Sig__r.ExtId__c", compute={"prefix": "SIG"})
    m = mapping(lookups=[lk])
    p = transform_record({"Id": "1", "ExtId__c": "x", "Sig__c": "s1", "Sig__r": {"ExtId__c": "S-9"}}, m)
    assert p["Sig__r"] == {"ExtId__c": "S-9"}
    p = transform_record({"Id": "1", "ExtId__c": "x", "Sig__c": "s1", "Sig__r": {"ExtId__c": None}}, m)
    assert p["Sig__r"] == {"ExtId__c": "SIG_s1"}


def test_reflexive_lookup_only_in_pass_2():
    lk = LookupMapping(source="Dbl__c", target="Dbl__c", relationship_name="Dbl__r", prefer="Dbl__r.ExtId__c", reflexive=True)
    m = mapping(two_pass=True, lookups=[lk], fields=[FieldMapping(source="Name", target="Name")])
    rec = {"Id": "1", "ExtId__c": "x", "Name": "n", "Dbl__c": "d", "Dbl__r": {"ExtId__c": "D"}}
    assert "Dbl__r" not in transform_record(rec, m, 1) and "Name" in transform_record(rec, m, 1)
    assert transform_record(rec, m, 2) == {"ExtId__c": "x", "Dbl__r": {"ExtId__c": "D"}}


def test_user_lookup_resolved_by_username_and_omitted_when_unknown():
    lk = LookupMapping(source="Owner.Username", target="OwnerId", user=True)
    m = mapping(lookups=[lk])
    rec = {"Id": "1", "ExtId__c": "x", "Owner": {"Username": "a@b.c"}}
    assert transform_record(rec, m, ctx=Context(user_resolver=lambda u: "005X"))["OwnerId"] == "005X"
    assert "OwnerId" not in transform_record(rec, m, ctx=Context(user_resolver=lambda u: None))


def test_idmap_lookup_for_parent_without_external_id():
    lk = LookupMapping(source="Acteur__c", target="Acteur__c", parent_key="acteur", via_idmap=True)
    m = mapping(lookups=[lk])
    rec = {"Id": "1", "ExtId__c": "x", "Acteur__c": "src1"}
    assert transform_record(rec, m, ctx=Context(id_maps={"acteur": {"src1": "tgt1"}}))["Acteur__c"] == "tgt1"


def test_anonymize_suffix_only_when_active():
    m = mapping(fields=[FieldMapping(source="Email", target="Email", append_suffix=".invalid", anonymize=True)])
    rec = {"Id": "1", "ExtId__c": "x", "Email": "a@b.c"}
    assert transform_record(rec, m, ctx=Context(anonymize=True))["Email"] == "a@b.c.invalid"
    assert transform_record(rec, m, ctx=Context(anonymize=False))["Email"] == "a@b.c"


def test_missing_external_id_is_error_without_compute_or_backfill():
    m = mapping(external_id_compute=None)
    with pytest.raises(TransformError):
        transform_record({"Id": "1", "ExtId__c": None}, m)


def test_backfill_routes_to_insert_when_no_value():
    m = mapping(external_id_compute=None, external_id_backfill=True)
    assert transform_record({"Id": "1", "ExtId__c": None}, m)["_source_id"] == "1"
    assert transform_record({"Id": "1", "ExtId__c": None}, m, ctx=Context(backfill_map={"1": "G"}))["ExtId__c"] == "G"


def test_soql_scope_where_watermark_and_limit():
    m = mapping(scope_filter="Statut__c = 'X'", fields=[FieldMapping(source="Name", target="Name")])
    q = build_soql(m, "2026-01-01T00:00:00.000+0000", "Name != null", 10)
    assert "(Statut__c = 'X') AND (Name != null) AND SystemModstamp > 2026-01-01T00:00:00.000Z" in q
    assert q.endswith("ORDER BY SystemModstamp, Id LIMIT 10") and normalize_watermark("x+0000") == "xZ"


def test_ledger_only_created_and_tombstones():
    rs = [LoadResult("E1", True, True, target_id="t1"), LoadResult("E2", True, False, target_id="t2"),
          LoadResult("E3", False, None)]
    es = entries_from_results("enq", "Enquete__c", rs, "ts")
    assert [e["target_id"] for e in es] == ["t1"]
    assert live_entries(es + [{"action": "purged", "target_id": "t1"}]) == []


def test_chunks():
    assert [len(c) for c in chunks(list(range(5)), 2)] == [2, 2, 1]


def test_select_with_parents_pulls_required_parents_recursively(monkeypatch):
    from sfdata import runner
    from sfdata.config import LookupMapping
    maps = {
        "sig": mapping(key="sig", level="1"),
        "enq": mapping(key="enq", level="2", lookups=[LookupMapping(source="Sig__c", target="Sig__c", parent_key="sig")]),
        "com": mapping(key="com", level="3", lookups=[LookupMapping(source="Enq__c", target="Enq__c", parent_key="enq")]),
    }
    data = {"sig": [{"Id": "s1"}, {"Id": "s2"}], "enq": [{"Id": "e1", "Sig__c": "s1"}, {"Id": "e2", "Sig__c": "s2"}],
            "com": [{"Id": "c1", "Enq__c": "e2"}]}

    class Src:
        def records(self, m, wm=None, where=None, limit=None):
            return data[m.key][:limit] if limit else data[m.key]

        def records_by_ids(self, m, ids):
            return [r for r in data[m.key] if r["Id"] in ids]

    monkeypatch.setattr(runner, "load_object_mapping", lambda k: maps[k])

    class O:
        where, limit = None, 1

    sel, seeds = runner.select_with_parents(Src(), list(maps), lambda k: None, O())
    assert {r["Id"] for r in sel["enq"]} == {"e1", "e2"} and {r["Id"] for r in sel["sig"]} == {"s1", "s2"}
    assert [r["Id"] for r in seeds["enq"]] == ["e1"]
