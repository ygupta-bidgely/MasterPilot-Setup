"""Evidence chain classification against captured masterpilot 88002 logs (2026-10-05)."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import mds_tou  # noqa: E402
import tou_evidence as te  # noqa: E402

U = "35072534-8436-4d02-9cd1-c7285bc4eee1"
ONB = HERE / "fixtures" / "onboarding_35072534"
DAY = (te.ts_of("2026-10-05 00:00:00,000"), te.ts_of("2026-10-05 23:59:59,999"))


def lines(p):
    return p.read_text().splitlines()


def onboarding_raw():
    return {"ingestion": lines(ONB / "ingestion.log"), "notifications-priority": lines(ONB / "notifications-priority.log"),
            "emailer": lines(ONB / "emailer.log")}


class OnboardingChainTest(unittest.TestCase):
    def test_published_processed_dropped_on_a_solar_home(self):
        c = te.classify(onboarding_raw(), U, "TOU_ONBOARDING", "ELECTRIC", DAY, mds_tou.KNOWN_ISSUES, solar_at_event=True)
        self.assertIn("Successfully published tou onboarding", c.published)
        self.assertIn("EventName=TOU_ONBOARDING", c.processed)
        self.assertIn("tou rate structure data not available", c.dropped)
        self.assertEqual(c.verdict, "DROPPED_CAUSE_UNCONFIRMED")            # a symptom, not a code-defect verdict
        self.assertEqual([k for _, k, _, _ in c.issues], ["HYPOTHESIS"])
        self.assertIn("TouOnboardingEmailEventProcessor:processGenericEventData:193", c.issues[0][3])

    def test_emailer_errors_of_other_events_are_not_blamed_on_onboarding(self):
        # the user's emailer lines are USER_WELCOME sends failing on invalid_token, not TOU_ONBOARDING
        c = te.classify(onboarding_raw(), U, "TOU_ONBOARDING", "ELECTRIC", DAY, mds_tou.KNOWN_ISSUES, solar_at_event=True)
        self.assertIsNone(c.store_failure)
        self.assertIsNone(c.provider_accepted)
        self.assertEqual(c.sends, [])
        self.assertEqual(c.bound.get("emailer", []), [])
        self.assertFalse(any(k == "ENV" for _, k, _, _ in c.issues))

    def test_solar_state_never_selects_a_cause_by_itself(self):
        """Review 2026-10-06 R3: solar / non-solar / unknown all yield the same symptom with every candidate listed."""
        for solar in (True, False, None):
            c = te.classify(onboarding_raw(), U, "TOU_ONBOARDING", "ELECTRIC", DAY, mds_tou.KNOWN_ISSUES, solar_at_event=solar)
            self.assertEqual([k for _, k, _, _ in c.issues], ["HYPOTHESIS"], solar)
            what = c.issues[0][0]
            self.assertIn("solar eligibility gate", what)
            self.assertIn("rateStructureImageUrl", what)
            self.assertEqual(c.verdict, "DROPPED_CAUSE_UNCONFIRMED")

    def test_no_table_entry_concludes_a_code_defect_from_a_generic_signature(self):
        for rx, what, kind, fix, events in mds_tou.KNOWN_ISSUES:
            self.assertNotIn(kind, ("TICKET", "SQL"), what)

    def test_time_window_binds_evidence(self):
        before = (te.ts_of("2026-10-05 09:00:00,000"), te.ts_of("2026-10-05 09:30:00,000"))
        c = te.classify(onboarding_raw(), U, "TOU_ONBOARDING", "ELECTRIC", before, mds_tou.KNOWN_ISSUES, True)
        self.assertIsNone(c.published)
        self.assertIsNone(c.processed)
        self.assertEqual(c.logs["notifications-priority"], "empty_in_window")
        self.assertEqual(c.verdict, "NO_EVIDENCE_IN_WINDOW")

    def test_other_users_lines_are_ignored(self):
        c = te.classify(onboarding_raw(), "00000000-0000-0000-0000-000000000000", "TOU_ONBOARDING", "ELECTRIC", DAY,
                        mds_tou.KNOWN_ISSUES, True)
        self.assertIsNone(c.published)
        self.assertIsNone(c.processed)

    def test_missing_logs_are_reported_as_missing(self):
        c = te.classify({"ingestion": None, "notifications-priority": None, "emailer": None}, U, "TOU_ONBOARDING",
                        "ELECTRIC", DAY, mds_tou.KNOWN_ISSUES, True)
        self.assertEqual(set(c.logs.values()), {"missing"})
        self.assertEqual(c.verdict, "LOGS_MISSING")


class DeliveryChainTest(unittest.TestCase):
    SENT = HERE / "fixtures" / "emailer_user_welcome_sent.log"
    U2 = "9b8b87c6-831c-44a1-9963-064221c9fab4"

    def test_provider_acceptance_is_the_furthest_stage(self):
        c = te.classify({"emailer": lines(self.SENT)}, self.U2, "USER_WELCOME", "ELECTRIC", DAY, mds_tou.KNOWN_ISSUES)
        self.assertIn("Successfully injected email to sparkpost", c.provider_accepted)
        self.assertIsNone(c.store_failure)
        # no stored rows supplied: the send path log line is NOT storage proof
        self.assertEqual(c.storage, "unverified")
        self.assertIsNone(c.stored)
        self.assertEqual(c.verdict, "ACCEPTED_BY_PROVIDER")
        self.assertTrue(any("inbox receipt is never inferred" in ln for ln in te.report(c)))

    def test_a_different_event_of_the_same_user_is_not_accepted_evidence(self):
        c = te.classify({"emailer": lines(self.SENT)}, self.U2, "TOU_ONBOARDING", "ELECTRIC", DAY, mds_tou.KNOWN_ISSUES)
        self.assertIsNone(c.provider_accepted)

    def test_store_failure_is_a_delivery_failure(self):
        blk = [ln.replace("Time for fetching configs", "x") for ln in lines(self.SENT)
               if "SparkPostEmailSender" not in ln and "successfully sent" not in ln]
        i = next(i for i, ln in enumerate(blk) if "sending #1 Mail" in ln)
        blk.insert(i + 1, blk[i].replace("Emailer:saveAndSendEmailToEndUsers:773", "Emailer:saveAndSendEmailToEndUsers:794")
                   .replace("sending #1 Mail for the user", "Unable to store notification data for user"))
        c = te.classify({"emailer": blk}, self.U2, "USER_WELCOME", "ELECTRIC", DAY, mds_tou.KNOWN_ISSUES)
        self.assertEqual(c.verdict, "DELIVERY_FAILURE")



def attempt_block(event, fuel, thread, hour, outcome):
    """One emailer send attempt built from the real USER_WELCOME send (fixture) by swapping event, fuel, thread and
    hour. outcome: accepted | store_failure."""
    src = [ln for ln in lines(DeliveryChainTest.SENT) if "pool-9-thread-1" in ln]
    out = []
    for ln in src:
        ln = ln.replace("USER_WELCOME", event).replace("pool-9-thread-1", thread)
        ln = ln.replace("measurement type: null", f"measurement type: {fuel}").replace(
            "measurementType: null", f"measurementType: {fuel}")
        ln = ln.replace("2026-10-05 10:", f"2026-10-05 {hour:02d}:")
        if outcome == "store_failure" and ("SparkPostEmailSender" in ln or "successfully sent" in ln):
            continue
        out.append(ln)
    if outcome == "store_failure":
        i = next(i for i, ln in enumerate(out) if "sending #1 Mail" in ln)
        out.insert(i + 1, out[i].replace("Emailer:saveAndSendEmailToEndUsers:773", "Emailer:saveAndSendEmailToEndUsers:794")
                   .replace("sending #1 Mail for the user", "Unable to store notification data for user"))
    return out


class FuelAndRepeatTest(unittest.TestCase):
    U2 = DeliveryChainTest.U2

    def test_fixture_block_names_no_fuel_so_fuel_is_unverified(self):
        c = te.classify({"emailer": lines(DeliveryChainTest.SENT)}, self.U2, "USER_WELCOME", "ELECTRIC", DAY,
                        mds_tou.KNOWN_ISSUES)
        self.assertEqual({a.fuel for a in c.sends}, {"unverified"})

    def test_mixed_fuel_logs_only_count_the_requested_fuel(self):
        raw = {"emailer": attempt_block("MONTHLY_SUMMARY", "GAS", "pool-31-thread-1", 11, "accepted")
               + attempt_block("MONTHLY_SUMMARY", "ELECTRIC", "pool-32-thread-1", 12, "store_failure")}
        elec = te.classify(raw, self.U2, "MONTHLY_SUMMARY", "ELECTRIC", DAY, mds_tou.KNOWN_ISSUES)
        self.assertEqual(elec.verdict, "DELIVERY_FAILURE")          # the GAS acceptance is not ELECTRIC evidence
        self.assertEqual([a.fuel for a in elec.sends], ["verified"])
        self.assertEqual(len(elec.excluded_other_fuel), 1)
        gas = te.classify(raw, self.U2, "MONTHLY_SUMMARY", "GAS", DAY, mds_tou.KNOWN_ISSUES)
        self.assertEqual(gas.verdict, "ACCEPTED_BY_PROVIDER")

    def test_recovered_attempt_after_earlier_failures(self):
        raw = {"emailer": attempt_block("TOU_ONBOARDING", "ELECTRIC", "pool-41-thread-1", 9, "store_failure")
               + attempt_block("TOU_ONBOARDING", "ELECTRIC", "pool-42-thread-1", 10, "store_failure")
               + attempt_block("TOU_ONBOARDING", "ELECTRIC", "pool-43-thread-1", 11, "accepted")}
        c = te.classify(raw, self.U2, "TOU_ONBOARDING", "ELECTRIC", DAY, mds_tou.KNOWN_ISSUES)
        self.assertEqual(c.verdict, "ACCEPTED_BY_PROVIDER")
        self.assertEqual([a.outcome for a in c.recovered_from], ["store_failure", "store_failure"])
        self.assertTrue(any("recovered after" in ln for ln in te.report(c)))

    def test_later_failure_after_an_earlier_acceptance_is_not_a_success(self):
        raw = {"emailer": attempt_block("TOU_ONBOARDING", "ELECTRIC", "pool-41-thread-1", 9, "accepted")
               + attempt_block("TOU_ONBOARDING", "ELECTRIC", "pool-42-thread-1", 11, "store_failure")}
        c = te.classify(raw, self.U2, "TOU_ONBOARDING", "ELECTRIC", DAY, mds_tou.KNOWN_ISSUES)
        self.assertEqual(c.verdict, "DELIVERY_FAILURE")
        self.assertEqual(c.recovered_from, [])

    def test_storage_needs_a_stored_row_of_this_event_fuel_and_window(self):
        raw = {"emailer": attempt_block("TOU_ONBOARDING", "ELECTRIC", "pool-43-thread-1", 11, "accepted")}
        gen = int(te.ts_of("2026-10-05 11:29:51,000") * 1000)
        rows = [{"notificationType": "TOU_ONBOARDING", "deliveryMode": "Email", "generationTimestamp": gen,
                 "notificationId": "n-1"},
                {"notificationType": "USER_WELCOME", "deliveryMode": "Email", "generationTimestamp": gen,
                 "notificationId": "n-2"}]
        ok = te.classify(raw, self.U2, "TOU_ONBOARDING", "ELECTRIC", DAY, mds_tou.KNOWN_ISSUES, stored_rows=rows)
        self.assertEqual((ok.storage, ok.stored.split()[0]), ("verified", "notificationId=n-1"))
        other = te.classify(raw, self.U2, "TOU_ONBOARDING", "ELECTRIC", DAY, mds_tou.KNOWN_ISSUES, stored_rows=rows[1:])
        self.assertEqual(other.storage, "absent")
        gas_row = [dict(rows[0], measurementType="GAS")]
        wrong_fuel = te.classify(raw, self.U2, "TOU_ONBOARDING", "ELECTRIC", DAY, mds_tou.KNOWN_ISSUES, stored_rows=gas_row)
        self.assertEqual(wrong_fuel.storage, "absent")
        late = (te.ts_of("2026-10-05 12:00:00,000"), te.ts_of("2026-10-05 13:00:00,000"))
        outside = te.classify(raw, self.U2, "TOU_ONBOARDING", "ELECTRIC", late, mds_tou.KNOWN_ISSUES, stored_rows=rows)
        self.assertEqual(outside.storage, "absent")

    def test_repeated_onboarding_processing_latest_decides(self):
        blk = lines(ONB / "notifications-priority.log")
        later = [ln.replace("2026-10-05 09:32:", "2026-10-05 11:32:").replace("pool-12-thread-1", "pool-13-thread-1")
                 .replace("tou rate structure data not available for this user", "processed for this user") for ln in blk]
        later = [ln for ln in later if "processed for this user" not in ln]
        c = te.classify({"ingestion": lines(ONB / "ingestion.log"), "notifications-priority": blk + later,
                         "emailer": []}, U, "TOU_ONBOARDING", "ELECTRIC", DAY, mds_tou.KNOWN_ISSUES, True)
        self.assertEqual([a.outcome for a in sorted(c.processing, key=lambda a: a.start)], ["dropped", "no_outcome"])
        self.assertEqual(c.verdict, "PROCESSED_NO_OUTCOME_SEEN")   # the earlier drop is history, not the verdict
        self.assertEqual(c.issues, [])


if __name__ == "__main__":
    unittest.main()
