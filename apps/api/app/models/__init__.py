"""ORM models. Every module is imported here so it registers on ``Base.metadata`` — Alembic
autogenerate (``alembic/env.py`` imports this package) only sees imported models.

Adding a model (``.claude/rules/25-sqlalchemy.md``):
  1. A 2.0-style module in this package subclassing ``app.db.Base`` (explicit lengths, named
     ``CheckConstraint``s), imported below.
  2. With a reachable dev database, generate a revision (needs a human + DB):
         uv run alembic revision --autogenerate -m "<message>"
     Offline SQL for review:
         uv run alembic upgrade head --sql
  3. Applying it (``alembic upgrade head``) is a deliberate, confirmed human step — never run
     from an agent.
"""

from __future__ import annotations

from app.models.conversation import Conversation, Message

__all__ = ["Conversation", "Message"]
