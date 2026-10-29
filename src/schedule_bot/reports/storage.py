"""SQLite report operations. All callers in asyncio use repository_async."""
from __future__ import annotations

import json
import secrets
from datetime import timedelta

from schedule_bot.reports.models import (
    CATEGORIES, CHOICE_PAGE_SIZE, CLOSED_DAYS, CONTEXT_DAYS, DAILY_LIMIT, DRAFT_HOURS, STATUSES,
    ReportError, admin_notification, canonical, digest, lesson_choices,
    message_chunks, normalize_description, timestamp,
)


def migrate_reports(connection) -> None:
    statements = (
        """CREATE TABLE report_contexts (
            id INTEGER PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
            source_kind TEXT NOT NULL CHECK(source_kind IN ('private','group')),
            owner_user_id INTEGER, source_chat_id INTEGER NOT NULL, source_message_id INTEGER,
            role TEXT NOT NULL CHECK(role IN ('student','teacher')), target TEXT NOT NULL,
            schedule_date TEXT NOT NULL, snapshot_json TEXT NOT NULL, snapshot_hash TEXT NOT NULL,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            CHECK((source_kind='private' AND owner_user_id IS NOT NULL) OR
                  (source_kind='group' AND owner_user_id IS NULL)))""",
        "CREATE INDEX idx_report_context_expiry ON report_contexts(expires_at)",
        """CREATE TABLE report_drafts (
            id TEXT PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE,
            context_id INTEGER NOT NULL REFERENCES report_contexts(id) ON DELETE CASCADE,
            selected_lesson_key TEXT, category TEXT, description TEXT,
            stage TEXT NOT NULL CHECK(stage IN ('lesson','category','description','confirm','submitted')),
            revision INTEGER NOT NULL DEFAULT 0, submitted_report_id INTEGER, lesson_page INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT NOT NULL)""",
        "CREATE INDEX idx_report_draft_expiry ON report_drafts(expires_at)",
        """CREATE TABLE reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id TEXT NOT NULL UNIQUE, user_id INTEGER NOT NULL,
            source_kind TEXT NOT NULL, source_chat_id INTEGER NOT NULL, source_message_id INTEGER, role TEXT NOT NULL,
            target TEXT NOT NULL, schedule_date TEXT NOT NULL, lesson_key TEXT NOT NULL,
            category TEXT NOT NULL CHECK(category IN ('room','teacher','subject','time','missing','other')),
            description TEXT NOT NULL, snapshot_json TEXT NOT NULL, snapshot_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('new','in_progress','resolved','rejected')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT)""",
        "CREATE INDEX idx_reports_user ON reports(user_id,created_at)",
        "CREATE INDEX idx_reports_status ON reports(status,closed_at)",
        """CREATE TABLE report_events (
            id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
            actor_id INTEGER NOT NULL, actor_role TEXT NOT NULL, event_type TEXT NOT NULL,
            text TEXT, operation_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)""",
        """CREATE TABLE report_outbox (
            id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
            event_id INTEGER REFERENCES report_events(id) ON DELETE CASCADE, destination_id INTEGER NOT NULL CHECK(destination_id>0),
            kind TEXT NOT NULL CHECK(kind IN ('admin','reply','closed')),
            payload TEXT NOT NULL, operation_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sending','sent','failed')),
            attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, next_attempt_at TEXT NOT NULL,
            delivered_at TEXT, telegram_message_id INTEGER, last_error TEXT,
            lease_token TEXT, lease_until TEXT)""",
        "CREATE INDEX idx_report_outbox_due ON report_outbox(status,next_attempt_at,lease_until,id)",
        """CREATE TABLE report_delivery_attempts (
            id INTEGER PRIMARY KEY, outbox_id INTEGER NOT NULL REFERENCES report_outbox(id) ON DELETE CASCADE,
            lease_token TEXT NOT NULL UNIQUE, outcome TEXT NOT NULL, error_category TEXT,
            telegram_message_id INTEGER, created_at TEXT NOT NULL, finished_at TEXT)""",
        """CREATE TABLE report_admin_drafts (
            id TEXT PRIMARY KEY, admin_id INTEGER NOT NULL UNIQUE,
            report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
            action TEXT NOT NULL CHECK(action IN ('reply','resolved','rejected')),
            text TEXT, stage TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL)""",
        "CREATE INDEX idx_report_admin_expiry ON report_admin_drafts(expires_at)",
    )
    for statement in statements:
        connection.execute(statement)


def _row(row):
    return dict(row) if row is not None else None


def _queue(connection, report_id, destination, kind, text, operation, now, event_id=None):
    chunks = message_chunks(text)
    for index, chunk in enumerate(chunks):
        connection.execute(
            "INSERT INTO report_outbox(report_id,event_id,destination_id,kind,payload,operation_key,created_at,next_attempt_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (report_id, event_id, destination, kind, canonical({"text": chunk, "buttons": kind == "admin" and index == len(chunks) - 1}),
             f"{operation}:{destination}:{index}", now, now),
        )


class ReportRepositoryMixin:
    def report_notification_snapshot(self, identifier):
        """Recover multipart v11 tails from the first stored part of that day."""
        with self._connect() as connection:
            current = connection.execute("SELECT * FROM notification_outbox WHERE id=?", (identifier,)).fetchone()
            if not current or not current["card_payload"]:
                return None
            header = json.loads(current["card_payload"])
            for row in connection.execute(
                "SELECT card_payload FROM notification_outbox WHERE import_id=? AND destination_id=? AND destination_kind=? "
                "AND chunk_index<=? AND card_payload IS NOT NULL ORDER BY chunk_index DESC",
                (current["import_id"], current["destination_id"], current["destination_kind"], current["chunk_index"]),
            ):
                payload = json.loads(row[0])
                if ("lessons" in payload and all(payload.get(key) == header.get(key) for key in ("date", "role", "target"))):
                    return payload
        return None

    def report_create_context(self, *, source_kind, owner_user_id, source_chat_id, snapshot, now=None):
        self.initialize()
        # Keep temporary tables bounded even if view traffic exceeds the daily
        # maintenance batch. Expired contexts have no in-flight delivery data.
        self.report_cleanup(now=now, limit=100, temporary_only=True)
        token = secrets.token_urlsafe(24)
        moment = timestamp(now)
        content = canonical(snapshot)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO report_contexts(token_hash,source_kind,owner_user_id,source_chat_id,role,target,schedule_date,"
                "snapshot_json,snapshot_hash,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (digest(token), source_kind, owner_user_id, source_chat_id, snapshot["role"], snapshot["target"],
                 snapshot["date"], content, digest(content), moment.isoformat(), (moment + timedelta(days=CONTEXT_DAYS)).isoformat()),
            )
            connection.commit()
        return token

    def report_bind_context(self, token, chat_id, message_id):
        with self._connect() as connection:
            connection.execute("UPDATE report_contexts SET source_message_id=? WHERE token_hash=? AND source_chat_id=? AND source_message_id IS NULL",
                               (message_id, digest(token), chat_id))
            connection.commit()

    def report_open_draft(self, token, user_id, *, now=None, source_chat_id=None, source_message_id=None, group_only=False):
        self.initialize()
        moment = timestamp(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            context = connection.execute("SELECT * FROM report_contexts WHERE token_hash=?", (digest(token),)).fetchone()
            if context is None or context["expires_at"] <= moment.isoformat():
                raise ReportError("Срок действия карточки истёк. Откройте свежее расписание.")
            if (context["source_kind"] == "private" and context["owner_user_id"] != user_id
                    or group_only and context["source_kind"] != "group"
                    or source_chat_id is not None and context["source_chat_id"] != source_chat_id
                    or source_message_id is not None and context["source_message_id"] is not None
                    and context["source_message_id"] != source_message_id):
                raise ReportError("Эта карточка недоступна.")
            current = self._report_draft(connection, user_id, moment.isoformat())
            # Repeated deep links resume the user's draft, including after restart.
            if current and current["context_id"] == context["id"] and current["stage"] != "submitted":
                return current
            connection.execute("DELETE FROM report_drafts WHERE user_id=?", (user_id,))
            connection.execute("DELETE FROM report_admin_drafts WHERE admin_id=?", (user_id,))
            identifier = secrets.token_urlsafe(12)
            connection.execute(
                "INSERT INTO report_drafts(id,user_id,context_id,stage,created_at,updated_at,expires_at) VALUES(?,?,?,'lesson',?,?,?)",
                (identifier, user_id, context["id"], moment.isoformat(), moment.isoformat(),
                 min(context["expires_at"], (moment + timedelta(hours=DRAFT_HOURS)).isoformat())),
            )
            connection.commit()
            return self._report_draft(connection, user_id, moment.isoformat())

    @staticmethod
    def _report_draft(connection, user_id, now):
        return _row(connection.execute(
            "SELECT d.*,c.snapshot_json,c.snapshot_hash,c.source_kind,c.source_chat_id,c.source_message_id,c.role,c.target,c.schedule_date "
            "FROM report_drafts d JOIN report_contexts c ON c.id=d.context_id "
            "WHERE d.user_id=? AND d.expires_at>? AND c.expires_at>?", (user_id, now, now),
        ).fetchone())

    def report_active_draft(self, user_id, *, now=None):
        self.initialize()
        with self._connect() as connection:
            draft = self._report_draft(connection, user_id, timestamp(now).isoformat())
            return draft if draft and draft["stage"] != "submitted" else None

    def report_cancel(self, user_id):
        self.initialize()
        with self._connect() as connection:
            connection.execute("DELETE FROM report_drafts WHERE user_id=?", (user_id,))
            connection.execute("DELETE FROM report_admin_drafts WHERE admin_id=?", (user_id,))
            connection.commit()

    def report_update_draft(self, identifier, user_id, revision, action, *, now=None):
        moment = timestamp(now).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            draft = self._report_draft(connection, user_id, moment)
            if not draft or draft["id"] != identifier:
                raise ReportError("Черновик недоступен или истёк. Откройте расписание заново.")
            if draft["revision"] != revision:
                raise ReportError("Эта кнопка устарела. Используйте последнее сообщение формы.")
            if action == "cancel":
                connection.execute("DELETE FROM report_drafts WHERE id=?", (identifier,))
                connection.commit()
                return None
            fields = {}
            if action.startswith("page:") and draft["stage"] == "lesson" and action[5:].isdigit():
                page = int(action[5:])
                last_page = max(0, (len(lesson_choices(json.loads(draft["snapshot_json"]))) - 1) // CHOICE_PAGE_SIZE)
                if not 0 <= page <= last_page:
                    raise ReportError("Страница недоступна.")
                fields = {"lesson_page": page}
            elif action.startswith("lesson:") and draft["stage"] == "lesson":
                key = action[7:]
                if key not in {"day", *dict(lesson_choices(json.loads(draft["snapshot_json"])))}:
                    raise ReportError("Занятие недоступно.")
                fields = {"selected_lesson_key": key, "category": None, "description": None, "stage": "category"}
            elif action.startswith("category:") and draft["stage"] == "category" and action[9:] in CATEGORIES:
                fields = {"category": action[9:], "stage": "description"}
            elif action == "back" and draft["stage"] in {"category", "description", "confirm"}:
                fields = {"stage": {"category": "lesson", "description": "category", "confirm": "description"}[draft["stage"]]}
            elif action == "edit" and draft["stage"] == "confirm":
                fields = {"stage": "description"}
            if not fields:
                raise ReportError("Действие недоступно на этом шаге.")
            assignments = ",".join(f"{name}=?" for name in fields)
            connection.execute(f"UPDATE report_drafts SET {assignments},revision=revision+1,updated_at=? WHERE id=?",
                               (*fields.values(), moment, identifier))
            connection.commit()
            return self._report_draft(connection, user_id, moment)

    def report_describe(self, user_id, text, *, now=None):
        text = normalize_description(text)
        moment = timestamp(now).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            draft = self._report_draft(connection, user_id, moment)
            if not draft or draft["stage"] != "description":
                raise ReportError("Сначала откройте форму и выберите категорию.")
            connection.execute("UPDATE report_drafts SET description=?,stage='confirm',revision=revision+1,updated_at=? WHERE id=?",
                               (text, moment, draft["id"]))
            connection.commit()
            return self._report_draft(connection, user_id, moment)

    def report_submit(self, identifier, user_id, revision, admin_ids, *, now=None):
        admin_ids = tuple(value for value in admin_ids if type(value) is int and value > 0)
        if not admin_ids:
            raise ReportError("Приём обращений сейчас недоступен.")
        moment = timestamp(now)
        current = moment.isoformat()
        cutoff = (moment - timedelta(hours=24)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM reports WHERE draft_id=? AND user_id=?", (identifier, user_id)).fetchone()
            if existing:
                return dict(existing)
            draft = self._report_draft(connection, user_id, current)
            if draft and draft["id"] == identifier and draft["stage"] == "submitted" and draft["submitted_report_id"]:
                return dict(connection.execute("SELECT * FROM reports WHERE id=? AND user_id=?",
                                               (draft["submitted_report_id"], user_id)).fetchone())
            if not draft or draft["id"] != identifier or draft["stage"] != "confirm" or draft["revision"] != revision:
                raise ReportError("Подтверждение устарело. Откройте последнюю форму обращения.")
            description = normalize_description(draft["description"])
            duplicate = connection.execute(
                "SELECT * FROM reports WHERE user_id=? AND snapshot_hash=? AND lesson_key=? AND category=? AND description=? "
                "AND created_at>? ORDER BY id LIMIT 1",
                (user_id, draft["snapshot_hash"], draft["selected_lesson_key"], draft["category"], description, cutoff),
            ).fetchone()
            if duplicate:
                # Keep this draft as a durable alias for repeated confirmations.
                connection.execute("UPDATE report_drafts SET stage='submitted',submitted_report_id=? WHERE id=?", (duplicate["id"], identifier))
                connection.commit()
                return dict(duplicate)
            if connection.execute("SELECT COUNT(*) FROM reports WHERE user_id=? AND created_at>?", (user_id, cutoff)).fetchone()[0] >= DAILY_LIMIT:
                raise ReportError("Можно отправить не более 3 обращений за скользящие 24 часа.")
            cursor = connection.execute(
                "INSERT INTO reports(draft_id,user_id,source_kind,source_chat_id,source_message_id,role,target,schedule_date,lesson_key,category,"
                "description,snapshot_json,snapshot_hash,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'new',?,?)",
                (identifier, user_id, draft["source_kind"], draft["source_chat_id"], draft["source_message_id"], draft["role"], draft["target"],
                 draft["schedule_date"], draft["selected_lesson_key"], draft["category"], description,
                 draft["snapshot_json"], draft["snapshot_hash"], current, current),
            )
            report = dict(connection.execute("SELECT * FROM reports WHERE id=?", (cursor.lastrowid,)).fetchone())
            connection.execute("INSERT INTO report_events(report_id,actor_id,actor_role,event_type,operation_key,created_at) "
                               "VALUES(?,?,'user','created',?,?)", (report["id"], user_id, f"create:{identifier}", current))
            for admin in dict.fromkeys(admin_ids):
                _queue(connection, report["id"], admin, "admin", admin_notification(report), f'new:{report["id"]}', current)
            connection.execute("UPDATE report_drafts SET stage='submitted' WHERE id=?", (identifier,))
            connection.commit()
            return report

    def report_get(self, identifier):
        with self._connect() as connection:
            return _row(connection.execute("SELECT * FROM reports WHERE id=?", (identifier,)).fetchone())

    @staticmethod
    def _report_admin(admin_id, admin_ids):
        if admin_id not in admin_ids:
            raise ReportError("Действие доступно только администратору.")

    def report_in_progress(self, identifier, admin_id, admin_ids, *, now=None):
        self._report_admin(admin_id, admin_ids)
        moment = timestamp(now).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute("UPDATE reports SET status='in_progress',updated_at=? WHERE id=? AND status='new'", (moment, identifier)).rowcount
            report = _row(connection.execute("SELECT * FROM reports WHERE id=?", (identifier,)).fetchone())
            if report is None:
                raise ReportError("Обращение больше недоступно.")
            if changed:
                connection.execute("INSERT INTO report_events(report_id,actor_id,actor_role,event_type,operation_key,created_at) "
                                   "VALUES(?,?,'admin','in_progress',?,?)", (identifier, admin_id, f"progress:{identifier}", moment))
            connection.commit()
            return report

    def report_admin_begin(self, identifier, admin_id, action, admin_ids, *, now=None):
        self._report_admin(admin_id, admin_ids)
        if action not in {"reply", "resolved", "rejected"}:
            raise ReportError("Недопустимое действие.")
        moment = timestamp(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            report = connection.execute("SELECT * FROM reports WHERE id=?", (identifier,)).fetchone()
            if not report:
                raise ReportError("Обращение больше недоступно.")
            if action != "reply" and report["status"] in {"resolved", "rejected"}:
                raise ReportError(f'Обращение уже закрыто: {STATUSES[report["status"]]}.')
            connection.execute("DELETE FROM report_admin_drafts WHERE admin_id=?", (admin_id,))
            connection.execute("DELETE FROM report_drafts WHERE user_id=?", (admin_id,))
            connection.execute("INSERT INTO report_admin_drafts(id,admin_id,report_id,action,stage,created_at,expires_at) "
                               "VALUES(?,?,?,?,'text',?,?)", (secrets.token_urlsafe(12), admin_id, identifier, action, moment.isoformat(),
                                                            (moment + timedelta(hours=DRAFT_HOURS)).isoformat()))
            connection.commit()
            return self.report_admin_active(admin_id, now=moment)

    def report_admin_active(self, admin_id, *, now=None):
        with self._connect() as connection:
            return _row(connection.execute("SELECT * FROM report_admin_drafts WHERE admin_id=? AND expires_at>?",
                                           (admin_id, timestamp(now).isoformat())).fetchone())

    def report_admin_text(self, admin_id, text, admin_ids, *, now=None):
        self._report_admin(admin_id, admin_ids)
        text = normalize_description(text)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM report_admin_drafts WHERE admin_id=? AND expires_at>? AND stage='text'",
                                     (admin_id, timestamp(now).isoformat())).fetchone()
            if not row:
                raise ReportError("Форма ответа устарела.")
            connection.execute("UPDATE report_admin_drafts SET text=?,stage='confirm',revision=revision+1 WHERE id=?", (text, row["id"]))
            connection.commit()
        return self.report_admin_active(admin_id, now=now)

    def report_admin_cancel(self, identifier, admin_id, revision, admin_ids, *, now=None):
        self._report_admin(admin_id, admin_ids)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute("DELETE FROM report_admin_drafts WHERE id=? AND admin_id=? AND revision=? AND expires_at>?",
                                         (identifier, admin_id, revision, timestamp(now).isoformat())).rowcount
            if not changed:
                raise ReportError("Кнопка устарела.")
            connection.commit()

    def report_admin_confirm(self, identifier, admin_id, revision, admin_ids, *, now=None):
        self._report_admin(admin_id, admin_ids)
        moment = timestamp(now).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT report_id FROM report_events WHERE operation_key=? AND actor_id=?",
                                          (f"admin:{identifier}", admin_id)).fetchone()
            if existing:
                return dict(connection.execute("SELECT * FROM reports WHERE id=?", (existing[0],)).fetchone())
            draft = connection.execute("SELECT * FROM report_admin_drafts WHERE id=? AND admin_id=? AND expires_at>? "
                                       "AND stage='confirm' AND revision=?", (identifier, admin_id, moment, revision)).fetchone()
            if not draft:
                raise ReportError("Подтверждение устарело.")
            report = dict(connection.execute("SELECT * FROM reports WHERE id=?", (draft["report_id"],)).fetchone())
            action = draft["action"]
            if action != "reply" and report["status"] in {"resolved", "rejected"}:
                return report
            text = normalize_description(draft["text"])
            event = connection.execute("INSERT INTO report_events(report_id,actor_id,actor_role,event_type,text,operation_key,created_at) "
                                       "VALUES(?,?,'admin',?,?,?,?)", (report["id"], admin_id, action, text, f"admin:{identifier}", moment))
            if action == "reply":
                notification = f'💬 Ответ на обращение #{report["id"]}\n\nАдминистратор:\n{text}\n\nСтатус: {STATUSES[report["status"]]}'
            else:
                connection.execute("UPDATE reports SET status=?,updated_at=?,closed_at=? WHERE id=?", (action, moment, moment, report["id"]))
                report["status"] = action
                notification = f'✅ Обращение #{report["id"]} закрыто\nРезультат: {STATUSES[action]}\n\nКомментарий администратора:\n{text}'
            _queue(connection, report["id"], report["user_id"], "reply" if action == "reply" else "closed", notification,
                   f"admin:{identifier}", moment, event.lastrowid)
            connection.execute("DELETE FROM report_admin_drafts WHERE id=?", (identifier,))
            connection.commit()
            return report

    def report_pending(self, *, now=None, limit=100):
        moment = timestamp(now).isoformat()
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT o.id,o.destination_id,r.user_id FROM report_outbox o JOIN reports r ON r.id=o.report_id "
                "WHERE ((o.status='pending' AND o.next_attempt_at<=?) OR (o.status='sending' AND o.lease_until<=?)) "
                "AND NOT EXISTS(SELECT 1 FROM report_outbox earlier WHERE earlier.report_id=o.report_id "
                "AND earlier.destination_id=o.destination_id AND earlier.id<o.id AND earlier.status IN ('pending','sending')) "
                "ORDER BY o.id LIMIT ?", (moment, moment, limit),
            )]

    def report_claim(self, identifier, *, now=None):
        moment = timestamp(now)
        current = moment.isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM report_outbox WHERE id=? AND "
                                     "((status='pending' AND next_attempt_at<=?) OR (status='sending' AND lease_until<=?))",
                                     (identifier, current, current)).fetchone()
            if not row:
                return None
            connection.execute("UPDATE report_delivery_attempts SET outcome='uncertain',error_category='lease_expired',finished_at=? "
                               "WHERE outbox_id=? AND outcome='started'", (current, identifier))
            token = secrets.token_urlsafe(16)
            connection.execute("UPDATE report_outbox SET status='sending',attempts=attempts+1,lease_token=?,lease_until=? WHERE id=?",
                               (token, (moment + timedelta(minutes=2)).isoformat(), identifier))
            connection.execute("INSERT INTO report_delivery_attempts(outbox_id,lease_token,outcome,created_at) VALUES(?,?,'started',?)",
                               (identifier, token, current))
            connection.commit()
            return dict(connection.execute("SELECT * FROM report_outbox WHERE id=?", (identifier,)).fetchone())

    def report_finish_delivery(self, identifier, lease_token, *, message_id=None, error_category=None,
                               permanent=False, uncertain=False, retry_after=None, now=None):
        moment = timestamp(now)
        current = moment.isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM report_outbox WHERE id=? AND status='sending' AND lease_token=?",
                                     (identifier, lease_token)).fetchone()
            if not row:
                return
            success = error_category is None
            status = "sent" if success else "failed" if permanent or row["attempts"] >= 12 else "pending"
            delay = max(1, retry_after) if retry_after is not None else min(3600, 5 * 2 ** min(row["attempts"], 10))
            connection.execute("UPDATE report_outbox SET status=?,delivered_at=?,telegram_message_id=?,last_error=?,"
                               "next_attempt_at=?,lease_token=NULL,lease_until=NULL WHERE id=?",
                               (status, current if success else None, message_id, error_category,
                                (moment + timedelta(seconds=delay)).isoformat(), identifier))
            connection.execute("UPDATE report_delivery_attempts SET outcome=?,error_category=?,telegram_message_id=?,finished_at=? WHERE lease_token=?",
                               ("accepted" if success else "uncertain" if uncertain else "rejected", error_category, message_id, current, lease_token))
            connection.commit()

    @staticmethod
    def report_delete_in_transaction(connection, user_id):
        changed = 0
        for sql in (
            "DELETE FROM report_admin_drafts WHERE admin_id=?",
            "DELETE FROM report_drafts WHERE user_id=?",
            "DELETE FROM report_contexts WHERE owner_user_id=?",
            "DELETE FROM reports WHERE user_id=?",
            "DELETE FROM report_events WHERE actor_id=?",
        ):
            changed += connection.execute(sql, (user_id,)).rowcount
        return changed > 0

    def report_cleanup(self, *, now=None, limit=100, dry_run=False, temporary_only=False):
        moment = timestamp(now)
        cutoff = (moment - timedelta(days=CLOSED_DAYS)).isoformat()
        counts = {}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for table, predicate, parameters in (
                ("report_admin_drafts", "expires_at<=?", (moment.isoformat(),)),
                ("report_drafts", "expires_at<=?", (moment.isoformat(),)),
                ("report_contexts", "expires_at<=?", (moment.isoformat(),)),
                ("reports", "closed_at IS NOT NULL AND closed_at<? AND NOT EXISTS "
                 "(SELECT 1 FROM report_outbox o WHERE o.report_id=reports.id AND o.status='sending')", (cutoff,)),
            ):
                if temporary_only and table == "reports":
                    continue
                counts[table] = connection.execute(f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE {predicate} LIMIT ?)",
                                                   (*parameters, limit)).rowcount
            if dry_run:
                connection.rollback()
            else:
                connection.commit()
        return counts
