from typing import Dict, Any, List

from posthog.hogql.database.models import StringDatabaseField, IntegerDatabaseField, Table, LazyJoin, LazyTable
from posthog.hogql.database.schema.persons import PersonsTable, join_with_persons_table


def select_from_cohort_people_table(requested_fields: Dict[str, List[str]]):
    from posthog.hogql import ast

    table_name = "raw_cohort_people"

    requested_fields = {"person_id": ["person_id"], "cohort_id": ["cohort_id"], **requested_fields}

    fields: List[ast.Expr] = [
        ast.Alias(alias=name, expr=ast.Field(chain=[table_name] + chain)) for name, chain in requested_fields.items()
    ]

    return ast.SelectQuery(
        select=fields,
        select_from=ast.JoinExpr(table=ast.Field(chain=[table_name])),
        group_by=fields,
        having=ast.CompareOperation(
            op=ast.CompareOperationOp.Gt,
            left=ast.Call(name="sum", args=[ast.Field(chain=[table_name, "sign"])]),
            right=ast.Constant(value=0),
        ),
    )


class RawCohortPeople(Table):
    person_id: StringDatabaseField = StringDatabaseField(name="person_id")
    cohort_id: IntegerDatabaseField = IntegerDatabaseField(name="cohort_id")
    team_id: IntegerDatabaseField = IntegerDatabaseField(name="team_id")
    sign: IntegerDatabaseField = IntegerDatabaseField(name="sign")
    version: IntegerDatabaseField = IntegerDatabaseField(name="version")

    person: LazyJoin = LazyJoin(
        from_field="person_id", join_table=PersonsTable(), join_function=join_with_persons_table
    )

    def clickhouse_table(self):
        return "cohortpeople"

    def hogql_table(self):
        return "cohort_people"


class CohortPeople(LazyTable):
    person_id: StringDatabaseField = StringDatabaseField(name="person_id")
    cohort_id: IntegerDatabaseField = IntegerDatabaseField(name="cohort_id")
    team_id: IntegerDatabaseField = IntegerDatabaseField(name="team_id")

    person: LazyJoin = LazyJoin(
        from_field="person_id", join_table=PersonsTable(), join_function=join_with_persons_table
    )

    def lazy_select(self, requested_fields: Dict[str, Any]):
        return select_from_cohort_people_table(requested_fields)

    def clickhouse_table(self):
        return "cohortpeople"

    def hogql_table(self):
        return "cohort_people"
