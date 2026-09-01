"""
board-mirror tests — the projection/prune logic, with an in-memory store.

Runnable standalone (`python3 test_board_mirror.py`) or under pytest.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import board_mirror as B   # noqa: E402

NOW = 1_700_000_000


class DictStore:
    """In-memory board double keyed (locus,item_id) -> row."""
    def __init__(self):
        self.rows = {}

    def upsert(self, row):
        self.rows[(row["locus"], row["item_id"])] = dict(row)

    def prune(self, locus, keep_ids):
        keep = set(keep_ids)
        drop = [k for k in self.rows if k[0] == locus and k[1] not in keep]
        for k in drop:
            del self.rows[k]
        return len(drop)

    def for_locus(self, locus):
        return {k[1]: v for k, v in self.rows.items() if k[0] == locus}


def _items(*specs):
    out = []
    for s in specs:
        out.append(s)
    return out


# ── tests ─────────────────────────────────────────────────────────────────────
def test_upserts_present_items_with_full_data():
    st = DictStore()
    items = [{"id": "t1", "status": "open", "type": "todo", "text": "a", "worker": "x"},
             {"id": "t2", "status": "doing", "type": "request", "text": "b"}]
    stats = B.mirror(st, "hugpy", items, NOW)
    assert stats["upserted"] == 2 and stats["skipped"] == 0
    rows = st.for_locus("hugpy")
    assert set(rows) == {"t1", "t2"}
    assert rows["t1"]["status"] == "open" and rows["t1"]["type"] == "todo"
    # unknown fields preserved verbatim in data
    assert rows["t1"]["data"]["worker"] == "x"


def test_prune_removes_items_deleted_from_the_file():
    st = DictStore()
    B.mirror(st, "hugpy", [{"id": "t1", "status": "open"}, {"id": "t2", "status": "open"}], NOW)
    # t2 disappears from the file on the next read
    stats = B.mirror(st, "hugpy", [{"id": "t1", "status": "done"}], NOW)
    assert stats["pruned"] == 1
    rows = st.for_locus("hugpy")
    assert set(rows) == {"t1"} and rows["t1"]["status"] == "done"


def test_empty_board_prunes_all_for_locus():
    st = DictStore()
    B.mirror(st, "hugpy", [{"id": "t1"}, {"id": "t2"}], NOW)
    stats = B.mirror(st, "hugpy", [], NOW)
    assert stats["pruned"] == 2 and st.for_locus("hugpy") == {}


def test_locus_isolation():
    st = DictStore()
    B.mirror(st, "hugpy", [{"id": "t1", "status": "open"}], NOW)
    B.mirror(st, "ae", [{"id": "t9", "status": "open"}], NOW)
    # re-mirroring hugpy (t1 gone) must not touch ae's rows
    B.mirror(st, "hugpy", [], NOW)
    assert st.for_locus("hugpy") == {}
    assert set(st.for_locus("ae")) == {"t9"}


def test_items_without_string_id_are_skipped_not_dropped_silently():
    st = DictStore()
    items = [{"id": "t1"}, {"no": "id"}, {"id": 5}, "notadict", {"id": ""}]
    stats = B.mirror(st, "hugpy", items, NOW)
    assert stats["upserted"] == 1 and stats["skipped"] == 4
    assert set(st.for_locus("hugpy")) == {"t1"}


def test_ts_falls_back_to_now_when_absent_or_bad():
    st = DictStore()
    B.mirror(st, "hugpy", [{"id": "t1"}, {"id": "t2", "ts": 42}, {"id": "t3", "ts": "x"}], NOW)
    rows = st.for_locus("hugpy")
    assert rows["t1"]["ts"] == NOW and rows["t2"]["ts"] == 42 and rows["t3"]["ts"] == NOW


def test_read_board_tolerates_bad_file(tmp_path=None):
    import tempfile
    d = tempfile.mkdtemp()
    good = os.path.join(d, "good.json")
    bad = os.path.join(d, "bad.json")
    with open(good, "w") as fh:
        fh.write('{"items":[{"id":"t1"}]}')
    with open(bad, "w") as fh:
        fh.write("{ not json")
    items, err = B.read_board(good)
    assert err is None and items[0]["id"] == "t1"
    items, err = B.read_board(bad)
    assert items is None and err  # a reason, not a crash
    items, err = B.read_board(os.path.join(d, "missing.json"))
    assert items is None and err


def test_no_db_env_yields_no_store():
    # with no SOLCATCHER_POSTGRESQL_* and a bogus dotenv, _db_kwargs is None -> open None
    saved = {k: os.environ.pop("SOLCATCHER_POSTGRESQL_%s" % k, None)
             for k in ("HOST", "PORT", "USER", "PASS", "NAME")}
    os.environ["BOARD_MIRROR_ENV_FILE"] = "/nonexistent/.env"
    try:
        assert B._db_kwargs() is None
        assert B.PgStore.open() is None      # fail-open, no exception
    finally:
        os.environ.pop("BOARD_MIRROR_ENV_FILE", None)
        for k, v in saved.items():
            if v is not None:
                os.environ["SOLCATCHER_POSTGRESQL_%s" % k] = v


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  %s" % fn.__name__)
    print("\n%d passed" % len(fns))


if __name__ == "__main__":
    _run_all()
