"""Bounded, read-only task queries shared by HTTP and local agents.

Cursors are versioned positions, not authorization credentials. Every query
reapplies ownership and filters, and a cursor is bound to its query scope.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from max_agent.db.models import TaskEventRow, TaskRow
from max_agent.kernel.readmodel import task_to_read
from max_agent.models.enums import EventType, TaskStatus
from max_agent.models.schemas import TaskEventRead, TaskEventPage, TaskPage


def _scope(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def _encode(scope: str, position: list) -> str:
    return base64.urlsafe_b64encode(json.dumps([1, scope, position]).encode()).decode().rstrip('=')


def _decode(cursor: str, scope: str) -> list:
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1024:
            raise ValueError
        raw = base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True)
        value = json.loads(raw)
        if not isinstance(value, list) or len(value) != 3 or value[0] != 1 or value[1] != scope:
            raise ValueError
        if not isinstance(value[2], list):
            raise ValueError
        return value[2]
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        raise ValueError('invalid cursor for this query') from None


def _limit(value: int, maximum: int) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f'limit must be between 1 and {maximum}')


class TaskQuery:
    def __init__(self, session: Session, principal: str):
        self.session = session
        self.principal = principal

    def authorize(self, task_id: str) -> TaskRow:
        row = self.session.get(TaskRow, task_id)
        if row is None:
            raise KeyError(f'task not found: {task_id}')
        if row.created_by != self.principal:
            raise PermissionError('task access denied')
        return row

    def tasks(self, *, q: str = '', status: TaskStatus | None = None,
              limit: int = 25, cursor: str | None = None) -> TaskPage:
        _limit(limit, 100)
        if not isinstance(q, str) or len(q) > 256:
            raise ValueError('q must be a string of at most 256 characters')
        if status is not None:
            status = TaskStatus(status)
        query = self.session.query(TaskRow).filter(TaskRow.created_by == self.principal)
        if status is not None:
            query = query.filter(TaskRow.status == status.value)
        if q:
            # Escape SQL LIKE metacharacters: a keyword is always literal.
            pattern = '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            query = query.filter(or_(*(column.ilike(pattern, escape='\\') for column in
                                      (TaskRow.id, TaskRow.goal, TaskRow.issue_ref))))
        total = query.count()
        scope = _scope('tasks', self.principal, q, None if status is None else status.value)
        if cursor is not None:
            position = _decode(cursor, scope)
            try:
                if len(position) != 2 or not isinstance(position[0], str) or not isinstance(position[1], str) or not 1 <= len(position[1]) <= 36:
                    raise ValueError
                timestamp = datetime.fromisoformat(position[0])
            except (ValueError, TypeError):
                raise ValueError('invalid cursor for this query') from None
            query = query.filter(or_(TaskRow.created_at < timestamp,
                and_(TaskRow.created_at == timestamp, TaskRow.id < position[1])))
        rows = query.order_by(TaskRow.created_at.desc(), TaskRow.id.desc()).limit(limit + 1).all()
        more = len(rows) > limit
        rows = rows[:limit]
        return TaskPage(items=[task_to_read(row) for row in rows], total=total,
            next_cursor=_encode(scope, [rows[-1].created_at.isoformat(), rows[-1].id]) if more else None)

    def events(self, task_id: str, *, limit: int = 50, cursor: str | None = None) -> TaskEventPage:
        self.authorize(task_id)
        _limit(limit, 200)
        query = self.session.query(TaskEventRow).filter(TaskEventRow.task_id == task_id)
        scope = _scope('events', self.principal, task_id)
        if cursor is not None:
            position = _decode(cursor, scope)
            if len(position) != 1 or type(position[0]) is not int or not 1 <= position[0] <= 2**63 - 1:
                raise ValueError('invalid cursor for this query')
            query = query.filter(TaskEventRow.id < position[0])
        rows = query.order_by(TaskEventRow.id.desc()).limit(limit + 1).all()
        more = len(rows) > limit
        rows = rows[:limit]
        return TaskEventPage(items=[TaskEventRead.model_validate(row) for row in reversed(rows)],
            next_cursor=_encode(scope, [rows[-1].id]) if more else None, has_more=more)


def latest_verification(session: Session, task_id: str) -> dict | None:
    row = (session.query(TaskEventRow).filter(TaskEventRow.task_id == task_id,
        TaskEventRow.event_type == EventType.VERIFICATION.value)
        .order_by(TaskEventRow.id.desc()).first())
    return None if row is None else dict(row.payload or {})
