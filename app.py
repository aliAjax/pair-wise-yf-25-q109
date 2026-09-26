"""学术会议同行评审系统：标准库 + SQLite 的可运行示例。"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "review.db"
VALID_DECISIONS = {"accept", "reject", "minor_revision", "major_revision"}


class BusinessError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ReviewStore:
    """领域逻辑。每个公开方法使用独立连接，避免 HTTP 线程共享 SQLite 连接。"""

    def __init__(self, db_path: str | Path = DEFAULT_DB):
        self.db_path = str(db_path)
        self._schema_lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def init_schema(self) -> None:
        with self._schema_lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('author','reviewer','chair')),
                    load_limit INTEGER NOT NULL DEFAULT 3 CHECK (load_limit >= 0)
                );
                CREATE TABLE IF NOT EXISTS papers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    author_id TEXT NOT NULL REFERENCES users(id),
                    title TEXT NOT NULL,
                    abstract TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'submitted'
                        CHECK (status IN ('submitted','under_review','decided','withdrawn')),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER NOT NULL REFERENCES papers(id),
                    version INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (paper_id, version)
                );
                CREATE TABLE IF NOT EXISTS conflicts (
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    paper_id INTEGER NOT NULL REFERENCES papers(id),
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','withdrawn')),
                    created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (reviewer_id, paper_id)
                );
                CREATE TABLE IF NOT EXISTS conflict_withdrawals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    paper_id INTEGER NOT NULL REFERENCES papers(id),
                    reason TEXT NOT NULL,
                    conflict_created_by TEXT NOT NULL,
                    conflict_created_at TEXT NOT NULL,
                    note TEXT NOT NULL,
                    disposition TEXT NOT NULL DEFAULT '{}',
                    withdrawn_by TEXT NOT NULL REFERENCES users(id),
                    withdrawn_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bids (
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    paper_id INTEGER NOT NULL REFERENCES papers(id),
                    interest TEXT NOT NULL CHECK (interest IN ('want','maybe','decline')),
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (reviewer_id, paper_id)
                );
                CREATE TABLE IF NOT EXISTS assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER NOT NULL REFERENCES papers(id),
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    status TEXT NOT NULL DEFAULT 'invited'
                        CHECK (status IN ('invited','accepted','declined','completed','cancelled','voided')),
                    score INTEGER CHECK (score IS NULL OR score BETWEEN 1 AND 5),
                    review_text TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rebuttals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER NOT NULL UNIQUE REFERENCES papers(id),
                    author_id TEXT NOT NULL REFERENCES users(id),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER NOT NULL UNIQUE REFERENCES papers(id),
                    decision TEXT NOT NULL CHECK (decision IN ('accept','reject','minor_revision','major_revision')),
                    note TEXT NOT NULL DEFAULT '',
                    decided_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER,
                    actor_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (paper_id) REFERENCES papers(id)
                );
                """
            )
            self._migrate(conn)
            # 同一论文同一评审人仅允许一条进行中的分配；已取消/作废的历史记录不占名额。
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_assignments_live_pair
                   ON assignments(paper_id, reviewer_id)
                   WHERE status IN ('invited','accepted','declined','completed')"""
            )

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """旧版本数据库就地升级：conflicts 增加状态列，assignments 重建以支持取消/作废。"""
        conflict_cols = {row["name"] for row in conn.execute("PRAGMA table_info(conflicts)")}
        if "status" not in conflict_cols:
            conn.execute(
                "ALTER TABLE conflicts ADD COLUMN status TEXT NOT NULL DEFAULT 'active' "
                "CHECK (status IN ('active','withdrawn'))"
            )
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='assignments'"
        ).fetchone()
        if row and "'cancelled'" not in row["sql"]:
            conn.executescript(
                """
                CREATE TABLE assignments_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER NOT NULL REFERENCES papers(id),
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    status TEXT NOT NULL DEFAULT 'invited'
                        CHECK (status IN ('invited','accepted','declined','completed','cancelled','voided')),
                    score INTEGER CHECK (score IS NULL OR score BETWEEN 1 AND 5),
                    review_text TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO assignments_new SELECT * FROM assignments;
                DROP TABLE assignments;
                ALTER TABLE assignments_new RENAME TO assignments;
                """
            )

    def seed(self) -> None:
        self.init_schema()
        users = [
            ("alice", "Alice 作者", "author", 0),
            ("bob", "Bob 作者", "author", 0),
            ("r1", "评审人一号", "reviewer", 3),
            ("r2", "评审人二号", "reviewer", 3),
            ("r3", "评审人三号", "reviewer", 2),
            ("chair", "程序委员会主席", "chair", 0),
        ]
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name,role,load_limit) VALUES(?,?,?,?)", users
            )

    def _user(self, conn: sqlite3.Connection, user_id: str | None) -> sqlite3.Row:
        if not user_id:
            raise BusinessError("缺少 X-User-Id 请求头", 401, "authentication_required")
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise BusinessError("用户不存在", 401, "unknown_user")
        return row

    @staticmethod
    def _require(row: sqlite3.Row, role: str) -> None:
        if row["role"] != role:
            raise BusinessError(f"该操作仅允许 {role} 角色", 403, "forbidden")

    def _audit(self, conn: sqlite3.Connection, paper_id: int | None, actor: str, action: str, detail: dict) -> None:
        conn.execute(
            "INSERT INTO audit_log(paper_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (paper_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), utcnow()),
        )

    def submit_paper(self, user_id: str, title: str, abstract: str) -> dict:
        title, abstract = title.strip(), abstract.strip()
        if len(title) < 3 or len(abstract) < 20:
            raise BusinessError("标题至少 3 字，摘要至少 20 字", 422, "invalid_paper")
        digest = hashlib.sha256(f"{title}\n{abstract}".encode()).hexdigest()
        with self.connect() as conn:
            user = self._user(conn, user_id)
            self._require(user, "author")
            cur = conn.execute(
                "INSERT INTO papers(author_id,title,abstract,created_at) VALUES(?,?,?,?)",
                (user_id, title, abstract, utcnow()),
            )
            paper_id = cur.lastrowid
            conn.execute(
                "INSERT INTO paper_versions(paper_id,version,content_hash,created_at) VALUES(?,?,?,?)",
                (paper_id, 1, digest, utcnow()),
            )
            self._audit(conn, paper_id, user_id, "paper.submit", {"version": 1, "sha256": digest})
            return {"id": paper_id, "status": "submitted", "version": 1, "sha256": digest}

    def _paper_view(self, conn: sqlite3.Connection, paper: sqlite3.Row, viewer: sqlite3.Row) -> dict:
        data = {
            "id": paper["id"],
            "title": paper["title"],
            "abstract": paper["abstract"],
            "status": paper["status"],
            "created_at": paper["created_at"],
        }
        if viewer["role"] == "chair" or viewer["id"] == paper["author_id"]:
            data["author_id"] = paper["author_id"]
        else:
            data["author_id"] = None  # 双盲：评审人看不到作者身份。
        return data

    def list_papers(self, user_id: str) -> list[dict]:
        with self.connect() as conn:
            user = self._user(conn, user_id)
            if user["role"] == "chair":
                rows = conn.execute("SELECT * FROM papers ORDER BY id").fetchall()
            elif user["role"] == "author":
                rows = conn.execute("SELECT * FROM papers WHERE author_id=? ORDER BY id", (user_id,)).fetchall()
            else:
                rows = conn.execute(
                    """SELECT p.* FROM papers p
                       LEFT JOIN assignments a ON a.paper_id=p.id AND a.reviewer_id=?
                       LEFT JOIN bids b ON b.paper_id=p.id AND b.reviewer_id=?
                       WHERE a.id IS NOT NULL OR b.paper_id IS NOT NULL ORDER BY p.id""",
                    (user_id, user_id),
                ).fetchall()
            return [self._paper_view(conn, row, user) for row in rows]

    def get_paper(self, user_id: str, paper_id: int) -> dict:
        with self.connect() as conn:
            user = self._user(conn, user_id)
            paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper:
                raise BusinessError("论文不存在", 404, "not_found")
            if user["role"] == "reviewer":
                allowed = conn.execute(
                    "SELECT 1 FROM assignments WHERE paper_id=? AND reviewer_id=? UNION SELECT 1 FROM bids WHERE paper_id=? AND reviewer_id=?",
                    (paper_id, user_id, paper_id, user_id),
                ).fetchone()
                if not allowed:
                    raise BusinessError("评审人未获授权查看该论文", 403, "forbidden")
            elif user["role"] == "author" and paper["author_id"] != user_id:
                raise BusinessError("作者只能查看自己的论文", 403, "forbidden")
            return self._paper_view(conn, paper, user)

    def add_conflict(self, chair_id: str, paper_id: int, reviewer_id: str, reason: str) -> dict:
        if not reason.strip():
            raise BusinessError("利益冲突原因不能为空", 422, "invalid_reason")
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            if not conn.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone():
                raise BusinessError("论文不存在", 404, "not_found")
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            existing = conn.execute(
                "SELECT status FROM conflicts WHERE reviewer_id=? AND paper_id=?", (reviewer_id, paper_id)
            ).fetchone()
            if existing and existing["status"] == "active":
                raise BusinessError("利益冲突已登记", 409, "conflict_exists")
            if existing:
                # 已撤回的记录可重新登记；历史撤回仍保留在 conflict_withdrawals 档案中。
                conn.execute(
                    "UPDATE conflicts SET reason=?,status='active',created_by=?,created_at=? WHERE reviewer_id=? AND paper_id=?",
                    (reason.strip(), chair_id, utcnow(), reviewer_id, paper_id),
                )
                reinstated = True
            else:
                conn.execute(
                    "INSERT INTO conflicts(reviewer_id,paper_id,reason,created_by,created_at) VALUES(?,?,?,?,?)",
                    (reviewer_id, paper_id, reason.strip(), chair_id, utcnow()),
                )
                reinstated = False
            self._audit(conn, paper_id, chair_id, "conflict.add", {"reviewer_id": reviewer_id, "reason": reason.strip(), "reinstated": reinstated})
            return {"paper_id": paper_id, "reviewer_id": reviewer_id, "reason": reason.strip(), "reinstated": reinstated}

    def withdraw_conflict(self, chair_id: str, paper_id: int, reviewer_id: str, note: str) -> dict:
        """撤回误登记的利益冲突。具体规则见 README「冲突撤回规则」一节。"""
        note = note.strip()
        if not note:
            raise BusinessError("撤回说明不能为空", 422, "invalid_note")
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            try:
                conn.execute("BEGIN IMMEDIATE")
                paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                if not paper:
                    raise BusinessError("论文不存在", 404, "not_found")
                conflict = conn.execute(
                    "SELECT * FROM conflicts WHERE reviewer_id=? AND paper_id=?", (reviewer_id, paper_id)
                ).fetchone()
                if not conflict:
                    raise BusinessError("利益冲突不存在", 404, "not_found")
                if conflict["status"] == "withdrawn":
                    raise BusinessError("利益冲突已撤回，请勿重复操作", 409, "conflict_already_withdrawn")
                now = utcnow()
                cancelled: list[int] = []
                voided: list[int] = []
                if paper["status"] in {"submitted", "under_review"}:
                    # 未处理（未完成的）邀请同步取消。
                    pending = conn.execute(
                        "SELECT id,status FROM assignments WHERE paper_id=? AND reviewer_id=? AND status IN ('invited','accepted')",
                        (paper_id, reviewer_id),
                    ).fetchall()
                    for row in pending:
                        conn.execute("UPDATE assignments SET status='cancelled',updated_at=? WHERE id=?", (now, row["id"]))
                        cancelled.append(row["id"])
                        self._audit(conn, paper_id, chair_id, "assignment.cancel",
                                    {"assignment_id": row["id"], "reviewer_id": reviewer_id,
                                     "previous_status": row["status"], "cause": "conflict_withdrawn"})
                    # 已完成意见保留在时间线，但作废：不再计入决定，也不能用于补位。
                    done = conn.execute(
                        "SELECT id FROM assignments WHERE paper_id=? AND reviewer_id=? AND status='completed'",
                        (paper_id, reviewer_id),
                    ).fetchall()
                    for row in done:
                        conn.execute("UPDATE assignments SET status='voided',updated_at=? WHERE id=?", (now, row["id"]))
                        voided.append(row["id"])
                        self._audit(conn, paper_id, chair_id, "review.void",
                                    {"assignment_id": row["id"], "reviewer_id": reviewer_id, "cause": "conflict_withdrawn"})
                # 论文已决定或已撤稿时只记录处置，不改变结论，也不改动任何分配。
                disposition = {
                    "paper_status": paper["status"],
                    "cancelled_assignment_ids": cancelled,
                    "voided_assignment_ids": voided,
                    "decision_unchanged": paper["status"] not in {"submitted", "under_review"},
                }
                conn.execute(
                    "UPDATE conflicts SET status='withdrawn' WHERE reviewer_id=? AND paper_id=?",
                    (reviewer_id, paper_id),
                )
                conn.execute(
                    """INSERT INTO conflict_withdrawals(reviewer_id,paper_id,reason,conflict_created_by,conflict_created_at,note,disposition,withdrawn_by,withdrawn_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (reviewer_id, paper_id, conflict["reason"], conflict["created_by"], conflict["created_at"],
                     note, json.dumps(disposition, ensure_ascii=False, sort_keys=True), chair_id, now),
                )
                self._audit(conn, paper_id, chair_id, "conflict.withdraw",
                            {"reviewer_id": reviewer_id, "note": note, **disposition})
                return {"paper_id": paper_id, "reviewer_id": reviewer_id, "status": "withdrawn",
                        "note": note, "withdrawn_by": chair_id, "withdrawn_at": now, **disposition}
            except Exception:
                conn.rollback()
                raise

    def list_conflicts(self, chair_id: str, paper_id: int) -> dict:
        """冲突档案：当前冲突记录（含已撤回）与全部撤回档案，仅主席可见。"""
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            if not conn.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone():
                raise BusinessError("论文不存在", 404, "not_found")
            withdrawals = [
                {
                    "id": row["id"],
                    "reviewer_id": row["reviewer_id"],
                    "reason": row["reason"],
                    "conflict_created_by": row["conflict_created_by"],
                    "conflict_created_at": row["conflict_created_at"],
                    "note": row["note"],
                    "disposition": json.loads(row["disposition"]),
                    "withdrawn_by": row["withdrawn_by"],
                    "withdrawn_at": row["withdrawn_at"],
                }
                for row in conn.execute(
                    "SELECT * FROM conflict_withdrawals WHERE paper_id=? ORDER BY id", (paper_id,)
                ).fetchall()
            ]
            latest = {w["reviewer_id"]: w for w in withdrawals}
            items = []
            for row in conn.execute(
                "SELECT * FROM conflicts WHERE paper_id=? ORDER BY created_at, reviewer_id", (paper_id,)
            ).fetchall():
                items.append({
                    "reviewer_id": row["reviewer_id"],
                    "paper_id": row["paper_id"],
                    "reason": row["reason"],
                    "status": row["status"],
                    "created_by": row["created_by"],
                    "created_at": row["created_at"],
                    "withdrawal": latest.get(row["reviewer_id"]) if row["status"] == "withdrawn" else None,
                })
            return {"items": items, "withdrawals": withdrawals}

    def bid(self, reviewer_id: str, paper_id: int, interest: str, note: str = "") -> dict:
        if interest not in {"want", "maybe", "decline"}:
            raise BusinessError("意向必须为 want、maybe 或 decline", 422, "invalid_interest")
        with self.connect() as conn:
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            paper = conn.execute("SELECT status FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper or paper["status"] not in {"submitted", "under_review"}:
                raise BusinessError("论文不存在或当前不可表达意向", 409, "paper_unavailable")
            if conn.execute("SELECT 1 FROM conflicts WHERE reviewer_id=? AND paper_id=? AND status='active'", (reviewer_id, paper_id)).fetchone():
                raise BusinessError("存在利益冲突，不能表达评审意向", 409, "conflict_of_interest")
            conn.execute(
                """INSERT INTO bids(reviewer_id,paper_id,interest,note,created_at) VALUES(?,?,?,?,?)
                   ON CONFLICT(reviewer_id,paper_id) DO UPDATE SET interest=excluded.interest,note=excluded.note,created_at=excluded.created_at""",
                (reviewer_id, paper_id, interest, note.strip(), utcnow()),
            )
            self._audit(conn, paper_id, reviewer_id, "bid.set", {"interest": interest, "note": note.strip()})
            return {"paper_id": paper_id, "reviewer_id": reviewer_id, "interest": interest}

    def assign(self, chair_id: str, paper_id: int, reviewer_id: str) -> dict:
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            try:
                conn.execute("BEGIN IMMEDIATE")
                paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                if not paper or paper["status"] not in {"submitted", "under_review"}:
                    raise BusinessError("论文不存在或不可分配", 409, "paper_unavailable")
                reviewer = self._user(conn, reviewer_id)
                self._require(reviewer, "reviewer")
                if conn.execute("SELECT 1 FROM conflicts WHERE reviewer_id=? AND paper_id=? AND status='active'", (reviewer_id, paper_id)).fetchone():
                    raise BusinessError("评审人与论文存在利益冲突", 409, "conflict_of_interest")
                # 冲突曾被撤回的，评审人必须在撤回之后重新表达意愿，主席才可再次邀请。
                last_withdrawn_at = conn.execute(
                    "SELECT MAX(withdrawn_at) FROM conflict_withdrawals WHERE reviewer_id=? AND paper_id=?",
                    (reviewer_id, paper_id),
                ).fetchone()[0]
                if last_withdrawn_at and not conn.execute(
                    "SELECT 1 FROM bids WHERE reviewer_id=? AND paper_id=? AND interest IN ('want','maybe') AND created_at>=?",
                    (reviewer_id, paper_id, last_withdrawn_at),
                ).fetchone():
                    raise BusinessError("冲突撤回后，评审人需重新表达评审意愿才能再次邀请", 409, "rebid_required")
                load = conn.execute(
                    "SELECT COUNT(*) FROM assignments WHERE reviewer_id=? AND status IN ('invited','accepted')",
                    (reviewer_id,),
                ).fetchone()[0]
                if load >= reviewer["load_limit"]:
                    raise BusinessError("评审人已达到负载上限", 409, "reviewer_at_capacity")
                try:
                    cur = conn.execute(
                        "INSERT INTO assignments(paper_id,reviewer_id,created_at,updated_at) VALUES(?,?,?,?)",
                        (paper_id, reviewer_id, utcnow(), utcnow()),
                    )
                except sqlite3.IntegrityError:
                    raise BusinessError("该评审人已被分配此论文", 409, "assignment_exists")
                conn.execute("UPDATE papers SET status='under_review' WHERE id=?", (paper_id,))
                assignment_id = cur.lastrowid
                self._audit(conn, paper_id, chair_id, "assignment.invite", {"assignment_id": assignment_id, "reviewer_id": reviewer_id})
                return {"id": assignment_id, "paper_id": paper_id, "reviewer_id": reviewer_id, "status": "invited"}
            except Exception:
                conn.rollback()
                raise

    def respond_assignment(self, reviewer_id: str, assignment_id: int, accepted: bool) -> dict:
        with self.connect() as conn:
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            row = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if not row or row["reviewer_id"] != reviewer_id:
                raise BusinessError("分配不存在或不属于当前评审人", 404, "not_found")
            if row["status"] != "invited":
                raise BusinessError("邀请已经处理", 409, "invitation_already_answered")
            status = "accepted" if accepted else "declined"
            conn.execute("UPDATE assignments SET status=?,updated_at=? WHERE id=?", (status, utcnow(), assignment_id))
            self._audit(conn, row["paper_id"], reviewer_id, "assignment.respond", {"assignment_id": assignment_id, "status": status})
            return {"id": assignment_id, "status": status}

    def submit_review(self, reviewer_id: str, assignment_id: int, score: int, text: str) -> dict:
        if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
            raise BusinessError("评分必须是 1 到 5 的整数", 422, "invalid_score")
        if len(text.strip()) < 10:
            raise BusinessError("评审意见至少 10 字", 422, "review_too_short")
        with self.connect() as conn:
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            row = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if not row or row["reviewer_id"] != reviewer_id:
                raise BusinessError("分配不存在或不属于当前评审人", 404, "not_found")
            if row["status"] != "accepted":
                raise BusinessError("只有已接受邀请的评审人可以提交评审", 409, "invalid_assignment_state")
            conn.execute(
                "UPDATE assignments SET status='completed',score=?,review_text=?,updated_at=? WHERE id=?",
                (score, text.strip(), utcnow(), assignment_id),
            )
            self._audit(conn, row["paper_id"], reviewer_id, "review.submit", {"assignment_id": assignment_id, "score": score})
            return {"id": assignment_id, "status": "completed", "score": score}

    def submit_rebuttal(self, author_id: str, paper_id: int, content: str) -> dict:
        if len(content.strip()) < 10:
            raise BusinessError("Rebuttal 至少 10 字", 422, "rebuttal_too_short")
        with self.connect() as conn:
            author = self._user(conn, author_id)
            self._require(author, "author")
            paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper or paper["author_id"] != author_id:
                raise BusinessError("论文不存在或不属于当前作者", 404, "not_found")
            completed = conn.execute("SELECT COUNT(*) FROM assignments WHERE paper_id=? AND status='completed'", (paper_id,)).fetchone()[0]
            if completed < 1:
                raise BusinessError("至少收到一份完整评审后才能提交 Rebuttal", 409, "reviews_not_ready")
            try:
                cur = conn.execute(
                    "INSERT INTO rebuttals(paper_id,author_id,content,created_at) VALUES(?,?,?,?)",
                    (paper_id, author_id, content.strip(), utcnow()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("每篇论文只能提交一次 Rebuttal", 409, "rebuttal_exists")
            self._audit(conn, paper_id, author_id, "rebuttal.submit", {"rebuttal_id": cur.lastrowid})
            return {"id": cur.lastrowid, "paper_id": paper_id, "content": content.strip()}

    def decide(self, chair_id: str, paper_id: int, decision: str, note: str = "") -> dict:
        if decision not in VALID_DECISIONS:
            raise BusinessError("决定值不合法", 422, "invalid_decision")
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            try:
                conn.execute("BEGIN IMMEDIATE")
                paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                if not paper or paper["status"] not in {"submitted", "under_review"}:
                    raise BusinessError("论文不存在或已经决定", 409, "paper_decided")
                completed = conn.execute("SELECT COUNT(*) FROM assignments WHERE paper_id=? AND status='completed'", (paper_id,)).fetchone()[0]
                if completed < 2:
                    raise BusinessError("至少需要两份已完成评审才能作出决定", 409, "insufficient_reviews")
                cur = conn.execute(
                    "INSERT INTO decisions(paper_id,decision,note,decided_by,created_at) VALUES(?,?,?,?,?)",
                    (paper_id, decision, note.strip(), chair_id, utcnow()),
                )
                conn.execute("UPDATE papers SET status='decided' WHERE id=?", (paper_id,))
                self._audit(conn, paper_id, chair_id, "decision.record", {"decision": decision, "note": note.strip()})
                return {"id": cur.lastrowid, "paper_id": paper_id, "decision": decision, "note": note.strip()}
            except Exception:
                conn.rollback()
                raise

    def history(self, user_id: str, paper_id: int) -> list[dict]:
        self.get_paper(user_id, paper_id)  # 权限检查。
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM audit_log WHERE paper_id=? ORDER BY id", (paper_id,)).fetchall()
            return [dict(row) | {"detail": json.loads(row["detail"])} for row in rows]


class ReviewHandler(BaseHTTPRequestHandler):
    server_version = "AcademicReview/1.0"

    def _store(self) -> ReviewStore:
        return self.server.store  # type: ignore[attr-defined]

    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise BusinessError("请求体必须是合法 JSON", 400, "invalid_json")

    def _user_id(self) -> str:
        return self.headers.get("X-User-Id", "")

    def _serve_html(self, name: str) -> None:
        html = (BASE_DIR / "web" / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if method == "GET" and path == "/":
            return self._serve_html("index.html")
        if method == "GET" and path == "/conflicts":
            return self._serve_html("conflicts.html")
        if method == "GET" and path == "/health":
            return self._send(200, {"ok": True})
        store = self._store()
        parts = [p for p in path.split("/") if p]
        if not parts or parts[0] != "api":
            raise BusinessError("接口不存在", 404, "not_found")
        if parts == ["api", "papers"] and method == "GET":
            return self._send(200, {"items": store.list_papers(self._user_id())})
        if parts == ["api", "papers"] and method == "POST":
            data = self._body()
            return self._send(201, store.submit_paper(self._user_id(), data.get("title", ""), data.get("abstract", "")))
        if len(parts) >= 3 and parts[:2] == ["api", "papers"]:
            paper_id = int(parts[2])
            if len(parts) == 3 and method == "GET":
                return self._send(200, store.get_paper(self._user_id(), paper_id))
            if len(parts) == 4 and parts[3] == "bids" and method == "POST":
                data = self._body()
                return self._send(201, store.bid(self._user_id(), paper_id, data.get("interest", ""), data.get("note", "")))
            if len(parts) == 4 and parts[3] == "conflicts" and method == "GET":
                return self._send(200, store.list_conflicts(self._user_id(), paper_id))
            if len(parts) == 4 and parts[3] == "conflicts" and method == "POST":
                data = self._body()
                return self._send(201, store.add_conflict(self._user_id(), paper_id, data.get("reviewer_id", ""), data.get("reason", "")))
            if len(parts) == 5 and parts[3] == "conflicts" and parts[4] == "withdraw" and method == "POST":
                data = self._body()
                return self._send(200, store.withdraw_conflict(self._user_id(), paper_id, data.get("reviewer_id", ""), data.get("note", "")))
            if len(parts) == 4 and parts[3] == "assignments" and method == "POST":
                data = self._body()
                return self._send(201, store.assign(self._user_id(), paper_id, data.get("reviewer_id", "")))
            if len(parts) == 4 and parts[3] == "rebuttal" and method == "POST":
                data = self._body()
                return self._send(201, store.submit_rebuttal(self._user_id(), paper_id, data.get("content", "")))
            if len(parts) == 4 and parts[3] == "decision" and method == "POST":
                data = self._body()
                return self._send(201, store.decide(self._user_id(), paper_id, data.get("decision", ""), data.get("note", "")))
            if len(parts) == 4 and parts[3] == "history" and method == "GET":
                return self._send(200, {"items": store.history(self._user_id(), paper_id)})
        if len(parts) == 4 and parts[:2] == ["api", "assignments"] and method == "POST":
            assignment_id = int(parts[2])
            data = self._body()
            if parts[3] == "respond":
                return self._send(200, store.respond_assignment(self._user_id(), assignment_id, bool(data.get("accepted"))))
            if parts[3] == "review":
                return self._send(201, store.submit_review(self._user_id(), assignment_id, data.get("score"), data.get("text", "")))
        raise BusinessError("接口不存在", 404, "not_found")

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        try:
            self._dispatch(method)
        except BusinessError as exc:
            self._send(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except ValueError:
            self._send(400, {"error": {"code": "invalid_path", "message": "路径参数格式错误"}})
        except Exception as exc:
            self._send(500, {"error": {"code": "internal_error", "message": str(exc)}})

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store: ReviewStore):
        self.store = store
        super().__init__(address, ReviewHandler)


def parse_args():
    parser = argparse.ArgumentParser(description="学术会议同行评审系统")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--port", type=int, default=8101)
    parser.add_argument("--init", action="store_true", help="初始化数据库")
    parser.add_argument("--seed", action="store_true", help="写入演示用户")
    parser.add_argument("--no-init", action="store_true", help="启动时不自动初始化")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    store = ReviewStore(args.db)
    if args.init or args.seed or not args.no_init:
        store.init_schema()
    if args.seed:
        store.seed()
    if args.init or args.seed:
        print(f"数据库已初始化: {args.db}")
        return
    server = ReviewServer(("127.0.0.1", args.port), store)
    print(f"评审系统运行于 http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
