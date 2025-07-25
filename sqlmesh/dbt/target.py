from __future__ import annotations

import abc
import typing as t
from pathlib import Path

from dbt.adapters.base import BaseRelation, Column
from pydantic import Field

from sqlmesh.core.console import get_console
from sqlmesh.core.config.connection import (
    AthenaConnectionConfig,
    BigQueryConnectionConfig,
    BigQueryConnectionMethod,
    BigQueryPriority,
    ClickhouseConnectionConfig,
    ConnectionConfig,
    DatabricksConnectionConfig,
    DuckDBConnectionConfig,
    MSSQLConnectionConfig,
    PostgresConnectionConfig,
    RedshiftConnectionConfig,
    SnowflakeConnectionConfig,
    TrinoAuthenticationMethod,
    TrinoConnectionConfig,
)
from sqlmesh.core.model import (
    IncrementalByTimeRangeKind,
    IncrementalByUniqueKeyKind,
    IncrementalUnmanagedKind,
)
from sqlmesh.dbt.common import DbtConfig
from sqlmesh.dbt.relation import Policy
from sqlmesh.dbt.util import DBT_VERSION
from sqlmesh.utils import AttributeDict, classproperty
from sqlmesh.utils.errors import ConfigError
from sqlmesh.utils.pydantic import field_validator, model_validator

IncrementalKind = t.Union[
    t.Type[IncrementalByUniqueKeyKind],
    t.Type[IncrementalByTimeRangeKind],
    t.Type[IncrementalUnmanagedKind],
]

# We only serialize a subset of fields in order to avoid persisting sensitive information
SERIALIZABLE_FIELDS = {
    "type",
    "name",
    "database",
    "schema_",
}


class TargetConfig(abc.ABC, DbtConfig):
    """
    Configuration for DBT profile target

    Args:
        type: The type of the data warehouse
        name: The name of this target
        database: Name of the database
        schema_: Name of the schema
        threads: The number of threads to run on
    """

    # dbt
    type: str = "none"
    name: str
    database: str
    schema_: str = Field(alias="schema")
    threads: int = 1
    profile_name: t.Optional[str] = None

    @classmethod
    def load(cls, data: t.Dict[str, t.Any]) -> TargetConfig:
        """
        Loads the configuration from the yaml provided for a profile target

        Args:
            data: The yaml for the project's target output

        Returns:
            The configuration of the provided profile target
        """
        db_type = data["type"]
        if config_class := TARGET_TYPE_TO_CONFIG_CLASS.get(db_type):
            return config_class(**data)

        raise ConfigError(f"{db_type} not supported.")

    def default_incremental_strategy(self, kind: IncrementalKind) -> str:
        """The default incremental strategy for the db"""
        raise NotImplementedError

    def to_sqlmesh(self, **kwargs: t.Any) -> ConnectionConfig:
        """Converts target config to SQLMesh connection config"""
        raise NotImplementedError

    @classmethod
    def from_sqlmesh(cls, config: ConnectionConfig, **kwargs: t.Dict[str, t.Any]) -> "TargetConfig":
        raise NotImplementedError

    def attribute_dict(self) -> AttributeDict:
        fields = self.dict(include=SERIALIZABLE_FIELDS).copy()
        fields["target_name"] = self.name
        return AttributeDict(fields)

    @classproperty
    def quote_policy(cls) -> Policy:
        return Policy()

    @property
    def extra(self) -> t.Set[str]:
        return self.extra_fields(set(self.dict()))

    @classproperty
    def relation_class(cls) -> t.Type[BaseRelation]:
        return BaseRelation

    @classproperty
    def column_class(cls) -> t.Type[Column]:
        return Column

    @property
    def dialect(self) -> str:
        return self.type


DUCKDB_IN_MEMORY = ":memory:"


class DuckDbConfig(TargetConfig):
    """
    Connection config for DuckDb target

    Args:
        path: Location of the database file. If not specified, an in memory database is used.
        extensions: A list of autoloadable extensions to load.
        settings: A dictionary of settings to pass into the duckdb connector.
        secrets: A list of secrets to pass to the secret manager in the duckdb connector.
    """

    type: t.Literal["duckdb"] = "duckdb"
    database: str = "main"
    schema_: str = Field(default="main", alias="schema")
    path: str = DUCKDB_IN_MEMORY
    extensions: t.Optional[t.List[str]] = None
    settings: t.Optional[t.Dict[str, t.Any]] = None
    secrets: t.Optional[t.List[t.Dict[str, t.Any]]] = None

    @model_validator(mode="before")
    def validate_authentication(cls, data: t.Any) -> t.Any:
        if not isinstance(data, dict):
            return data

        if "database" not in data and DBT_VERSION >= (1, 5, 0):
            path = data.get("path")
            data["database"] = (
                "memory"
                if path is None or path == DUCKDB_IN_MEMORY
                else Path(t.cast(str, path)).stem
            )

        if "threads" in data and t.cast(int, data["threads"]) > 1:
            get_console().log_warning("DuckDB does not support concurrency - setting threads to 1.")

        return data

    def default_incremental_strategy(self, kind: IncrementalKind) -> str:
        return "delete+insert"

    @classproperty
    def relation_class(cls) -> t.Type[BaseRelation]:
        from dbt.adapters.duckdb.relation import DuckDBRelation

        return DuckDBRelation

    def to_sqlmesh(self, **kwargs: t.Any) -> ConnectionConfig:
        if self.extensions is not None:
            kwargs["extensions"] = self.extensions
        if self.settings is not None:
            kwargs["connector_config"] = self.settings
        if self.secrets is not None:
            kwargs["secrets"] = self.secrets
        return DuckDBConnectionConfig(
            database=self.path,
            concurrent_tasks=1,
            **kwargs,
        )

    @classmethod
    def from_sqlmesh(cls, config: ConnectionConfig, **kwargs: t.Dict[str, t.Any]) -> "DuckDbConfig":
        if not isinstance(config, DuckDBConnectionConfig):
            raise ValueError(f"Incorrect config type: {type(config)}")

        return cls(
            path=config.database,
            extensions=config.extensions,
            settings=config.connector_config,
            **kwargs,
        )


class SnowflakeConfig(TargetConfig):
    """
    Project connection and operational configuration for the Snowflake target

    Args:
        account: Snowflake account
        warehouse: Name of the warehouse
        user: Name of the user
        password: User's password
        role: Role of the user
        client_session_keep_alive: A boolean flag to extend the duration of the Snowflake session beyond 4 hours
        query_tag: tag for the query in Snowflake
        connect_retries: Number of times to retry if the Snowflake connector encounters an error
        connect_timeout: Number of seconds to wait between failed attempts
        retry_on_database_errors: A boolean flag to retry if a Snowflake connector Database error is encountered
        retry_all: A boolean flag to retry on all Snowflake connector errors
        authenticator: SSO authentication: Snowflake authentication method
        private_key: Key pair authentication: Private key
        private_key_path: Key pair authentication: Path to the private key, used instead of private_key
        private_key_passphrase: Key pair authentication: passphrase used to decrypt private key (if encrypted)
        token: OAuth authentication: The Snowflake OAuth 2.0 access token
    """

    type: t.Literal["snowflake"] = "snowflake"
    account: str
    user: str

    # User and password authentication
    password: t.Optional[str] = None

    # SSO authentication
    authenticator: t.Optional[str] = None

    # Key Pair Auth
    private_key: t.Optional[str] = None
    private_key_path: t.Optional[str] = None
    private_key_passphrase: t.Optional[str] = None

    # TODO add other forms of authentication

    # oauth access token
    token: t.Optional[str] = None

    # Optional
    warehouse: t.Optional[str] = None
    role: t.Optional[str] = None
    client_session_keep_alive: bool = False
    query_tag: t.Optional[str] = None
    connect_retries: int = 0
    connect_timeout: int = 10
    retry_on_database_errors: bool = False
    retry_all: bool = False

    @model_validator(mode="before")
    @classmethod
    def validate_authentication(cls, data: t.Any) -> t.Any:
        if not isinstance(data, dict) or (
            data.get("password")
            or data.get("authenticator")
            or data.get("private_key")
            or data.get("private_key_path")
        ):
            return data

        raise ConfigError("No supported Snowflake authentication method found in target profile.")

    def default_incremental_strategy(self, kind: IncrementalKind) -> str:
        return "merge"

    @classproperty
    def relation_class(cls) -> t.Type[BaseRelation]:
        from dbt.adapters.snowflake import SnowflakeRelation

        return SnowflakeRelation

    @classproperty
    def column_class(cls) -> t.Type[Column]:
        from dbt.adapters.snowflake import SnowflakeColumn

        return SnowflakeColumn

    def to_sqlmesh(self, **kwargs: t.Any) -> ConnectionConfig:
        return SnowflakeConnectionConfig(
            user=self.user,
            password=self.password,
            authenticator=self.authenticator,
            account=self.account,
            warehouse=self.warehouse,
            database=self.database,
            role=self.role,
            concurrent_tasks=self.threads,
            token=self.token,
            private_key=self.private_key,
            private_key_path=self.private_key_path,
            private_key_passphrase=self.private_key_passphrase,
            **kwargs,
        )

    @classproperty
    def quote_policy(cls) -> Policy:
        return Policy(database=False, schema=False, identifier=False)


class PostgresConfig(TargetConfig):
    """
    Project connection and operational configuration for the Postgres target

    Args:
        host: The Postgres host to connect to
        user: Name of the user
        password: User's password
        port: The port to connect to
        dbname: Name of the database
        keepalives_idle: Seconds between TCP keepalive packets
        connect_timeout: Number of seconds to wait between failed attempts
        retries: Number of times to retry if the Postgres connector encounters an error
        search_path: Overrides the default search path
        role: Role of the user
        sslmode: SSL Mode used to connect to the database
    """

    type: t.Literal["postgres"] = "postgres"
    host: str
    user: str
    password: str
    port: int
    dbname: str
    keepalives_idle: t.Optional[int] = None
    connect_timeout: int = 10
    retries: int = 1  # Currently Unsupported by SQLMesh
    search_path: t.Optional[str] = None  # Currently Unsupported by SQLMesh
    role: t.Optional[str] = None
    sslmode: t.Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def validate_database(cls, data: t.Any) -> t.Any:
        if not isinstance(data, dict):
            return data

        data["database"] = data.get("database") or data.get("dbname")
        if not data["database"]:
            raise ConfigError("Either database or dbname must be set")

        return data

    @field_validator("port")
    @classmethod
    def _validate_port(cls, v: t.Union[int, str]) -> int:
        return int(v)

    def default_incremental_strategy(self, kind: IncrementalKind) -> str:
        return "delete+insert" if kind is IncrementalByUniqueKeyKind else "append"

    def to_sqlmesh(self, **kwargs: t.Any) -> ConnectionConfig:
        return PostgresConnectionConfig(
            host=self.host,
            user=self.user,
            password=self.password,
            port=self.port,
            database=self.dbname,
            keepalives_idle=self.keepalives_idle,
            concurrent_tasks=self.threads,
            connect_timeout=self.connect_timeout,
            role=self.role,
            sslmode=self.sslmode,
            **kwargs,
        )

    @classmethod
    def from_sqlmesh(
        cls, config: ConnectionConfig, **kwargs: t.Dict[str, t.Any]
    ) -> "PostgresConfig":
        if not isinstance(config, PostgresConnectionConfig):
            raise ValueError(f"Incorrect config type: {type(config)}")

        return cls(
            schema="public",
            host=config.host,
            user=config.user,
            password=config.password,
            port=config.port,
            dbname=config.database,
            keepalives_idle=config.keepalives_idle,
            threads=config.concurrent_tasks,
            connect_timeout=config.connect_timeout,
            role=config.role,
            sslmode=config.sslmode,
            **kwargs,
        )


class RedshiftConfig(TargetConfig):
    """
    Project connection and operational configuration for the Redshift target

    Args:
        host: The Redshift host to connect to
        user: Name of the user
        password: User's password
        port: The port to connect to
        dbname: Name of the database
        connect_timeout: Number of seconds to wait between failed attempts
        ra3_node: Enables cross-database sources
        search_path: Overrides the default search path
        sslmode: SSL Mode used to connect to the database
    """

    # TODO add other forms of authentication
    type: t.Literal["redshift"] = "redshift"
    host: str
    user: str
    password: str
    port: int
    dbname: str
    connect_timeout: t.Optional[int] = None
    ra3_node: bool = True
    search_path: t.Optional[str] = None
    sslmode: t.Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def validate_database(cls, data: t.Any) -> t.Any:
        if not isinstance(data, dict):
            return data

        data["database"] = data.get("database") or data.get("dbname")
        if not data["database"]:
            raise ConfigError("Either database or dbname must be set")

        return data

    def default_incremental_strategy(self, kind: IncrementalKind) -> str:
        return "append"

    @classproperty
    def relation_class(cls) -> t.Type[BaseRelation]:
        from dbt.adapters.redshift import RedshiftRelation

        return RedshiftRelation

    @classproperty
    def column_class(cls) -> t.Type[Column]:
        if DBT_VERSION < (1, 6, 0):
            from dbt.adapters.redshift import RedshiftColumn  # type: ignore

            return RedshiftColumn
        return super(RedshiftConfig, cls).column_class

    def to_sqlmesh(self, **kwargs: t.Any) -> ConnectionConfig:
        return RedshiftConnectionConfig(
            user=self.user,
            password=self.password,
            database=self.database,
            host=self.host,
            port=self.port,
            sslmode=self.sslmode,
            timeout=self.connect_timeout,
            concurrent_tasks=self.threads,
            **kwargs,
        )


class DatabricksConfig(TargetConfig):
    """
    Project connection and operational configuration for the Databricks target

    Args:
        catalog: Catalog name to use for Unity Catalog
        host: The Databricks host to connect to
        http_path: The Databricks compute resources URL
        token: Personal access token
        database: Name of the database. Not applicable for Databricks and ignored
    """

    type: t.Literal["databricks"] = "databricks"
    host: str
    http_path: str
    token: t.Optional[str] = None  # only required if auth_type is not set to 'oauth'
    database: t.Optional[str] = Field(alias="catalog")  # type: ignore
    auth_type: t.Optional[str] = None
    client_id: t.Optional[str] = None
    client_secret: t.Optional[str] = None

    def default_incremental_strategy(self, kind: IncrementalKind) -> str:
        return "merge"

    @classproperty
    def relation_class(cls) -> t.Type[BaseRelation]:
        from dbt.adapters.databricks.relation import DatabricksRelation

        return DatabricksRelation

    @classproperty
    def column_class(cls) -> t.Type[Column]:
        from dbt.adapters.databricks.column import DatabricksColumn

        return DatabricksColumn

    def to_sqlmesh(self, **kwargs: t.Any) -> ConnectionConfig:
        return DatabricksConnectionConfig(
            server_hostname=self.host,
            http_path=self.http_path,
            access_token=self.token,
            concurrent_tasks=self.threads,
            catalog=self.database,
            auth_type="databricks-oauth" if self.auth_type == "oauth" else self.auth_type,
            oauth_client_id=self.client_id,
            oauth_client_secret=self.client_secret,
            **kwargs,
        )


class BigQueryConfig(TargetConfig):
    """
    Project connection and operational configuration for the BigQuery target

    Args:
        type: The type of the target (bigquery)
        method: The BigQuery authentication method to use
        project: The BigQuery project to connect to
        location: The BigQuery location to connect to
        keyfile: The path to the BigQuery keyfile
        keyfile_json: The BigQuery keyfile as a JSON string
        token: The BigQuery token
        refresh_token: The BigQuery refresh token
        client_id: The BigQuery client ID
        client_secret: The BigQuery client secret
        token_uri: The BigQuery token URI
        scopes: The BigQuery scopes
        impersonated_service_account: The service account to impersonate
        job_creation_timeout_seconds: The maximum amount of time, in seconds, to wait for the underlying job to be created
        job_execution_timeout_seconds: The maximum amount of time, in seconds, to wait for the underlying job to complete
        timeout_seconds: Alias for job_execution_timeout_seconds
        job_retries: The number of times to retry the underlying job if it fails
        retries: Alias for job_retries
        job_retry_deadline_seconds: Total number of seconds to wait while retrying the same query
        priority: The priority of the underlying job
        maximum_bytes_billed: The maximum number of bytes to be billed for the underlying job
    """

    type: t.Literal["bigquery"] = "bigquery"
    method: t.Optional[str] = BigQueryConnectionMethod.OAUTH
    dataset: t.Optional[str] = None
    project: t.Optional[str] = None
    execution_project: t.Optional[str] = None
    quota_project: t.Optional[str] = None
    location: t.Optional[str] = None
    keyfile: t.Optional[str] = None
    keyfile_json: t.Optional[t.Dict[str, t.Any]] = None
    token: t.Optional[str] = None
    refresh_token: t.Optional[str] = None
    client_id: t.Optional[str] = None
    client_secret: t.Optional[str] = None
    token_uri: t.Optional[str] = None
    scopes: t.Tuple[str, ...] = (
        "https://www.googleapis.com/auth/bigquery",
        "https://www.googleapis.com/auth/cloud-platform",
        "https://www.googleapis.com/auth/drive",
    )
    impersonated_service_account: t.Optional[str] = None
    job_creation_timeout_seconds: t.Optional[int] = None
    job_execution_timeout_seconds: t.Optional[int] = None
    timeout_seconds: t.Optional[int] = None  # To support legacy config
    job_retries: t.Optional[int] = None
    retries: int = 1  # To support legacy config
    job_retry_deadline_seconds: t.Optional[int] = None
    priority: BigQueryPriority = BigQueryPriority.INTERACTIVE
    maximum_bytes_billed: t.Optional[int] = None

    @model_validator(mode="before")
    @classmethod
    def validate_fields(cls, data: t.Any) -> t.Any:
        if not isinstance(data, dict):
            return data

        data["schema"] = data.get("schema") or data.get("dataset")
        if not data["schema"]:
            raise ConfigError("Either schema or dataset must be set")
        data["database"] = data.get("database") or data.get("project")
        if not data["database"]:
            raise ConfigError("Either database or project must be set")

        return data

    def default_incremental_strategy(self, kind: IncrementalKind) -> str:
        return "merge"

    @classproperty
    def relation_class(cls) -> t.Type[BaseRelation]:
        from dbt.adapters.bigquery.relation import BigQueryRelation

        return BigQueryRelation

    @classproperty
    def column_class(cls) -> t.Type[Column]:
        from dbt.adapters.bigquery import BigQueryColumn

        return BigQueryColumn

    def to_sqlmesh(self, **kwargs: t.Any) -> ConnectionConfig:
        job_retries = self.job_retries if self.job_retries is not None else self.retries
        job_execution_timeout_seconds = (
            self.job_execution_timeout_seconds
            if self.job_execution_timeout_seconds is not None
            else self.timeout_seconds
        )
        return BigQueryConnectionConfig(
            method=self.method,
            project=self.database,
            execution_project=self.execution_project,
            quota_project=self.quota_project,
            location=self.location,
            concurrent_tasks=self.threads,
            keyfile=self.keyfile,
            keyfile_json=self.keyfile_json,
            token=self.token,
            refresh_token=self.refresh_token,
            client_id=self.client_id,
            client_secret=self.client_secret,
            token_uri=self.token_uri,
            scopes=self.scopes,
            impersonated_service_account=self.impersonated_service_account,
            job_creation_timeout_seconds=self.job_creation_timeout_seconds,
            job_execution_timeout_seconds=job_execution_timeout_seconds,
            job_retries=job_retries,
            job_retry_deadline_seconds=self.job_retry_deadline_seconds,
            priority=self.priority,
            maximum_bytes_billed=self.maximum_bytes_billed,
            **kwargs,
        )

    @classmethod
    def from_sqlmesh(
        cls, config: ConnectionConfig, **kwargs: t.Dict[str, t.Any]
    ) -> "BigQueryConfig":
        if not isinstance(config, BigQueryConnectionConfig):
            raise ValueError(f"Incorrect config type: {type(config)}")

        return cls(
            schema="__unknown__",
            method=config.method,
            project=config.project,
            execution_project=config.execution_project,
            quota_project=config.quota_project,
            location=config.location,
            threads=config.concurrent_tasks,
            keyfile=config.keyfile,
            keyfile_json=config.keyfile_json,
            token=config.token,
            refresh_token=config.refresh_token,
            client_id=config.client_id,
            client_secret=config.client_secret,
            token_uri=config.token_uri,
            scopes=config.scopes,
            impersonated_service_account=config.impersonated_service_account,
            job_creation_timeout_seconds=config.job_creation_timeout_seconds,
            job_execution_timeout_seconds=config.job_execution_timeout_seconds,
            job_retries=config.job_retries,
            job_retry_deadline_seconds=config.job_retry_deadline_seconds,
            priority=config.priority,
            maximum_bytes_billed=config.maximum_bytes_billed,
            **kwargs,
        )


class MSSQLConfig(TargetConfig):
    """
    Project connection and operational configuration for the SQL Server (MSSQL) target

    Args:
        host: The MSSQL server host to connect to
        server: Alias for host
        port: The MSSQL server port to connect to
        user: User name for authentication
        username: Alias for user
        UID: Alias for user
        password: User password for authentication
        PWD: Alias for password
        login_timeout: The number of seconds to wait for a login to complete
        query_timeout: The number of seconds to wait for a query to complete
        authentication: The authentication method to use (only "sql" is supported)
        schema_authorization: The principal who should own created schemas, not supported by SQLMesh
        driver: ODBC driver to use, not used by SQLMesh
        encrypt: A boolean flag to enable server connection encryption, not used by SQLMesh
        trust_cert: A boolean flag to trust the server certificate, not used by SQLMesh
        retries: Number of times to retry if the SQL Server connector encounters an error, not used by SQLMesh
        windows_login: A boolean flag to use Windows Authentication, not used by SQLMesh
        tenant_id: The tenant ID of the Azure Active Directory instance, not used by SQLMesh
        client_id: The client ID of the Azure Active Directory service principal, not used by SQLMesh
        client_secret: The client secret of the Azure Active Directory service principal, not used by SQLMesh
    """

    type: t.Literal["sqlserver"] = "sqlserver"
    host: t.Optional[str] = None
    server: t.Optional[str] = None
    port: int = 1433
    database: str = Field(default="master")
    schema_: str = Field(default="dbo", alias="schema")
    user: t.Optional[str] = None
    username: t.Optional[str] = None
    UID: t.Optional[str] = None
    password: t.Optional[str] = None
    PWD: t.Optional[str] = None
    threads: int = 4
    login_timeout: t.Optional[int] = None
    query_timeout: t.Optional[int] = None
    authentication: t.Optional[str] = "sql"
    schema_authorization: t.Optional[str] = None  # Not supported by SQLMesh

    # Unused ODBC parameters (SQLMesh uses pymssql instead of ODBC)
    driver: t.Optional[str] = None
    encrypt: t.Optional[bool] = None
    trust_cert: t.Optional[bool] = None
    retries: t.Optional[int] = None

    # Unused authentication parameters (not supported by pymssql)
    windows_login: t.Optional[bool] = None  # pymssql doesn't require this flag for Windows Auth
    tenant_id: t.Optional[str] = None  # Azure Active Directory auth
    client_id: t.Optional[str] = None  # Azure Active Directory auth
    client_secret: t.Optional[str] = None  # Azure Active Directory auth

    @model_validator(mode="before")
    @classmethod
    def validate_alias_fields(cls, data: t.Any) -> t.Any:
        if not isinstance(data, dict):
            return data

        data["host"] = data.get("host") or data.get("server")
        if not data["host"]:
            raise ConfigError("Either host or server must be set")

        data["user"] = data.get("user") or data.get("username") or data.get("UID")
        if not data["user"]:
            raise ConfigError("One of user, username, or UID must be set")

        data["password"] = data.get("password") or data.get("PWD")
        if not data["password"]:
            raise ConfigError("Either password or PWD must be set")

        return data

    @field_validator("authentication")
    @classmethod
    def _validate_authentication(cls, v: str) -> str:
        if v != "sql":
            raise ConfigError("Only SQL and Windows Authentication are supported for SQL Server")
        return v

    @field_validator("port")
    @classmethod
    def _validate_port(cls, v: t.Union[int, str]) -> int:
        return int(v)

    def default_incremental_strategy(self, kind: IncrementalKind) -> str:
        # https://github.com/microsoft/dbt-fabric/blob/main/dbt/include/fabric/macros/materializations/models/incremental/incremental_strategies.sql
        return "delete+insert" if kind is IncrementalByUniqueKeyKind else "append"

    @classproperty
    def column_class(cls) -> t.Type[Column]:
        try:
            # 1.8.0+
            from dbt.adapters.sqlserver.sqlserver_column import SQLServerColumn
        except ImportError:
            # <1.8.0
            from dbt.adapters.sqlserver.sql_server_column import SQLServerColumn  # type: ignore

        return SQLServerColumn

    @property
    def dialect(self) -> str:
        return "tsql"

    def to_sqlmesh(self, **kwargs: t.Any) -> ConnectionConfig:
        return MSSQLConnectionConfig(
            host=self.host,
            user=self.user,
            password=self.password,
            port=self.port,
            database=self.database,
            timeout=self.query_timeout,
            login_timeout=self.login_timeout,
            concurrent_tasks=self.threads,
            **kwargs,
        )


class TrinoConfig(TargetConfig):
    """
    Project connection and operational configuration for the Trino target.

    Args:
        method: The Trino authentication method to use
        host: The server host to connect to
        port: The MSSQL server port to connect to
        database: Name of the Trino database/catalog
        schema: Name of the Trino schema
        user: User name for authentication
        password: User password for authentication
        roles: Trino catalog roles
        session_properties: Trino session properties
        retries: Number of times to retry if the Trino connector encounters an error
        timezone: The timezone to use for the Trino session
        http_headers: HTTP Headers to send alongside requests to Trino
        http_scheme: The HTTP scheme to use for requests to Trino (default: http, or https if kerberos, ldap or jwt auth)
        threads: The number of threads to run on
        impersonation_user:  LDAP authentication: override the provided username
        keytab: Kerberos authentication: Path to keytab
        krb5_config: Kerberos authentication: Path to config
        principal: Kerberos authentication: Principal
        service_name: Kerberos authentication: Service name
        hostname_override: Kerberos authentication: hostname for a host whose DNS name doesn't match
        mutual_authentication: Kerberos authentication: Boolean flag for mutual authentication.
        force_preemptive: Kerberos authentication: Boolean flag to preemptively initiate the GSS exchange.
        sanitize_mutual_error_response: Kerberos authentication: Boolean flag to strip content and headers from error responses.
        delegate: Kerberos authentication: Boolean flag for credential delegation (`GSS_C_DELEG_FLAG`)
        jwt_token: JWT authentication: JWT string
        client_certificate: Certification authentication: Path to client certificate
        client_private_key: Certification authentication: Path to client private key
        cert: Certification authentication: Full path to a certificate file
    """

    _method_to_auth_enum: t.ClassVar[t.Dict[str, TrinoAuthenticationMethod]] = {
        "none": TrinoAuthenticationMethod.NO_AUTH,
        "ldap": TrinoAuthenticationMethod.LDAP,
        "kerberos": TrinoAuthenticationMethod.KERBEROS,
        "jwt": TrinoAuthenticationMethod.JWT,
        "certificate": TrinoAuthenticationMethod.CERTIFICATE,
        "oauth": TrinoAuthenticationMethod.OAUTH,
        "oauth_console": TrinoAuthenticationMethod.OAUTH,
    }

    type: t.Literal["trino"] = "trino"
    host: str
    database: str
    schema_: str = Field(alias="schema")
    port: int = 443
    method: str
    user: t.Optional[str] = None

    threads: int = 1
    roles: t.Optional[t.Dict[str, str]] = None
    session_properties: t.Optional[t.Dict[str, str]] = None
    retries: int = 3
    timezone: t.Optional[str] = None
    http_headers: t.Optional[t.Dict[str, str]] = None
    http_scheme: t.Optional[str] = None
    prepared_statements_enabled: bool = True  # not used by SQLMesh

    # ldap authentication
    password: t.Optional[str] = None
    impersonation_user: t.Optional[str] = None

    # kerberos authentication
    keytab: t.Optional[str] = None
    krb5_config: t.Optional[str] = None
    principal: t.Optional[str] = None
    service_name: str = "trino"
    hostname_override: t.Optional[str] = None
    mutual_authentication: bool = False
    force_preemptive: bool = False
    sanitize_mutual_error_response: bool = True
    delegate: bool = False

    # jwt authentication
    jwt_token: t.Optional[str] = None

    # certificate authentication
    client_certificate: t.Optional[str] = None
    client_private_key: t.Optional[str] = None
    cert: t.Optional[str] = None

    def default_incremental_strategy(self, kind: IncrementalKind) -> str:
        return "append"

    @classproperty
    def relation_class(cls) -> t.Type[BaseRelation]:
        from dbt.adapters.trino.relation import TrinoRelation

        return TrinoRelation

    @classproperty
    def column_class(cls) -> t.Type[Column]:
        from dbt.adapters.trino.column import TrinoColumn

        return TrinoColumn

    def to_sqlmesh(self, **kwargs: t.Any) -> ConnectionConfig:
        return TrinoConnectionConfig(
            method=self._method_to_auth_enum[self.method],
            host=self.host,
            user=self.user,
            catalog=self.database,
            port=self.port,
            http_scheme=self.http_scheme,
            roles=self.roles,
            http_headers=self.http_headers,
            session_properties=self.session_properties,
            retries=self.retries,
            timezone=self.timezone,
            password=self.password,
            impersonation_user=self.impersonation_user,
            keytab=self.keytab,
            krb5_config=self.krb5_config,
            principal=self.principal,
            service_name=self.service_name,
            hostname_override=self.hostname_override,
            mutual_authentication=self.mutual_authentication,
            force_preemptive=self.force_preemptive,
            sanitize_mutual_error_response=self.sanitize_mutual_error_response,
            delegate=self.delegate,
            jwt_token=self.jwt_token,
            client_certificate=self.client_certificate,
            client_private_key=self.client_private_key,
            cert=self.cert,
            concurrent_tasks=self.threads,
            **kwargs,
        )


class ClickhouseConfig(TargetConfig):
    """
    Project connection and operational configuration for the Clickhouse target

    Args:
      host: [localhost]
      user: [default] # User for all database operations
      password: [<empty string>] # Password for the user
      secure: [False] # Use TLS (native protocol) or HTTPS (http protocol)
      port: [8123]  # If not set, defaults to 8123, 8443 depending on the secure and driver settings
      connect_timeout: [10] # Timeout in seconds to establish a connection to ClickHouse
      send_receive_timeout: [300] # Timeout in seconds to receive data from the ClickHouse server
      verify: [True] # Validate TLS certificate if using TLS/SSL
      cluster: [<empty string>] # If set, certain DDL/table operations will be executed with the `ON CLUSTER` clause using this cluster.
      custom_settings: [{}] # A dictionary/mapping of custom ClickHouse settings for the connection - default is empty.
      schema: [default] # ClickHouse database for dbt models, not used by SQLMesh
      driver: [http] # http or native.  If not set this will be autodetermined based on port setting, not used by SQLMesh
      retries: [1] # Number of times to retry a "retriable" database exception (such as a 503 'Service Unavailable' error), not used by SQLMesh
      compression: [<empty string>] # Use gzip compression if truthy (http), or compression type for a native connection, not used by SQLMesh
      cluster_mode: [False] # Use specific settings designed to improve operation on Replicated databases (recommended for ClickHouse Cloud), not used by SQLMesh
      use_lw_deletes: [False] # Use the strategy `delete+insert` as the default incremental strategy, not used by SQLMesh
      check_exchange: [True] # Validate that clickhouse support the atomic EXCHANGE TABLES command. Not used by SQLMesh.
      local_suffix: [_local] # Table suffix of local tables on shards for distributed materializations, not used by SQLMesh
      local_db_prefix: [<empty string>] # Database prefix of local tables on shards for distributed materializations, not used by SQLMesh
      allow_automatic_deduplication: [False] # Enable ClickHouse automatic deduplication for Replicated tables, not used by SQLMesh
      tcp_keepalive: [False] # Native client only, specify TCP keepalive configuration. Specify custom keepalive settings as [idle_time_sec, interval_sec, probes], not used by SQLMesh
      sync_request_timeout: [5] # Timeout for server ping, not used by SQLMesh
      compress_block_size: [1048576] # Compression block size if compression is enabled, not used by SQLMesh
    """

    host: str = "localhost"
    user: str = Field(default="default", alias="username")
    password: str = ""
    port: t.Optional[int] = None
    cluster: t.Optional[str] = None
    schema_: str = Field(default="default", alias="schema")
    connect_timeout: int = 10
    send_receive_timeout: int = 300
    verify: bool = True
    compression: str = ""
    custom_settings: t.Optional[t.Dict[str, t.Any]] = None

    # Not used by SQLMesh
    driver: t.Optional[str] = None
    secure: bool = False
    retries: int = 1
    database_engine: t.Optional[str] = None
    cluster_mode: bool = False
    sync_request_timeout: int = 5
    compress_block_size: int = 1048576
    check_exchange: bool = True
    use_lw_deletes: bool = False
    allow_automatic_deduplication: bool = False
    tcp_keepalive: t.Union[bool, t.Tuple[int, ...], t.List[int]] = False
    database: str = ""
    local_suffix: str = "local"
    local_db_prefix: str = ""

    type: t.Literal["clickhouse"] = "clickhouse"

    def default_incremental_strategy(self, kind: IncrementalKind) -> str:
        # dbt-clickhouse name for temp table swap. That is sqlmesh's default
        #   strategy so doesn't require special handling during conversion.
        return "legacy"

    @classproperty
    def relation_class(cls) -> t.Type[BaseRelation]:
        from dbt.adapters.clickhouse.relation import ClickHouseRelation

        return ClickHouseRelation

    @classproperty
    def column_class(cls) -> t.Type[Column]:
        from dbt.adapters.clickhouse.column import ClickHouseColumn

        return ClickHouseColumn

    def to_sqlmesh(self, **kwargs: t.Any) -> ConnectionConfig:
        return ClickhouseConnectionConfig(
            host=self.host,
            username=self.user,
            password=self.password,
            port=self.port,
            cluster=self.cluster,
            connect_timeout=self.connect_timeout,
            send_receive_timeout=self.send_receive_timeout,
            verify=self.verify,
            compression_method=self.compression,
            connection_settings=self.custom_settings,
            **kwargs,
        )


class AthenaConfig(TargetConfig):
    """
    Project connection and operational configuration for the Athena target.

    Args:
        s3_staging_dir: S3 location to store Athena query results and metadata
        s3_data_dir: Prefix for storing tables, if different from the connection's s3_staging_dir
        s3_data_naming: How to generate table paths in s3_data_dir
        s3_tmp_table_dir: Prefix for storing temporary tables, if different from the connection's s3_data_dir
        region_name: AWS region of your Athena instance
        schema: Specify the schema (Athena database) to build models into (lowercase only)
        database: Specify the database (Data catalog) to build models into (lowercase only)
        poll_interval: Interval in seconds to use for polling the status of query results in Athena
        debug_query_state: Flag if debug message with Athena query state is needed
        aws_access_key_id: Access key ID of the user performing requests
        aws_secret_access_key: Secret access key of the user performing requests
        aws_profile_name: Profile to use from your AWS shared credentials file
        work_group: Identifier of Athena workgroup
        skip_workgroup_check: Indicates if the WorkGroup check (additional AWS call) can be skipped
        num_retries: Number of times to retry a failing query
        num_boto3_retries: Number of times to retry boto3 requests (e.g. deleting S3 files for materialized tables)
        num_iceberg_retries: Number of times to retry iceberg commit queries to fix ICEBERG_COMMIT_ERROR
        spark_work_group: Identifier of Athena Spark workgroup for running Python models
        seed_s3_upload_args: Dictionary containing boto3 ExtraArgs when uploading to S3
        lf_tags_database: Default LF tags for new database if it's created by dbt
    """

    type: t.Literal["athena"] = "athena"
    threads: int = 4

    s3_staging_dir: t.Optional[str] = None
    s3_data_dir: t.Optional[str] = None
    s3_data_naming: t.Optional[str] = None
    s3_tmp_table_dir: t.Optional[str] = None
    poll_interval: t.Optional[int] = None
    debug_query_state: bool = False
    work_group: t.Optional[str] = None
    skip_workgroup_check: t.Optional[bool] = None
    spark_work_group: t.Optional[str] = None

    aws_access_key_id: t.Optional[str] = None
    aws_secret_access_key: t.Optional[str] = None
    aws_profile_name: t.Optional[str] = None
    region_name: t.Optional[str] = None

    num_retries: t.Optional[int] = None
    num_boto3_retries: t.Optional[int] = None
    num_iceberg_retries: t.Optional[int] = None

    seed_s3_upload_args: t.Dict[str, str] = {}
    lf_tags_database: t.Dict[str, str] = {}

    @classproperty
    def relation_class(cls) -> t.Type[BaseRelation]:
        from dbt.adapters.athena.relation import AthenaRelation

        return AthenaRelation

    @classproperty
    def column_class(cls) -> t.Type[Column]:
        from dbt.adapters.athena.column import AthenaColumn

        return AthenaColumn

    def default_incremental_strategy(self, kind: IncrementalKind) -> str:
        return "insert_overwrite"

    def to_sqlmesh(self, **kwargs: t.Any) -> ConnectionConfig:
        return AthenaConnectionConfig(
            type="athena",
            aws_access_key_id=self.aws_access_key_id,
            aws_secret_access_key=self.aws_secret_access_key,
            region_name=self.region_name,
            work_group=self.work_group,
            s3_staging_dir=self.s3_staging_dir,
            s3_warehouse_location=self.s3_data_dir,
            schema_name=self.schema_,
            catalog_name=self.database,
            concurrent_tasks=self.threads,
            **kwargs,
        )


TARGET_TYPE_TO_CONFIG_CLASS: t.Dict[str, t.Type[TargetConfig]] = {
    "databricks": DatabricksConfig,
    "duckdb": DuckDbConfig,
    "postgres": PostgresConfig,
    "redshift": RedshiftConfig,
    "snowflake": SnowflakeConfig,
    "bigquery": BigQueryConfig,
    "sqlserver": MSSQLConfig,
    "tsql": MSSQLConfig,
    "trino": TrinoConfig,
    "athena": AthenaConfig,
    "clickhouse": ClickhouseConfig,
}
