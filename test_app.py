import tempfile
import unittest
from pathlib import Path

from app import BusinessError, ReviewStore


class ReviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _paper(self):
        return self.store.submit_paper("alice", "可靠分布式提交协议", "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。")["id"]

    def _completed_review(self, paper_id, reviewer, score=4):
        assignment_id = self.store.assign("chair", paper_id, reviewer)["id"]
        self.store.respond_assignment(reviewer, assignment_id, True)
        self.store.submit_review(reviewer, assignment_id, score, "内容扎实的评审意见，覆盖方法、实验与写作。")
        return assignment_id

    def _assignment_status(self, assignment_id):
        with self.store.connect() as conn:
            return conn.execute("SELECT status FROM assignments WHERE id=?", (assignment_id,)).fetchone()["status"]

    def test_complete_flow_and_double_blind_view(self):
        paper_id = self._paper()
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        a2 = self.store.assign("chair", paper_id, "r2")["id"]
        self.store.respond_assignment("r1", a1, True)
        self.store.respond_assignment("r2", a2, True)
        self.store.submit_review("r1", a1, 4, "方法严谨，缺少与最近工作的对比。")
        self.store.submit_review("r2", a2, 3, "实验充分，但部分结论需要进一步解释。")
        self.store.submit_rebuttal("alice", paper_id, "感谢意见，我们将补充对比并解释实验结论。")
        result = self.store.decide("chair", paper_id, "minor_revision", "补充实验后接收。")
        self.assertEqual(result["decision"], "minor_revision")
        self.assertIsNone(self.store.get_paper("r1", paper_id)["author_id"])
        self.assertIsNotNone(self.store.get_paper("chair", paper_id)["author_id"])
        history = self.store.history("chair", paper_id)
        self.assertEqual(history[-1]["action"], "decision.record")
        self.assertGreaterEqual(len(history), 8)

    def test_conflict_blocks_assignment_and_role_is_enforced(self):
        paper_id = self._paper()
        self.store.add_conflict("chair", paper_id, "r1", "同一导师团队成员")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", paper_id, "r1")
        self.assertEqual(ctx.exception.code, "conflict_of_interest")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("alice", paper_id, "r2")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_paper("r2", paper_id)
        self.assertEqual(ctx.exception.status, 403)

    def test_withdraw_conflict_releases_reviewer(self):
        paper_id = self._paper()
        self.store.add_conflict("chair", paper_id, "r1", "同名作者误登记")
        with self.assertRaises(BusinessError):
            self.store.bid("r1", paper_id, "want")
        result = self.store.withdraw_conflict("chair", paper_id, "r1", "经核实为同名误登记")
        self.assertEqual(result["status"], "withdrawn")
        self.assertEqual(result["withdrawn_by"], "chair")
        self.store.bid("r1", paper_id, "want")  # 撤回后可重新表达意愿
        self.assertEqual(self.store.assign("chair", paper_id, "r1")["status"], "invited")

    def test_withdraw_requires_note_active_record_and_chair(self):
        paper_id = self._paper()
        with self.assertRaises(BusinessError) as ctx:
            self.store.withdraw_conflict("chair", paper_id, "r1", "说明")
        self.assertEqual(ctx.exception.status, 404)
        self.store.add_conflict("chair", paper_id, "r1", "合作项目")
        with self.assertRaises(BusinessError) as ctx:
            self.store.withdraw_conflict("chair", paper_id, "r1", "   ")
        self.assertEqual(ctx.exception.code, "invalid_note")
        with self.assertRaises(BusinessError) as ctx:
            self.store.withdraw_conflict("alice", paper_id, "r1", "非主席操作")
        self.assertEqual(ctx.exception.status, 403)
        self.store.withdraw_conflict("chair", paper_id, "r1", "误登记，已澄清")
        with self.assertRaises(BusinessError) as ctx:
            self.store.withdraw_conflict("chair", paper_id, "r1", "重复撤回")
        self.assertEqual(ctx.exception.code, "conflict_already_withdrawn")

    def test_withdraw_cancels_pending_invitation(self):
        paper_id = self._paper()
        assignment_id = self.store.assign("chair", paper_id, "r1")["id"]
        self.store.add_conflict("chair", paper_id, "r1", "邀请后补登的冲突")
        result = self.store.withdraw_conflict("chair", paper_id, "r1", "误登记，已澄清")
        self.assertEqual(result["cancelled_assignment_ids"], [assignment_id])
        self.assertEqual(self._assignment_status(assignment_id), "cancelled")
        with self.assertRaises(BusinessError) as ctx:
            self.store.respond_assignment("r1", assignment_id, True)
        self.assertEqual(ctx.exception.code, "invitation_already_answered")
        actions = [h["action"] for h in self.store.history("chair", paper_id)]
        self.assertIn("assignment.cancel", actions)

    def test_withdraw_voids_completed_review_and_requires_rebid(self):
        paper_id = self._paper()
        old_assignment = self._completed_review(paper_id, "r1", 5)
        self._completed_review(paper_id, "r2", 4)
        self.store.add_conflict("chair", paper_id, "r1", "误登记")
        result = self.store.withdraw_conflict("chair", paper_id, "r1", "澄清后撤回")
        self.assertEqual(result["voided_assignment_ids"], [old_assignment])
        self.assertEqual(self._assignment_status(old_assignment), "voided")
        # 已作废评审不计入决定 quorum：只剩 r2 一份有效评审。
        with self.assertRaises(BusinessError) as ctx:
            self.store.decide("chair", paper_id, "accept")
        self.assertEqual(ctx.exception.code, "insufficient_reviews")
        # 主席不能直接再次邀请：评审人需先重新表达意愿。
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", paper_id, "r1")
        self.assertEqual(ctx.exception.code, "rebid_required")
        self.store.bid("r1", paper_id, "want")
        new_assignment = self.store.assign("chair", paper_id, "r1")["id"]
        self.assertNotEqual(new_assignment, old_assignment)  # 新分配，旧记录不被补位
        self.store.respond_assignment("r1", new_assignment, True)
        self.store.submit_review("r1", new_assignment, 4, "重新评审：方法严谨，实验充分。")
        self.store.decide("chair", paper_id, "accept")
        # 时间线保留原评审与作废记录。
        actions = [h["action"] for h in self.store.history("chair", paper_id)]
        self.assertIn("review.submit", actions)
        self.assertIn("review.void", actions)

    def test_withdraw_on_decided_paper_only_records_disposition(self):
        paper_id = self._paper()
        assignment_id = self._completed_review(paper_id, "r1", 5)
        self._completed_review(paper_id, "r2", 4)
        self.store.decide("chair", paper_id, "accept", "达到接收标准")
        self.store.add_conflict("chair", paper_id, "r1", "决定后补登的冲突")
        result = self.store.withdraw_conflict("chair", paper_id, "r1", "误登记，仅记录处置")
        self.assertTrue(result["decision_unchanged"])
        self.assertEqual(result["cancelled_assignment_ids"], [])
        self.assertEqual(result["voided_assignment_ids"], [])
        # 结论与已完成评审均不受影响。
        self.assertEqual(self.store.get_paper("chair", paper_id)["status"], "decided")
        self.assertEqual(self._assignment_status(assignment_id), "completed")

    def test_conflict_archive_records_operators_and_reinstatement(self):
        paper_id = self._paper()
        self.store.add_conflict("chair", paper_id, "r1", "同一单位")
        self.store.withdraw_conflict("chair", paper_id, "r1", "核实后撤回")
        archive = self.store.list_conflicts("chair", paper_id)
        item = archive["items"][0]
        self.assertEqual(item["status"], "withdrawn")
        self.assertEqual(item["created_by"], "chair")
        self.assertEqual(item["withdrawal"]["note"], "核实后撤回")
        self.assertEqual(item["withdrawal"]["withdrawn_by"], "chair")
        self.assertEqual(len(archive["withdrawals"]), 1)
        with self.assertRaises(BusinessError) as ctx:
            self.store.list_conflicts("r1", paper_id)
        self.assertEqual(ctx.exception.status, 403)
        # 撤回后再次发现真实冲突可重新登记，历史撤回仍保留在档案中。
        self.store.add_conflict("chair", paper_id, "r1", "新发现的合作关系")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", paper_id, "r1")
        self.assertEqual(ctx.exception.code, "conflict_of_interest")
        archive = self.store.list_conflicts("chair", paper_id)
        self.assertEqual(archive["items"][0]["status"], "active")
        self.assertEqual(len(archive["withdrawals"]), 1)


if __name__ == "__main__":
    unittest.main()
