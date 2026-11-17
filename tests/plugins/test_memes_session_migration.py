"""会话身份表改名迁移：保留数据、约束名不再包含历史前缀。"""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fxbot.plugins.memes.migrate import migrate_session_tables

OLD_SCHEMA = """
CREATE TABLE old_uninfo_botmodel (
    id INTEGER NOT NULL,
    self_id VARCHAR(64) NOT NULL,
    adapter VARCHAR(32) NOT NULL,
    scope VARCHAR(32) NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT old_unique_bot UNIQUE (self_id, adapter)
);
CREATE TABLE old_uninfo_scenemodel (
    id INTEGER NOT NULL,
    bot_persist_id INTEGER NOT NULL,
    parent_scene_persist_id INTEGER,
    scene_id VARCHAR(64) NOT NULL,
    scene_type INTEGER NOT NULL,
    scene_data JSON NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT old_unique_scene UNIQUE (bot_persist_id, scene_id, scene_type)
);
CREATE TABLE old_uninfo_usermodel (
    id INTEGER NOT NULL,
    bot_persist_id INTEGER NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    user_data JSON NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT old_unique_user UNIQUE (bot_persist_id, user_id)
);
CREATE TABLE old_uninfo_sessionmodel (
    id INTEGER NOT NULL,
    bot_persist_id INTEGER NOT NULL,
    scene_persist_id INTEGER NOT NULL,
    user_persist_id INTEGER NOT NULL,
    member_data JSON,
    PRIMARY KEY (id),
    CONSTRAINT old_unique_session UNIQUE (bot_persist_id, scene_persist_id, user_persist_id)
);
"""


class SessionTableMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "fxbot.db"
        connection = sqlite3.connect(self.database)
        try:
            connection.executescript(OLD_SCHEMA)
            connection.execute(
                "INSERT INTO old_uninfo_botmodel (id, self_id, adapter, scope) VALUES (1, '100', 'onebot11', 'group')"
            )
            connection.execute(
                "INSERT INTO old_uninfo_scenemodel "
                "(id, bot_persist_id, parent_scene_persist_id, scene_id, scene_type, scene_data) "
                "VALUES (2, 1, NULL, '300', 1, ?)",
                (json.dumps({"name": "群"}),),
            )
            connection.execute(
                "INSERT INTO old_uninfo_usermodel (id, bot_persist_id, user_id, user_data) VALUES (3, 1, '200', ?)",
                (json.dumps({"name": "用户"}),),
            )
            connection.execute(
                "INSERT INTO old_uninfo_sessionmodel "
                "(id, bot_persist_id, scene_persist_id, user_persist_id, member_data) "
                "VALUES (4, 1, 2, 3, NULL)"
            )
            connection.commit()
        finally:
            connection.close()

    def test_migration_preserves_rows_and_removes_old_names(self) -> None:
        result = migrate_session_tables(self.database)
        self.assertTrue(Path(result["backup"]).is_file())
        connection = sqlite3.connect(self.database)
        try:
            tables = {name for (name,) in connection.execute("select name from sqlite_master where type='table'")}
            self.assertEqual(
                tables,
                {"session_bot", "session_scene", "session_user", "session_identity"},
            )
            self.assertEqual(connection.execute("select self_id from session_bot").fetchall(), [("100",)])
            self.assertEqual(connection.execute("select scene_id from session_scene").fetchall(), [("300",)])
            self.assertEqual(connection.execute("select user_id from session_user").fetchall(), [("200",)])
            self.assertEqual(connection.execute("select id from session_identity").fetchall(), [(4,)])
            for (sql,) in connection.execute("select sql from sqlite_master where sql is not null"):
                self.assertNotIn("uninfo", sql.lower())
        finally:
            connection.close()

    def test_migration_is_idempotent(self) -> None:
        migrate_session_tables(self.database, backup=False)
        second = migrate_session_tables(self.database, backup=False)
        self.assertEqual(second["copied"], {})
        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(connection.execute("select count(*) from session_identity").fetchone(), (1,))
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
