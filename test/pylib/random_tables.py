#
# Copyright (C) 2022-present ScyllaDB
#
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""This module provides helper classes to manage CQL tables, perform random schema changes,
and verify expected current schema.

Classes:
    RandomTables
        A list of managed tables stored in self.tables.
        .add_tables() creates multiple (ntables) random tables with (ncolumns) random columns.
        .add_table() create a table of specified number of random type columns or a custom table if
        given list of columns.
        Provides list access by position with [pos].
        Custom tables can be .append()ed.
        A list of tables can be merged with extend().
        drop_table() either random one or a specified one by name.
        drop_all_tables()
        verify_schema() checks expected schema for all managed and active tables.
        .removed_tables keeps previous tables after dropping them.

    RandomTable
        A managed table.
    Column
        Manage a table's column and generate a value from a seed.
        Usually tests should generate deterministic sequential values.
"""


from __future__ import annotations
from abc import ABCMeta
import asyncio
import itertools
import logging
import random
import uuid
from typing import Optional, Type, List, Set, Union, TYPE_CHECKING
if TYPE_CHECKING:
    from cassandra.cluster import Session as CassandraSession            # type: ignore


logger = logging.getLogger('random_tables')
new_keyspace_id = itertools.count(start=1).__next__


class ColumnNotFound(Exception):
    pass


class ValueType(metaclass=ABCMeta):
    """Base value type"""
    name: str = ""

    def val(self, seed: int):
        """Return next value for this type"""
        pass


class IntType(ValueType):
    def __init__(self):
        self.name: str = 'int'

    def val(self, seed: int) -> int:
        return seed


class TextType(ValueType):
    def __init__(self):
        self.name: str = 'text'

    def val(self, seed) -> str:
        return str(seed)


class FloatType(ValueType):
    def __init__(self):
        self.name: str = 'float'

    def val(self, seed: int) -> float:
        return float(seed)


class UUIDType(ValueType):
    def __init__(self):
        self.name: str = 'uuid'

    def val(self, seed: int) -> uuid.UUID:
        return uuid.UUID(f"{{00000000-0000-0000-0000-{seed:012}}}")


class Column():
    """A column definition.
       If no value type specified it picks a random one.
       There is no support for collection or user-defined types."""
    def __init__(self, name: str, ctype: Optional[Type[ValueType]] = None):
        self.name: str = name
        if ctype is not None:
            self.ctype = ctype()
        else:
            self.ctype = random.choice([IntType, TextType, FloatType, UUIDType])()

        self.cql: str = f"{self.name} {self.ctype.name}"

    def val(self, seed):
        """Generate a random value"""
        return self.ctype.val(seed)

    def __str__(self):
        return self.name


class RandomTable():
    """A managed random table
    """
    # Sequential unique id
    newid = itertools.count(start=1).__next__

    def __init__(self, cql: CassandraSession, keyspace: str, ncolumns: Optional[int]=None,
                 columns: Optional[List[Column]]=None, pks: int=2, name: str=None):
        """Set up a new table definition from column definitions.
           If column definitions not specified pick a random number of columns with random types.
           By default there will be 4 columns with first column as Primary Key"""
        self.id: int = RandomTable.newid()
        self.cql: CassandraSession = cql
        self.keyspace: str = keyspace
        self.name: str = name if name is not None else f"t_{self.id:02}"
        self.full_name: str = keyspace + "." + self.name
        self.next_clustering_id = itertools.count(start=1).__next__
        self.next_value_id = itertools.count(start=1).__next__
        # TODO: assumes primary key is composed of first self.pks columns
        self.pks = pks

        if columns is not None:
            assert len(columns) > pks, "Not enough value columns provided"
            self.columns = columns
        else:
            assert isinstance(ncolumns, int) and ncolumns > pks, "Not enough value columns provided"
            # Primary key pk, clustering columns c_xx, value columns v_xx
            self.columns = [Column("pk")]
            self.columns += [Column(f"c_{self.next_clustering_id():02}", ctype=TextType)
                             for i in range(1, pks)]
            self.columns += [Column(f"v_{self.next_value_id():02}")
                             for i in range(1, ncolumns - pks + 1)]

        self.removed_columns: List[Column] = []
        # Counter for sequential values to insert
        self.next_seq = itertools.count(start=1).__next__
        self.next_idx_id = itertools.count(start=1).__next__
        self.indexes: Set[str] = set()
        self.removed_indexes: Set[str] = set()

    @property
    def all_col_names(self) -> str:
        """Get all column names comma separated for CQL query generation convenience"""
        return ", ".join([c.name for c in self.columns])

    async def create(self) -> asyncio.Future:
        """Create this table"""
        col_defs = ", ".join(f"{c.cql}" for c in self.columns)
        pk_names = ", ".join(c.name for c in self.columns[:self.pks])
        cql_stmt = f"CREATE TABLE {self.full_name} ({col_defs}, , primary key({pk_names}))"
        logger.debug(cql_stmt)
        return await self.cql.run_async(cql_stmt)

    async def drop(self) -> asyncio.Future:
        """Drop this table"""
        cql_stmt = f"DROP TABLE {self.full_name}"
        logger.debug(cql_stmt)
        return await self.cql.run_async(cql_stmt)

    async def add_column(self, name: str = None, ctype: Type[ValueType] = None, column: Column = None):
        if column is not None:
            assert type(column) is Column, "Wrong column type to add_column"
        else:
            name = name if name is not None else f"c_{self.next_clustering_id():02}"
            ctype = ctype if ctype is not None else TextType
            column = Column(name, ctype=ctype)
        self.columns.append(column)
        await self.cql.run_async(f"ALTER TABLE {self.full_name} ADD {column.name} {column.ctype.name}")

    async def drop_column(self, column: Union[Column, str] = None):
        if column is None:
            col = random.choice(self.columns[self.pks:])
        elif type(column) is int:
            assert column >= self.pks, f"Cannot remove {self.name} PK column at pos {column}"
            col = self.columns[column]
        elif type(column) is str:
            try:
                col = next(col for col in self.columns if col.name == column)
            except StopIteration:
                raise ColumnNotFound(f"Column {column} not found in table {self.name}")
        else:
            assert type(column) is Column, f"can not remove unknown type {type(column)}"
            assert column in self.columns, f"column {column.name} not present"
            col = column
        assert len(self.columns) - 1 > self.pks, f"Cannot remove last value column {col.name} from {self.name}"
        self.columns.remove(col)
        self.removed_columns.append(col)
        await self.cql.run_async(f"ALTER TABLE {self.full_name} DROP {col.name}")

    async def insert_seq(self) -> asyncio.Future:
        """Insert a row of next sequential values"""
        seed = self.next_seq()
        return await self.cql.run_async(f"INSERT INTO {self.full_name} ({self.all_col_names}) " +
                                        f"VALUES ({', '.join(['%s'] * len(self.columns)) })",
                                        parameters=[c.val(seed) for c in self.columns])

    async def add_index(self, column: Union[Column, str], name: str = None) -> str:
        if isinstance(column, int):
            assert column > 0, f"Cannot create secondary index " \
                               f"on partition key column {self.columns[0].name}"
            col_name = self.columns[column].name
        elif isinstance(column, str):
            col_name = column
        elif isinstance(column, Column):
            assert column in self.columns
            col_name = column.name
        else:
            raise TypeError(f"Wrong column type {type(column)} given to add_column")

        name = name if name is not None else f"{self.name}_{col_name}_{self.next_idx_id():02}"
        await self.cql.run_async(f"CREATE INDEX {name} on {self.full_name} ({col_name})")
        self.indexes.add(name)
        return name

    async def drop_index(self, name: str) -> None:
        self.indexes.remove(name)
        await self.cql.run_async(f"DROP INDEX {self.keyspace}.{name}")
        self.removed_indexes.add(name)

    def __str__(self):
        return self.full_name


class RandomTables():
    """A list of managed random tables"""
    def __init__(self, test_name: str, cql: CassandraSession, keyspace: str):
        self.test_name = test_name
        self.cql = cql
        self.keyspace = keyspace
        self.tables: List[RandomTable] = []
        self.removed_tables: List[RandomTable] = []

    async def add_tables(self, ntables: int = 1, ncolumns: int = 5) -> None:
        """Add random tables to the list.
        ntables specifies how many tables.
        ncolumns specifies how many random columns per table."""
        tables = [RandomTable(self.cql, self.keyspace, ncolumns) for _ in range(ntables)]
        await asyncio.gather(*(t.create() for t in tables))
        self.tables.extend(tables)

    async def add_table(self, ncolumns: int = None, columns: List[Column] = None,
                        pks: int = 2, name: str = None) -> RandomTable:
        """Add a random table. See random_tables.RandomTable()"""
        table = RandomTable(self.cql, self.keyspace, ncolumns=ncolumns, columns=columns,
                            pks=pks, name=name)
        await table.create()
        self.tables.append(table)
        return table

    def __getitem__(self, pos: int) -> RandomTable:
        return self.tables[pos]

    def append(self, table: RandomTable) -> None:
        self.tables.append(table)

    def extend(self, tables: List[RandomTable]) -> None:
        self.tables.extend(tables)

    async def drop_table(self, table: Union[str, RandomTable]) -> RandomTable:
        """Drop managed RandomTable by name or by RandomTable instance"""
        if isinstance(table, str):
            table = next(t for t in self.tables if table in [t.name, t.full_name])
        else:
            assert isinstance(table, RandomTable), f"Invalid table type {type(table)}"
        await table.drop()
        self.tables.remove(table)
        self.removed_tables.append(table)
        return table

    async def drop_all_tables(self) -> None:
        """Drop all active managed tables"""
        await asyncio.gather(*(t.drop() for t in self.tables))
        self.removed_tables.extend(self.tables)

    async def verify_schema(self, table: Union[RandomTable, str] = None) -> None:
        """Verify schema of all active managed random tables"""
        if isinstance(table, RandomTable):
            tables = {table.name}
            cql_stmt1 = f"SELECT table_name FROM system_schema.tables " \
                        f"WHERE keyspace_name = '{self.keyspace}' AND table_name = '{table.name}'"
        elif isinstance(table, str):
            if table.startswith(f"{self.keyspace}."):
                table = table[len(self.keyspace) + 1:]
            tables = {table}
            cql_stmt1 = f"SELECT table_name FROM system_schema.tables " \
                        f"WHERE keyspace_name = '{self.keyspace}' AND table_name = '{table}'"
        else:
            tables = set(t.name for t in self.tables)
            cql_stmt1 = f"SELECT table_name FROM system_schema.tables " \
                        f"WHERE keyspace_name = '{self.keyspace}'"

        logger.debug(cql_stmt1)
        res1 = {row.table_name for row in await self.cql.run_async(cql_stmt1)}
        assert not tables - res1, f"Tables {tables - res1} not present"

        for table_name in tables:
            table = next(t for t in self.tables if t.name == table_name)
            cols = {c.name: c for c in table.columns}
            c_pos = {c.name: i for i, c in enumerate(table.columns)}
            cql_stmt2 = f"SELECT column_name, position, kind, type FROM system_schema.columns " \
                        f"WHERE keyspace_name = '{self.keyspace}' AND table_name = '{table_name}'"
            logger.debug(cql_stmt2)
            res2 = {row.column_name: row for row in await self.cql.run_async(cql_stmt2)}
            assert res2.keys() == cols.keys(), f"Column names for {table_name} do not match " \
                                               f"expected ({', '.join(cols.keys())}) " \
                                               f"got ({', '.join(res2.keys())})"
            for c_name, c in res2.items():
                pos = c_pos[c_name]
                col = cols[c_name]
                assert c.type == col.ctype.name, f"Column {c_name} type does not match " \
                                                 f"{c.type} {col.ctype.name}"
                if pos == 0:
                    kind = "partition_key"
                    schema_pos = 0
                elif pos < table.pks:
                    kind = "clustering"
                    schema_pos = 0
                else:
                    kind = "regular"
                    schema_pos = -1
                assert c.kind == kind, f"Column {c_name} kind does not match {c.kind} {kind}"
                assert c.position == schema_pos, f"Column {c_name} position {c.position} " \
                                                 f"does not match {schema_pos}"
