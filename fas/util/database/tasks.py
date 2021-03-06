import asyncio
import datetime as dt
import os
from pathlib import Path
from typing import Dict

from blessings import Terminal
from dynaconf import settings
from invoke import task, Collection

from fas.environment import ENV
from .client import DBClient, DBPool
from .migration import load_versions, lock_scripts, check_no_scripts_not_locked, check_no_locked_scripts_changed
from .transaction import transactional

t = Terminal()


@task(name='create')
def create_database_if_not_exist(c):
    """
    Create database
    """
    env = os.environ.copy()
    env['PGPASSWORD'] = c.db.owner.password
    if is_database_existed(c, env):
        print(t.yellow(f'Cannot create database {c.db.database}: already exist'))
        return
    c.run(f'''
        createdb -h {c.db.host} -p {c.db.port} -U {c.db.owner.name} {c.db.database} \
        -T template0 -E UTF-8 --locale=C.UTF-8
        ''', env=env)
    print(t.green(f'Created database {c.db.database}'))


@task(name='drop')
def drop_database(c):
    """
    Drop database
    """
    if not (ENV.is_dev or ENV.is_test):
        raise Exception('Cannot drop database under environments other than dev or test')
    env = os.environ.copy()
    env['PGPASSWORD'] = c.db.owner.password
    if not is_database_existed(c, env):
        print(t.yellow(f'Cannot drop database {c.db.database}: not found'))
        return
    c.run(f'dropdb -h {c.db.host} -p {c.db.port} -U {c.db.owner.name} {c.db.database}', env=env)
    print(t.green(f'Dropped database {c.db.database}'))


@task(name='migrate')
def migrate_database(c):
    """
    Migrate database
    """
    if not (ENV.is_dev or ENV.is_test):
        check_no_scripts_not_locked()
        check_no_locked_scripts_changed()
    create_database_if_not_exist(c)
    asyncio.run(_migrate_database(c.db.database))


@task(name='reset')
def reset_database(c):
    """
    Reset database
    """
    drop_database(c)
    migrate_database(c)
    print(t.green(f'Reset database {c.db.database}'))


@task(name='lock-scripts')
def lock_migration_scripts(c):
    """
    Lock migration scripts
    """
    locked_count = lock_scripts()
    if locked_count == 0:
        print(t.yellow(f'Did not lock migration scripts for database {c.db.database}: no not-locked scripts'))
    else:
        print(t.green(f'Locked {locked_count} migration scripts for database {c.db.database}'))


@task(name='backup')
def create_backup(c, to_file=None):
    """
    Create database backup
    """
    env = os.environ.copy()
    env['PGPASSWORD'] = c.db.owner.password
    to_file = to_file or f'{c.db.database}.dump'
    print(f'Be about to back up {c.db.database} to {to_file}')
    c.run(f'pg_dump -h {c.db.host} -p {c.db.port} -U {c.db.owner.name} -d {c.db.database} -v -b -Fc -f "{to_file}"',
          env=env)
    print(t.green(f'Backed up {c.db.database} to {to_file}'))


@task(name='restore')
def restore_backup(c, from_file=None):
    """
    Restore database backup
    """
    env = os.environ.copy()
    env['PGPASSWORD'] = c.db.owner.password
    from_file = from_file or f'{c.db.database}.dump'
    print(f'Be about to restore {c.db.database} from {from_file}')
    c.run(
        f'pg_restore -h {c.db.host} -p {c.db.port} -U {c.db.owner.name} -d postgres -v -C -c -e -O -Fc "{from_file}"',
        env=env)
    print(t.green(f'Restored {c.db.database} from {from_file}'))


def is_database_existed(c, env):
    r = c.run(f'''
        psql -h {c.db.host} -p {c.db.port} -U {c.db.owner.name} -lqt | cut -d \\| -f 1 | awk '{{$1=$1}};1' | \
        grep -x {c.db.database} | wc -l
        ''', hide='out', env=env)
    return 1 == int(r.stdout)


MIGRATION_TABLE = 'database_migration'
MIGRATION_LOCK_KEY = hash(f'i48gEtCCfX1lxPpWgVyBYH1z4VQpFNz7+{MIGRATION_TABLE}')


async def _migrate_database(db_name: str):
    async with DBPool(**settings.DB) as pool:
        async with pool.acquire() as db:
            await _migrate(db, db_name)


@transactional
async def _migrate(db: DBClient, db_name: str):
    locked = await db.try_transaction_lock(MIGRATION_LOCK_KEY)
    if not locked:
        print(t.yellow(f'Did not migrate database {db_name}: cannot run migration in parallel'))
        return

    await create_database_migration_table_if_not_exist(db)
    current_version = await db.get_scalar(
        'SELECT to_version FROM database_migration ORDER BY id DESC LIMIT 1') or 0

    new_versions = load_versions(after=current_version)
    if not new_versions:
        print(t.yellow(f'Did not migrate database {db_name}: no scripts after version {current_version}'))
        return

    to_version = max(new_versions)
    print(f'Be about to migrate {db_name} from {current_version} to {to_version}')
    await execute_migration_scripts(db, current_version, to_version, new_versions)
    print(t.green(f'Migrated {db_name} from {current_version} to {to_version}'))


@transactional
async def execute_migration_scripts(db: DBClient, from_version: int, to_version: int, versions: Dict[int, Path]):
    for version in range(from_version + 1, to_version + 1):
        print(f'Applying version: {version}')
        await db.execute(versions[version].read_text(encoding='UTF-8'))
    await db.insert('database_migration', from_version=from_version, to_version=to_version,
                    migrated_at=dt.datetime.now(dt.timezone.utc))


async def create_database_migration_table_if_not_exist(db: DBClient):
    await db.execute(f'''
        CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} (
            id INT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            from_version INT NOT NULL,
            to_version INT NOT NULL,
            migrated_at TIMESTAMP WITH TIME ZONE NOT NULL,

            CHECK (to_version > from_version),
            EXCLUDE USING GIST (NUMRANGE(from_version, to_version, '(]') WITH &&)
        )
        ''')


db_tasks = Collection('db', create_database_if_not_exist, drop_database, reset_database, migrate_database,
                      lock_migration_scripts, create_backup, restore_backup)
db_tasks.configure({'db': {**settings.DB, 'owner': settings.DB_OWNER}})
