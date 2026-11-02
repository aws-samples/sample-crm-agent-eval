# SPDX-License-Identifier: MIT
"""DynamoDB access: entity-per-table persistence, tenant-prefixed GSI queries, and scoped teardown.

Every DynamoDB item read and write in the application goes through :class:`Store`. AWS errors are
translated to :mod:`crm.errors` members by :func:`crm.errors.wrap_aws_error`.

* **Idempotent writes.** :meth:`Store.put_idempotent` treats a retry with the same
  ``transaction_key`` and payload as a no-op and refuses a different payload with :class:`Conflict`;
  :meth:`Store.transact_write` derives its ``ClientRequestToken`` from the key
  (:func:`client_request_token`).
* **Derived index attributes.** Every ``Put`` recomputes the attributes
  :data:`crm.store.keys.DERIVED_ATTRIBUTES` declares; an ``Update`` (transactional or
  :meth:`Store.update_item`) that assigns a derived attribute's source without the derived
  attribute raises ``ValueError``.
* **Conditional writes.** :meth:`Store.update_item` updates an existing item only (unless
  ``create_if_absent``) and raises :class:`Conflict` when its condition fails;
  :meth:`Store.put_if_absent` reports an existing item as ``False``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import time
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextvars import ContextVar
from decimal import Decimal
from typing import Any, Final

import boto3
from botocore.exceptions import ClientError

from crm.errors import Conflict, CrmError, ServiceBusy, ValidationFailed, wrap_aws_error
from crm.store.keys import derive, derive_encoded, derived_attributes
from crm.store.schema import TABLE_NAMES, TENANT_SCOPED_TABLES, table_schema

#: The fixed UUIDv5 namespace :func:`client_request_token` derives every token in.
CLIENT_REQUEST_TOKEN_NAMESPACE: Final[uuid.UUID] = uuid.UUID("6f9b2f4e-4d1a-5c3e-9a7b-2e8d1c0f5a63")


#: Joins a token's UUIDv5 name parts: a control character no tenant, rep id or tool carries.
_SCOPE_SEPARATOR: Final[str] = "\x1f"


def client_request_token(transaction_key: str, scope: Sequence[str] = ()) -> str:
    """The 36-character ``ClientRequestToken`` for ``transaction_key`` within ``scope``.

    A UUIDv5 over ``scope`` (``(tenant, rep_id, tool)`` for a handler's store) and the key. The
    same inputs always yield the same token; a different key, tenant, rep or tool yields a
    different one, so two callers reusing a key never collide.
    """
    name = _SCOPE_SEPARATOR.join((*scope, transaction_key))
    return str(uuid.uuid5(CLIENT_REQUEST_TOKEN_NAMESPACE, name))


#: The ``(tenant, rep_id, tool)`` scope :meth:`Store.transact_write` derives its token within.
_TOKEN_SCOPE: ContextVar[tuple[str, ...]] = ContextVar("crm_store_token_scope", default=())


@contextlib.contextmanager
def token_scope(tenant: str, rep_id: str, tool: str) -> Iterator[None]:
    """Scope every :meth:`Store.transact_write` token in this context to ``(tenant, rep_id,
    tool)``; outside any such context the token is a function of the key alone."""
    reset = _TOKEN_SCOPE.set((tenant, rep_id, tool))
    try:
        yield
    finally:
        _TOKEN_SCOPE.reset(reset)


#: The ``boto3`` DynamoDB low-level client type (untyped: no ``mypy-boto3-dynamodb`` stubs).
DynamoDBClient = Any

#: Attributes recording a :meth:`Store.put_idempotent` write's key and payload fingerprint.
_IDEMPOTENCY_ATTR: Final[str] = "_idempotency_key"
_IDEMPOTENCY_FINGERPRINT_ATTR: Final[str] = "_idempotency_fingerprint"

#: DynamoDB's per-request ceiling for items per ``BatchWriteItem``.
_BATCH_WRITE_CHUNK_SIZE: Final[int] = 25
#: DynamoDB's per-request ceiling for ``BatchGetItem`` keys.
_BATCH_GET_CHUNK_SIZE: Final[int] = 100
#: ``BatchWriteItem`` attempts per :meth:`Store.batch_put_items` chunk before unprocessed items
#: raise; the delay before each re-submission doubles from the base.
_BATCH_PUT_MAX_ATTEMPTS: Final[int] = 6
_BATCH_PUT_BACKOFF_BASE_SECONDS: Final[float] = 0.05


def _fingerprint(payload: Mapping[str, object]) -> str:
    """Return a stable digest of a write payload, for idempotency-key replay comparison."""
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Store:
    """Entity-per-table DynamoDB access, tenant-prefixed GSIs, transactional and idempotent writes.

    A logical table name outside :data:`crm.store.schema.TABLE_NAMES` raises ``ValueError``.

    Args:
        table_prefix: Prefix applied to every table name to form the physical DynamoDB table name.
        client: An existing ``boto3`` DynamoDB client. When omitted, one is constructed from the
            ambient AWS configuration.
    """

    def __init__(self, *, table_prefix: str = "", client: DynamoDBClient | None = None) -> None:
        self._table_prefix = table_prefix
        self._client: DynamoDBClient = client if client is not None else boto3.client("dynamodb")

    def _physical_name(self, table: str) -> str:
        if table not in TABLE_NAMES:
            raise ValueError(f"unrecognised table {table!r}; expected one of {sorted(TABLE_NAMES)}")
        return f"{self._table_prefix}{table}"

    def physical_name(self, table: str) -> str:
        """Return the prefixed physical DynamoDB table name for a logical table name.

        Raises:
            ValueError: ``table`` is not one of :data:`crm.store.schema.TABLE_NAMES`.
        """
        return self._physical_name(table)

    # -- single-item operations -------------------------------------------------------------

    def get_item(
        self, table: str, key: Mapping[str, str], *, consistent: bool = False
    ) -> dict[str, object] | None:
        """Return the item at ``key`` in ``table``, or ``None`` if absent.

        Args:
            table: One of :data:`crm.store.schema.TABLE_NAMES`.
            key: The table's primary key attribute(s) and their values.
            consistent: Issue a strongly consistent read (``ConsistentRead``).
        """
        schema = table_schema(table)
        try:
            response = self._client.get_item(
                TableName=self._physical_name(table),
                Key=_encode_key(schema.partition_key, schema.sort_key, key),
                ConsistentRead=consistent,
            )
        except ClientError as exc:
            raise wrap_aws_error(exc, operation=f"get_item:{table}") from exc
        item = response.get("Item")
        return _decode_item(item) if item is not None else None

    def batch_get_items(
        self, table: str, keys: Sequence[Mapping[str, str]]
    ) -> list[dict[str, object]]:
        """Return the full base-table items at ``keys``, in no guaranteed order.

        Absent keys are omitted. Unprocessed keys are re-requested; a request making no progress
        raises :class:`crm.errors.CrmError`.

        Args:
            table: One of :data:`crm.store.schema.TABLE_NAMES`.
            keys: Primary keys to fetch. An empty sequence returns ``[]`` without an AWS call.
        """
        if not keys:
            return []
        schema = table_schema(table)
        physical = self._physical_name(table)
        encoded = [_encode_key(schema.partition_key, schema.sort_key, key) for key in keys]
        items: list[dict[str, object]] = []
        # BatchGetItem reports throttled keys as UnprocessedKeys rather than raising.
        for start in range(0, len(encoded), _BATCH_GET_CHUNK_SIZE):
            pending = encoded[start : start + _BATCH_GET_CHUNK_SIZE]
            while pending:
                try:
                    response = self._client.batch_get_item(
                        RequestItems={physical: {"Keys": pending}}
                    )
                except ClientError as exc:
                    raise wrap_aws_error(exc, operation=f"batch_get_items:{table}") from exc
                for raw in response.get("Responses", {}).get(physical, []):
                    items.append(_decode_item(raw))
                unprocessed = response.get("UnprocessedKeys", {}).get(physical, {}).get("Keys", [])
                if len(unprocessed) == len(pending):
                    raise CrmError(
                        f"batch_get_items:{table} made no progress: all {len(pending)} key(s) came "
                        "back unprocessed. Refusing to retry an identical request indefinitely."
                    )
                pending = list(unprocessed)
        return items

    def put_item(self, table: str, item: Mapping[str, object]) -> None:
        """Unconditionally write ``item`` into ``table``, overwriting any existing item at its key.

        Derived index attributes are recomputed by :func:`crm.store.keys.derive` before the write.
        """
        try:
            self._client.put_item(
                TableName=self._physical_name(table), Item=_encode_item(derive(table, item))
            )
        except ClientError as exc:
            raise wrap_aws_error(exc, operation=f"put_item:{table}") from exc

    def batch_put_items(self, table: str, items: Sequence[Mapping[str, object]]) -> int:
        """Unconditionally write every item into ``table`` through ``BatchWriteItem``.

        Each item gets :func:`crm.store.keys.derive` applied, as :meth:`put_item` does, and
        overwrites any existing item at its key. Items are sent 25 per request; unprocessed items
        are re-submitted with bounded exponential backoff.

        Returns:
            The number of items written.

        Raises:
            ValueError: Two items share a primary key, or an item lacks a primary-key attribute.
            ServiceBusy: Items were still unprocessed after the last attempt.
        """
        schema = table_schema(table)
        physical = self._physical_name(table)
        key_attrs = [schema.partition_key] + ([schema.sort_key] if schema.sort_key else [])
        requests: list[dict[str, object]] = []
        seen: set[tuple[str, ...]] = set()
        for item in items:
            derived = derive(table, item)
            missing = [attr for attr in key_attrs if derived.get(attr) is None]
            if missing:
                raise ValueError(
                    f"batch_put_items:{table}: an item lacks key attribute(s) {missing}"
                )
            key = tuple(str(derived[attr]) for attr in key_attrs)
            if key in seen:
                raise ValueError(f"batch_put_items:{table}: duplicate primary key {key!r}")
            seen.add(key)
            requests.append({"PutRequest": {"Item": _encode_item(derived)}})
        for start in range(0, len(requests), _BATCH_WRITE_CHUNK_SIZE):
            self._batch_write_until_processed(
                table, {physical: requests[start : start + _BATCH_WRITE_CHUNK_SIZE]}
            )
        return len(requests)

    def _batch_write_until_processed(self, table: str, request_items: dict[str, Any]) -> None:
        pending = request_items
        for attempt in range(_BATCH_PUT_MAX_ATTEMPTS):
            if attempt:
                time.sleep(_BATCH_PUT_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
            try:
                response = self._client.batch_write_item(RequestItems=pending)
            except ClientError as exc:
                raise wrap_aws_error(exc, operation=f"batch_put_items:{table}") from exc
            pending = response.get("UnprocessedItems") or {}
            if not pending:
                return
        unprocessed = sum(len(entries) for entries in pending.values())
        raise ServiceBusy(
            "The data store is busy and could not accept every write; retry the operation.",
            detail=(
                f"batch_put_items:{table}: {unprocessed} item(s) unprocessed after "
                f"{_BATCH_PUT_MAX_ATTEMPTS} attempts"
            ),
        )

    def put_idempotent(
        self, table: str, item: Mapping[str, object], *, transaction_key: str
    ) -> bool:
        """Write ``item`` under ``transaction_key``, idempotent under retry.

        Derived index attributes are recomputed before the payload is fingerprinted.

        Args:
            table: One of :data:`crm.store.schema.TABLE_NAMES`.
            item: The full item to write, including its primary key attribute(s).
            transaction_key: Caller-supplied idempotency token.

        Returns:
            ``True`` if this call performed the write, ``False`` if an identical write already
            existed under this key.

        Raises:
            ValidationFailed: ``transaction_key`` is empty.
            Conflict: If ``transaction_key`` was already used for a payload with a different
                content fingerprint.
        """
        if not transaction_key:
            raise ValidationFailed(
                "A transaction key is required for an idempotent write.", source="transaction_key"
            )
        item = derive(table, item)
        schema = table_schema(table)
        key = {schema.partition_key: item[schema.partition_key]}
        if schema.sort_key is not None:
            key[schema.sort_key] = item[schema.sort_key]

        existing = self.get_item(table, {k: str(v) for k, v in key.items()})
        fingerprint = _fingerprint(item)
        if existing is not None:
            existing_key = existing.get(_IDEMPOTENCY_ATTR)
            if existing_key == transaction_key:
                if existing.get(_IDEMPOTENCY_FINGERPRINT_ATTR) == fingerprint:
                    return False
                raise Conflict(
                    "This transaction key was already used for a different payload.",
                    detail=f"table={table} transaction_key={transaction_key}",
                )

        stamped = {
            **item,
            _IDEMPOTENCY_ATTR: transaction_key,
            _IDEMPOTENCY_FINGERPRINT_ATTR: fingerprint,
        }
        self.put_item(table, stamped)
        return True

    def delete_item(self, table: str, key: Mapping[str, str]) -> None:
        """Unconditionally delete the item at ``key`` in ``table``."""
        schema = table_schema(table)
        try:
            self._client.delete_item(
                TableName=self._physical_name(table),
                Key=_encode_key(schema.partition_key, schema.sort_key, key),
            )
        except ClientError as exc:
            raise wrap_aws_error(exc, operation=f"delete_item:{table}") from exc

    def put_if_absent(self, table: str, item: Mapping[str, object]) -> bool:
        """Write ``item`` only if no item exists at its primary key.

        Derived index attributes are recomputed before the write.

        Returns:
            ``True`` if this call created the item, ``False`` if an item already existed.
        """
        schema = table_schema(table)
        try:
            self._client.put_item(
                TableName=self._physical_name(table),
                Item=_encode_item(derive(table, item)),
                ConditionExpression="attribute_not_exists(#pk)",
                ExpressionAttributeNames={"#pk": schema.partition_key},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise wrap_aws_error(exc, operation=f"put_if_absent:{table}") from exc
        return True

    def update_item(
        self,
        table: str,
        key: Mapping[str, str],
        *,
        set_values: Mapping[str, object],
        add_values: Mapping[str, int | float | Decimal] | None = None,
        condition: str | None,
        names: Mapping[str, str] | None = None,
        values: Mapping[str, object] | None = None,
        create_if_absent: bool = False,
    ) -> dict[str, object]:
        """Apply a conditional ``SET``/``ADD`` update to the item at ``key`` and return it.

        Attribute names in ``set_values`` and ``add_values`` are bound to generated ``#_s<n>`` /
        ``#_a<n>`` placeholders. ``condition`` may reference only the caller's ``names`` and
        ``values``; any ``#_``/``:_`` token in it, or in their keys, is refused.

        Args:
            table: One of :data:`crm.store.schema.TABLE_NAMES`.
            key: The table's primary key attribute(s) and their values.
            set_values: Attribute name to plain value, assigned with ``SET``.
            add_values: Attribute name to number, incremented with ``ADD``.
            condition: ``ConditionExpression``, or ``None`` for no condition beyond existence.
            names: ``ExpressionAttributeNames`` the condition uses.
            values: Plain values for the condition's ``:placeholders``, encoded here.
            create_if_absent: When ``False`` (the default) the item must already exist —
                ``attribute_exists`` on the partition key is ANDed with ``condition``. When
                ``True`` an absent item is created from ``key`` and the assigned attributes.

        Returns:
            The whole item after the update (``ReturnValues=ALL_NEW``), decoded.

        Raises:
            ValueError: No attribute is assigned, an attribute is both set and added, a key
                attribute is assigned, an ``ADD`` amount is not a number, a reserved ``#_``/``:_``
                placeholder appears in ``names``, ``values`` or ``condition``, ``names`` or
                ``values`` is given without ``condition``, a derived attribute's source is
                assigned without the derived attribute, or a derived attribute is assigned a value
                other than the one its assigned sources build.
            Conflict: The condition failed; ``reason="target_missing"`` when no item exists at
                ``key``, otherwise ``reason`` is ``None``.
        """
        schema = table_schema(table)
        add_values = add_values or {}
        names = names or {}
        values = values or {}
        _validate_update_arguments(schema.partition_key, schema.sort_key, set_values, add_values)
        if condition is None and (names or values):
            raise ValueError("names and values are only accepted together with a condition")
        reserved = sorted(p for p in (*names, *values) if p.startswith(("#_", ":_")))
        reserved += sorted(set(_RESERVED_PLACEHOLDER_RE.findall(condition or "")))
        if reserved:
            raise ValueError(f"placeholders {reserved} use the reserved '#_'/':_' prefix")
        _reject_mismatched_derived_values(table, set_values)

        expression_names: dict[str, str] = dict(names)
        expression_values: dict[str, object] = {p: _encode_value(v) for p, v in values.items()}
        clauses: list[str] = []
        if set_values:
            assignments = []
            for index, (attribute, value) in enumerate(set_values.items()):
                expression_names[f"#_s{index}"] = attribute
                expression_values[f":_s{index}"] = _encode_value(value)
                assignments.append(f"#_s{index} = :_s{index}")
            clauses.append("SET " + ", ".join(assignments))
        if add_values:
            increments = []
            for index, (attribute, amount) in enumerate(add_values.items()):
                expression_names[f"#_a{index}"] = attribute
                expression_values[f":_a{index}"] = _encode_value(amount)
                increments.append(f"#_a{index} :_a{index}")
            clauses.append("ADD " + ", ".join(increments))
        update_expression = " ".join(clauses)
        _reject_stale_derived_update(
            table,
            {"UpdateExpression": update_expression, "ExpressionAttributeNames": expression_names},
        )

        effective_condition = condition
        if not create_if_absent:
            expression_names["#_pk"] = schema.partition_key
            exists = "attribute_exists(#_pk)"
            effective_condition = exists if condition is None else f"{exists} AND ({condition})"

        kwargs: dict[str, object] = {
            "TableName": self._physical_name(table),
            "Key": _encode_key(schema.partition_key, schema.sort_key, key),
            "UpdateExpression": update_expression,
            "ExpressionAttributeNames": expression_names,
            "ExpressionAttributeValues": expression_values,
            "ReturnValues": "ALL_NEW",
        }
        if effective_condition is not None:
            kwargs["ConditionExpression"] = effective_condition
            kwargs["ReturnValuesOnConditionCheckFailure"] = "ALL_OLD"
        try:
            response = self._client.update_item(**kwargs)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise _condition_conflict(table, exc) from exc
            raise wrap_aws_error(exc, operation=f"update_item:{table}") from exc
        return _decode_item(response["Attributes"])

    # -- queries -----------------------------------------------------------------------------

    def query_gsi(
        self,
        table: str,
        gsi_name: str,
        *,
        partition_value: str,
        sort_key_prefix: str | None = None,
        limit: int | None = None,
        exclusive_start_key: Mapping[str, object] | None = None,
        scan_forward: bool = True,
    ) -> tuple[list[dict[str, object]], dict[str, object] | None]:
        """Query one GSI by its partition key value.

        Args:
            table: One of :data:`crm.store.schema.TABLE_NAMES`.
            gsi_name: Name of a GSI declared on ``table`` in :mod:`crm.store.schema`.
            partition_value: The index partition-key value; for a tenant-scoped table, the tenant or
                a :func:`crm.store.keys.tenant_prefixed` composite.
            sort_key_prefix: Optional ``begins_with`` condition on the index sort key.
            limit: Optional page size cap, forwarded to DynamoDB's own ``Limit``.
            exclusive_start_key: Optional pagination cursor from a prior call's returned cursor.
            scan_forward: Index sort-key order; ``False`` returns items in descending order.

        Returns:
            A tuple of ``(items, next_cursor)``. ``next_cursor`` is ``None`` when the query
            exhausted the index; otherwise it is the raw ``LastEvaluatedKey`` to pass back as
            ``exclusive_start_key``.
        """
        schema = table_schema(table)
        gsi = schema.gsi(gsi_name)

        condition = "#pk = :pk"
        names = {"#pk": gsi.partition_key}
        values: dict[str, object] = {":pk": {"S": partition_value}}
        if sort_key_prefix is not None and gsi.sort_key is not None:
            condition += " AND begins_with(#sk, :sk)"
            names["#sk"] = gsi.sort_key
            values[":sk"] = {"S": sort_key_prefix}

        kwargs: dict[str, object] = {
            "TableName": self._physical_name(table),
            "IndexName": gsi_name,
            "KeyConditionExpression": condition,
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
            "ScanIndexForward": scan_forward,
        }
        if limit is not None:
            kwargs["Limit"] = limit
        if exclusive_start_key is not None:
            kwargs["ExclusiveStartKey"] = exclusive_start_key

        try:
            response = self._client.query(**kwargs)
        except ClientError as exc:
            raise wrap_aws_error(exc, operation=f"query_gsi:{table}.{gsi_name}") from exc

        items = [_decode_item(raw) for raw in response.get("Items", [])]
        next_cursor = response.get("LastEvaluatedKey")
        return items, (dict(next_cursor) if next_cursor else None)

    def query_tenant_scan(
        self,
        table: str,
        *,
        tenant: str,
        limit: int | None = None,
        exclusive_start_key: Mapping[str, object] | None = None,
        scan_forward: bool = True,
    ) -> tuple[list[dict[str, object]], dict[str, object] | None]:
        """Query the ``gsi_tenant`` index for every item under ``tenant`` in ``table``.

        ``scan_forward=False`` returns items in descending ``gsi_tenant`` sort-key order, and a
        returned cursor continues in that order.

        Raises ``ValueError`` for a table not in :data:`crm.store.schema.TENANT_SCOPED_TABLES`.
        """
        if table not in TENANT_SCOPED_TABLES:
            raise ValueError(f"table {table!r} has no tenant-wide scan index")
        return self.query_gsi(
            table,
            "gsi_tenant",
            partition_value=tenant,
            limit=limit,
            exclusive_start_key=exclusive_start_key,
            scan_forward=scan_forward,
        )

    def count_tenant(self, table: str, *, tenant: str) -> int:
        """Count every item under ``tenant`` in ``table`` with ``Select="COUNT"`` on ``gsi_tenant``.

        Pages until the index is exhausted and sums each page's ``Count``; no item attribute is
        returned. Raises ``ValueError`` for a table not in
        :data:`crm.store.schema.TENANT_SCOPED_TABLES`.
        """
        if table not in TENANT_SCOPED_TABLES:
            raise ValueError(f"table {table!r} has no tenant-wide scan index")
        gsi = table_schema(table).gsi("gsi_tenant")
        kwargs: dict[str, object] = {
            "TableName": self._physical_name(table),
            "IndexName": gsi.name,
            "KeyConditionExpression": "#pk = :pk",
            "ExpressionAttributeNames": {"#pk": gsi.partition_key},
            "ExpressionAttributeValues": {":pk": {"S": tenant}},
            "Select": "COUNT",
        }
        total = 0
        while True:
            try:
                response = self._client.query(**kwargs)
            except ClientError as exc:
                raise wrap_aws_error(exc, operation=f"count_tenant:{table}") from exc
            total += int(response["Count"])
            next_cursor = response.get("LastEvaluatedKey")
            if not next_cursor:
                return total
            kwargs["ExclusiveStartKey"] = next_cursor

    def scan_key_prefix(
        self, table: str, key_attribute: str, prefix: str
    ) -> Iterator[dict[str, object]]:
        """Yield every item in ``table`` whose ``key_attribute`` begins with ``prefix``.

        A paginated, strongly consistent ``Scan`` with a ``begins_with`` filter, which reads the
        whole table. Items are decoded exactly: a non-integer number is a ``Decimal``, and string,
        number and binary sets and binary values (``SS``/``NS``/``BS``/``B``) are decoded too, so a
        yielded value re-encodes to the stored one.

        Args:
            table: One of :data:`crm.store.schema.TABLE_NAMES`.
            key_attribute: The table's partition or sort key attribute.
            prefix: The non-empty key prefix to match.

        Raises:
            ValueError: ``key_attribute`` is not one of the table's key attributes, or ``prefix``
                is empty.
            CrmError: A taxonomy member (:func:`crm.errors.wrap_aws_error`) for a failed page.
        """
        schema = table_schema(table)
        if key_attribute not in (schema.partition_key, schema.sort_key):
            raise ValueError(f"{key_attribute!r} is not a key attribute of table {table!r}")
        if not prefix:
            raise ValueError("scan_key_prefix requires a non-empty prefix")
        kwargs: dict[str, object] = {
            "TableName": self._physical_name(table),
            "FilterExpression": "begins_with(#k, :p)",
            "ExpressionAttributeNames": {"#k": key_attribute},
            "ExpressionAttributeValues": {":p": {"S": prefix}},
            "ConsistentRead": True,
        }
        while True:
            try:
                response = self._client.scan(**kwargs)
            except ClientError as exc:
                raise wrap_aws_error(exc, operation=f"scan_key_prefix:{table}") from exc
            for raw in response.get("Items", []):
                yield _decode_item(raw, exact=True)
            cursor = response.get("LastEvaluatedKey")
            if not cursor:
                return
            kwargs["ExclusiveStartKey"] = cursor

    # -- transactional writes -----------------------------------------------------------------

    def transact_write(
        self,
        operations: Iterable[dict[str, Any]],
        *,
        transaction_key: str | None = None,
    ) -> None:
        """Execute a ``TransactWriteItems`` call, translating cancellation into taxonomy errors.

        Each operation's ``TableName`` is a logical name and is rewritten to its physical name
        before the call; a ``Put`` item has its derived index attributes recomputed.

        Args:
            operations: Raw ``TransactWriteItems`` operations (``Put``, ``Update``, ``Delete`` or
                ``ConditionCheck``), each a single-key dict naming a logical ``TableName``.
            transaction_key: Optional idempotency key; its :func:`client_request_token` is sent
                as ``ClientRequestToken``.

        Raises:
            ValueError: An operation names an unrecognised table, carries none of the four action
                keys, or is an ``Update`` that would leave a derived attribute stale.
            ValidationFailed: ``operations`` is empty.
            Conflict: On ``TransactionCanceledException``, such as a failed condition check.
        """
        ops = [self._resolve_operation_table_name(op) for op in operations]
        if not ops:
            raise ValidationFailed("A transaction requires at least one operation.")
        kwargs: dict[str, object] = {"TransactItems": ops}
        if transaction_key:
            kwargs["ClientRequestToken"] = client_request_token(transaction_key, _TOKEN_SCOPE.get())
        try:
            self._client.transact_write_items(**kwargs)
        except ClientError as exc:
            raise wrap_aws_error(exc, operation="transact_write") from exc

    #: The four ``TransactWriteItems`` action keys, each carrying its own ``TableName``.
    _TRANSACT_ACTION_KEYS: Final[tuple[str, ...]] = ("Put", "Update", "Delete", "ConditionCheck")

    def _resolve_operation_table_name(self, operation: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of ``operation`` with its ``TableName`` resolved and derived keys enforced.

        A ``Put`` item is passed through :func:`crm.store.keys.derive_encoded`; an ``Update`` is
        checked by :func:`_reject_stale_derived_update`. The caller's dict is not mutated.
        """
        for action in self._TRANSACT_ACTION_KEYS:
            if action in operation:
                inner = operation[action]
                logical_table = inner["TableName"]
                if action == "Put" and "Item" in inner:
                    inner = {**inner, "Item": derive_encoded(logical_table, inner["Item"])}
                elif action == "Update":
                    _reject_stale_derived_update(logical_table, inner)
                resolved_inner = {**inner, "TableName": self.physical_name(logical_table)}
                return {**operation, action: resolved_inner}
        raise ValueError(
            f"transact-item operation carries none of {self._TRANSACT_ACTION_KEYS}: "
            f"keys={sorted(operation)}"
        )

    # -- scoped teardown -----------------------------------------------------------------------

    def delete_tenant(self, tenant: str) -> dict[str, int]:
        """Delete every item belonging to ``tenant`` across every tenant-scoped table.

        Each table is enumerated through its ``gsi_tenant`` index.

        Args:
            tenant: The tenant to delete. This method does not refuse any tenant name.

        Returns:
            A mapping of table name to the count of items deleted from that table.

        Raises:
            Conflict: Items remained unprocessed after one batch retry.
        """
        deleted: dict[str, int] = {}
        for table in sorted(TENANT_SCOPED_TABLES):
            deleted[table] = self._delete_all_in_tenant(table, tenant)
        return deleted

    def _delete_all_in_tenant(self, table: str, tenant: str) -> int:
        schema = table_schema(table)
        key_attrs = [schema.partition_key] + ([schema.sort_key] if schema.sort_key else [])
        count = 0
        cursor: dict[str, object] | None = None
        while True:
            items, cursor = self.query_tenant_scan(table, tenant=tenant, exclusive_start_key=cursor)
            for chunk in _chunk(items, _BATCH_WRITE_CHUNK_SIZE):
                self._batch_delete(table, key_attrs, chunk)
                count += len(chunk)
            if cursor is None:
                break
        return count

    def _batch_delete(
        self, table: str, key_attrs: list[str], items: list[dict[str, object]]
    ) -> None:
        request_items = {
            self._physical_name(table): [
                {"DeleteRequest": {"Key": _encode_key_from_item(key_attrs, item)}} for item in items
            ]
        }
        try:
            response = self._client.batch_write_item(RequestItems=request_items)
        except ClientError as exc:
            raise wrap_aws_error(exc, operation=f"batch_delete:{table}") from exc
        unprocessed = response.get("UnprocessedItems") or {}
        if unprocessed:
            # One synchronous retry; items still unprocessed after it raise Conflict.
            try:
                retry_response = self._client.batch_write_item(RequestItems=unprocessed)
            except ClientError as exc:
                raise wrap_aws_error(exc, operation=f"batch_delete_retry:{table}") from exc
            if retry_response.get("UnprocessedItems"):
                raise Conflict(
                    "Tenant teardown could not remove all items; retry the operation.",
                    detail=f"table={table} unprocessed after retry",
                )


def _chunk(items: list[dict[str, object]], size: int) -> Iterable[list[dict[str, object]]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


#: Clause keywords an ``UpdateExpression`` is split on before its assigned attributes are read.
_UPDATE_CLAUSE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(SET|REMOVE|ADD|DELETE)\b", flags=re.IGNORECASE
)
#: One ``SET`` assignment target: the attribute (or ``#placeholder``) left of an ``=``.
_SET_TARGET_RE: Final[re.Pattern[str]] = re.compile(r"(?:^|,)\s*(#?[A-Za-z0-9_]+)\s*=")
#: One comma-separated attribute name in a ``REMOVE``/``ADD``/``DELETE`` clause.
_BARE_TARGET_RE: Final[re.Pattern[str]] = re.compile(r"(?:^|,)\s*(#?[A-Za-z0-9_]+)")


def _assigned_attributes(
    update_expression: str, attribute_names: Mapping[str, object]
) -> frozenset[str]:
    """Return every attribute name an ``UpdateExpression`` writes.

    Reads assignment targets out of the ``SET`` clause and names out of ``REMOVE``/``ADD``/
    ``DELETE``, resolving each ``#placeholder`` through ``ExpressionAttributeNames``. Attribute
    *paths* (``a.b``, ``a[0]``) resolve to their leading element, which is the attribute the
    write lands in.
    """
    parts = _UPDATE_CLAUSE_RE.split(update_expression)
    assigned: set[str] = set()
    for index in range(1, len(parts) - 1, 2):
        keyword = parts[index].upper()
        body = parts[index + 1]
        pattern = _SET_TARGET_RE if keyword == "SET" else _BARE_TARGET_RE
        for token in pattern.findall(body):
            resolved = attribute_names.get(token) if token.startswith("#") else token
            if isinstance(resolved, str):
                assigned.add(resolved)
    return frozenset(assigned)


#: A reserved generated-placeholder token (``#_x`` / ``:_x``) inside a caller's condition.
_RESERVED_PLACEHOLDER_RE: Final[re.Pattern[str]] = re.compile(r"[#:]_[A-Za-z0-9_]*")


def _condition_conflict(table: str, exc: ClientError) -> Conflict:
    """Build the :class:`Conflict` for a failed ``update_item`` condition.

    ``reason="target_missing"`` when the failure response carries no old item.
    """
    if "Item" not in exc.response:
        return Conflict(
            "The record does not exist.",
            reason="target_missing",
            detail=f"update_item:{table} condition failed on an absent item",
        )
    return Conflict(
        "The record changed before this operation could be applied.",
        detail=f"update_item:{table} condition failed",
    )


def _reject_mismatched_derived_values(table: str, set_values: Mapping[str, object]) -> None:
    """Refuse a derived attribute assigned a value its assigned sources do not build.

    Only derived attributes whose every source is also in ``set_values`` are checked.

    Raises:
        ValueError: The supplied derived value differs from the one built from its sources.
    """
    for spec in derived_attributes(table):
        if spec.name not in set_values or not all(s in set_values for s in spec.sources):
            continue
        sources = [set_values[s] for s in spec.sources]
        if not all(isinstance(v, str) and v for v in sources):
            continue
        expected = spec.build(*sources)
        if set_values[spec.name] != expected:
            raise ValueError(
                f"Update on {table!r} assigns {spec.name}={set_values[spec.name]!r} but its "
                f"sources {list(spec.sources)} build {expected!r}"
            )


def _validate_update_arguments(
    partition_key: str,
    sort_key: str | None,
    set_values: Mapping[str, object],
    add_values: Mapping[str, object],
) -> None:
    """Refuse a :meth:`Store.update_item` attribute set DynamoDB would reject or misapply.

    Raises:
        ValueError: Nothing is assigned, an attribute is both set and added, a key attribute is
            assigned, or an ``ADD`` amount is not a number.
    """
    if not set_values and not add_values:
        raise ValueError("update_item requires at least one set_values or add_values attribute")
    overlap = sorted(set(set_values) & set(add_values))
    if overlap:
        raise ValueError(f"attributes {overlap} are in both set_values and add_values")
    key_attributes = {partition_key} | ({sort_key} if sort_key else set())
    assigned_keys = sorted(key_attributes & (set(set_values) | set(add_values)))
    if assigned_keys:
        raise ValueError(f"update_item cannot assign primary key attributes {assigned_keys}")
    for attribute, amount in add_values.items():
        if isinstance(amount, bool) or not isinstance(amount, (int, float, Decimal)):
            raise ValueError(
                f"add_values[{attribute!r}] must be a number, got {type(amount).__name__}"
            )


def _reject_stale_derived_update(table: str, inner: Mapping[str, Any]) -> None:
    """Refuse an ``Update`` that would leave a derived index key attribute stale.

    Raises:
        ValueError: The expression assigns a derived attribute's source without assigning the
            derived attribute itself.
    """
    specs = derived_attributes(table)
    if not specs:
        return
    expression = inner.get("UpdateExpression")
    if not isinstance(expression, str) or not expression:
        return
    names = inner.get("ExpressionAttributeNames") or {}
    assigned = _assigned_attributes(expression, names if isinstance(names, Mapping) else {})
    for spec in specs:
        stale_sources = sorted(source for source in spec.sources if source in assigned)
        if stale_sources and spec.name not in assigned:
            raise ValueError(
                f"Update on {table!r} assigns {stale_sources} but not {spec.name!r}, which is "
                f"derived from {list(spec.sources)} — the derived attribute would be left stale "
                "and its GSI would keep reporting the previous value. Assign it in the "
                "same expression, or write the whole item as a Put."
            )


def _encode_key(
    partition_key: str, sort_key: str | None, key: Mapping[str, str]
) -> dict[str, dict[str, str]]:
    encoded = {partition_key: {"S": str(key[partition_key])}}
    if sort_key is not None:
        encoded[sort_key] = {"S": str(key[sort_key])}
    return encoded


def _encode_key_from_item(
    key_attrs: list[str], item: Mapping[str, object]
) -> dict[str, dict[str, str]]:
    return {attr: {"S": str(item[attr])} for attr in key_attrs}


def _encode_item(item: Mapping[str, object]) -> dict[str, object]:
    """Encode a plain Python mapping into DynamoDB's typed attribute-value wire format."""
    return {key: _encode_value(value) for key, value in item.items()}


def _encode_value(value: object) -> dict[str, object]:
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float, Decimal)):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, Mapping):
        return {"M": _encode_item(value)}
    if isinstance(value, (list, tuple)):
        return {"L": [_encode_value(v) for v in value]}
    raise TypeError(f"cannot encode value of type {type(value).__name__} for DynamoDB")


def _decode_item(item: Mapping[str, object], *, exact: bool = False) -> dict[str, object]:
    return {key: _decode_value(value, exact=exact) for key, value in item.items()}


def _decode_number(raw: str, *, exact: bool) -> int | float | Decimal:
    if _is_int(raw):
        return int(raw)
    return Decimal(raw) if exact else float(raw)


def _decode_value(value: object, *, exact: bool = False) -> object:
    """Decode one typed attribute value; ``exact`` keeps non-integer numbers and sets lossless."""
    if not isinstance(value, Mapping):
        raise TypeError(f"expected a DynamoDB typed attribute value, got {type(value).__name__}")
    if "S" in value:
        return value["S"]
    if "N" in value:
        return _decode_number(str(value["N"]), exact=exact)
    if "BOOL" in value:
        return bool(value["BOOL"])
    if "NULL" in value:
        return None
    if "M" in value:
        inner = value["M"]
        if not isinstance(inner, Mapping):
            raise TypeError("malformed M attribute value")
        return _decode_item(inner, exact=exact)
    if "L" in value:
        inner_list = value["L"]
        if not isinstance(inner_list, list):
            raise TypeError("malformed L attribute value")
        return [_decode_value(v, exact=exact) for v in inner_list]
    if exact and "SS" in value:
        return frozenset(str(v) for v in _set_members(value["SS"]))
    if exact and "NS" in value:
        return frozenset(_decode_number(str(v), exact=True) for v in _set_members(value["NS"]))
    if exact and "BS" in value:
        return frozenset(bytes(v) for v in _set_members(value["BS"]))
    if exact and "B" in value:
        return bytes(value["B"])
    raise TypeError(f"unsupported DynamoDB attribute value shape: {sorted(value)}")


def _set_members(members: object) -> list[Any]:
    if not isinstance(members, list):
        raise TypeError("malformed set attribute value")
    return members


def _is_int(raw: str) -> bool:
    try:
        return float(raw) == int(raw)
    except (ValueError, OverflowError):
        return False
