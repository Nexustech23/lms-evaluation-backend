"""HTTP access regressions using an isolated app and in-memory records only."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from bson import ObjectId
from fastapi import FastAPI, HTTPException, Request
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_current_identity
from app.api.routers import answers, exams, evaluation
from app.db.mongodb import get_database
from app.models.user import FACULTY, INSTITUTE, INSTITUTE_STUDENT, SELF_LEARNER, SUPERADMIN


class Collection:
    def __init__(self, documents=()):
        self.documents = deepcopy(list(documents))
        self.writes = []

    def matching(self, query):
        return next((d for d in self.documents if all(d.get(k) == v for k, v in query.items())), None)

    async def find_one(self, query, *args):
        return deepcopy(self.matching(query))

    def find(self, query):
        async def cursor():
            for doc in self.documents:
                if all(doc.get(k) == v for k, v in query.items()):
                    yield deepcopy(doc)
        return cursor()

    async def insert_one(self, data):
        self.writes.append("insert")
        doc = deepcopy(data)
        doc.setdefault("_id", ObjectId())
        self.documents.append(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def update_one(self, query, change, upsert=False):
        self.writes.append("update")
        doc = self.matching(query)
        if doc is None and upsert:
            doc = {"_id": ObjectId(), **query, **change.get("$setOnInsert", {})}
            self.documents.append(doc)
        if doc is not None:
            doc.update(deepcopy(change["$set"]))
        return SimpleNamespace(modified_count=int(doc is not None), matched_count=int(doc is not None))

    async def delete_one(self, query):
        self.writes.append("delete")
        doc = self.matching(query)
        if doc is not None:
            self.documents.remove(doc)
        return SimpleNamespace(deleted_count=int(doc is not None))

    async def delete_many(self, query):
        self.writes.append("delete_many")
        old_count = len(self.documents)
        self.documents[:] = [
            doc for doc in self.documents
            if not all(doc.get(k) == v for k, v in query.items())
        ]
        return SimpleNamespace(deleted_count=old_count - len(self.documents))


@pytest_asyncio.fixture
async def isolated(monkeypatch):
    owner_user, other_user, foreign_user, admin_user, foreign_admin = [ObjectId() for _ in range(5)]
    owner_faculty, other_faculty, foreign_faculty, institute, foreign_institute, school, exam, answer = [ObjectId() for _ in range(8)]
    identities = {
        "owner": {"user_id": str(owner_user), "role": FACULTY},
        "other_faculty": {"user_id": str(other_user), "role": FACULTY},
        "foreign_faculty": {"user_id": str(foreign_user), "role": FACULTY},
        "admin": {"user_id": str(admin_user), "role": INSTITUTE},
        "foreign_admin": {"user_id": str(foreign_admin), "role": INSTITUTE},
        "student": {"user_id": str(ObjectId()), "role": INSTITUTE_STUDENT},
        "learner": {"user_id": str(ObjectId()), "role": SELF_LEARNER},
        "superadmin": {"user_id": str(ObjectId()), "role": SUPERADMIN},
    }
    db = {
        "users": Collection([{"_id": user} for user in [owner_user, other_user, foreign_user]]),
        "facultyDetails": Collection([
            {"_id": owner_faculty, "user_id": owner_user, "institute_id": institute},
            {"_id": other_faculty, "user_id": other_user, "institute_id": institute},
            {"_id": foreign_faculty, "user_id": foreign_user, "institute_id": foreign_institute},
        ]),
        "instituteDetails": Collection([
            {"_id": institute, "user_id": admin_user},
            {"_id": foreign_institute, "user_id": foreign_admin},
        ]),
        "schoolDetails": Collection([{"_id": school, "institute_id": institute}]),
        "newsavedDocs": Collection([{"_id": exam, "faculty_id": owner_faculty, "school_id": school}]),
        "answerDetails": Collection([{
            "_id": answer, "exam_id": exam, "faculty_id": owner_faculty,
            "filename": "student.pdf", "fileId": "answer-file", "evaluated_report_fileId": "report-file",
            "total_final_marks": 5,
        }]),
        "evaluationDetails": Collection(),
    }
    deletion = Mock()
    enqueue = AsyncMock()
    monkeypatch.setattr(answers, "delete_imagekit_file", deletion)
    monkeypatch.setattr(answers, "enqueue", enqueue)
    app = FastAPI()
    app.include_router(answers.router)
    app.include_router(exams.router)
    app.include_router(evaluation.router)

    async def identity(request: Request):
        selected = request.headers.get("x-test-user")
        if selected not in identities:
            raise HTTPException(status_code=401, detail="Login required")
        return identities[selected]

    app.dependency_overrides[get_current_identity] = identity
    app.dependency_overrides[get_database] = lambda: db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(client=client, db=db, exam=str(exam), answer=str(answer),
                              deletion=deletion, enqueue=enqueue, owner_faculty=owner_faculty)


OPERATIONS = ["list", "upload", "rename", "delete", "review", "manual"]


async def request_operation(s, operation, user):
    kwargs = {"headers": {"x-test-user": user}} if user else {}
    if operation == "list":
        return await s.client.get(f"/get-answer-scripts/{s.exam}", **kwargs)
    if operation == "upload":
        return await s.client.post(f"/upload-answer-script/{s.exam}", json={
            "answer_script_url": "https://cdn.test/student.pdf", "fileId": "new-file", "filename": "new.pdf",
        }, **kwargs)
    if operation == "rename":
        return await s.client.put("/rename-file", json={"answer_id": s.answer, "newFilename": "renamed.pdf"}, **kwargs)
    if operation == "delete":
        return await s.client.request("DELETE", "/delete-file", json={"answer_id": s.answer}, **kwargs)
    if operation == "review":
        return await s.client.post("/save-self-evaluation", json={
            "answer_id": s.answer, "questionwise_marking": [{"ai_awarded_marks": 3, "grace_marks": 1}],
        }, **kwargs)
    return await s.client.post(f"/manual-marks-entry/{s.exam}", json={
        "max_marks": 10, "entries": [{"student_id": "S001", "marks": 7}],
    }, **kwargs)


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("user", [None, "other_faculty", "foreign_faculty", "admin", "foreign_admin", "student", "learner", "superadmin"])
async def test_access_matrix_prevents_unauthorised_side_effects(isolated, operation, user):
    before = {name: deepcopy(col.documents) for name, col in isolated.db.items()}
    response = await request_operation(isolated, operation, user)
    expected = 403
    if user is None:
        expected = 401
    elif user in ("other_faculty", "foreign_faculty"):
        expected = 404
    elif operation == "list" and user in ("admin", "foreign_admin"):
        expected = 200 if user == "admin" else 404
    assert response.status_code == expected, response.text
    if expected == 200:
        assert response.json()["count"] == 1
    assert before == {name: col.documents for name, col in isolated.db.items()}
    assert not any(col.writes for col in isolated.db.values())
    isolated.deletion.assert_not_called()
    isolated.enqueue.assert_not_awaited()


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_owner_can_use_answer_workflows(isolated, operation):
    response = await request_operation(isolated, operation, "owner")
    assert response.status_code == 200, response.text
    docs = isolated.db["answerDetails"].documents
    if operation == "delete":
        assert not docs
        assert [c.args[0] for c in isolated.deletion.call_args_list] == ["answer-file", "report-file"]
    elif operation == "rename":
        assert docs[0]["filename"] == "renamed.pdf"
    elif operation == "review":
        assert docs[0]["total_final_marks"] == 4
        isolated.enqueue.assert_awaited_once()
    elif operation == "upload":
        assert len(docs) == 2
        assert docs[1]["faculty_id"] == isolated.owner_faculty
    elif operation == "manual":
        assert response.json()["saved_count"] == 1
        assert docs[1]["total_final_marks"] == 7


@pytest.mark.parametrize("operation", ["rename", "delete", "review"])
@pytest.mark.parametrize("broken_parent", [None, "invalid", "missing_exam"])
async def test_orphaned_answers_fail_closed(isolated, operation, broken_parent):
    doc = isolated.db["answerDetails"].documents[0]
    doc["exam_id"] = ObjectId() if broken_parent == "missing_exam" else broken_parent
    before = deepcopy(doc)
    response = await request_operation(isolated, operation, "owner")
    assert response.status_code == 404
    assert doc == before
    isolated.deletion.assert_not_called()
    isolated.enqueue.assert_not_awaited()


async def test_uploader_does_not_override_exam_ownership(isolated):
    # A legacy wrong uploader ID must not give that uploader access to an exam.
    isolated.db["answerDetails"].documents[0]["faculty_id"] = isolated.db["facultyDetails"].documents[1]["_id"]
    response = await request_operation(isolated, "delete", "other_faculty")
    assert response.status_code == 404
    isolated.deletion.assert_not_called()
    response = await request_operation(isolated, "rename", "owner")
    assert response.status_code == 200


@pytest.mark.parametrize("field", ["faculty_id", "school_id"])
async def test_legacy_string_exam_ids_are_supported(isolated, field):
    doc = isolated.db["newsavedDocs"].documents[0]
    doc[field] = str(doc[field])
    response = await request_operation(isolated, "list", "admin" if field == "school_id" else "owner")
    assert response.status_code == 200


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_malformed_ids_return_400_without_changes(isolated, operation):
    isolated.exam = "invalid"
    isolated.answer = "invalid"
    response = await request_operation(isolated, operation, "owner")
    assert response.status_code == 400
    assert not any(col.writes for col in isolated.db.values())
    isolated.deletion.assert_not_called()


@pytest.mark.parametrize("missing", ["school", "institute", "school_institute_id"])
async def test_admin_reads_fail_closed_without_tenant_link(isolated, missing):
    if missing == "school":
        isolated.db["schoolDetails"].documents.clear()
    elif missing == "institute":
        isolated.db["instituteDetails"].documents.clear()
    else:
        isolated.db["schoolDetails"].documents[0].pop("institute_id")
    response = await request_operation(isolated, "list", "admin")
    assert response.status_code == 404


async def test_reassigned_exam_access_follows_current_exam_owner(isolated):
    isolated.db["newsavedDocs"].documents[0]["faculty_id"] = isolated.db["facultyDetails"].documents[1]["_id"]
    response = await request_operation(isolated, "delete", "owner")
    assert response.status_code == 404
    isolated.deletion.assert_not_called()
    response = await request_operation(isolated, "rename", "other_faculty")
    assert response.status_code == 200


@pytest.mark.parametrize("user,status", [
    (None, 401), ("other_faculty", 404), ("foreign_faculty", 404),
    ("admin", 403), ("foreign_admin", 403), ("student", 403),
    ("learner", 403), ("superadmin", 403),
])
async def test_exam_delete_denied_without_any_writes(isolated, user, status):
    isolated.db["evaluationDetails"].documents.append({"exam_id": ObjectId(isolated.exam)})
    before = {name: deepcopy(col.documents) for name, col in isolated.db.items()}
    headers = {"x-test-user": user} if user else {}
    response = await isolated.client.request(
        "DELETE", "/delete-folder", json={"_id": isolated.exam}, headers=headers,
    )
    assert response.status_code == status, response.text
    assert before == {name: col.documents for name, col in isolated.db.items()}
    assert not any(col.writes for col in isolated.db.values())


@pytest.mark.parametrize("legacy_owner", [False, True])
async def test_owner_deletes_only_selected_exam_and_children(isolated, legacy_owner):
    exam = isolated.db["newsavedDocs"].documents[0]
    if legacy_owner:
        exam["faculty_id"] = str(exam["faculty_id"])
    unrelated_exam = ObjectId()
    for name in ("newsavedDocs", "answerDetails", "evaluationDetails"):
        isolated.db[name].documents.append({"_id": unrelated_exam, "exam_id": unrelated_exam})
    isolated.db["evaluationDetails"].documents.append({"exam_id": ObjectId(isolated.exam)})
    response = await isolated.client.request(
        "DELETE", "/delete-folder", json={"_id": isolated.exam},
        headers={"x-test-user": "owner"},
    )
    assert response.status_code == 200, response.text
    for name in ("newsavedDocs", "answerDetails", "evaluationDetails"):
        assert isolated.db[name].documents == [{"_id": unrelated_exam, "exam_id": unrelated_exam}]


@pytest.mark.parametrize("exam_id,status", [("invalid", 400), (str(ObjectId()), 404)])
async def test_exam_delete_bad_id_preserves_records(isolated, exam_id, status):
    response = await isolated.client.request(
        "DELETE", "/delete-folder", json={"_id": exam_id},
        headers={"x-test-user": "owner"},
    )
    assert response.status_code == status
    assert not any(col.writes for col in isolated.db.values())


@pytest.mark.parametrize("change", ["reassigned", "deleted"])
async def test_exam_change_before_delete_preserves_children(isolated, monkeypatch, change):
    exams_collection = isolated.db["newsavedDocs"]
    original_delete = exams_collection.delete_one
    async def concurrent_delete(query):
        if change == "reassigned":
            exams_collection.documents[0]["faculty_id"] = ObjectId()
        else:
            exams_collection.documents.clear()
        return await original_delete(query)
    monkeypatch.setattr(exams_collection, "delete_one", concurrent_delete)
    isolated.db["evaluationDetails"].documents.append({"exam_id": ObjectId(isolated.exam)})
    children = {name: deepcopy(isolated.db[name].documents)
                for name in ("answerDetails", "evaluationDetails")}
    response = await isolated.client.request(
        "DELETE", "/delete-folder", json={"_id": isolated.exam},
        headers={"x-test-user": "owner"},
    )
    assert response.status_code == 409
    for name, documents in children.items():
        assert isolated.db[name].documents == documents
        assert isolated.db[name].writes == []


@pytest.mark.parametrize("user,status", [
    (None, 401), ("other_faculty", 404), ("foreign_faculty", 404),
    ("admin", 403), ("foreign_admin", 403), ("student", 403),
    ("learner", 403), ("superadmin", 403),
])
async def test_exam_update_denied_without_writes(isolated, user, status):
    before = {name: deepcopy(col.documents) for name, col in isolated.db.items()}
    response = await isolated.client.put(
        f"/update-exam/{isolated.exam}", data={"examdetails": "Unauthorized change"},
        headers={"x-test-user": user} if user else {},
    )
    assert response.status_code == status, response.text
    assert before == {name: col.documents for name, col in isolated.db.items()}
    assert not any(col.writes for col in isolated.db.values())


@pytest.mark.parametrize("legacy_owner", [False, True])
async def test_exam_owner_can_update_selected_exam(isolated, legacy_owner):
    exam = isolated.db["newsavedDocs"].documents[0]
    if legacy_owner:
        exam["faculty_id"] = str(exam["faculty_id"])
    unrelated = {"_id": ObjectId(), "faculty_id": ObjectId(), "exam_title": "Unchanged"}
    isolated.db["newsavedDocs"].documents.append(deepcopy(unrelated))
    response = await isolated.client.put(
        f"/update-exam/{isolated.exam}", data={"examdetails": "Updated midterm", "semester": "5"},
        headers={"x-test-user": "owner"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["success"] is True
    assert exam["exam_title"] == "Updated midterm"
    assert exam["semester"] == 5
    assert isolated.db["newsavedDocs"].documents[1] == unrelated
    assert not isolated.db["answerDetails"].writes
    assert not isolated.db["evaluationDetails"].writes


@pytest.mark.parametrize("exam_id,status", [("invalid", 400), (str(ObjectId()), 404)])
async def test_exam_update_invalid_or_missing_exam(isolated, exam_id, status):
    response = await isolated.client.put(
        f"/update-exam/{exam_id}", data={"examdetails": "New title"},
        headers={"x-test-user": "owner"},
    )
    assert response.status_code == status
    assert not any(col.writes for col in isolated.db.values())


async def test_exam_update_stays_locked_after_question_paper_upload(isolated):
    isolated.db["newsavedDocs"].documents[0]["question_paper"] = {"url": "https://cdn.test/paper.pdf"}
    response = await isolated.client.put(
        f"/update-exam/{isolated.exam}", data={"examdetails": "New title"},
        headers={"x-test-user": "owner"},
    )
    assert response.status_code == 400
    assert not any(col.writes for col in isolated.db.values())


@pytest.mark.parametrize("change", ["reassigned", "deleted"])
async def test_exam_change_before_update_prevents_edit(isolated, monkeypatch, change):
    collection = isolated.db["newsavedDocs"]
    original_update = collection.update_one
    async def concurrent_update(query, update):
        if change == "reassigned":
            collection.documents[0]["faculty_id"] = ObjectId()
        else:
            collection.documents.clear()
        return await original_update(query, update)
    monkeypatch.setattr(collection, "update_one", concurrent_update)
    response = await isolated.client.put(
        f"/update-exam/{isolated.exam}", data={"examdetails": "Must not be saved"},
        headers={"x-test-user": "owner"},
    )
    assert response.status_code == 409
    assert all("exam_title" not in doc for doc in collection.documents)


PHASE1_OPERATIONS = ['folder_read', 'rubric_read', 'rubric_save', 'exam_rename', 'archive', 'restore']


def seed_rubric(s):
    s.db['newsavedDocs'].documents[0]['covered_cos'] = ['CO1']
    s.db['evaluationDetails'].documents.append({
        '_id': ObjectId(), 'exam_id': ObjectId(s.exam), 'faculty_id': s.owner_faculty,
        'totalMarks': 10, 'questionEvaluationDetails': [],
    })


async def phase1_request(s, operation, user):
    headers = {'x-test-user': user} if user else {}
    if operation == 'folder_read':
        return await s.client.get(f'/folder-details/{s.exam}', headers=headers)
    if operation == 'rubric_read':
        return await s.client.get(f'/evaluation-details/{s.exam}', headers=headers)
    if operation == 'rubric_save':
        return await s.client.post(f'/evaluation-details/{s.exam}', headers=headers, json={
            'totalMarks': 10, 'questionEvaluationDetails': [{
                'maxMarks': 10, 'cos': [{'co_code': 'CO1', 'marks': 10}],
                'parameters': [{'name': 'Accuracy', 'percentage': 100}],
            }],
        })
    if operation == 'exam_rename':
        return await s.client.put('/rename-folder', headers=headers,
                                  json={'_id': s.exam, 'newFoldername': 'Updated exam'})
    return await s.client.post(f'/set-archive-status/{s.exam}', headers=headers,
                               json={'is_archived': operation == 'archive'})


@pytest.mark.parametrize('operation', PHASE1_OPERATIONS)
@pytest.mark.parametrize('user', [None, 'other_faculty', 'foreign_faculty', 'admin',
                                 'foreign_admin', 'student', 'learner', 'superadmin'])
async def test_phase1_permissions_and_unchanged_records(isolated, operation, user):
    seed_rubric(isolated)
    before = {name: deepcopy(col.documents) for name, col in isolated.db.items()}
    response = await phase1_request(isolated, operation, user)
    expected = 403
    if user is None:
        expected = 401
    elif user in ('other_faculty', 'foreign_faculty'):
        expected = 404
    elif operation in ('folder_read', 'rubric_read') and user in ('admin', 'foreign_admin'):
        expected = 200 if user == 'admin' else 404
    assert response.status_code == expected, response.text
    if expected == 200:
        if operation == 'folder_read':
            assert response.json()['total_scripts'] == 1
        else:
            assert response.json()['evaluation']['totalMarks'] == 10
    assert before == {name: col.documents for name, col in isolated.db.items()}
    assert not any(col.writes for col in isolated.db.values())


@pytest.mark.parametrize('operation', PHASE1_OPERATIONS)
@pytest.mark.parametrize('legacy_owner', [False, True])
async def test_phase1_owner_success(isolated, operation, legacy_owner):
    seed_rubric(isolated)
    if legacy_owner:
        isolated.db['newsavedDocs'].documents[0]['faculty_id'] = str(isolated.owner_faculty)
    response = await phase1_request(isolated, operation, 'owner')
    assert response.status_code == 200, response.text
    exam = isolated.db['newsavedDocs'].documents[0]
    if operation == 'rubric_save':
        assert len(isolated.db['evaluationDetails'].documents) == 1
        assert isolated.db['evaluationDetails'].documents[0]['questionEvaluationDetails'][0]['maxMarks'] == 10
    elif operation == 'exam_rename':
        assert exam['folder_name'] == 'Updated exam'
    elif operation in ('archive', 'restore'):
        assert exam['is_archived'] is (operation == 'archive')
        assert ('archived_at' if operation == 'archive' else 'restored_at') in exam
    else:
        assert not any(col.writes for col in isolated.db.values())


@pytest.mark.parametrize('operation', PHASE1_OPERATIONS)
@pytest.mark.parametrize('exam_id,status', [('invalid', 400), (str(ObjectId()), 404)])
async def test_phase1_invalid_or_missing_exam(isolated, operation, exam_id, status):
    isolated.exam = exam_id
    response = await phase1_request(isolated, operation, 'owner')
    assert response.status_code == status
    assert not any(col.writes for col in isolated.db.values())


async def test_rubric_owner_can_insert_and_read_empty_state(isolated):
    isolated.db['newsavedDocs'].documents[0]['covered_cos'] = ['CO1']
    response = await phase1_request(isolated, 'rubric_read', 'owner')
    assert response.status_code == 200
    assert response.json()['evaluation'] is None
    response = await phase1_request(isolated, 'rubric_save', 'owner')
    assert response.status_code == 200
    doc = isolated.db['evaluationDetails'].documents[0]
    assert doc['faculty_id'] == isolated.owner_faculty
    assert doc['exam_id'] == ObjectId(isolated.exam)


@pytest.mark.parametrize('operation', ['exam_rename', 'archive', 'restore'])
@pytest.mark.parametrize('change', ['reassigned', 'deleted'])
async def test_phase1_exam_write_checks_current_owner(isolated, monkeypatch, operation, change):
    collection = isolated.db['newsavedDocs']
    original = collection.update_one
    async def concurrent_update(query, update):
        if change == 'reassigned':
            collection.documents[0]['faculty_id'] = ObjectId()
        else:
            collection.documents.clear()
        return await original(query, update)
    monkeypatch.setattr(collection, 'update_one', concurrent_update)
    response = await phase1_request(isolated, operation, 'owner')
    assert response.status_code == 409
    assert all('folder_name' not in d and 'is_archived' not in d for d in collection.documents)


@pytest.mark.parametrize('operation', ['folder_read', 'rubric_read'])
@pytest.mark.parametrize('missing', ['school', 'institute'])
async def test_phase1_admin_read_requires_tenant_link(isolated, operation, missing):
    isolated.db['schoolDetails' if missing == 'school' else 'instituteDetails'].documents.clear()
    response = await phase1_request(isolated, operation, 'admin')
    assert response.status_code == 404
    assert not any(col.writes for col in isolated.db.values())


async def test_same_name_rename_is_successful(isolated, monkeypatch):
    isolated.db['newsavedDocs'].documents[0]['folder_name'] = 'Updated exam'
    monkeypatch.setattr(isolated.db['newsavedDocs'], 'update_one',
                        AsyncMock(return_value=SimpleNamespace(matched_count=1, modified_count=0)))
    response = await phase1_request(isolated, 'exam_rename', 'owner')
    assert response.status_code == 200
