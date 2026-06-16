# ABOUTME: Deliberately vulnerable fixture for the SAST gate (Story 9.1-001).
# ABOUTME: Contains a textbook SQL injection so semgrep p/owasp-top-ten flags an ERROR.

import sqlite3


def get_user(conn: sqlite3.Connection, user_id: str) -> list:
    """VULNERABLE: builds SQL via string formatting — classic SQL injection.

    A SAST scan must flag this with an ERROR-severity finding so the gate
    returns BLOCK. Do not "fix" this file: it exists to prove the gate fires.
    """
    cursor = conn.cursor()
    query = "SELECT * FROM users WHERE id = '%s'" % user_id
    cursor.execute(query)
    return cursor.fetchall()
