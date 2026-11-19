"""Workspace-scoped reusable project notes and text artifacts."""
from uuid import uuid4


class Projects:
    def __init__(self, handoffs):
        self.handoffs = handoffs
        with handoffs.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS project (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, context TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS artifact (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL, content TEXT NOT NULL)""")

    def create(self, title, context=""):
        if not isinstance(title, str) or not 0 < len(title.strip()) <= 200:
            raise ValueError("Project title limit is 200 characters.")
        if not isinstance(context, str) or len(context) > 24000:
            raise ValueError("Project context limit is 24000 characters.")
        record = dict(id=uuid4().hex, title=title, context=context)
        with self.handoffs.connection() as db:
            db.execute("INSERT INTO project (id,title,context) VALUES (:id,:title,:context)", record)
        return record

    def list(self):
        with self.handoffs.connection() as db:
            return [dict(row) for row in db.execute("SELECT id,title FROM project ORDER BY rowid DESC")]

    def get(self, project_id):
        with self.handoffs.connection() as db:
            row = db.execute("SELECT * FROM project WHERE id=?", (project_id,)).fetchone()
            if row is None:
                raise ValueError("Project not found.")
            return dict(row) | {"artifacts": [dict(item) for item in db.execute(
                "SELECT id,name FROM artifact WHERE project_id=?", (project_id,))]}

    def save(self, project_id, name, content):
        self.get(project_id)
        if not isinstance(name, str) or not 0 < len(name.strip()) <= 200:
            raise ValueError("Artifact name limit is 200 characters.")
        if not isinstance(content, str) or len(content) > 100000:
            raise ValueError("Text artifact limit is 100000 characters.")
        record = dict(id=uuid4().hex, project_id=project_id, name=name, content=content)
        with self.handoffs.connection() as db:
            db.execute("INSERT INTO artifact (id,project_id,name,content) VALUES (:id,:project_id,:name,:content)", record)
        return record

    def artifact(self, artifact_id):
        with self.handoffs.connection() as db:
            row = db.execute("SELECT * FROM artifact WHERE id=?", (artifact_id,)).fetchone()
            if row is None:
                raise ValueError("Artifact not found.")
            return dict(row)
