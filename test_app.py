import sqlite3
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

    def test_withdrawal_cancels_invitation_and_requires_rebid(self):
        paper_id = self._paper()
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        self.store.add_conflict("chair", paper_id, "r1", "误登记：同名同姓")
        result = self.store.withdraw_conflict("chair", paper_id, "r1", "已澄清并非同一人")
        self.assertEqual(result["status"], "withdrawn")
        self.assertEqual(result["withdrawn_by"], "chair")
        self.assertEqual(result["cancelled_assignment_ids"], [a1])
        self.assertEqual(result["excluded_assignment_ids"], [])
        # 未处理邀请已同步取消，不能再响应。
        with self.assertRaises(BusinessError) as ctx:
            self.store.respond_assignment("r1", a1, True)
        self.assertEqual(ctx.exception.code, "invitation_cancelled")
        # 撤回后须重新表达意愿，主席才可再次邀请。
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", paper_id, "r1")
        self.assertEqual(ctx.exception.code, "willingness_required")
        self.store.bid("r1", paper_id, "decline")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", paper_id, "r1")
        self.assertEqual(ctx.exception.code, "willingness_required")
        self.store.bid("r1", paper_id, "want")
        a2 = self.store.assign("chair", paper_id, "r1")["id"]
        self.assertNotEqual(a1, a2)
        # 评审人视图不因历史分配行而重复。
        self.assertEqual([p["id"] for p in self.store.list_papers("r1")].count(paper_id), 1)
        # 冲突档案保留原记录、说明与双方操作者和时间。
        archive = self.store.list_conflicts("chair", paper_id)
        self.assertEqual(len(archive), 1)
        record = archive[0]
        self.assertEqual(record["status"], "withdrawn")
        self.assertEqual(record["reason"], "误登记：同名同姓")
        self.assertEqual(record["created_by"], "chair")
        self.assertEqual(record["withdrawn_by"], "chair")
        self.assertEqual(record["withdraw_note"], "已澄清并非同一人")
        self.assertIsNotNone(record["created_at"])
        self.assertIsNotNone(record["withdrawn_at"])
        actions = [h["action"] for h in self.store.history("chair", paper_id)]
        self.assertIn("conflict.withdraw", actions)
        self.assertIn("assignment.cancel", actions)

    def test_withdrawal_excludes_completed_review_from_decision(self):
        paper_id = self._paper()
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        a2 = self.store.assign("chair", paper_id, "r2")["id"]
        self.store.respond_assignment("r1", a1, True)
        self.store.respond_assignment("r2", a2, True)
        self.store.submit_review("r1", a1, 5, "非常扎实的工作，建议直接接收。")
        self.store.submit_review("r2", a2, 2, "问题不少，需要大幅修改。")
        self.store.add_conflict("chair", paper_id, "r1", "误登记")
        result = self.store.withdraw_conflict("chair", paper_id, "r1", "澄清后撤回")
        self.assertEqual(result["excluded_assignment_ids"], [a1])
        # 已完成意见留在时间线，但不再计入补位与决定。
        with self.assertRaises(BusinessError) as ctx:
            self.store.decide("chair", paper_id, "accept")
        self.assertEqual(ctx.exception.code, "insufficient_reviews")
        actions = [h["action"] for h in self.store.history("chair", paper_id)]
        self.assertIn("review.submit", actions)
        self.assertIn("review.exclude", actions)
        # 重新表达意愿后可再邀请，新评审正常计入。
        self.store.bid("r1", paper_id, "want")
        a3 = self.store.assign("chair", paper_id, "r1")["id"]
        self.store.respond_assignment("r1", a3, True)
        self.store.submit_review("r1", a3, 4, "重新评审：工作整体可靠。")
        result = self.store.decide("chair", paper_id, "minor_revision")
        self.assertEqual(result["decision"], "minor_revision")

    def test_excluded_review_does_not_unlock_rebuttal(self):
        paper_id = self._paper()
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        self.store.respond_assignment("r1", a1, True)
        self.store.submit_review("r1", a1, 4, "不错的论文，建议小改后接收。")
        self.store.add_conflict("chair", paper_id, "r1", "误登记")
        self.store.withdraw_conflict("chair", paper_id, "r1", "澄清后撤回")
        # 唯一的已完成评审被排除，不能据此提交 Rebuttal。
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_rebuttal("alice", paper_id, "感谢评审意见，我们会认真修改。")
        self.assertEqual(ctx.exception.code, "reviews_not_ready")

    def test_withdrawal_after_decision_only_records(self):
        paper_id = self._paper()
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        a2 = self.store.assign("chair", paper_id, "r2")["id"]
        self.store.respond_assignment("r1", a1, True)
        self.store.respond_assignment("r2", a2, True)
        self.store.submit_review("r1", a1, 4, "方法严谨，实验充分。")
        self.store.submit_review("r2", a2, 3, "整体可行，部分细节待完善。")
        self.store.decide("chair", paper_id, "accept", "达到接收标准。")
        self.store.add_conflict("chair", paper_id, "r1", "决定后补登的误登记")
        result = self.store.withdraw_conflict("chair", paper_id, "r1", "澄清：误登记")
        # 只记录处置：不取消邀请、不排除评审、结论不变。
        self.assertEqual(result["cancelled_assignment_ids"], [])
        self.assertEqual(result["excluded_assignment_ids"], [])
        self.assertEqual(self.store.get_paper("chair", paper_id)["status"], "decided")
        actions = [h["action"] for h in self.store.history("chair", paper_id)]
        self.assertIn("conflict.withdraw", actions)
        self.assertNotIn("assignment.cancel", actions)
        self.assertNotIn("review.exclude", actions)

    def test_withdrawal_validation_and_reregister(self):
        paper_id = self._paper()
        with self.assertRaises(BusinessError) as ctx:
            self.store.withdraw_conflict("chair", paper_id, "r1", "  ")
        self.assertEqual(ctx.exception.code, "invalid_note")
        with self.assertRaises(BusinessError) as ctx:
            self.store.withdraw_conflict("chair", paper_id, "r1", "说明")
        self.assertEqual(ctx.exception.code, "not_found")
        self.store.add_conflict("chair", paper_id, "r1", "同事关系")
        with self.assertRaises(BusinessError) as ctx:
            self.store.withdraw_conflict("alice", paper_id, "r1", "非主席操作")
        self.assertEqual(ctx.exception.status, 403)
        self.store.withdraw_conflict("chair", paper_id, "r1", "说明")
        with self.assertRaises(BusinessError) as ctx:
            self.store.withdraw_conflict("chair", paper_id, "r1", "再次撤回")
        self.assertEqual(ctx.exception.code, "conflict_already_withdrawn")
        # 撤回后可重新登记并重新生效。
        self.store.add_conflict("chair", paper_id, "r1", "新发现的合作冲突")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", paper_id, "r1")
        self.assertEqual(ctx.exception.code, "conflict_of_interest")
        record = self.store.list_conflicts("chair", paper_id)[0]
        self.assertEqual(record["status"], "active")
        self.assertIsNone(record["withdrawn_by"])

    def test_migration_from_old_schema(self):
        db = Path(self.tmp.name) / "old.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('author','reviewer','chair')), load_limit INTEGER NOT NULL DEFAULT 3);
            CREATE TABLE papers (id INTEGER PRIMARY KEY AUTOINCREMENT, author_id TEXT NOT NULL REFERENCES users(id),
                title TEXT NOT NULL, abstract TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'submitted', created_at TEXT NOT NULL);
            CREATE TABLE conflicts (reviewer_id TEXT NOT NULL, paper_id INTEGER NOT NULL, reason TEXT NOT NULL,
                created_by TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (reviewer_id, paper_id));
            CREATE TABLE assignments (id INTEGER PRIMARY KEY AUTOINCREMENT, paper_id INTEGER NOT NULL, reviewer_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'invited' CHECK (status IN ('invited','accepted','declined','completed')),
                score INTEGER, review_text TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE (paper_id, reviewer_id));
            """
        )
        conn.execute("INSERT INTO users VALUES('chair','主席','chair',0)")
        conn.execute("INSERT INTO users VALUES('alice','作者','author',0)")
        conn.execute("INSERT INTO users VALUES('r1','评审','reviewer',3)")
        conn.execute("INSERT INTO papers(author_id,title,abstract,created_at) VALUES('alice','旧库论文','旧库迁移测试用论文摘要内容','2026-01-01T00:00:00+00:00')")
        conn.execute("INSERT INTO assignments(paper_id,reviewer_id,created_at,updated_at) VALUES(1,'r1','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')")
        conn.commit()
        conn.close()
        store = ReviewStore(db)
        store.init_schema()
        # 旧数据保留，撤回流程在迁移后的库上可用。
        store.add_conflict("chair", 1, "r1", "误登记")
        result = store.withdraw_conflict("chair", 1, "r1", "澄清")
        self.assertEqual(result["cancelled_assignment_ids"], [1])


if __name__ == "__main__":
    unittest.main()
