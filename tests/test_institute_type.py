"""Institute type (college / school / coaching): stored, validated, returned; no type means college, as before."""
import pytest
from bson import ObjectId
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import app.api.routers.auth as auth
import app.api.routers.profile as profile
from app.api.deps import get_current_identity
from app.db.mongodb import get_database
from app.models.institute import (
    DEFAULT_INSTITUTE_TYPE, INSTITUTE_TYPES, create_institute_document, institute_type_of,
    normalize_institute_type, serialize_institute, update_institute_document,
)

USER = ObjectId()


# ---------------- the model ----------------

def test_the_three_types_and_the_default():
    assert INSTITUTE_TYPES == ("college", "school", "coaching") and DEFAULT_INSTITUTE_TYPE == "college"


@pytest.mark.parametrize("value,expected", [
    (None, "college"), ("", "college"), ("   ", "college"),
    ("college", "college"), ("school", "school"), ("coaching", "coaching"),
    ("School", "school"), ("  COACHING ", "coaching"),
])
def test_a_type_is_normalised_and_blank_means_college(value, expected):
    assert normalize_institute_type(value) == expected


@pytest.mark.parametrize("value", ["university", "kindergarten", 5, ["school"], True])
def test_an_unknown_type_is_refused(value):
    with pytest.raises(ValueError, match="institute_type must be one of: college, school, coaching"):
        normalize_institute_type(value)


@pytest.mark.parametrize("doc,expected", [
    ({}, "college"), (None, "college"), ({"institute_type": None}, "college"),
    ({"institute_type": "school"}, "school"), ({"institute_type": "coaching"}, "coaching"),
    ({"institute_type": "garbage"}, "college"),  # a bad stored value never breaks the screens: it reads as college
])
def test_a_stored_institute_reads_as_its_type_or_college(doc, expected):
    assert institute_type_of(doc) == expected


def test_a_new_institute_is_a_college_unless_a_type_is_given():
    base = {"user_id": str(USER), "institute_name": "Demo"}
    assert create_institute_document(base)["institute_type"] == "college"
    assert create_institute_document({**base, "institute_type": "school"})["institute_type"] == "school"
    assert create_institute_document({**base, "institute_type": "Coaching"})["institute_type"] == "coaching"
    with pytest.raises(ValueError):
        create_institute_document({**base, "institute_type": "factory"})


def test_a_new_institute_keeps_every_other_field_as_before():
    doc = create_institute_document({"user_id": str(USER), "institute_name": "Demo", "city": "Kolkata"})
    assert doc["institute_name"] == "Demo" and doc["city"] == "Kolkata" and doc["is_active"] is True
    assert doc["token_limit"]["plan"] == "demo" and "token_usage" in doc


def test_an_update_changes_the_type_only_when_one_is_sent():
    assert "institute_type" not in update_institute_document({"city": "Pune"})["$set"]
    assert update_institute_document({"institute_type": "school"})["$set"]["institute_type"] == "school"
    with pytest.raises(ValueError):
        update_institute_document({"institute_type": "factory"})


def test_a_legacy_institute_without_a_type_is_serialised_as_a_college():
    legacy = {"_id": ObjectId(), "user_id": USER, "institute_name": "Old University"}
    assert serialize_institute(legacy)["institute_type"] == "college"
    assert serialize_institute({**legacy, "institute_type": "coaching"})["institute_type"] == "coaching"


# ---------------- registering an institute ----------------

class FakeCollection:
    def __init__(self, docs=None):
        self.docs, self.inserted = list(docs or []), []

    async def find_one(self, query=None, *args, **kwargs):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in (query or {}).items() if not isinstance(v, dict)):
                return doc
        return None

    async def insert_one(self, doc):
        doc = {**doc, "_id": doc.get("_id") or ObjectId()}
        self.inserted.append(doc)
        self.docs.append(doc)
        return type("Result", (), {"inserted_id": doc["_id"]})()


class FakeDb(dict):
    def __getitem__(self, name):
        return self.setdefault(name, FakeCollection())


async def register(institute):
    admin = {"_id": ObjectId(), "role": 1}
    db = FakeDb(users=FakeCollection([admin]))
    data = {"role": "institute", "fullName": "Demo", "email": "a@b.co", "institute": institute}
    result = await auth._register_institute(db, data, "hash", str(admin["_id"]))
    return result, db


async def test_registering_an_institute_without_a_type_makes_a_college():
    (body, code), db = await register({"institute_name": "Demo"})
    assert code == 201 and db["instituteDetails"].inserted[0]["institute_type"] == "college"


@pytest.mark.parametrize("given", ["school", "coaching", "college"])
async def test_registering_an_institute_with_a_type_stores_it(given):
    (body, code), db = await register({"institute_name": "Demo", "institute_type": given})
    assert code == 201 and db["instituteDetails"].inserted[0]["institute_type"] == given


async def test_an_unknown_type_is_refused_before_anything_is_saved():
    (body, code), db = await register({"institute_name": "Demo", "institute_type": "factory"})
    assert code == 400 and "institute_type must be one of" in body["error"]
    assert db["instituteDetails"].inserted == [] and [u for u in db["users"].inserted] == []


# ---------------- the profile ----------------

def client(monkeypatch, db, role):
    app = FastAPI()
    app.include_router(profile.router)
    app.dependency_overrides[get_current_identity] = lambda: {"user_id": str(USER), "role": role}
    app.dependency_overrides[get_database] = lambda: db
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.parametrize("stored,expected", [(None, "college"), ("school", "school"), ("coaching", "coaching")])
async def test_an_institute_admin_profile_carries_the_type(monkeypatch, stored, expected):
    institute = {"_id": ObjectId(), "user_id": USER, "institute_name": "Demo", **({"institute_type": stored} if stored else {})}
    db = FakeDb(users=FakeCollection([{"_id": USER, "role": 2, "fullName": "Admin"}]), instituteDetails=FakeCollection([institute]))
    async with client(monkeypatch, db, 2) as c:
        body = (await c.get("/profile")).json()
    assert body["institute_profile"]["institute_type"] == expected and body["institute_profile"]["institute_name"] == "Demo"


@pytest.mark.parametrize("stored,expected", [(None, "college"), ("coaching", "coaching")])
async def test_a_faculty_profile_carries_the_type_of_their_institute(monkeypatch, stored, expected):
    institute_id = ObjectId()
    institute = {"_id": institute_id, "institute_name": "Demo", **({"institute_type": stored} if stored else {})}
    db = FakeDb(users=FakeCollection([{"_id": USER, "role": 3}]), facultyDetails=FakeCollection([{"user_id": USER, "institute_id": institute_id}]),
                instituteDetails=FakeCollection([institute]))
    async with client(monkeypatch, db, 3) as c:
        body = (await c.get("/profile")).json()
    assert body["institute_type"] == expected and body["institute_name"] == "Demo"


# ---------------- the super admin's institute screens ----------------

class FakeCursor:
    def __init__(self, docs):
        self.docs = list(docs)

    def skip(self, n):
        self.docs = self.docs[n:]
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    def __aiter__(self):
        async def gen():
            for doc in self.docs:
                yield doc
        return gen()


class FakeCollection2(FakeCollection):
    def __init__(self, docs=None):
        super().__init__(docs)
        self.updates = []

    def find(self, query=None, projection=None):
        def matches(doc):
            for key, value in (query or {}).items():
                if isinstance(value, dict) and "$in" in value:
                    if doc.get(key) not in value["$in"]:
                        return False
                elif isinstance(value, dict):
                    continue
                elif doc.get(key) != value:
                    return False
            return True
        return FakeCursor([d for d in self.docs if matches(d)])

    async def count_documents(self, query=None):
        return len(list(self.find(query).docs))

    async def update_one(self, query, update, **kwargs):
        self.updates.append((query, update))
        return type("Result", (), {"matched_count": 1})()


async def test_the_super_admin_institute_list_shows_each_type_and_old_institutes_as_college():
    old, school = ObjectId(), ObjectId()
    db = FakeDb(
        users=FakeCollection2([{"_id": old, "role": 2, "fullName": "Old University"}, {"_id": school, "role": 2, "fullName": "New School"}]),
        instituteDetails=FakeCollection2([{"user_id": old, "token_usage": {}}, {"user_id": school, "institute_type": "school", "token_usage": {}}]),
    )
    async with client(None, db, 1) as c:
        rows = (await c.get("/institutes")).json()["data"]
    assert {r["fullName"]: r["institute_type"] for r in rows} == {"Old University": "college", "New School": "school"}


@pytest.mark.parametrize("stored,expected", [(None, "college"), ("coaching", "coaching")])
async def test_reading_one_institute_returns_its_type(stored, expected):
    institute = {"_id": ObjectId(), "user_id": USER, "institute_name": "Demo", **({"institute_type": stored} if stored else {})}
    db = FakeDb(users=FakeCollection2([{"_id": USER, "role": 2}]), instituteDetails=FakeCollection2([institute]))
    async with client(None, db, 1) as c:
        body = (await c.get(f"/institute/{USER}")).json()
    assert body["institute"]["institute_type"] == expected


async def test_updating_an_institute_with_an_unknown_type_is_refused_and_nothing_is_saved():
    institute = {"_id": ObjectId(), "user_id": USER, "institute_name": "Demo"}
    db = FakeDb(users=FakeCollection2([{"_id": USER, "role": 2}]), instituteDetails=FakeCollection2([institute]))
    async with client(None, db, 1) as c:
        response = await c.put(f"/institute/{USER}", json={"institute": {"institute_type": "factory"}})
    assert response.status_code == 400 and "institute_type must be one of" in response.json()["detail"]
    assert db["instituteDetails"].updates == []


async def test_only_the_super_admin_can_change_an_institute_type():
    db = FakeDb(users=FakeCollection2([{"_id": USER, "role": 2}]), instituteDetails=FakeCollection2([{"_id": ObjectId(), "user_id": USER}]))
    async with client(None, db, 2) as c:
        response = await c.put(f"/institute/{USER}", json={"institute": {"institute_type": "school"}})
    assert response.status_code == 403 and db["instituteDetails"].updates == []
