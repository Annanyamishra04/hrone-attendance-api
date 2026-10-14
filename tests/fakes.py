"""Minimal in-memory stand-ins for a PyMongo Database, used ONLY by unit tests that must not need
a server. They prove our control flow (create / verify / fail loudly), not MongoDB behaviour:
index creation against a real server is covered by tests/test_mongo_integration.py."""
from pymongo.errors import ConnectionFailure


class FakeCollection:
    def __init__(self, down=False):
        self.indexes = [{"v": 2, "key": {"_id": 1}, "name": "_id_"}]
        self.down = down
        self.fail_create = None      # exception instance to raise from create_index
        self.silent_create = False   # create_index "succeeds" but the index never appears
        self.create_calls = []

    def list_indexes(self):
        if self.down:
            raise ConnectionFailure("down")
        return iter([dict(i) for i in self.indexes])

    def drop_index(self, name):
        """Used by tests to simulate an operator deleting an index; the app code never calls this."""
        self.indexes = [i for i in self.indexes if i["name"] != name]

    def create_index(self, keys, name=None, unique=False, **_kw):
        self.create_calls.append((list(keys), name, unique))
        if self.down:
            raise ConnectionFailure("down")
        if self.fail_create is not None:
            raise self.fail_create
        if not self.silent_create:
            info = {"v": 2, "key": dict(keys), "name": name}
            if unique:
                info["unique"] = True
            self.indexes.append(info)
        return name


class _Admin:
    def __init__(self, db):
        self._db = db

    def command(self, _cmd):
        if self._db.down:
            raise ConnectionFailure("down")
        return {"ok": 1}


class _Client:
    def __init__(self, db):
        self.admin = _Admin(db)


class FakeDB:
    def __init__(self, down=False):
        self.down = down
        self.client = _Client(self)
        self._collections = {}

    def __getitem__(self, name):
        if name not in self._collections:
            self._collections[name] = FakeCollection(down=self.down)
        return self._collections[name]
