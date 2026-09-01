#!/usr/bin/env python3
"""Offline unit tests for bugreport scanner. No network, no journalctl.

Run: python3 -m unittest -v test_scanner
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import scanner


class TestDedup(unittest.TestCase):
    def test_collapses_by_signature(self):
        lines = [
            {"source": "j", "severity": "warning", "ts": 100.0,
             "message": "conn 12345 reset from host(7):999"},
            {"source": "j", "severity": "warning", "ts": 200.0,
             "message": "conn 67890 reset from host(2):111"},
            {"source": "j", "severity": "warning", "ts": 150.0,
             "message": "conn 55555 reset from host(9):222"},
            {"source": "j", "severity": "err", "ts": 300.0,
             "message": "disk full on /dev/sda1"},
        ]
        clusters = scanner.dedup(lines)
        # 3 numeric-varying conn lines collapse to one; disk-full is its own.
        self.assertEqual(len(clusters), 2)
        conn = [c for c in clusters if "conn" in c["message"]][0]
        self.assertEqual(conn["count"], 3)
        self.assertEqual(conn["first_ts"], 100.0)
        self.assertEqual(conn["last_ts"], 200.0)

    def test_most_severe_wins(self):
        lines = [
            {"source": "s", "severity": "info", "ts": 1.0, "message": "thing 1 happened"},
            {"source": "s", "severity": "err", "ts": 2.0, "message": "thing 2 happened"},
        ]
        clusters = scanner.dedup(lines)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["severity"], "err")
        self.assertEqual(clusters[0]["count"], 2)

    def test_different_sources_do_not_merge(self):
        lines = [
            {"source": "a", "severity": "info", "ts": 1.0, "message": "same text 1"},
            {"source": "b", "severity": "info", "ts": 1.0, "message": "same text 2"},
        ]
        self.assertEqual(len(scanner.dedup(lines)), 2)


class TestDebounceRelayTransients(unittest.TestCase):
    @staticmethod
    def _cluster(count, severity="err", source="keeper-relay-audit",
                 message='{"decision":"error","reason":"fetch-failed:HTTPError"}'):
        return {"source": source, "severity": severity, "count": count,
                "first_ts": 1.0, "last_ts": 2.0, "message": message, "_seq": 0}

    def test_single_blip_downgraded(self):
        clusters = [self._cluster(1)]
        scanner.debounce_relay_transients(clusters)
        self.assertEqual(clusters[0]["severity"], "warning")

    def test_timeout_error_also_downgraded(self):
        clusters = [self._cluster(
            1, message='{"decision":"error","reason":"fetch-failed:TimeoutError"}')]
        scanner.debounce_relay_transients(clusters)
        self.assertEqual(clusters[0]["severity"], "warning")

    def test_url_error_also_downgraded(self):
        # urllib's superclass for network-level failures (DNS blip, connection
        # refused) — same transient class; escaped as bugerr:1 on 2026-07-22.
        clusters = [self._cluster(
            1, message='{"decision":"error","reason":"fetch-failed:URLError"}')]
        scanner.debounce_relay_transients(clusters)
        self.assertEqual(clusters[0]["severity"], "warning")

    @staticmethod
    def _spanning(count, span,
                  message='{"decision":"error","reason":"fetch-failed:HTTPError"}'):
        """A cluster carrying per-row timestamps spread over `span` seconds."""
        base = 1_000_000.0
        tss = [base + (span * i / max(1, count - 1)) for i in range(count)]
        return {"source": "keeper-relay-audit", "severity": "err", "count": count,
                "first_ts": tss[0], "last_ts": tss[-1], "message": message,
                "_seq": 0, "_all_ts": tss}

    def test_boundary_count(self):
        """Count alone no longer decides: below MIN_BURST is a flap either way."""
        below = self._cluster(scanner.RELAY_TRANSIENT_MIN_BURST - 1)
        at = self._cluster(scanner.RELAY_TRANSIENT_MIN_BURST)
        scanner.debounce_relay_transients([below])
        scanner.debounce_relay_transients([at])
        self.assertEqual(below["severity"], "warning")
        # no _all_ts -> duration unknown -> fail toward err
        self.assertEqual(at["severity"], "err")

    def test_large_but_brief_burst_is_downgraded(self):
        """The 2026-07-25 recalibration: 14 rows in 195s self-recovered. Big but
        short is a flap, not an outage, and must not page."""
        c = self._spanning(14, 195)
        scanner.debounce_relay_transients([c])
        self.assertEqual(c["severity"], "warning")

    def test_large_and_sustained_burst_keeps_err(self):
        """A real upstream outage: 34 rows over 13 min. Must still alert."""
        c = self._spanning(34, 781)
        scanner.debounce_relay_transients([c])
        self.assertEqual(c["severity"], "err")

    def test_sustained_but_small_burst_is_downgraded(self):
        """Two failures an hour apart are not an outage — they are two blips."""
        c = self._spanning(2, 3600)
        scanner.debounce_relay_transients([c])
        self.assertEqual(c["severity"], "warning")

    def test_span_boundary(self):
        big = scanner.RELAY_TRANSIENT_MIN_BURST
        just_under = self._spanning(big, scanner.RELAY_TRANSIENT_MIN_SPAN - 1)
        exactly = self._spanning(big, scanner.RELAY_TRANSIENT_MIN_SPAN)
        scanner.debounce_relay_transients([just_under])
        scanner.debounce_relay_transients([exactly])
        self.assertEqual(just_under["severity"], "warning")
        self.assertEqual(exactly["severity"], "err")

    def test_non_transient_relay_error_untouched(self):
        c = self._cluster(
            1, message='{"decision":"error","reason":"disk-write-failed"}')
        scanner.debounce_relay_transients([c])
        self.assertEqual(c["severity"], "err")

    def test_other_source_untouched(self):
        c = self._cluster(1, source="journal-warnings")
        scanner.debounce_relay_transients([c])
        self.assertEqual(c["severity"], "err")

    def test_never_upgrades(self):
        c = self._cluster(1, severity="info")
        scanner.debounce_relay_transients([c])
        self.assertEqual(c["severity"], "info")


class TestDebounceRelayBootBursts(unittest.TestCase):
    """2026-07-22: 4 reboots -> an 8x fetch-failed cluster -> bugerr:1@1784726019.

    Boot power-cycles make keeper-relay's poll fail for ~90s after each boot;
    debounce_relay_boot_bursts() must forgive that (downgrade to warning) only
    once the burst is both boot-aligned and over, and must never touch a
    burst that is ongoing, unaligned with any boot, or of unknown timing.
    """

    @staticmethod
    def _cluster(all_ts, severity="err", source="keeper-relay-audit",
                 message='{"decision":"error","reason":"fetch-failed:URLError"}'):
        return {"source": source, "severity": severity, "count": len(all_ts),
                "first_ts": min(all_ts) if all_ts else None,
                "last_ts": max(all_ts) if all_ts else None,
                "message": message, "_seq": 0, "_all_ts": list(all_ts)}

    def test_recovered_boot_burst_downgraded(self):
        # Boot at t=1000; 8 failures in the ~90s grace window; scan happens
        # well after RELAY_RECOVERY_QUIET has elapsed since the last one.
        boot = 1000.0
        all_ts = [boot + d for d in (5, 20, 35, 50, 65, 80, 90, 95)]
        c = self._cluster(all_ts)
        now = all_ts[-1] + scanner.RELAY_RECOVERY_QUIET + 1
        scanner.debounce_relay_boot_bursts([c], now=now, boot_times=[boot])
        self.assertEqual(c["severity"], "warning")

    def test_ongoing_burst_kept_err(self):
        # Same boot-aligned burst, but the scan lands right after the last
        # failure — not yet "recovered" per RELAY_RECOVERY_QUIET.
        boot = 1000.0
        all_ts = [boot + d for d in (5, 20, 35, 50, 65, 80, 90, 95)]
        c = self._cluster(all_ts)
        now = all_ts[-1] + 5  # far short of RELAY_RECOVERY_QUIET
        scanner.debounce_relay_boot_bursts([c], now=now, boot_times=[boot])
        self.assertEqual(c["severity"], "err")

    def test_sustained_outage_outside_boot_window_kept_err(self):
        # 2026-07-17-style: dozens of consecutive failures continuing well
        # past any boot's grace window (and still failing at scan time) —
        # this must still alert as err.
        boot = 1000.0
        all_ts = [boot + d for d in range(0, 3600, 60)]  # an hour of failures
        c = self._cluster(all_ts)
        now = all_ts[-1] + scanner.RELAY_RECOVERY_QUIET + 1
        scanner.debounce_relay_boot_bursts([c], now=now, boot_times=[boot])
        self.assertEqual(c["severity"], "err")

    def test_unknown_boot_times_kept_err(self):
        boot = 1000.0
        all_ts = [boot + d for d in (5, 20, 35)]
        c = self._cluster(all_ts)
        now = all_ts[-1] + scanner.RELAY_RECOVERY_QUIET + 1
        scanner.debounce_relay_boot_bursts([c], now=now, boot_times=[])
        self.assertEqual(c["severity"], "err")

    def test_missing_per_row_timestamps_kept_err(self):
        c = self._cluster([])  # no _all_ts data available
        scanner.debounce_relay_boot_bursts(
            [c], now=100000.0, boot_times=[1000.0])
        self.assertEqual(c["severity"], "err")

    def test_multiple_boots_each_with_a_burst_downgraded(self):
        # Today's actual shape: 4 separate boots, each contributing its own
        # short burst, all normalizing into ONE cluster (same message
        # pattern). Every row must still land in SOME boot's grace window.
        boots = [1000.0, 4000.0, 6000.0, 6500.0]
        all_ts = []
        for b in boots:
            all_ts.extend(b + d for d in (5, 20, 40))
        c = self._cluster(all_ts)
        now = max(all_ts) + scanner.RELAY_RECOVERY_QUIET + 1
        scanner.debounce_relay_boot_bursts([c], now=now, boot_times=boots)
        self.assertEqual(c["severity"], "warning")

    def test_row_in_gap_between_boot_windows_kept_err(self):
        # One row falls between two boot windows (not explained by either) —
        # first_ts/last_ts alone would look boot-bounded, but the per-row
        # check must catch this and keep err.
        boots = [1000.0, 5000.0]
        all_ts = [1000.0 + 5, 1000.0 + 20, 3000.0, 5000.0 + 10]
        c = self._cluster(all_ts)
        now = max(all_ts) + scanner.RELAY_RECOVERY_QUIET + 1
        scanner.debounce_relay_boot_bursts([c], now=now, boot_times=boots)
        self.assertEqual(c["severity"], "err")

    def test_burst_outside_any_boot_window_kept_err(self):
        c = self._cluster([2000.0, 2010.0, 2020.0])
        now = 2020.0 + scanner.RELAY_RECOVERY_QUIET + 1
        scanner.debounce_relay_boot_bursts([c], now=now, boot_times=[1000.0])
        self.assertEqual(c["severity"], "err")

    def test_never_upgrades(self):
        boot = 1000.0
        all_ts = [boot + 5, boot + 20]
        c = self._cluster(all_ts, severity="info")
        now = all_ts[-1] + scanner.RELAY_RECOVERY_QUIET + 1
        scanner.debounce_relay_boot_bursts([c], now=now, boot_times=[boot])
        self.assertEqual(c["severity"], "info")

    def test_other_source_untouched(self):
        boot = 1000.0
        all_ts = [boot + 5, boot + 20]
        c = self._cluster(all_ts, source="journal-warnings")
        now = all_ts[-1] + scanner.RELAY_RECOVERY_QUIET + 1
        scanner.debounce_relay_boot_bursts([c], now=now, boot_times=[boot])
        self.assertEqual(c["severity"], "err")


class TestCollectFileSourceAuditTs(unittest.TestCase):
    """collect_file_source() must parse audit_ts out of JSON file rows so
    cluster-level debounces (e.g. boot-burst) have real per-row timestamps."""

    def test_audit_ts_parsed_from_json_rows(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "audit.jsonl")
        with open(p, "w") as f:
            f.write(json.dumps({"audit_ts": 123.5, "reason": "fetch-failed:URLError"}) + "\n")
            f.write("not json at all\n")
        src = {"name": "keeper-relay-audit", "kind": "file", "path": p}
        tagged, _, warnings = scanner.collect_file_source(src, {})
        self.assertEqual(warnings, [])
        self.assertEqual(tagged[0]["ts"], 123.5)
        self.assertIsNone(tagged[1]["ts"])  # non-JSON line falls back to None


class TestCollectFileRotation(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "log.txt")

    def test_incremental_and_rotation(self):
        with open(self.p, "w") as f:
            f.write("line one\nline two\n")
        lines, off = scanner.collect_file(self.p, 0)
        self.assertEqual(lines, ["line one", "line two"])
        self.assertEqual(off, os.path.getsize(self.p))

        # nothing new
        lines2, off2 = scanner.collect_file(self.p, off)
        self.assertEqual(lines2, [])
        self.assertEqual(off2, off)

        # append
        with open(self.p, "a") as f:
            f.write("line three\n")
        lines3, off3 = scanner.collect_file(self.p, off2)
        self.assertEqual(lines3, ["line three"])

        # rotation: file replaced with a smaller one, offset > size => reset
        with open(self.p, "w") as f:
            f.write("fresh\n")
        lines4, off4 = scanner.collect_file(self.p, off3)
        self.assertEqual(lines4, ["fresh"])
        self.assertEqual(off4, os.path.getsize(self.p))

    def test_partial_line_held_back(self):
        with open(self.p, "w") as f:
            f.write("complete\npartial-no-newline")
        lines, off = scanner.collect_file(self.p, 0)
        self.assertEqual(lines, ["complete"])
        # offset stops at the newline; partial re-read once completed
        with open(self.p, "a") as f:
            f.write(" now-done\n")
        lines2, off2 = scanner.collect_file(self.p, off)
        self.assertEqual(lines2, ["partial-no-newline now-done"])

    def test_missing_file(self):
        lines, off = scanner.collect_file(os.path.join(self.d, "nope.txt"), 0)
        self.assertEqual(lines, [])
        self.assertEqual(off, 0)


class TestCollectFileSourceGlob(unittest.TestCase):
    def test_glob_and_missing_warning(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "audit1.jsonl"), "w") as f:
            f.write('{"a":1}\n')
        with open(os.path.join(d, "audit2.jsonl"), "w") as f:
            f.write('{"b":2}\n')
        src = {"name": "aud", "kind": "file",
               "path": os.path.join(d, "audit*.jsonl")}
        tagged, offsets, warnings = scanner.collect_file_source(src, {})
        self.assertEqual(len(tagged), 2)
        self.assertEqual(warnings, [])
        self.assertEqual(len(offsets), 2)

        missing = {"name": "m", "kind": "file", "path": "/no/such/path.log"}
        tagged2, _, warnings2 = scanner.collect_file_source(missing, {})
        self.assertEqual(tagged2, [])
        self.assertEqual(len(warnings2), 1)


class TestCitationGate(unittest.TestCase):
    def test_keeps_cited_discards_uncited(self):
        valid = {1, 2, 3}
        reply = (
            "- console-ui-dev throwing TLS errors repeatedly [L1]\n"
            "- keeper-relay audit shows a hold decision [L2][L3]\n"
            "- The system will crash tomorrow at noon\n"          # no citation -> discard
            "- Something about line ninety-nine [L99]\n"          # cites invalid line -> discard
        )
        kept, discarded = scanner.citation_gate(reply, valid)
        self.assertEqual(len(kept), 2)
        self.assertEqual(discarded, 2)
        # citations resolved and filtered to valid ones
        self.assertEqual(kept[0][1], [1])
        self.assertEqual(kept[1][1], [2, 3])

    def test_all_uncited(self):
        kept, discarded = scanner.citation_gate(
            "- claim one\n- claim two\n", {1, 2})
        self.assertEqual(kept, [])
        self.assertEqual(discarded, 2)

    def test_paragraph_claims(self):
        reply = "First finding about disk [L1].\n\nSecond unfounded finding.\n"
        kept, discarded = scanner.citation_gate(reply, {1})
        self.assertEqual(len(kept), 1)
        self.assertEqual(discarded, 1)


# Real leaked strings observed from the fleet's local brain during weight
# cliff-testing (2026-07-18). Used verbatim as fixtures below.
LEAK_THINK_MIXED = (
    "**Issue 1: Timeout Error.** [L1] and [L6] both report "
    "fetch-failed:TimeoutError. This is an error (err in L1, though L6 says"
    "</think>"
)
ECHO_MUST_CITE = (
    "EVERY bullet MUST cite exact input line(s) with [Ln] tags "
    "(e.g., [L3], [L2][L7])."
)
ECHO_NO_ACTIONABLE = "If nothing actionable, reply: '- No actionable issues [L1]'."


class TestStripReasoning(unittest.TestCase):
    def test_well_formed_block_removed_bullet_survives(self):
        reply = (
            "<think>The user wants findings. [L1] looks scary but is fine.</think>\n"
            "- console-ui-dev throwing TLS errors repeatedly [L1]"
        )
        out = scanner.strip_reasoning(reply)
        self.assertNotIn("scary", out)
        self.assertNotIn("think", out.lower())
        # the real bullet (and only it) produces a finding
        kept, counts = scanner.gate_claims(out, {1})
        self.assertEqual(len(kept), 1)
        self.assertIn("TLS errors", kept[0][0])

    def test_multiple_blocks_removed(self):
        reply = "<think>one</think>keep A [L1]<think>two</think>keep B [L2]"
        out = scanner.strip_reasoning(reply)
        self.assertNotIn("one", out)
        self.assertNotIn("two", out)
        self.assertIn("keep A", out)
        self.assertIn("keep B", out)

    def test_lone_closer_no_opener_drops_reasoning(self):
        # reasoning-then-answer where the opener was trimmed off
        reply = "internally the model decided X was benign</think>\n- Real issue [L1]"
        out = scanner.strip_reasoning(reply)
        self.assertNotIn("benign", out)
        self.assertNotIn("think", out.lower())
        self.assertEqual(out, "- Real issue [L1]")

    def test_unterminated_opener_dropped_to_end(self):
        reply = "- Real finding [L1]\n<think>now I ramble with no closer forever"
        out = scanner.strip_reasoning(reply)
        self.assertEqual(out, "- Real finding [L1]")
        self.assertNotIn("ramble", out)

    def test_mixed_line_finding_retained_tag_gone(self):
        # stray trailing </think> on a real bullet: keep the finding, drop the tag
        out = scanner.strip_reasoning(LEAK_THINK_MIXED)
        self.assertNotIn("think", out.lower())
        self.assertIn("Timeout Error", out)
        self.assertIn("[L1]", out)
        self.assertIn("[L6]", out)
        # after strip it is a real cited finding, not garbage
        kept, counts = scanner.gate_claims(out, {1, 6})
        self.assertEqual(len(kept), 1)
        self.assertEqual(counts["scaffolding"], 0)

    def test_empty_and_none(self):
        self.assertEqual(scanner.strip_reasoning(""), "")
        self.assertEqual(scanner.strip_reasoning(None), "")


class TestScaffoldingEcho(unittest.TestCase):
    def test_framing_markers_are_present_in_prompt(self):
        # Drift guard: every framing fragment must actually occur in DIGEST_SYSTEM
        # so the reject-signal stays derived from the prompt, not a stale copy.
        low = scanner.DIGEST_SYSTEM.lower()
        for frag in scanner._SCAFFOLD_FRAMING:
            self.assertIn(frag, low, "framing fragment drifted from prompt: %r" % frag)

    def test_echo_must_cite_discarded(self):
        self.assertTrue(scanner.is_scaffolding_echo(ECHO_MUST_CITE))
        # even though it staples on valid-looking [L3] etc., it is not a finding
        kept, counts = scanner.gate_claims(ECHO_MUST_CITE, {1, 2, 3, 7})
        self.assertEqual(kept, [])
        self.assertEqual(counts["scaffolding"], 1)

    def test_echo_no_actionable_framing_discarded(self):
        self.assertTrue(scanner.is_scaffolding_echo(ECHO_NO_ACTIONABLE))
        kept, counts = scanner.gate_claims(ECHO_NO_ACTIONABLE, {1})
        self.assertEqual(kept, [])
        self.assertEqual(counts["scaffolding"], 1)

    def test_legit_no_actionable_bullet_survives(self):
        # THE CRUX: rule-4's expected reply must NOT be treated as an echo.
        reply = "- No actionable issues [L1]"
        self.assertFalse(scanner.is_scaffolding_echo("No actionable issues [L1]"))
        kept, counts = scanner.gate_claims(reply, {1})
        self.assertEqual(len(kept), 1)
        self.assertEqual(counts["scaffolding"], 0)
        self.assertEqual(kept[0][1], [1])

    def test_normal_finding_passes_unchanged(self):
        # regression guard: an ordinary cited finding is never rejected
        reply = "- keeper-relay audit shows a hold decision [L2][L3]"
        self.assertFalse(scanner.is_scaffolding_echo(
            "keeper-relay audit shows a hold decision [L2][L3]"))
        kept, counts = scanner.gate_claims(reply, {2, 3})
        self.assertEqual(len(kept), 1)
        self.assertEqual(counts, {"uncited": 0, "scaffolding": 0})

    def test_real_log_line_with_instruction_word_survives(self):
        # a genuine log finding that merely contains an instruction-ish word
        # (low overlap with the prompt vocabulary) is kept
        claim = "upstream server did not reply with an ACK before timeout [L1]"
        self.assertFalse(scanner.is_scaffolding_echo(claim))

    def test_residual_think_token_claim_discarded(self):
        self.assertTrue(scanner.is_scaffolding_echo("stray </think> leaked here [L1]"))

    def test_full_reply_strip_then_gate(self):
        # end-to-end: a messy reply with reasoning + echoes + one real finding
        reply = (
            "<think>let me plan my answer [L1]</think>\n\n"
            + ECHO_MUST_CITE + "\n\n"
            + ECHO_NO_ACTIONABLE + "\n\n"
            "- console-ui-dev throwing TLS errors repeatedly [L1]\n"
            "- The system will crash tomorrow\n"
        )
        kept, counts = scanner.gate_claims(scanner.strip_reasoning(reply), {1})
        self.assertEqual(len(kept), 1)
        self.assertIn("TLS errors", kept[0][0])
        self.assertEqual(counts["scaffolding"], 2)   # two prompt echoes
        self.assertEqual(counts["uncited"], 1)        # the crash claim
        # back-compat wrapper folds both discard kinds together
        _, discarded = scanner.citation_gate(scanner.strip_reasoning(reply), {1})
        self.assertEqual(discarded, 3)


class TestBuildSample(unittest.TestCase):
    def test_ordering_and_cap(self):
        clusters = [
            {"source": "s", "severity": "info", "count": 1, "message": "info thing",
             "first_ts": None, "last_ts": None},
            {"source": "s", "severity": "err", "count": 5, "message": "err thing",
             "first_ts": None, "last_ts": None},
        ]
        sample, line_map, capped = scanner.build_sample(clusters, cap=9000)
        # most severe first
        self.assertTrue(sample.startswith("[L1] count=5 sev=err"))
        self.assertEqual(len(line_map), 2)
        self.assertFalse(capped)

    def test_cap_truncates(self):
        clusters = [
            {"source": "s", "severity": "warning", "count": 1,
             "message": "x" * 100, "first_ts": None, "last_ts": None}
            for _ in range(50)
        ]
        sample, line_map, capped = scanner.build_sample(clusters, cap=300)
        self.assertTrue(capped)
        self.assertLess(len(line_map), 50)
        self.assertGreaterEqual(len(line_map), 1)


class TestConsumeRequest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "req.json")

    def test_at_most_once(self):
        with open(self.p, "w") as f:
            json.dump({"op": "scan", "sources": ["journal-warnings"]}, f)
        req = scanner.consume_request(self.p)
        self.assertEqual(req["op"], "scan")
        self.assertFalse(os.path.exists(self.p))  # deleted after read
        # second consume => nothing
        self.assertIsNone(scanner.consume_request(self.p))

    def test_malformed(self):
        with open(self.p, "w") as f:
            f.write("{not valid json")
        req = scanner.consume_request(self.p)
        self.assertTrue(req.get("_malformed"))
        self.assertFalse(os.path.exists(self.p))

    def test_non_object(self):
        with open(self.p, "w") as f:
            f.write("[1,2,3]")
        req = scanner.consume_request(self.p)
        self.assertTrue(req.get("_malformed"))


class TestAtomicWrite(unittest.TestCase):
    def test_write_and_no_tmp_left(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "out.json")
        scanner.atomic_write(p, '{"ok": true}')
        with open(p) as f:
            self.assertEqual(json.load(f), {"ok": True})
        # no leftover temp files
        leftovers = [n for n in os.listdir(d) if n.startswith(".bugreport-tmp-")]
        self.assertEqual(leftovers, [])

    def test_overwrite(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "out.txt")
        scanner.atomic_write(p, "first")
        scanner.atomic_write(p, "second")
        with open(p) as f:
            self.assertEqual(f.read(), "second")


class TestNormalize(unittest.TestCase):
    def test_hex_and_numbers(self):
        a = scanner.normalize_message("sha256 1d93bdb79201e03ae38933ca97e2f5a4 at 12:00:01")
        b = scanner.normalize_message("sha256 abcdef0123456789abcdef0123456789 at 09:44:59")
        self.assertEqual(a, b)


# --------------------------------------------------------------------------
# Watches — S1 validation (compile_watches is PURE: no I/O, fail-closed)
# --------------------------------------------------------------------------
class TestWatchValidation(unittest.TestCase):
    def _one(self, w):
        out = scanner.compile_watches([w])
        self.assertEqual(len(out), 1)
        return out[0]

    def test_good_file_watch_with_status(self):
        e = self._one({
            "name": "svc-health", "enabled": True,
            "source": {"kind": "file", "path": "/var/log/x.log"},
            "pattern": r"svc=(?P<svc>\w+) status=(?P<st>\w+)",
            "key": "svc", "status": "st"})
        self.assertIsNone(e["error"])
        self.assertEqual(e["key"], "svc")
        self.assertEqual(e["status"], "st")
        self.assertEqual(e["kind"], "file")
        self.assertEqual(e["path"], "/var/log/x.log")
        self.assertIsNotNone(e["regex"])

    def test_good_journal_watch_countmode(self):
        e = self._one({
            "name": "unit-events",
            "source": {"kind": "journal", "unit": "keeper-relay"},
            "pattern": r"event=(?P<ev>\S+)", "key": "ev"})
        self.assertIsNone(e["error"])
        self.assertIsNone(e["status"])          # status is optional -> count mode
        self.assertEqual(e["unit"], "keeper-relay")
        self.assertTrue(e["enabled"])           # defaults True

    def test_bad_regex_fails_closed(self):
        e = self._one({"name": "b", "source": {"kind": "file", "path": "/x"},
                       "pattern": r"(?P<k>unterminated", "key": "k"})
        self.assertIsNotNone(e["error"])
        self.assertIn("bad regex", e["error"])
        self.assertIsNone(e["regex"])

    def test_missing_key_capture(self):
        e = self._one({"name": "b", "source": {"kind": "file", "path": "/x"},
                       "pattern": r"(?P<other>\w+)", "key": "k"})
        self.assertIn("key 'k' is not a named capture", e["error"])

    def test_missing_status_capture(self):
        e = self._one({"name": "b", "source": {"kind": "file", "path": "/x"},
                       "pattern": r"(?P<k>\w+)", "key": "k", "status": "st"})
        self.assertIn("status 'st' is not a named capture", e["error"])

    def test_unknown_kind(self):
        e = self._one({"name": "b", "source": {"kind": "socket"},
                       "pattern": r"(?P<k>\w+)", "key": "k"})
        self.assertIn("unknown source kind", e["error"])

    def test_missing_source(self):
        e = self._one({"name": "b", "pattern": r"(?P<k>\w+)", "key": "k"})
        self.assertIn("source", e["error"])

    def test_journal_requires_unit(self):
        e = self._one({"name": "b", "source": {"kind": "journal"},
                       "pattern": r"(?P<k>\w+)", "key": "k"})
        self.assertIn("unit", e["error"])

    def test_missing_pattern(self):
        e = self._one({"name": "b", "source": {"kind": "file", "path": "/x"},
                       "key": "k"})
        self.assertIn("pattern", e["error"])

    def test_missing_key(self):
        e = self._one({"name": "b", "source": {"kind": "file", "path": "/x"},
                       "pattern": r"(?P<k>\w+)"})
        self.assertIn("key", e["error"])

    def test_non_object_watch(self):
        e = self._one("not-a-dict")
        self.assertFalse(e["enabled"])
        self.assertIn("not an object", e["error"])

    def test_order_preserved_mixed_valid_invalid(self):
        out = scanner.compile_watches([
            {"name": "ok", "source": {"kind": "file", "path": "/x"},
             "pattern": r"(?P<k>\w+)", "key": "k"},
            {"name": "bad", "source": {"kind": "file", "path": "/x"},
             "pattern": r"(?P<k>\w+)", "key": "nope"},
        ])
        self.assertEqual([w["name"] for w in out], ["ok", "bad"])
        self.assertIsNone(out[0]["error"])
        self.assertIsNotNone(out[1]["error"])


class TestWatchSanitize(unittest.TestCase):
    def test_slashes_and_spaces(self):
        self.assertEqual(scanner.sanitize_watch_name("relay events/2"), "relay_events_2")

    def test_traversal_neutralized(self):
        stem = scanner.sanitize_watch_name("../../etc/passwd")
        self.assertNotIn("/", stem)
        self.assertFalse(stem.startswith("."))

    def test_empty_and_dots_fallback(self):
        self.assertEqual(scanner.sanitize_watch_name(""), "watch")
        self.assertEqual(scanner.sanitize_watch_name("..."), "watch")

    def test_series_path_stays_in_dir(self):
        p = scanner.series_path("a/b/../c")
        self.assertEqual(os.path.dirname(os.path.realpath(p)),
                         os.path.realpath(scanner.SERIES_DIR))


# --------------------------------------------------------------------------
# Watches — S2 evaluation, series append/ring-cap, payload folding
# --------------------------------------------------------------------------
class _WatchStateMixin:
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._orig_series = scanner.SERIES_DIR
        scanner.SERIES_DIR = os.path.join(self.d, "series")

    def tearDown(self):
        scanner.SERIES_DIR = self._orig_series


class TestWatchEvaluate(_WatchStateMixin, unittest.TestCase):
    def _watch(self):
        self.log = os.path.join(self.d, "app.log")
        return scanner.compile_watches([{
            "name": "svc", "source": {"kind": "file", "path": self.log},
            "pattern": r"svc=(?P<svc>\S+) status=(?P<st>\S+) code=(?P<code>\d+)",
            "key": "svc", "status": "st"}])

    def test_incremental_and_extraction(self):
        compiled = self._watch()
        with open(self.log, "w") as f:
            f.write("svc=api status=ok code=200\n")
            f.write("svc=api status=degraded code=503\n")
            f.write("noise line, no match here\n")
            f.write("svc=db status=ok code=200\n")
        cur = {}
        entries, cur, ev, matches, warns = scanner.evaluate_watches(
            compiled, cur, "-1h", 1000.0)
        self.assertEqual(ev, 1)
        self.assertEqual(matches, 3)               # 3 matched, noise skipped
        e = entries[0]
        self.assertEqual(e["matched_last_scan"], 3)
        self.assertEqual(e["matched_total"], 3)
        # named-capture extraction into key/status/fields
        self.assertEqual(e["groups"]["api"]["count"], 2)
        self.assertEqual(e["groups"]["api"]["status"], "degraded")   # latest by ts
        self.assertEqual(e["groups"]["api"]["statuses"], {"ok": 1, "degraded": 1})
        self.assertEqual(e["groups"]["db"]["status"], "ok")
        # 'code' is neither key nor status -> lands in the series record fields
        series = os.path.join(scanner.SERIES_DIR, "svc.jsonl")
        with open(series) as f:
            recs = [json.loads(l) for l in f]
        self.assertEqual(recs[0]["fields"], {"code": "200"})
        self.assertEqual(recs[0]["key"], "api")

    def test_same_line_never_matched_twice(self):
        compiled = self._watch()
        with open(self.log, "w") as f:
            f.write("svc=api status=ok code=200\n")
        cur = {}
        _, cur, _, m1, _ = scanner.evaluate_watches(compiled, cur, "-1h", 1000.0)
        self.assertEqual(m1, 1)
        # re-evaluate with the advanced cursor, no new lines
        entries, cur, _, m2, _ = scanner.evaluate_watches(compiled, cur, "-1h", 1001.0)
        self.assertEqual(m2, 0)
        self.assertEqual(entries[0]["matched_last_scan"], 0)
        self.assertEqual(entries[0]["matched_total"], 1)     # series unchanged
        # append a new line -> only that one matches
        with open(self.log, "a") as f:
            f.write("svc=api status=ok code=201\n")
        entries, cur, _, m3, _ = scanner.evaluate_watches(compiled, cur, "-1h", 1002.0)
        self.assertEqual(m3, 1)
        self.assertEqual(entries[0]["matched_total"], 2)

    def test_armed_but_dry_empty_groups(self):
        compiled = self._watch()
        with open(self.log, "w") as f:
            f.write("nothing matches this watch\n")
        entries, _, ev, m, _ = scanner.evaluate_watches(compiled, {}, "-1h", 1000.0)
        self.assertEqual(ev, 1)
        self.assertEqual(m, 0)
        e = entries[0]
        self.assertTrue(e["enabled"])
        self.assertIsNone(e["error"])
        self.assertEqual(e["groups"], {})           # armed but dry: visible, empty
        self.assertEqual(e["matched_total"], 0)
        self.assertFalse(e["groups_truncated"])

    def test_disabled_and_invalid_still_appear(self):
        compiled = scanner.compile_watches([
            {"name": "off", "enabled": False,
             "source": {"kind": "file", "path": "/x"},
             "pattern": r"(?P<k>\w+)", "key": "k"},
            {"name": "broken", "source": {"kind": "file", "path": "/x"},
             "pattern": r"(?P<k>\w+)", "key": "missing"},
        ])
        entries, _, ev, m, _ = scanner.evaluate_watches(compiled, {}, "-1h", 1000.0)
        self.assertEqual(ev, 0)                     # neither evaluated
        self.assertEqual(len(entries), 2)
        self.assertFalse(entries[0]["enabled"])
        self.assertIsNotNone(entries[1]["error"])

    def test_countmode_no_status(self):
        log = os.path.join(self.d, "c.log")
        compiled = scanner.compile_watches([{
            "name": "ev", "source": {"kind": "file", "path": log},
            "pattern": r'"event":\s*"(?P<event>[^"]+)"', "key": "event"}])
        with open(log, "w") as f:
            f.write('{"event": "todo-notify", "n": 1}\n')
            f.write('{"decision": "hold"}\n')
            f.write('{"event": "todo-notify", "n": 2}\n')
        entries, _, _, m, _ = scanner.evaluate_watches(compiled, {}, "-1h", 5.0)
        self.assertEqual(m, 2)
        g = entries[0]["groups"]["todo-notify"]
        self.assertEqual(g["count"], 2)
        self.assertIsNone(g["status"])              # count mode: no status
        self.assertEqual(g["statuses"], {})


class TestSeriesRingCap(_WatchStateMixin, unittest.TestCase):
    def test_cap_keeps_newest(self):
        name = "ring"
        recs = [{"ts": float(i), "key": "k", "status": None,
                 "fields": {"i": i}} for i in range(scanner.SERIES_CAP + 1)]
        scanner.append_series(name, recs)          # 5001 -> triggers cap to 4000
        path = scanner.series_path(name)
        with open(path) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), scanner.SERIES_KEEP)
        first = json.loads(lines[0])
        last = json.loads(lines[-1])
        # newest kept: indices (5001-4000) .. 5000  => 1001 .. 5000
        self.assertEqual(first["fields"]["i"],
                         scanner.SERIES_CAP + 1 - scanner.SERIES_KEEP)
        self.assertEqual(last["fields"]["i"], scanner.SERIES_CAP)

    def test_no_cap_under_threshold(self):
        name = "small"
        scanner.append_series(name, [{"ts": 1.0, "key": "k", "status": None,
                                      "fields": {}} for _ in range(10)])
        with open(scanner.series_path(name)) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 10)


class TestWatchGroupsTruncated(_WatchStateMixin, unittest.TestCase):
    def test_groups_capped_to_most_recent(self):
        name = "many"
        # 60 distinct keys, ascending ts => keys "k59".."k10" are most recent
        recs = [{"ts": float(i), "key": "k%d" % i, "status": None, "fields": {}}
                for i in range(60)]
        scanner.append_series(name, recs)
        w = {"name": name, "enabled": True, "error": None}
        entry = scanner.build_watch_entry(w, 0)
        self.assertTrue(entry["groups_truncated"])
        self.assertEqual(len(entry["groups"]), scanner.WATCH_GROUPS_CAP)
        self.assertEqual(entry["matched_total"], 60)   # total counts ALL records
        self.assertIn("k59", entry["groups"])          # most recent kept
        self.assertNotIn("k0", entry["groups"])        # oldest dropped


# --------------------------------------------------------------------------
# Watches — S3: the `http` source kind (poll + transition-dedup + accumulate)
# --------------------------------------------------------------------------
def _http_cfg(**over):
    cfg = {"name": "gen", "enabled": True,
           "source": {"kind": "http", "url": "https://x/api/llm/jobs"},
           "id_field": "id", "key_field": "model_key", "status_field": "status",
           "capture": ["kind", "worker", "error"]}
    cfg.update(over)
    return cfg


def _row(rid, model_key, status, **extra):
    r = {"id": rid, "model_key": model_key, "status": status}
    r.update(extra)
    return r


class TestHttpWatchValidation(unittest.TestCase):
    def _one(self, w):
        out = scanner.compile_watches([w])
        self.assertEqual(len(out), 1)
        return out[0]

    def test_good_http_watch(self):
        e = self._one(_http_cfg())
        self.assertIsNone(e["error"])
        self.assertEqual(e["kind"], "http")
        self.assertEqual(e["url"], "https://x/api/llm/jobs")
        self.assertEqual(e["id_field"], "id")
        self.assertEqual(e["key_field"], "model_key")
        self.assertEqual(e["status_field"], "status")
        self.assertEqual(e["capture"], ["kind", "worker", "error"])
        # line-kind fields stay None — http is field-mapped, not regex-mapped
        self.assertIsNone(e["regex"])
        self.assertIsNone(e["key"])
        self.assertIsNone(e["status"])

    def test_http_does_not_require_pattern_key_status(self):
        # NO pattern/key/status supplied at all -> still valid (line-kind fields
        # must not be required for an http watch).
        e = self._one({"name": "g", "source": {"kind": "http", "url": "https://x"},
                       "id_field": "id", "key_field": "model_key",
                       "status_field": "status"})
        self.assertIsNone(e["error"])
        self.assertEqual(e["capture"], [])          # capture optional -> []

    def test_http_missing_url(self):
        e = self._one(_http_cfg(source={"kind": "http"}))
        self.assertIn("url", e["error"])

    def test_http_missing_field_names(self):
        e = self._one({"name": "g", "source": {"kind": "http", "url": "https://x"}})
        self.assertIsNotNone(e["error"])
        for fn in ("id_field", "key_field", "status_field"):
            self.assertIn(fn, e["error"])

    def test_http_single_missing_field(self):
        e = self._one(_http_cfg(status_field=""))
        self.assertIn("status_field", e["error"])
        self.assertNotIn("id_field", e["error"])

    def test_http_bad_capture(self):
        self.assertIn("capture", self._one(_http_cfg(capture="kind"))["error"])
        self.assertIn("capture", self._one(_http_cfg(capture=[1, 2]))["error"])
        self.assertIn("capture", self._one(_http_cfg(capture=[""]))["error"])

    def test_http_capture_none_ok(self):
        e = self._one(_http_cfg(capture=None))
        self.assertIsNone(e["error"])
        self.assertEqual(e["capture"], [])

    def test_line_kind_validation_unchanged_by_http(self):
        # A file watch missing its pattern is STILL invalid (byte-identical path).
        e = self._one({"name": "f", "source": {"kind": "file", "path": "/x"},
                       "key": "k"})
        self.assertIn("pattern", e["error"])


class TestParseEpochOrIso(unittest.TestCase):
    def test_epoch_float_and_int(self):
        self.assertEqual(scanner.parse_epoch_or_iso(1784248982.078), 1784248982.078)
        self.assertEqual(scanner.parse_epoch_or_iso(1784248982), 1784248982.0)

    def test_numeric_string(self):
        self.assertEqual(scanner.parse_epoch_or_iso("1784248982.5"), 1784248982.5)

    def test_iso_with_tz(self):
        # 2026-07-17T00:43:02+00:00 -> known epoch
        self.assertAlmostEqual(
            scanner.parse_epoch_or_iso("2026-07-17T00:43:02.078077+00:00"),
            1784248982.078077, places=3)

    def test_iso_with_z(self):
        self.assertAlmostEqual(
            scanner.parse_epoch_or_iso("2026-07-17T00:43:02Z"),
            1784248982.0, places=3)

    def test_naive_iso_read_as_utc(self):
        self.assertAlmostEqual(
            scanner.parse_epoch_or_iso("2026-07-17T00:43:02"),
            1784248982.0, places=3)

    def test_garbage_and_none_and_bool(self):
        self.assertIsNone(scanner.parse_epoch_or_iso("not-a-date"))
        self.assertIsNone(scanner.parse_epoch_or_iso(None))
        self.assertIsNone(scanner.parse_epoch_or_iso(""))
        self.assertIsNone(scanner.parse_epoch_or_iso(True))   # bool != ts


class TestHttpExtractRows(unittest.TestCase):
    def test_bare_array(self):
        self.assertEqual(scanner._extract_rows([{"a": 1}]), [{"a": 1}])

    def test_jobs_wrapper(self):
        self.assertEqual(scanner._extract_rows({"counts": {}, "jobs": [1, 2]}), [1, 2])

    def test_data_and_results_wrappers(self):
        self.assertEqual(scanner._extract_rows({"data": [1]}), [1])
        self.assertEqual(scanner._extract_rows({"results": [2]}), [2])

    def test_unrecognised_shapes(self):
        self.assertIsNone(scanner._extract_rows({"nope": [1]}))
        self.assertIsNone(scanner._extract_rows("string"))
        self.assertIsNone(scanner._extract_rows(42))


class _FakeResp:
    def __init__(self, body):
        self._body = body.encode() if isinstance(body, str) else body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestFetchHttpRows(unittest.TestCase):
    def test_bare_array_ok(self):
        with mock.patch("scanner.urllib.request.urlopen",
                        return_value=_FakeResp('[{"id": "a"}]')):
            rows, err = scanner.fetch_http_rows("https://x")
        self.assertIsNone(err)
        self.assertEqual(rows, [{"id": "a"}])

    def test_jobs_wrapper_ok(self):
        with mock.patch("scanner.urllib.request.urlopen",
                        return_value=_FakeResp('{"jobs": [{"id": "a"}]}')):
            rows, err = scanner.fetch_http_rows("https://x")
        self.assertEqual(rows, [{"id": "a"}])

    def test_http_500_surfaces_error(self):
        import urllib.error
        exc = urllib.error.HTTPError("https://x", 500, "Server Error", {}, None)
        with mock.patch("scanner.urllib.request.urlopen", side_effect=exc):
            rows, err = scanner.fetch_http_rows("https://x")
        self.assertIsNone(rows)
        self.assertIn("HTTPError", err)

    def test_timeout_surfaces_error(self):
        with mock.patch("scanner.urllib.request.urlopen",
                        side_effect=TimeoutError("timed out")):
            rows, err = scanner.fetch_http_rows("https://x")
        self.assertIsNone(rows)
        self.assertIn("TimeoutError", err)

    def test_non_json_surfaces_error(self):
        with mock.patch("scanner.urllib.request.urlopen",
                        return_value=_FakeResp("<html>nope</html>")):
            rows, err = scanner.fetch_http_rows("https://x")
        self.assertIsNone(rows)
        self.assertIn("non-JSON", err)

    def test_bad_shape_surfaces_error(self):
        with mock.patch("scanner.urllib.request.urlopen",
                        return_value=_FakeResp('{"nope": 1}')):
            rows, err = scanner.fetch_http_rows("https://x")
        self.assertIsNone(rows)
        self.assertIn("unexpected JSON shape", err)


class TestPollHttpWatch(unittest.TestCase):
    def setUp(self):
        self.w = scanner.compile_watches([_http_cfg()])[0]

    def _poll(self, rows, seen, now=1000.0):
        with mock.patch("scanner.fetch_http_rows", return_value=(rows, None)):
            return scanner.poll_http_watch(self.w, seen, now)

    def test_new_id_fires(self):
        seen = {}
        recs, err, skipped, pruned = self._poll(
            [_row("j1", "modelA", "pending", kind="v1")], seen)
        self.assertIsNone(err)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["key"], "modelA")
        self.assertEqual(recs[0]["status"], "pending")
        self.assertEqual(recs[0]["fields"]["id"], "j1")
        self.assertEqual(recs[0]["fields"]["kind"], "v1")
        self.assertEqual(seen["j1"], "pending")

    def test_status_change_fires(self):
        seen = {"j1": "pending"}
        recs, _, _, _ = self._poll([_row("j1", "modelA", "done")], seen)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["status"], "done")
        self.assertEqual(seen["j1"], "done")

    def test_same_id_status_noop(self):
        seen = {"j1": "done"}
        recs, _, _, _ = self._poll([_row("j1", "modelA", "done")], seen)
        self.assertEqual(recs, [])
        self.assertEqual(seen["j1"], "done")       # unchanged

    def test_malformed_rows_skipped_counted(self):
        seen = {}
        rows = [
            "not-a-dict",
            {"id": "j2", "model_key": "m"},        # missing status
            {"model_key": "m", "status": "done"},  # missing id
            _row("j3", "m", "processing"),          # good
        ]
        recs, _, skipped, _ = self._poll(rows, seen)
        self.assertEqual(len(recs), 1)
        self.assertEqual(skipped, 3)

    def test_ts_fallback_chain(self):
        seen = {}
        # progressed_at (epoch float) preferred
        recs, _, _, _ = self._poll(
            [_row("a", "m", "done", progressed_at=111.0,
                  updated_at="2026-07-17T00:43:02+00:00")], seen)
        self.assertEqual(recs[0]["ts"], 111.0)
        # falls to updated_at ISO when progressed_at absent
        recs, _, _, _ = self._poll(
            [_row("b", "m", "done", updated_at="2026-07-17T00:43:02+00:00")], seen)
        self.assertAlmostEqual(recs[0]["ts"], 1784248982.0, places=3)
        # falls to scan time when no ts fields
        recs, _, _, _ = self._poll([_row("c", "m", "done")], seen, now=777.0)
        self.assertEqual(recs[0]["ts"], 777.0)

    def test_captured_null_fields_dropped(self):
        seen = {}
        recs, _, _, _ = self._poll(
            [_row("a", "m", "done", kind="v1", worker=None, error=None)], seen)
        self.assertEqual(recs[0]["fields"], {"id": "a", "kind": "v1"})

    def test_error_leaves_seen_untouched(self):
        seen = {"j1": "pending"}
        with mock.patch("scanner.fetch_http_rows", return_value=(None, "boom")):
            recs, err, skipped, pruned = scanner.poll_http_watch(self.w, seen, 1.0)
        self.assertEqual(recs, [])
        self.assertEqual(err, "boom")
        self.assertEqual(seen, {"j1": "pending"})   # state intact

    def test_prune_and_reappear_fires_again(self):
        seen = {}
        with mock.patch("scanner.HTTP_STATE_CAP", 2):
            recs, _, _, pruned = self._poll(
                [_row("a", "m", "s1"), _row("b", "m", "s1"),
                 _row("c", "m", "s1")], seen)
            self.assertEqual(len(recs), 3)
            self.assertTrue(pruned)                 # 3 > cap 2 -> pruned oldest
            self.assertNotIn("a", seen)             # 'a' (oldest) pruned out
            self.assertEqual(set(seen), {"b", "c"})
            # 'a' reappears with the SAME status -> fires again (documented, OK)
            recs2, _, _, _ = self._poll([_row("a", "m", "s1")], seen)
            self.assertEqual(len(recs2), 1)


class TestEvaluateHttpWatch(_WatchStateMixin, unittest.TestCase):
    def _watch(self):
        return scanner.compile_watches([_http_cfg()])

    def _eval(self, compiled, http_state, rows, now, err=None):
        ret = (None, err) if err else (rows, None)
        with mock.patch("scanner.fetch_http_rows", return_value=ret):
            entries, _, ev, matches, warns = scanner.evaluate_watches(
                compiled, {}, "-1h", now, http_state)
        return entries[0], matches, warns

    def test_success_appends_and_folds_histogram(self):
        compiled = self._watch()
        http_state = {}
        rows = [_row("j1", "modelA", "pending"), _row("j2", "modelA", "done"),
                _row("j3", "modelB", "failed")]
        entry, matches, _ = self._eval(compiled, http_state, rows, 1000.0)
        self.assertEqual(matches, 3)
        self.assertEqual(entry["matched_last_scan"], 3)
        self.assertIsNone(entry["error"])
        # groups keyed by model_key, statuses histogram accumulates
        self.assertEqual(entry["groups"]["modelA"]["count"], 2)
        self.assertEqual(entry["groups"]["modelA"]["statuses"], {"pending": 1, "done": 1})
        self.assertEqual(entry["groups"]["modelB"]["statuses"], {"failed": 1})
        # transition state persisted
        self.assertEqual(http_state["gen"]["seen"]["j1"], "pending")

    def test_second_scan_unchanged_rows_no_dup(self):
        compiled = self._watch()
        http_state = {}
        rows = [_row("j1", "modelA", "done"), _row("j2", "modelB", "done")]
        e1, m1, _ = self._eval(compiled, http_state, rows, 1000.0)
        self.assertEqual(m1, 2)
        # SAME rows re-served (rolling window re-read) -> no new records
        e2, m2, _ = self._eval(compiled, http_state, rows, 1001.0)
        self.assertEqual(m2, 0)
        self.assertEqual(e2["matched_last_scan"], 0)
        self.assertEqual(e2["matched_total"], 2)    # series unchanged

    def test_error_surfaces_and_state_intact(self):
        compiled = self._watch()
        http_state = {}
        rows = [_row("j1", "modelA", "done")]
        self._eval(compiled, http_state, rows, 1000.0)
        seen_before = dict(http_state["gen"]["seen"])
        entry, matches, warns = self._eval(compiled, http_state, None, 1001.0,
                                           err="HTTPError: 500")
        self.assertEqual(matches, 0)
        self.assertEqual(entry["error"], "HTTPError: 500")   # surfaced in the row
        self.assertEqual(entry["matched_total"], 1)          # series NOT wiped
        self.assertEqual(http_state["gen"]["seen"], seen_before)  # state intact

    def test_recovery_after_error_no_dup(self):
        compiled = self._watch()
        http_state = {}
        rows = [_row("j1", "modelA", "done")]
        self._eval(compiled, http_state, rows, 1000.0)       # establish
        self._eval(compiled, http_state, None, 1001.0, err="boom")  # fail
        entry, matches, _ = self._eval(compiled, http_state, rows, 1002.0)  # recover
        self.assertEqual(matches, 0)                 # unchanged (id,status): no re-fire
        self.assertIsNone(entry["error"])            # error cleared on recovery
        self.assertEqual(entry["matched_total"], 1)

    def test_audit_once_per_distinct_error(self):
        compiled = self._watch()
        http_state = {}
        with mock.patch("scanner.audit_append") as aud:
            self._eval(compiled, http_state, None, 1.0, err="boom")
            self._eval(compiled, http_state, None, 2.0, err="boom")   # same -> no re-audit
            self._eval(compiled, http_state, None, 3.0, err="other")  # distinct -> audit
        events = [c.args[0] for c in aud.call_args_list
                  if c.args and c.args[0].get("event") == "watch-http-error"]
        self.assertEqual([e["error"] for e in events], ["boom", "other"])

    def test_audit_once_on_first_prune(self):
        compiled = self._watch()
        http_state = {}
        with mock.patch("scanner.HTTP_STATE_CAP", 2), \
             mock.patch("scanner.audit_append") as aud:
            # first poll overflows cap -> prune + audit
            self._eval(compiled, http_state,
                       [_row("a", "m", "s"), _row("b", "m", "s"),
                        _row("c", "m", "s")], 1.0)
            # second poll prunes again but must NOT re-audit
            self._eval(compiled, http_state,
                       [_row("d", "m", "s"), _row("e", "m", "s")], 2.0)
        prunes = [c for c in aud.call_args_list
                  if c.args and c.args[0].get("event") == "watch-http-prune"]
        self.assertEqual(len(prunes), 1)


# --------------------------------------------------------------------------
# Activity / system-vitals snapshot (t46) — defensive, fail-soft
# --------------------------------------------------------------------------
class TestReadRelayActivity(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "state.json")

    def _write(self, obj):
        with open(self.p, "w") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))

    def test_real_value(self):
        self._write({"last_activity_ts": 1784353925.1191492, "last_ts": 1.0})
        la, bq = scanner.read_relay_activity(self.p)
        self.assertEqual(la, 1784353925.1191492)
        self.assertIsNone(bq)                 # relay doesn't persist quiet window

    def test_missing_file(self):
        la, bq = scanner.read_relay_activity(os.path.join(self.d, "nope.json"))
        self.assertIsNone(la)
        self.assertIsNone(bq)

    def test_garbage_json(self):
        self._write("{not valid json")
        la, bq = scanner.read_relay_activity(self.p)
        self.assertIsNone(la)
        self.assertIsNone(bq)

    def test_missing_key(self):
        self._write({"last_ts": 42.0, "queue": []})     # no last_activity_ts
        la, _ = scanner.read_relay_activity(self.p)
        self.assertIsNone(la)

    def test_non_dict_body(self):
        self._write("[1, 2, 3]")
        la, bq = scanner.read_relay_activity(self.p)
        self.assertIsNone(la)
        self.assertIsNone(bq)

    def test_wrong_typed_value(self):
        self._write({"last_activity_ts": "not-a-number"})
        la, _ = scanner.read_relay_activity(self.p)
        self.assertIsNone(la)

    def test_bool_not_accepted_as_number(self):
        self._write({"last_activity_ts": True})         # bool is not a real ts
        la, _ = scanner.read_relay_activity(self.p)
        self.assertIsNone(la)

    def test_forward_compat_quiet_window(self):
        # We never reimplement the relay's env/default resolution, but if the
        # relay ever persists the window in its state we surface it (either spelling).
        self._write({"last_activity_ts": 5.0, "board_quiet_sec": 600})
        _, bq = scanner.read_relay_activity(self.p)
        self.assertEqual(bq, 600)
        self._write({"last_activity_ts": 5.0, "board_quiet_secs": 900})
        _, bq2 = scanner.read_relay_activity(self.p)
        self.assertEqual(bq2, 900)


class TestReadMeminfo(unittest.TestCase):
    def _write(self, text):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "meminfo")
        with open(p, "w") as f:
            f.write(text)
        return p

    def test_parses_fields(self):
        p = self._write("MemTotal:        6041656 kB\n"
                        "MemFree:          200000 kB\n"
                        "MemAvailable:    4958156 kB\n"
                        "Buffers:           10000 kB\n")
        avail, total = scanner.read_meminfo(p)
        self.assertEqual(avail, 4958156)
        self.assertEqual(total, 6041656)

    def test_missing_file(self):
        avail, total = scanner.read_meminfo("/no/such/meminfo")
        self.assertIsNone(avail)
        self.assertIsNone(total)

    def test_partial_and_garbage(self):
        p = self._write("MemTotal:   notanumber kB\nMemAvailable:  123 kB\n")
        avail, total = scanner.read_meminfo(p)
        self.assertEqual(avail, 123)            # good field still parsed
        self.assertIsNone(total)                # garbage field -> None, no crash


class TestCollectActivity(unittest.TestCase):
    def test_shape_present(self):
        act = scanner.collect_activity()
        for key in ("keeper_last_activity_ts", "board_quiet_secs", "loadavg",
                    "mem_available_kb", "mem_total_kb", "disk_free_gb",
                    "disk_total_gb", "collected_ts"):
            self.assertIn(key, act)
        self.assertIsInstance(act["collected_ts"], float)

    def test_all_probes_broken_yield_nulls(self):
        with mock.patch("scanner.read_relay_activity", return_value=(None, None)), \
             mock.patch("scanner.read_meminfo", return_value=(None, None)), \
             mock.patch("scanner.os.getloadavg", side_effect=OSError("boom")), \
             mock.patch("scanner.shutil.disk_usage", side_effect=OSError("boom")):
            act = scanner.collect_activity()
        for k in ("keeper_last_activity_ts", "board_quiet_secs", "loadavg",
                  "mem_available_kb", "mem_total_kb", "disk_free_gb",
                  "disk_total_gb"):
            self.assertIsNone(act[k])
        self.assertIsInstance(act["collected_ts"], float)   # snapshot ts always set


class _ScanEnvMixin:
    """Redirect every path run_scan writes so a full scan is hermetic."""
    _ATTRS = ("REPORT_PATH", "CURSOR_PATH", "META_PATH", "AUDIT_PATH",
              "STATE_DIR", "WATCH_CURSOR_PATH", "SERIES_DIR",
              "HTTP_STATE_PATH", "KEEPER_RELAY_STATE_PATH")

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._saved = {a: getattr(scanner, a) for a in self._ATTRS}
        st = os.path.join(self.d, "state")
        scanner.STATE_DIR = st
        scanner.REPORT_PATH = os.path.join(self.d, "bugreport.json")
        scanner.CURSOR_PATH = os.path.join(st, "cursors.json")
        scanner.META_PATH = os.path.join(st, "meta.json")
        scanner.AUDIT_PATH = os.path.join(st, "audit.jsonl")
        scanner.WATCH_CURSOR_PATH = os.path.join(st, "watch_cursors.json")
        scanner.SERIES_DIR = os.path.join(st, "series")
        scanner.HTTP_STATE_PATH = os.path.join(st, "watch_http_state.json")
        scanner.KEEPER_RELAY_STATE_PATH = os.path.join(self.d, "relay-state.json")

    def tearDown(self):
        for a, v in self._saved.items():
            setattr(scanner, a, v)


class TestRunScanActivity(_ScanEnvMixin, unittest.TestCase):
    CONFIG = {"first_run_since": "-1h", "sources": [], "watches": []}

    def test_activity_section_in_report(self):
        with open(scanner.KEEPER_RELAY_STATE_PATH, "w") as f:
            json.dump({"last_activity_ts": 123456.0, "last_ts": 1.0}, f)
        with mock.patch("scanner.get_status",
                        return_value={"failed_units": [], "key_units": {}}):
            report, _ = scanner.run_scan(self.CONFIG, trigger="test", do_llm=False)
        self.assertIn("activity", report)
        self.assertEqual(report["activity"]["keeper_last_activity_ts"], 123456.0)
        self.assertIn("collected_ts", report["activity"])
        # the atomically-written report carries the section too
        with open(scanner.REPORT_PATH) as f:
            self.assertIn("activity", json.load(f))

    def test_every_probe_broken_scan_still_succeeds(self):
        # relay state file absent (missing -> null) + all system probes raising
        with mock.patch("scanner.get_status",
                        return_value={"failed_units": [], "key_units": {}}), \
             mock.patch("scanner.read_meminfo", return_value=(None, None)), \
             mock.patch("scanner.os.getloadavg", side_effect=OSError), \
             mock.patch("scanner.shutil.disk_usage", side_effect=OSError):
            report, _ = scanner.run_scan(self.CONFIG, trigger="test", do_llm=False)
        self.assertEqual(report["schema"], "bugreport.v1")     # scan succeeded
        act = report["activity"]
        for k in ("keeper_last_activity_ts", "board_quiet_secs", "loadavg",
                  "mem_available_kb", "mem_total_kb", "disk_free_gb",
                  "disk_total_gb"):
            self.assertIsNone(act[k])
        self.assertIsInstance(act["collected_ts"], float)


class TestRunScanHttpWatch(_ScanEnvMixin, unittest.TestCase):
    def _config(self):
        return {"first_run_since": "-1h", "sources": [], "watches": [_http_cfg()]}

    def test_http_watch_in_payload_and_persists(self):
        rows = [_row("j1", "modelA", "pending", kind="v1"),
                _row("j2", "modelA", "done", kind="v1"),
                _row("j3", "modelB", "expired", kind="chat")]
        with mock.patch("scanner.get_status",
                        return_value={"failed_units": [], "key_units": {}}), \
             mock.patch("scanner.fetch_http_rows", return_value=(rows, None)):
            report, _ = scanner.run_scan(self._config(), trigger="t", do_llm=False)
            wentry = [w for w in report["watches"] if w["name"] == "gen"][0]
            self.assertEqual(wentry["matched_last_scan"], 3)
            self.assertEqual(wentry["groups"]["modelA"]["statuses"],
                             {"pending": 1, "done": 1})
            self.assertEqual(wentry["groups"]["modelB"]["statuses"], {"expired": 1})
            # state file written, seen persisted
            self.assertTrue(os.path.exists(scanner.HTTP_STATE_PATH))
            # a SECOND scan re-serving the same window adds no duplicates
            report2, _ = scanner.run_scan(self._config(), trigger="t", do_llm=False)
            wentry2 = [w for w in report2["watches"] if w["name"] == "gen"][0]
            self.assertEqual(wentry2["matched_last_scan"], 0)
            self.assertEqual(wentry2["matched_total"], 3)


class TestBenignJournalSignatures(unittest.TestCase):
    """collect_journal_source downgrades known self-healing transients to info."""

    def _run(self, entries):
        stdout = "\n".join(json.dumps(e) for e in entries)
        proc = mock.Mock(stdout=stdout, stderr="", returncode=0)
        with mock.patch.object(scanner.subprocess, "run", return_value=proc):
            return scanner.collect_journal_source(
                {"name": "j", "priority": "info"}, None, "-1h")

    def test_networkd_wait_online_timeout_downgraded(self):
        # err-priority wait-online timeout under an apt-daily unit -> info.
        tagged, _, _ = self._run([{
            "PRIORITY": 3,
            "MESSAGE": "Timeout occurred while waiting for network connectivity.",
            "_SYSTEMD_UNIT": "apt-daily.service",
            "__CURSOR": "c1"}])
        self.assertEqual(len(tagged), 1)
        self.assertEqual(tagged[0]["severity"], "info")

        # Also matches via the wait-online identifier itself.
        tagged, _, _ = self._run([{
            "PRIORITY": 3,
            "MESSAGE": "Timeout occurred while waiting for network connectivity.",
            "SYSLOG_IDENTIFIER": "systemd-networkd-wait-online",
            "__CURSOR": "c2"}])
        self.assertEqual(tagged[0]["severity"], "info")

    def test_similar_but_nonmatching_lines_not_downgraded(self):
        # Same message, unrelated unit -> stays err (unit pattern must match).
        tagged, _, _ = self._run([{
            "PRIORITY": 3,
            "MESSAGE": "Timeout occurred while waiting for network connectivity.",
            "_SYSTEMD_UNIT": "myapp.service",
            "__CURSOR": "c3"}])
        self.assertEqual(tagged[0]["severity"], "err")

        # Right unit, different (genuine) error message -> stays err.
        tagged, _, _ = self._run([{
            "PRIORITY": 3,
            "MESSAGE": "Failed to bring up eth0: no carrier",
            "_SYSTEMD_UNIT": "apt-daily.service",
            "__CURSOR": "c4"}])
        self.assertEqual(tagged[0]["severity"], "err")

    def test_shutdown_virtiofs_unmount_downgraded(self):
        # Poweroff-time busy unmount of the two virtiofs mounts -> info.
        tagged, _, _ = self._run([
            {"PRIORITY": 3,
             "MESSAGE": "Failed unmounting run-lxd_agent.mount - /run/lxd_agent.",
             "_SYSTEMD_UNIT": "init.scope", "__CURSOR": "u1"},
            {"PRIORITY": 3,
             "MESSAGE": "Failed unmounting srv-share-projects-blackbird.mount.",
             "_SYSTEMD_UNIT": "init.scope", "__CURSOR": "u2"},
        ])
        self.assertEqual([t["severity"] for t in tagged], ["info", "info"])

        # Some OTHER mount failing to unmount stays err (mount name must match).
        tagged, _, _ = self._run([{
            "PRIORITY": 3,
            "MESSAGE": "Failed unmounting home-ubuntu-data.mount.",
            "_SYSTEMD_UNIT": "init.scope", "__CURSOR": "u3"}])
        self.assertEqual(tagged[0]["severity"], "err")

        # Same message from a non-init.scope unit stays err.
        tagged, _, _ = self._run([{
            "PRIORITY": 3,
            "MESSAGE": "Failed unmounting run-lxd_agent.mount - /run/lxd_agent.",
            "_SYSTEMD_UNIT": "myapp.service", "__CURSOR": "u4"}])
        self.assertEqual(tagged[0]["severity"], "err")

    def test_lxd_agent_closenotify_downgraded(self):
        # Verbatim line from the journal (2026-07-25), the one that fired
        # bugerr:7@1784938150 after 3060 of them landed in a 3h window.
        CLOSENOTIFY = ('time="2026-07-25T01:36:09Z" level=warning msg="Failed '
                       'closing connection" err="tls: failed to send closeNotify '
                       'alert (but connection was closed anyway): write vsock '
                       'vm(4294967295):8443->host(2):1953486993: broken pipe" '
                       'remote="host(2):1953486993"')
        tagged, _, _ = self._run([
            {"PRIORITY": 4, "MESSAGE": CLOSENOTIFY,
             "_SYSTEMD_UNIT": "lxd-agent.service", "__CURSOR": "n1"},
            # falls back to SYSLOG_IDENTIFIER when _SYSTEMD_UNIT is absent
            {"PRIORITY": 4, "MESSAGE": CLOSENOTIFY,
             "SYSLOG_IDENTIFIER": "lxd-agent", "__CURSOR": "n2"},
        ])
        self.assertEqual([t["severity"] for t in tagged], ["info", "info"])

        # The REAL lxd-agent problems in that same window must stay visible —
        # suppressing by unit alone would have buried both of these.
        tagged, _, _ = self._run([{
            "PRIORITY": 1,
            "MESSAGE": "    root : unable to resolve host blackbird: "
                       "Temporary failure in name resolution",
            "_SYSTEMD_UNIT": "lxd-agent.service", "__CURSOR": "n3"}])
        self.assertEqual(tagged[0]["severity"], "alert")

        tagged, _, _ = self._run([{
            "PRIORITY": 3, "MESSAGE": "[ppid=611] pututline: No such file or directory",
            "_SYSTEMD_UNIT": "lxd-agent.service", "__CURSOR": "n4"}])
        self.assertEqual(tagged[0]["severity"], "err")

        # A closeNotify failure that is NOT the vsock teardown keeps its severity:
        # the whole point is that the connection closed anyway on this transport.
        tagged, _, _ = self._run([{
            "PRIORITY": 3,
            "MESSAGE": 'msg="Failed closing connection" err="tls: failed to send '
                       'closeNotify alert: write tcp 10.0.0.5:8443: broken pipe"',
            "_SYSTEMD_UNIT": "lxd-agent.service", "__CURSOR": "n5"}])
        self.assertEqual(tagged[0]["severity"], "err")

        # Same message from a different unit stays err (unit must match too).
        tagged, _, _ = self._run([{
            "PRIORITY": 3, "MESSAGE": CLOSENOTIFY,
            "_SYSTEMD_UNIT": "myapp.service", "__CURSOR": "n6"}])
        self.assertEqual(tagged[0]["severity"], "err")

    def test_normal_lines_unchanged(self):
        tagged, _, _ = self._run([
            {"PRIORITY": 3, "MESSAGE": "disk full on /dev/sda1",
             "_SYSTEMD_UNIT": "storage.service", "__CURSOR": "c5"},
            {"PRIORITY": 4, "MESSAGE": "high memory usage",
             "_SYSTEMD_UNIT": "app.service", "__CURSOR": "c6"},
        ])
        self.assertEqual([t["severity"] for t in tagged], ["err", "warning"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
