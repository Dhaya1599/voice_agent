import os
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
load_dotenv()


def get_connection():
    """Reads database credentials from the .env file and opens a network line."""
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "call_centre"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD")
    )

def execute_query(sql_instruction: str, data_parameters: tuple = (), fetch_mode: str = None):
    """
    Runs any database instruction safely.
    It handles opening the line, running the work, and hanging up automatically.
    """
    connection = get_connection()
    try:
        with connection:
            with connection.cursor(cursor_factory=psycopg2.extras.DictCursor) as teller:
                teller.execute(sql_instruction, data_parameters)
                if fetch_mode == 'one':
                    return teller.fetchone()
                if fetch_mode == 'all':
                    return teller.fetchall()
    except Exception as error:
        print(f"❌ Database Error: {error}")
        raise error
    finally:
        connection.close()