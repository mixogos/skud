from conftest import ADMIN, login


def make_user(client, admin, roles, name="ivan", role="Пользователь", password="ivan-pass-123"):
    r = client.post(
        "/users/",
        headers=admin,
        json={"name": name, "password": password, "role_id": roles[role]["id"], "real_name": "Иван"},
    )
    assert r.status_code == 201, r.text
    return r.json()


# --- Вход ---
def test_requires_token(client):
    assert client.get("/users/me/").status_code == 401
    assert client.get("/users/me/", headers={"Authorization": "Token bad"}).status_code == 401
    assert client.get("/users/me/", headers={"Authorization": "Bearer x"}).status_code == 401


def test_login_and_me(client, admin):
    me = client.get("/users/me/", headers=admin).json()
    assert me["name"] == "admin" and me["is_superadmin"] is True
    assert me["role_name"] == "Администратор"
    assert "user:add" in me["permissions"] and "view_auditlog" in me["permissions"]
    assert "password_hash" not in me


def test_wrong_password_and_lockout(client):
    for _ in range(3):
        r = client.post("/auth/login/", json={"name": "admin", "password": "wrong"})
        assert r.status_code == 401
    # После трёх неудач вход заблокирован даже с верным паролем.
    assert client.post("/auth/login/", json=ADMIN).status_code == 429


def test_unknown_user_same_error(client):
    r = client.post("/auth/login/", json={"name": "nobody", "password": "whatever-1"})
    assert r.status_code == 401 and r.json()["detail"] == "Неверный логин или пароль"


def test_logout_invalidates_token(client, admin):
    assert client.post("/auth/logout/", headers=admin).status_code == 204
    assert client.get("/users/me/", headers=admin).status_code == 401


def test_token_not_stored_in_plain(client, admin):
    from app import models
    from app.db import SessionLocal

    token = admin["Authorization"].split()[1]
    with SessionLocal() as db:
        s = db.query(models.Session).one()
        assert s.token_hash != token and len(s.token_hash) == 64


# --- Роли ---
def test_predefined_roles(roles):
    assert set(roles) == {"Администратор", "Оператор", "Пользователь"}
    op, user = roles["Оператор"]["permissions"], roles["Пользователь"]["permissions"]
    assert "humancard:add" in op and "user:add" not in op and "view_auditlog" not in op
    assert "humancard:view" in user and "humancard:add" not in user and "faceevent:change" in user


def test_role_crud(client, admin, roles):
    r = client.post("/groups/", headers=admin, json={"name": "Охрана", "permissions": ["faceevent:view"]})
    assert r.status_code == 201
    rid = r.json()["id"]
    assert client.post("/groups/", headers=admin, json={"name": "Охрана"}).status_code == 409
    assert client.post("/groups/", headers=admin, json={"name": "X", "permissions": ["nope:view"]}).status_code == 422
    r = client.patch(f"/groups/{rid}/", headers=admin, json={"permissions": ["faceevent:view", "camera:view"]})
    assert r.json()["permissions"] == ["camera:view", "faceevent:view"]
    assert client.delete(f"/groups/{rid}/", headers=admin).status_code == 204
    assert client.delete(f"/groups/{roles['Оператор']['id']}/", headers=admin).status_code == 400


def test_role_in_use_cannot_be_deleted(client, admin, roles):
    rid = client.post("/groups/", headers=admin, json={"name": "Временная"}).json()["id"]
    client.post("/users/", headers=admin, json={"name": "tmp", "password": "tmp-pass-123", "role_id": rid})
    assert client.delete(f"/groups/{rid}/", headers=admin).status_code == 409


# --- Пользователи и права ---
def test_user_crud_and_permissions(client, admin, roles):
    user = make_user(client, admin, roles)
    assert "password" not in user and "password_hash" not in user
    assert client.post(
        "/users/", headers=admin, json={"name": "ivan", "password": "another-pass", "role_id": user["role_id"]}
    ).status_code == 409
    assert client.post(
        "/users/", headers=admin, json={"name": "short", "password": "123", "role_id": user["role_id"]}
    ).status_code == 422

    ivan = login(client, "ivan", "ivan-pass-123")
    # Роль «Пользователь» не управляет пользователями, ролями и не видит журнал.
    assert client.get("/users/", headers=ivan).status_code == 403
    assert client.get("/groups/", headers=ivan).status_code == 403
    assert client.get("/auditlogs/", headers=ivan).status_code == 403
    assert client.patch(f"/users/{user['id']}/", headers=ivan, json={"role_id": roles["Администратор"]["id"]}).status_code == 403

    # Смена роли сразу меняет права.
    client.patch(f"/users/{user['id']}/", headers=admin, json={"role_id": roles["Администратор"]["id"]})
    assert client.get("/users/", headers=ivan).status_code == 200

    # Отключение закрывает сессии и запрещает вход.
    client.patch(f"/users/{user['id']}/", headers=admin, json={"active": False})
    assert client.get("/users/me/", headers=ivan).status_code == 401
    assert client.post("/auth/login/", json={"name": "ivan", "password": "ivan-pass-123"}).status_code == 401

    assert client.delete(f"/users/{user['id']}/", headers=admin).status_code == 204
    assert client.get(f"/users/{user['id']}/", headers=admin).status_code == 404


def test_superadmin_is_protected(client, admin, roles):
    me = client.get("/users/me/", headers=admin).json()
    assert client.delete(f"/users/{me['id']}/", headers=admin).status_code == 400
    assert client.patch(f"/users/{me['id']}/", headers=admin, json={"active": False}).status_code == 400
    assert client.patch(
        f"/users/{me['id']}/", headers=admin, json={"role_id": roles["Пользователь"]["id"]}
    ).status_code == 400
    # Другой администратор тоже не может изменить первого.
    make_user(client, admin, roles, name="admin2", role="Администратор", password="admin2-pass-1")
    admin2 = login(client, "admin2", "admin2-pass-1")
    assert client.patch(f"/users/{me['id']}/", headers=admin2, json={"password": "hacked-pass-1"}).status_code == 403
    assert client.delete(f"/users/{me['id']}/", headers=admin2).status_code == 400


def test_no_privilege_escalation_via_roles(client, admin, roles):
    # Роль, которой разрешено править роли, но не пользователей.
    rid = client.post(
        "/groups/", headers=admin, json={"name": "Редактор ролей", "permissions": ["group:view", "group:change", "group:add"]}
    ).json()["id"]
    client.post("/users/", headers=admin, json={"name": "editor", "password": "editor-pass-1", "role_id": rid})
    editor = login(client, "editor", "editor-pass-1")
    assert client.patch(f"/groups/{rid}/", headers=editor, json={"permissions": ["group:view", "group:change", "group:add", "user:add"]}).status_code == 403
    assert client.post("/groups/", headers=editor, json={"name": "Хитрая", "permissions": ["user:delete"]}).status_code == 403


def test_change_own_password(client, admin, roles):
    make_user(client, admin, roles)
    first = login(client, "ivan", "ivan-pass-123")
    second = login(client, "ivan", "ivan-pass-123")
    assert client.patch("/users/me/", headers=first, json={"old_password": "wrong", "new_password": "new-pass-12345"}).status_code == 400
    assert client.patch("/users/me/", headers=first, json={"old_password": "ivan-pass-123", "new_password": "new-pass-12345"}).status_code == 200
    assert client.get("/users/me/", headers=first).status_code == 200
    assert client.get("/users/me/", headers=second).status_code == 401  # остальные сессии закрыты
    login(client, "ivan", "new-pass-12345")


# --- Сессии ---
def test_sessions(client, admin, roles):
    make_user(client, admin, roles)
    a = login(client, "ivan", "ivan-pass-123")
    b = login(client, "ivan", "ivan-pass-123")
    sessions = client.get("/sessions/", headers=a).json()["results"]
    assert len(sessions) == 2 and sum(s["current"] for s in sessions) == 1
    other = next(s for s in sessions if not s["current"])
    assert client.delete(f"/sessions/{other['id']}/", headers=a).status_code == 204
    assert client.get("/users/me/", headers=b).status_code == 401
    # Чужие сессии обычный пользователь не видит и закрыть не может.
    assert client.get("/sessions/?all_users=true", headers=a).status_code == 403
    admin_sid = next(s for s in client.get("/sessions/", headers=admin).json()["results"] if s["current"])["id"]
    assert client.delete(f"/sessions/{admin_sid}/", headers=a).status_code == 403
    assert client.get("/sessions/?all_users=true", headers=admin).json()["count"] == 2


# --- Журнал ---
def test_audit_log(client, admin, roles):
    user = make_user(client, admin, roles)
    client.post("/auth/login/", json={"name": "ivan", "password": "bad"})
    logs = client.get("/auditlogs/", headers=admin).json()
    actions = [(l["action"], l["object_type"]) for l in logs["results"]]
    assert ("create", "user") in actions and ("login", "user") in actions and ("login_failed", "user") in actions
    # Пароль в журнал не попадает.
    client.patch(f"/users/{user['id']}/", headers=admin, json={"password": "secret-pass-999"})
    assert "secret-pass-999" not in client.get("/auditlogs/", headers=admin).text
    only = client.get("/auditlogs/?action=create&object_type=user", headers=admin).json()
    assert only["count"] == 1 and only["results"][0]["object_id"] == str(user["id"])
    assert only["results"][0]["user_name"] == "admin"
