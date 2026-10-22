"""Storage layer for the engineer logbook: PostgreSQL.

The public API used by the MCP server and the TUI (re-exported from db_pg).
Connection configuration comes from the environment — either
ENGINEER_LOGBOOK_PG_DSN, or the POSTGRES_HOST/POSTGRES_PORT/POSTGRES_DB/
POSTGRES_USER/POSTGRES_PASSWORD family; see db_pg.pg_dsn().
"""

from __future__ import annotations

from engineer_logbook import db_pg
from engineer_logbook import db_spec
from engineer_logbook.db_pg import *  # noqa: F401,F403  (public storage API)
from engineer_logbook.db_pg import (  # noqa: F401  (not covered by __all__-less star)
    DEFAULT_IDLE_MINUTES,
    SEARCHABLE_TYPES,
    VALID_PRIORITIES,
    VALID_TASK_STATUSES,
    chat_id,
    close_pool,
    connect,
    describe,
    heartbeat,
    pg_dsn,
    pool,
    set_user,
    utcnow,
)
from engineer_logbook.db_spec import (  # noqa: F401  (spec workflow API)
    DEFAULT_FOLDER as SPEC_DEFAULT_FOLDER,
    SpecOpError,
    active_workflow_nag,
    get_active_workflow,
    spec_complete_step,
    spec_finish,
    spec_progress,
    spec_report_artifact,
    spec_start,
    spec_step,
    spec_template,
)

# names starting with _ stay private to db_pg; expose the module itself for
# anything else the server/tests need (db_pg._HYDRATE_TABLE, ...). Schema
# changes live in engineer_logbook/migrations, applied by engineer_logbook.migrate.