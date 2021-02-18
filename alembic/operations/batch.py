from sqlalchemy import CheckConstraint
from sqlalchemy import Column
from sqlalchemy import ForeignKeyConstraint
from sqlalchemy import Index
from sqlalchemy import MetaData
from sqlalchemy import PrimaryKeyConstraint
from sqlalchemy import schema as sql_schema
from sqlalchemy import Table
from sqlalchemy import types as sqltypes
from sqlalchemy.events import SchemaEventTarget
from sqlalchemy.util import OrderedDict
from sqlalchemy.util import topological

from ..util import exc
from ..util.sqla_compat import _columns_for_constraint
from ..util.sqla_compat import _copy
from ..util.sqla_compat import _ensure_scope_for_ddl
from ..util.sqla_compat import _fk_is_self_referential
from ..util.sqla_compat import _insert_inline
from ..util.sqla_compat import _is_type_bound
from ..util.sqla_compat import _remove_column_from_collection
from ..util.sqla_compat import _select


class BatchOperationsImpl(object):
    def __init__(
        self,
        operations,
        table_name,
        schema,
        recreate,
        copy_from,
        table_args,
        table_kwargs,
        reflect_args,
        reflect_kwargs,
        naming_convention,
        partial_reordering,
    ):
        self.operations = operations
        self.table_name = table_name
        self.schema = schema
        if recreate not in ("auto", "always", "never"):
            raise ValueError(
                "recreate may be one of 'auto', 'always', or 'never'."
            )
        self.recreate = recreate
        self.copy_from = copy_from
        self.table_args = table_args
        self.table_kwargs = dict(table_kwargs)
        self.reflect_args = reflect_args
        self.reflect_kwargs = dict(reflect_kwargs)
        self.reflect_kwargs.setdefault(
            "listeners", list(self.reflect_kwargs.get("listeners", ()))
        )
        self.reflect_kwargs["listeners"].append(
            ("column_reflect", operations.impl.autogen_column_reflect)
        )
        self.naming_convention = naming_convention
        self.partial_reordering = partial_reordering
        self.batch = []

    @property
    def dialect(self):
        return self.operations.impl.dialect

    @property
    def impl(self):
        return self.operations.impl

    def _should_recreate(self):
        if self.recreate == "auto":
            return self.operations.impl.requires_recreate_in_batch(self)
        elif self.recreate == "always":
            return True
        else:
            return False

    def flush(self):
        should_recreate = self._should_recreate()

        with _ensure_scope_for_ddl(self.impl.connection):
            if not should_recreate:
                for opname, arg, kw in self.batch:
                    fn = getattr(self.operations.impl, opname)
                    fn(*arg, **kw)
            else:
                if self.naming_convention:
                    m1 = MetaData(naming_convention=self.naming_convention)
                else:
                    m1 = MetaData()

                if self.copy_from is not None:
                    existing_table = self.copy_from
                    reflected = False
                else:
                    existing_table = Table(
                        self.table_name,
                        m1,
                        schema=self.schema,
                        autoload_with=self.operations.get_bind(),
                        *self.reflect_args,
                        **self.reflect_kwargs
                    )
                    reflected = True

                batch_impl = ApplyBatchImpl(
                    self.impl,
                    existing_table,
                    self.table_args,
                    self.table_kwargs,
                    reflected,
                    partial_reordering=self.partial_reordering,
                )
                for opname, arg, kw in self.batch:
                    fn = getattr(batch_impl, opname)
                    fn(*arg, **kw)

                batch_impl._create(self.impl)

    def alter_column(self, *arg, **kw):
        self.batch.append(("alter_column", arg, kw))

    def add_column(self, *arg, **kw):
        if (
            "insert_before" in kw or "insert_after" in kw
        ) and not self._should_recreate():
            raise exc.CommandError(
                "Can't specify insert_before or insert_after when using "
                "ALTER; please specify recreate='always'"
            )
        self.batch.append(("add_column", arg, kw))

    def drop_column(self, *arg, **kw):
        self.batch.append(("drop_column", arg, kw))

    def add_constraint(self, const):
        self.batch.append(("add_constraint", (const,), {}))

    def drop_constraint(self, const):
        self.batch.append(("drop_constraint", (const,), {}))

    def rename_table(self, *arg, **kw):
        self.batch.append(("rename_table", arg, kw))

    def create_index(self, idx):
        self.batch.append(("create_index", (idx,), {}))

    def drop_index(self, idx):
        self.batch.append(("drop_index", (idx,), {}))

    def create_table(self, table):
        raise NotImplementedError("Can't create table in batch mode")

    def drop_table(self, table):
        raise NotImplementedError("Can't drop table in batch mode")

    def create_column_comment(self, column):
        self.batch.append(("create_column_comment", (column,), {}))


class ApplyBatchImpl(object):
    def __init__(
        self,
        impl,
        table,
        table_args,
        table_kwargs,
        reflected,
        partial_reordering=(),
    ):
        self.impl = impl
        self.table = table  # this is a Table object
        self.table_args = table_args
        self.table_kwargs = table_kwargs
        self.temp_table_name = self._calc_temp_name(table.name)
        self.new_table = None

        self.partial_reordering = partial_reordering  # tuple of tuples
        self.add_col_ordering = ()  # tuple of tuples

        self.column_transfers = OrderedDict(
            (c.name, {"expr": c}) for c in self.table.c
        )
        self.existing_ordering = list(self.column_transfers)

        self.reflected = reflected
        self._grab_table_elements()

    @classmethod
    def _calc_temp_name(cls, tablename):
        return ("_alembic_tmp_%s" % tablename)[0:50]

    def _grab_table_elements(self):
        schema = self.table.schema
        self.columns = OrderedDict()
        for c in self.table.c:
            c_copy = _copy(c, schema=schema)
            c_copy.unique = c_copy.index = False
            # ensure that the type object was copied,
            # as we may need to modify it in-place
            if isinstance(c.type, SchemaEventTarget):
                assert c_copy.type is not c.type
            self.columns[c.name] = c_copy
        self.named_constraints = {}
        self.unnamed_constraints = []
        self.col_named_constraints = {}
        self.indexes = {}
        self.new_indexes = {}

        for const in self.table.constraints:
            if _is_type_bound(const):
                continue
            elif self.reflected and isinstance(const, CheckConstraint):
                # TODO: we are skipping reflected CheckConstraint because
                # we have no way to determine _is_type_bound() for these.
                pass
            elif const.name:
                self.named_constraints[const.name] = const
            else:
                self.unnamed_constraints.append(const)

        if not self.reflected:
            for col in self.table.c:
                for const in col.constraints:
                    if const.name:
                        self.col_named_constraints[const.name] = (col, const)

        for idx in self.table.indexes:
            self.indexes[idx.name] = idx

        for k in self.table.kwargs:
            self.table_kwargs.setdefault(k, self.table.kwargs[k])

    def _adjust_self_columns_for_partial_reordering(self):
        pairs = set()

        col_by_idx = list(self.columns)

        if self.partial_reordering:
            for tuple_ in self.partial_reordering:
                for index, elem in enumerate(tuple_):
                    if index > 0:
                        pairs.add((tuple_[index - 1], elem))
        else:
            for index, elem in enumerate(self.existing_ordering):
                if index > 0:
                    pairs.add((col_by_idx[index - 1], elem))

        pairs.update(self.add_col_ordering)

        # this can happen if some columns were dropped and not removed
        # from existing_ordering.  this should be prevented already, but
        # conservatively making sure this didn't happen
        pairs = [p for p in pairs if p[0] != p[1]]

        sorted_ = list(
            topological.sort(pairs, col_by_idx, deterministic_order=True)
        )
        self.columns = OrderedDict((k, self.columns[k]) for k in sorted_)
        self.column_transfers = OrderedDict(
            (k, self.column_transfers[k]) for k in sorted_
        )

    def _transfer_elements_to_new_table(self):
        assert self.new_table is None, "Can only create new table once"

        m = MetaData()
        schema = self.table.schema

        if self.partial_reordering or self.add_col_ordering:
            self._adjust_self_columns_for_partial_reordering()

        self.new_table = new_table = Table(
            self.temp_table_name,
            m,
            *(list(self.columns.values()) + list(self.table_args)),
            schema=schema,
            **self.table_kwargs
        )

        for const in (
            list(self.named_constraints.values()) + self.unnamed_constraints
        ):

            const_columns = set(
                [c.key for c in _columns_for_constraint(const)]
            )

            if not const_columns.issubset(self.column_transfers):
                continue

            if isinstance(const, ForeignKeyConstraint):
                if _fk_is_self_referential(const):
                    # for self-referential constraint, refer to the
                    # *original* table name, and not _alembic_batch_temp.
                    # This is consistent with how we're handling
                    # FK constraints from other tables; we assume SQLite
                    # no foreign keys just keeps the names unchanged, so
                    # when we rename back, they match again.
                    const_copy = _copy(
                        const, schema=schema, target_table=self.table
                    )
                else:
                    # "target_table" for ForeignKeyConstraint.copy() is
                    # only used if the FK is detected as being
                    # self-referential, which we are handling above.
                    const_copy = _copy(const, schema=schema)
            else:
                const_copy = _copy(
                    const, schema=schema, target_table=new_table
                )
            if isinstance(const, ForeignKeyConstraint):
                self._setup_referent(m, const)
            new_table.append_constraint(const_copy)

    def _gather_indexes_from_both_tables(self):
        idx = []
        idx.extend(self.indexes.values())
        for index in self.new_indexes.values():
            idx.append(
                Index(
                    index.name,
                    unique=index.unique,
                    *[self.new_table.c[col] for col in index.columns.keys()],
                    **index.kwargs
                )
            )
        return idx

    def _setup_referent(self, metadata, constraint):
        spec = constraint.elements[0]._get_colspec()
        parts = spec.split(".")
        tname = parts[-2]
        if len(parts) == 3:
            referent_schema = parts[0]
        else:
            referent_schema = None

        if tname != self.temp_table_name:
            key = sql_schema._get_table_key(tname, referent_schema)
            if key in metadata.tables:
                t = metadata.tables[key]
                for elem in constraint.elements:
                    colname = elem._get_colspec().split(".")[-1]
                    if colname not in t.c:
                        t.append_column(Column(colname, sqltypes.NULLTYPE))
            else:
                Table(
                    tname,
                    metadata,
                    *[
                        Column(n, sqltypes.NULLTYPE)
                        for n in [
                            elem._get_colspec().split(".")[-1]
                            for elem in constraint.elements
                        ]
                    ],
                    schema=referent_schema
                )

    def _create(self, op_impl):
        self._transfer_elements_to_new_table()

        op_impl.prep_table_for_batch(self, self.table)
        op_impl.create_table(self.new_table)

        try:
            op_impl._exec(
                _insert_inline(self.new_table).from_select(
                    list(
                        k
                        for k, transfer in self.column_transfers.items()
                        if "expr" in transfer
                    ),
                    _select(
                        *[
                            transfer["expr"]
                            for transfer in self.column_transfers.values()
                            if "expr" in transfer
                        ]
                    ),
                )
            )
            op_impl.drop_table(self.table)
        except:
            op_impl.drop_table(self.new_table)
            raise
        else:
            op_impl.rename_table(
                self.temp_table_name, self.table.name, schema=self.table.schema
            )
            self.new_table.name = self.table.name
            try:
                for idx in self._gather_indexes_from_both_tables():
                    op_impl.create_index(idx)
            finally:
                self.new_table.name = self.temp_table_name

    def alter_column(
        self,
        table_name,
        column_name,
        nullable=None,
        server_default=False,
        name=None,
        type_=None,
        autoincrement=None,
        comment=False,
        **kw
    ):
        existing = self.columns[column_name]
        existing_transfer = self.column_transfers[column_name]
        if name is not None and name != column_name:
            # note that we don't change '.key' - we keep referring
            # to the renamed column by its old key in _create().  neat!
            existing.name = name
            existing_transfer["name"] = name

        if type_ is not None:
            type_ = sqltypes.to_instance(type_)
            # old type is being discarded so turn off eventing
            # rules. Alternatively we can
            # erase the events set up by this type, but this is simpler.
            # we also ignore the drop_constraint that will come here from
            # Operations.implementation_for(alter_column)
            if isinstance(existing.type, SchemaEventTarget):
                existing.type._create_events = (
                    existing.type.create_constraint
                ) = False

            self.impl.cast_for_batch_migrate(
                existing, existing_transfer, type_
            )

            existing.type = type_

            # we *dont* however set events for the new type, because
            # alter_column is invoked from
            # Operations.implementation_for(alter_column) which already
            # will emit an add_constraint()

        if nullable is not None:
            existing.nullable = nullable
        if server_default is not False:
            if server_default is None:
                existing.server_default = None
            else:
                sql_schema.DefaultClause(server_default)._set_parent(existing)
        if autoincrement is not None:
            existing.autoincrement = bool(autoincrement)

        if comment is not False:
            existing.comment = comment

    def _setup_dependencies_for_add_column(
        self, colname, insert_before, insert_after
    ):
        index_cols = self.existing_ordering
        col_indexes = {name: i for i, name in enumerate(index_cols)}

        if not self.partial_reordering:
            if insert_after:
                if not insert_before:
                    if insert_after in col_indexes:
                        # insert after an existing column
                        idx = col_indexes[insert_after] + 1
                        if idx < len(index_cols):
                            insert_before = index_cols[idx]
                    else:
                        # insert after a column that is also new
                        insert_before = dict(self.add_col_ordering)[
                            insert_after
                        ]
            if insert_before:
                if not insert_after:
                    if insert_before in col_indexes:
                        # insert before an existing column
                        idx = col_indexes[insert_before] - 1
                        if idx >= 0:
                            insert_after = index_cols[idx]
                    else:
                        # insert before a column that is also new
                        insert_after = dict(
                            (b, a) for a, b in self.add_col_ordering
                        )[insert_before]

        if insert_before:
            self.add_col_ordering += ((colname, insert_before),)
        if insert_after:
            self.add_col_ordering += ((insert_after, colname),)

        if (
            not self.partial_reordering
            and not insert_before
            and not insert_after
            and col_indexes
        ):
            self.add_col_ordering += ((index_cols[-1], colname),)

    def add_column(
        self, table_name, column, insert_before=None, insert_after=None, **kw
    ):
        self._setup_dependencies_for_add_column(
            column.name, insert_before, insert_after
        )
        # we copy the column because operations.add_column()
        # gives us a Column that is part of a Table already.
        self.columns[column.name] = _copy(column, schema=self.table.schema)
        self.column_transfers[column.name] = {}

    def drop_column(self, table_name, column, **kw):
        if column.name in self.table.primary_key.columns:
            _remove_column_from_collection(
                self.table.primary_key.columns, column
            )
        del self.columns[column.name]
        del self.column_transfers[column.name]
        self.existing_ordering.remove(column.name)

    def create_column_comment(self, column):
        """the batch table creation function will issue create_column_comment
        on the real "impl" as part of the create table process.

        That is, the Column object will have the comment on it already,
        so when it is received by add_column() it will be a normal part of
        the CREATE TABLE and doesn't need an extra step here.

        """

    def add_constraint(self, const):
        if not const.name:
            raise ValueError("Constraint must have a name")
        if isinstance(const, sql_schema.PrimaryKeyConstraint):
            if self.table.primary_key in self.unnamed_constraints:
                self.unnamed_constraints.remove(self.table.primary_key)

        self.named_constraints[const.name] = const

    def drop_constraint(self, const):
        if not const.name:
            raise ValueError("Constraint must have a name")
        try:
            if const.name in self.col_named_constraints:
                col, const = self.col_named_constraints.pop(const.name)

                for col_const in list(self.columns[col.name].constraints):
                    if col_const.name == const.name:
                        self.columns[col.name].constraints.remove(col_const)
            else:
                const = self.named_constraints.pop(const.name)
        except KeyError:
            if _is_type_bound(const):
                # type-bound constraints are only included in the new
                # table via their type object in any case, so ignore the
                # drop_constraint() that comes here via the
                # Operations.implementation_for(alter_column)
                return
            raise ValueError("No such constraint: '%s'" % const.name)
        else:
            if isinstance(const, PrimaryKeyConstraint):
                for col in const.columns:
                    self.columns[col.name].primary_key = False

    def create_index(self, idx):
        self.new_indexes[idx.name] = idx

    def drop_index(self, idx):
        try:
            del self.indexes[idx.name]
        except KeyError:
            raise ValueError("No such index: '%s'" % idx.name)

    def rename_table(self, *arg, **kw):
        raise NotImplementedError("TODO")
