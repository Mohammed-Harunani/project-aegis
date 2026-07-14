import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


class SQLConnector:
    """
    Database connector layer for Aegis.

    Supports:
    - SQL Server
    - PostgreSQL
    - MySQL

    Returns data as Pandas DataFrame for validation.
    """

    def __init__(self, connection_string: str):
        self.connection_string = connection_string
        self.engine: Engine | None = None

    def connect(self) -> None:
        """
        Establish database connection.
        """
        try:
            self.engine = create_engine(self.connection_string)
        except Exception as e:
            raise ConnectionError(f"Database connection failed: {str(e)}")

    def fetch_table(self, table_name: str) -> pd.DataFrame:
        """
        Fetch entire table into DataFrame.
        """
        if not self.engine:
            raise RuntimeError("Database engine not initialized. Call connect() first.")

        try:
            query = f"SELECT * FROM {table_name}"
            df = pd.read_sql(query, self.engine)
            return df
        except Exception as e:
            raise RuntimeError(f"Failed to fetch table '{table_name}': {str(e)}")

    def close(self) -> None:
        """
        Dispose database connection.
        """
        if self.engine:
            self.engine.dispose()
            self.engine = None