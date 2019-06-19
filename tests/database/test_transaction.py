import pytest
from dynaconf import settings

from fas.util.database import DBPool, DBClient, transactional


@pytest.mark.asyncio
async def test_transaction():
    name = 'ORG#1'
    new_name = 'NEW-ORG#1'

    @transactional
    async def func(db_: DBClient, *, raise_exception: bool = False) -> None:
        await db_.insert('organization', conflict_target='(name)', conflict_action='DO NOTHING', name=name)
        if raise_exception:
            raise Exception
        await db_.execute('UPDATE organization SET name=:new_name WHERE name=:name', name=name, new_name=new_name)

    async with DBPool(**settings.DB) as pool:
        async with pool.acquire() as db:
            await func(db)
            assert not await db.exists('SELECT name FROM organization WHERE name=:name', name=name)
            assert await db.exists('SELECT name FROM organization WHERE name=:name', name=new_name)
            await db.execute('DELETE FROM organization WHERE name=:name', name=new_name)

            with pytest.raises(Exception):
                await func(db, raise_exception=True)
            assert not await db.exists('SELECT name FROM organization WHERE name=:name', name=name)
            assert not await db.exists('SELECT name FROM organization WHERE name=:name', name=new_name)