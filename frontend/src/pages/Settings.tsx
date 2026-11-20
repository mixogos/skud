import { useEffect, useState } from "react";
import { del, fmtDate, get, Page, patch, post, useLoad } from "../api";
import { useApp } from "../ctx";
import { Check, Empty, ErrorBox, Field, Icon, Modal, Pager, Spinner, useAction } from "../ui";
import Webhooks from "./Webhooks";

interface Role { id: number; name: string; permissions: string[]; predefined: boolean }
interface User { id: number; name: string; real_name: string; active: boolean; is_superadmin: boolean; role_id: number; created_date: string }

export default function SettingsPage() {
  const { can } = useApp();
  const tabs = [
    ["profile", "Профиль", true],
    ["general", "Общие", true],
    ["users", "Пользователи", can("user:view")],
    ["roles", "Роли", can("group:view")],
    ["webhooks", "Вебхуки", can("webhook:view")],
    ["audit", "Журнал действий", can("view_auditlog")],
  ].filter((t) => t[2]) as [string, string, boolean][];
  const [tab, setTab] = useState("profile");
  return (
    <>
      <div className="tabs" role="tablist">
        {tabs.map(([key, label]) => <button key={key} role="tab" aria-selected={tab === key} className={tab === key ? "on" : ""} onClick={() => setTab(key)}>{label}</button>)}
      </div>
      {tab === "profile" && <Profile />}
      {tab === "general" && <General />}
      {tab === "users" && <Users />}
      {tab === "roles" && <Roles />}
      {tab === "webhooks" && <Webhooks />}
      {tab === "audit" && <Audit />}
    </>
  );
}

// --- Профиль ---
function Profile() {
  const { me } = useApp();
  const run = useAction();
  const [realName, setRealName] = useState(me.real_name);
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const sessions = useLoad<Page<any>>(() => get("/sessions/"), []);
  const mismatch = newPassword !== "" && repeat !== "" && newPassword !== repeat;

  const saveName = () => run(() => patch("/users/me/", { real_name: realName }), "Имя сохранено");
  const changePassword = async () => {
    if (await run(() => patch("/users/me/", { old_password: oldPassword, new_password: newPassword }), "Пароль изменён, остальные сессии закрыты")) {
      setOldPassword(""); setNewPassword(""); setRepeat(""); sessions.reload();
    }
  };
  const closeSession = async (id: number) => { if (await run(() => del(`/sessions/${id}/`))) sessions.reload(); };

  return (
    <div className="col" style={{ maxWidth: 620, gap: 18 }}>
      <div className="panel col">
        <h2>{me.name} <span className="badge">{me.role_name}</span></h2>
        <div className="row" style={{ alignItems: "flex-end" }}>
          <div className="grow"><Field label="Отображаемое имя"><input className="input" value={realName} onChange={(e) => setRealName(e.target.value)} maxLength={256} /></Field></div>
          <button className="btn" onClick={saveName} disabled={realName === me.real_name}>Сохранить</button>
        </div>
      </div>
      <div className="panel col">
        <h2>Смена пароля</h2>
        <Field label="Текущий пароль"><input className="input" type="password" autoComplete="current-password" value={oldPassword} onChange={(e) => setOldPassword(e.target.value)} /></Field>
        <Field label="Новый пароль" hint="Не короче 8 символов"><input className="input" type="password" autoComplete="new-password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} /></Field>
        <Field label="Новый пароль ещё раз"><input className="input" type="password" autoComplete="new-password" value={repeat} onChange={(e) => setRepeat(e.target.value)} /></Field>
        <ErrorBox>{mismatch ? "Пароли не совпадают" : ""}</ErrorBox>
        <div><button className="btn primary" disabled={!oldPassword || newPassword.length < 8 || newPassword !== repeat} onClick={changePassword}>Изменить пароль</button></div>
      </div>
      <div className="panel col">
        <h2>Мои сессии</h2>
        <ErrorBox>{sessions.error}</ErrorBox>
        <div className="table-wrap"><table className="table"><tbody>
          {sessions.data?.results.map((s) => (
            <tr key={s.id}>
              <td><div className="ellipsis" style={{ maxWidth: 300 }}>{s.user_agent || "Неизвестное устройство"}</div><div className="small muted mono">{s.ip} · активность {fmtDate(s.last_used)}</div></td>
              <td style={{ textAlign: "right" }}>{s.current ? <span className="badge ok">эта сессия</span> : <button className="btn sm" onClick={() => closeSession(s.id)}>Закрыть</button>}</td>
            </tr>
          ))}
        </tbody></table></div>
      </div>
    </div>
  );
}

// --- Общие настройки ---
function General() {
  const { can } = useApp();
  const run = useAction();
  const loaded = useLoad<any>(() => get("/settings/"), []);
  const [form, setForm] = useState<any>(null);
  useEffect(() => { if (loaded.data) setForm(loaded.data); }, [loaded.data]);
  const editable = can("change_runtimesetting");
  if (loaded.error) return <ErrorBox>{loaded.error}</ErrorBox>;
  if (!form) return <Spinner />;
  const set = (p: any) => setForm({ ...form, ...p });
  const save = async () => { if (await run(() => patch("/settings/", form), "Настройки сохранены")) loaded.reload(); };

  return (
    <fieldset disabled={!editable} className="col" style={{ border: 0, padding: 0, margin: 0, maxWidth: 620, gap: 18 }}>
      <div className="panel col">
        <h2>Распознавание</h2>
        <Field label={`Общий порог схожести: ${Math.round(form.face_confidence_threshold * 100)}%`}
          hint="Лицо считается совпавшим с карточкой, если схожесть не ниже порога. У списка наблюдения и группы камер может быть свой порог.">
          <input type="range" min={50} max={99} value={Math.round(form.face_confidence_threshold * 100)}
            onChange={(e) => set({ face_confidence_threshold: Number(e.target.value) / 100 })} style={{ accentColor: "var(--accent)" }} />
        </Field>
      </div>
      <div className="panel col">
        <h2>Проверка живости</h2>
        <Field label={`Порог живости: ${Math.round((form.liveness_threshold ?? 0.5) * 100)}%`}
          hint="Лицо с оценкой ниже порога считается подделкой (фото или экран). Проверка включается в настройках группы камер с контролем доступа.">
          <input type="range" min={10} max={95} value={Math.round((form.liveness_threshold ?? 0.5) * 100)}
            onChange={(e) => set({ liveness_threshold: Number(e.target.value) / 100 })} style={{ accentColor: "var(--accent)" }} />
        </Field>
      </div>
      <div className="panel col">
        <h2>Лица без совпадения</h2>
        <Check label="Создавать события для людей, которых нет в картотеке" checked={form.unmatched_events_enabled} onChange={(v) => set({ unmatched_events_enabled: v })} />
        <Check label="Требовать принятия таких событий оператором" checked={form.unmatched_acknowledge} onChange={(v) => set({ unmatched_acknowledge: v })} />
        <Check label="Отсеивать картины и фото по проверке живости" checked={form.unmatched_liveness_filter}
          onChange={(v) => set({ unmatched_liveness_filter: v })} />
        {form.unmatched_liveness_filter && (
          <Field label={`Порог отсева: ${Math.round((form.unmatched_liveness_min ?? 0.35) * 100)}%`}
            hint="Лицо без совпадения с оценкой живости ниже порога считается картиной, фото или экраном — событие не создаётся. Ниже порог — строже отсев, но выше риск отсеять настоящее лицо.">
            <input type="range" min={5} max={80} value={Math.round((form.unmatched_liveness_min ?? 0.35) * 100)}
              onChange={(e) => set({ unmatched_liveness_min: Number(e.target.value) / 100 })} style={{ accentColor: "var(--accent)" }} />
          </Field>
        )}
      </div>
      <div className="panel col">
        <h2>Удержание человека</h2>
        <Field label="Вести человека и не плодить события, секунд"
          hint="Пока человек в кадре, камера продолжает вести его столько секунд, даже если лицо на время пропало — отвернулся или его заслонили. Возвращение того же лица (определяется по математической модели лица) продолжает прежний трек и не создаёт нового события в той же группе камер. 0 — отключить.">
          <input className="input" type="number" min={0} max={3600} value={form.dedup_window ?? 0} onChange={(e) => set({ dedup_window: Number(e.target.value) })} />
        </Field>
      </div>
      <div className="panel col">
        <h2>Эпизоды</h2>
        <Field label="Закрывать эпизод, если нет новых событий, секунд">
          <input className="input" type="number" min={1} max={3600} value={form.episode_event_timeout} onChange={(e) => set({ episode_event_timeout: Number(e.target.value) })} />
        </Field>
        <Field label="Наибольшая длительность эпизода, секунд">
          <input className="input" type="number" min={1} max={86400} value={form.episode_max_duration} onChange={(e) => set({ episode_max_duration: Number(e.target.value) })} />
        </Field>
      </div>
      {editable ? <div><button className="btn primary" onClick={save}>Сохранить</button></div> : <div className="muted">Изменять настройки может только администратор.</div>}
    </fieldset>
  );
}

// --- Пользователи ---
function Users() {
  const { can, me } = useApp();
  const users = useLoad<Page<User>>(() => get("/users/", { limit: 500 }), []);
  const roles = useLoad<Page<Role>>(() => (can("group:view") ? get("/groups/") : Promise.resolve({ count: 0, results: [] })), []);
  const [editing, setEditing] = useState<User | "new" | null>(null);
  const roleName = (id: number) => roles.data?.results.find((r) => r.id === id)?.name ?? `№ ${id}`;

  return (
    <>
      <div className="toolbar">
        <div className="grow" />
        {can("user:add") && <button className="btn primary" onClick={() => setEditing("new")}><Icon name="plus" size={15} />Новый пользователь</button>}
      </div>
      <ErrorBox>{users.error}</ErrorBox>
      {users.data && (
        <div className="table-wrap"><table className="table">
          <thead><tr><th>Логин</th><th>Имя</th><th>Роль</th><th>Состояние</th><th>Создан</th></tr></thead>
          <tbody>
            {users.data.results.map((u) => (
              <tr key={u.id} style={{ cursor: can("user:change") ? "pointer" : "default" }} onClick={() => can("user:change") && setEditing(u)}>
                <td><strong>{u.name}</strong>{u.id === me.id && <span className="small muted"> · это вы</span>}</td>
                <td>{u.real_name || <span className="muted">—</span>}</td>
                <td>{roleName(u.role_id)}{u.is_superadmin && <span className="badge" style={{ marginLeft: 6 }}>первый администратор</span>}</td>
                <td>{u.active ? <span className="badge ok">активен</span> : <span className="badge">отключён</span>}</td>
                <td className="mono small">{fmtDate(u.created_date)}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
      )}
      {editing && <UserModal user={editing === "new" ? null : editing} roles={roles.data?.results ?? []} onClose={() => setEditing(null)} onSaved={users.reload} />}
    </>
  );
}

function UserModal({ user, roles, onClose, onSaved }: { user: User | null; roles: Role[]; onClose: () => void; onSaved: () => void }) {
  const { can, me } = useApp();
  const run = useAction();
  const [form, setForm] = useState({ name: user?.name ?? "", real_name: user?.real_name ?? "", role_id: user?.role_id ?? roles[roles.length - 1]?.id ?? 0, active: user?.active ?? true });
  const [password, setPassword] = useState("");
  const set = (p: Partial<typeof form>) => setForm((f) => ({ ...f, ...p }));
  const locked = !!user?.is_superadmin; // роль и активность первого администратора не меняются
  const foreignSuper = locked && user!.id !== me.id;

  const save = async () => {
    const body: any = user ? { real_name: form.real_name } : { ...form, password };
    if (user && !locked) { body.role_id = form.role_id; body.active = form.active; }
    if (user && password) body.password = password;
    if (await run(() => (user ? patch(`/users/${user.id}/`, body) : post("/users/", body)), "Пользователь сохранён")) { onSaved(); onClose(); }
  };
  const remove = async () => {
    if (!user || !confirm(`Удалить пользователя «${user.name}»?`)) return;
    if (await run(() => del(`/users/${user.id}/`), "Пользователь удалён")) { onSaved(); onClose(); }
  };

  return (
    <Modal title={user ? user.name : "Новый пользователь"} onClose={onClose} footer={<>
      {user && !locked && user.id !== me.id && can("user:delete") && <button className="btn danger" style={{ marginRight: "auto" }} onClick={remove}><Icon name="trash" size={15} />Удалить</button>}
      <button className="btn" onClick={onClose}>Отмена</button>
      <button className="btn primary" disabled={foreignSuper || !form.name.trim() || (!user && password.length < 8) || (!!password && password.length < 8)} onClick={save}>Сохранить</button>
    </>}>
      {foreignSuper && <div className="muted">Изменять первого администратора может только он сам.</div>}
      <fieldset disabled={foreignSuper} className="col" style={{ border: 0, padding: 0, margin: 0 }}>
        <Field label="Логин" hint={user ? undefined : "Латинские буквы, цифры и знаки . _ - @ +"}>
          <input className="input" value={form.name} disabled={!!user} onChange={(e) => set({ name: e.target.value })} autoComplete="off" autoFocus={!user} />
        </Field>
        <Field label="Имя"><input className="input" value={form.real_name} onChange={(e) => set({ real_name: e.target.value })} maxLength={256} /></Field>
        <Field label="Роль">
          <select className="input" value={form.role_id} disabled={locked} onChange={(e) => set({ role_id: Number(e.target.value) })}>
            {roles.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
          </select>
        </Field>
        <Field label={user ? "Новый пароль" : "Пароль"} hint={user ? "Оставьте пустым, чтобы не менять. Сброс пароля закрывает сессии пользователя." : "Не короче 8 символов"}>
          <input className="input" type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <Check label="Учётная запись активна" checked={form.active} disabled={locked || user?.id === me.id} onChange={(v) => set({ active: v })} />
      </fieldset>
    </Modal>
  );
}

// --- Роли ---
function Roles() {
  const { can } = useApp();
  const roles = useLoad<Page<Role>>(() => get("/groups/"), []);
  const [editing, setEditing] = useState<Role | "new" | null>(null);
  return (
    <>
      <div className="toolbar">
        <div className="muted grow" style={{ maxWidth: 620 }}>Роль определяет, что пользователь может видеть и изменять.</div>
        <div className="grow" />
        {can("group:add") && <button className="btn primary" onClick={() => setEditing("new")}><Icon name="plus" size={15} />Новая роль</button>}
      </div>
      <ErrorBox>{roles.error}</ErrorBox>
      {roles.data && (
        <div className="table-wrap"><table className="table">
          <thead><tr><th>Название</th><th>Прав</th><th /></tr></thead>
          <tbody>
            {roles.data.results.map((r) => (
              <tr key={r.id} style={{ cursor: "pointer" }} onClick={() => setEditing(r)}>
                <td><strong>{r.name}</strong></td>
                <td className="mono">{r.permissions.length}</td>
                <td>{r.predefined && <span className="badge">предустановленная</span>}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
      )}
      {editing && <RoleModal role={editing === "new" ? null : editing} onClose={() => setEditing(null)} onSaved={roles.reload} />}
    </>
  );
}

const ACTION_LABELS: Record<string, string> = { view: "Просмотр", add: "Создание", change: "Изменение", delete: "Удаление" };

function RoleModal({ role, onClose, onSaved }: { role: Role | null; onClose: () => void; onSaved: () => void }) {
  const { can } = useApp();
  const run = useAction();
  const catalog = useLoad<any>(() => get("/permissions/"), []);
  const [name, setName] = useState(role?.name ?? "");
  const [perms, setPerms] = useState<Set<string>>(new Set(role?.permissions ?? []));
  const editable = role ? can("group:change") : can("group:add");
  const toggle = (perm: string, on: boolean) => setPerms((old) => { const next = new Set(old); on ? next.add(perm) : next.delete(perm); return next; });

  const save = async () => {
    const body: any = { permissions: [...perms] };
    if (!role?.predefined) body.name = name;
    if (await run(() => (role ? patch(`/groups/${role.id}/`, body) : post("/groups/", body)), "Роль сохранена")) { onSaved(); onClose(); }
  };
  const remove = async () => {
    if (!role || !confirm(`Удалить роль «${role.name}»?`)) return;
    if (await run(() => del(`/groups/${role.id}/`), "Роль удалена")) { onSaved(); onClose(); }
  };

  return (
    <Modal wide title={role ? role.name : "Новая роль"} onClose={onClose} footer={<>
      {role && !role.predefined && can("group:delete") && <button className="btn danger" style={{ marginRight: "auto" }} onClick={remove}><Icon name="trash" size={15} />Удалить</button>}
      <button className="btn" onClick={onClose}>Отмена</button>
      {editable && <button className="btn primary" disabled={!name.trim()} onClick={save}>Сохранить</button>}
    </>}>
      <Field label="Название"><input className="input" value={name} disabled={!editable || !!role?.predefined} onChange={(e) => setName(e.target.value)} maxLength={128} /></Field>
      <ErrorBox>{catalog.error}</ErrorBox>
      {catalog.data && (
        <fieldset disabled={!editable} className="col" style={{ border: 0, padding: 0, margin: 0 }}>
          <div className="table-wrap"><table className="table matrix">
            <thead><tr><th>Раздел</th>{catalog.data.actions.map((a: string) => <th key={a}>{ACTION_LABELS[a] ?? a}</th>)}</tr></thead>
            <tbody>
              {Object.entries<string>(catalog.data.entities).map(([entity, label]) => (
                <tr key={entity}>
                  <td>{label}</td>
                  {catalog.data.actions.map((a: string) => (
                    <td key={a}><input type="checkbox" aria-label={`${label}: ${ACTION_LABELS[a]}`} style={{ accentColor: "var(--accent)" }}
                      checked={perms.has(`${entity}:${a}`)} onChange={(e) => toggle(`${entity}:${a}`, e.target.checked)} /></td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table></div>
          {Object.entries<string>(catalog.data.flags).map(([flag, label]) => (
            <Check key={flag} label={label} checked={perms.has(flag)} onChange={(v) => toggle(flag, v)} />
          ))}
        </fieldset>
      )}
    </Modal>
  );
}

// --- Журнал действий ---
const ACTIONS: Record<string, string> = {
  login: "Вход", logout: "Выход", login_failed: "Неудачный вход", create: "Создание", update: "Изменение", delete: "Удаление",
  acknowledge: "Принятие", unacknowledge: "Снятие принятия", acknowledge_all: "Принятие всех", batch_upload: "Пакетная загрузка",
  process: "Запуск обработки", stop: "Остановка обработки", regenerate_token: "Замена токена",
};
const OBJECTS: Record<string, string> = {
  user: "Пользователь", group: "Роль", session: "Сессия", humancard: "Карточка", faceobject: "Фото лица", watchlist: "Список наблюдения",
  cameragroup: "Группа камер", camera: "Детектор", webhook: "Вебхук", videoarchive: "Видео", faceevent: "Событие", humanepisode: "Эпизод", runtimesetting: "Настройки",
};

function Audit() {
  const [action, setAction] = useState("");
  const [objectType, setObjectType] = useState("");
  const [user, setUser] = useState("");
  const [offset, setOffset] = useState(0);
  const LIMIT = 50;
  const logs = useLoad<Page<any>>(
    () => get("/auditlogs/", { action, object_type: objectType, user_name: user, limit: LIMIT, offset }),
    [action, objectType, user, offset],
  );
  return (
    <>
      <div className="toolbar">
        <Field label="Действие">
          <select className="input" value={action} onChange={(e) => { setAction(e.target.value); setOffset(0); }}>
            <option value="">Любое</option>
            {Object.entries(ACTIONS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </Field>
        <Field label="Объект">
          <select className="input" value={objectType} onChange={(e) => { setObjectType(e.target.value); setOffset(0); }}>
            <option value="">Любой</option>
            {Object.entries(OBJECTS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </Field>
        <Field label="Логин пользователя"><input className="input" value={user} onChange={(e) => { setUser(e.target.value.trim()); setOffset(0); }} placeholder="Точное совпадение" /></Field>
      </div>
      <ErrorBox>{logs.error}</ErrorBox>
      {logs.data && (logs.data.results.length === 0 ? <Empty>Записей нет.</Empty> : (
        <div className="table-wrap"><table className="table">
          <thead><tr><th>Время</th><th>Пользователь</th><th>Действие</th><th>Объект</th><th>Подробности</th><th>Адрес</th></tr></thead>
          <tbody>
            {logs.data.results.map((l) => (
              <tr key={l.id}>
                <td className="mono small" style={{ whiteSpace: "nowrap" }}>{fmtDate(l.created_date)}</td>
                <td>{l.user_name || <span className="muted">—</span>}</td>
                <td>{l.action === "login_failed" ? <span className="badge bad">{ACTIONS[l.action]}</span> : ACTIONS[l.action] ?? l.action}</td>
                <td>{OBJECTS[l.object_type] ?? l.object_type}{l.object_id && <span className="mono muted"> № {l.object_id}</span>}</td>
                <td className="mono small muted" style={{ maxWidth: 340, wordBreak: "break-word" }}>{Object.keys(l.details).length ? JSON.stringify(l.details) : ""}</td>
                <td className="mono small muted">{l.ip}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
      ))}
      {logs.data && <Pager count={logs.data.count} limit={LIMIT} offset={offset} onChange={setOffset} />}
    </>
  );
}
