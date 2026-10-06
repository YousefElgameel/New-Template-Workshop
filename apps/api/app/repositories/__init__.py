"""Query functions that take an ``AsyncSession`` — keeps SQL out of the routers.

Repositories never commit: the caller (the request's unit of work) decides when to commit.
"""
