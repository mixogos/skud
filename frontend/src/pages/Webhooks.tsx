import { useState } from "react";
import { del, fmtDate, get, Page, patch, post, useLoad } from "../api";
import { useApp } from "../ctx";
import { Check, Empty, ErrorBox, Field, Icon, Modal, Seg, useAction, useToast } from "../ui";

interface Filters { matched: boolean | null; watch_lists: number[]; camera_groups: number[]; cameras: number[]; access: "granted" | "denied" | null }
interface Hook {
  id: number; name: string; url: string; active: boolean; filters: Filters; has_secret: boolean; send_attempts: number;
  last_status: string; last_error: string; last_sent_date: string | null;
}

export default function Webhooks() {
  const { can } = useApp();
  const list = useLoad<Page<Hook>>(() => get("/webhooks/"), []);
  const [editing, setEditing] = useState<Hook | "new" | null>(null);
  return (
    <>
      <div className="toolbar">
        <div className="muted grow" style={{ maxWidth: 640 }}>
          Вебхук отправляет каждое новое событие на указанный адрес — например, контроллеру, который открывает дверь при разрешённом проходе.
        </div>
        <div className="grow" />
        {can("webhook:add") && <button className="btn primary" onClick={() => setEditing("new")}><Icon name="plus" size={15} />Новый вебхук</button>}
      </div>
      <ErrorBox>{list.error}</ErrorBox>
      {list.data && (list.data.results.length === 0 ? <Empty>Вебхуков нет.</Empty> : (
        <div className="table-wrap"><table className="table">
          <thead><tr><th>Название</th><th>Адрес</th><th>Последняя отправка</th><th>Состояние</th></tr></thead>
          <tbody>
            {list.data.results.map((h) => (
              <tr key={h.id} style={{ cursor: "pointer" }} onClick={() => setEditing(h)}>
                <td><strong>{h.name}</strong></td>
                <td className="mono small" style={{ maxWidth: 320, wordBreak: "break-all" }}>{h.url}</td>
                <td>
                  {h.last_status === "ok" && <span className="badge ok">доставлено</span>}
                  {h.last_status === "error" && <span className="badge bad" title={h.last_error}>ошибка</span>}
                  {h.last_sent_date && <div className="small muted mono">{fmtDate(h.last_sent_date)}</div>}
                  {h.last_status === "error" && <div className="small" style={{ color: "var(--danger)" }}>{h.last_error}</div>}
                </td>
                <td>{h.active ? <span className="badge ok">активен</span> : <span className="badge">выключен</span>}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
      ))}
      {editing && <HookModal hook={editing === "new" ? null : editing} onClose={() => setEditing(null)} onSaved={list.reload} />}
    </>
  );
}

function HookModal({ hook, onClose, onSaved }: { hook: Hook | null; onClose: () => void; onSaved: () => void }) {
  const { can, lists, groups } = useApp();
  const run = useAction();
  const toast = useToast();
  const [form, setForm] = useState({ name: hook?.name ?? "", url: hook?.url ?? "", active: hook?.active ?? true, send_attempts: hook?.send_attempts ?? 3 });
  const [filters, setFilters] = useState<Filters>(hook?.filters ?? { matched: null, watch_lists: [], camera_groups: [], cameras: [], access: null });
  const [secret, setSecret] = useState("");
  const [clearSecret, setClearSecret] = useState(false);
  const editable = hook ? can("webhook:change") : can("webhook:add");
  const set = (p: Partial<typeof form>) => setForm((f) => ({ ...f, ...p }));
  const toggle = (key: "watch_lists" | "camera_groups", id: number, on: boolean) =>
    setFilters((f) => ({ ...f, [key]: on ? [...f[key], id] : f[key].filter((x) => x !== id) }));

  const save = async () => {
    const body: any = { ...form, filters };
    if (secret) body.secret = secret; else if (clearSecret) body.secret = "";
    if (await run(() => (hook ? patch(`/webhooks/${hook.id}/`, body) : post("/webhooks/", body)), "Вебхук сохранён")) { onSaved(); onClose(); }
  };
  const test = async () => {
    if (!hook) return;
    try {
      const result = await post(`/webhooks/${hook.id}/test/`);
      toast(result.ok ? "Пробное сообщение доставлено" : `Не доставлено: ${result.error}`, !result.ok);
      onSaved();
    } catch (e: any) { toast(e.message, true); }
  };
  const remove = async () => {
    if (!hook || !confirm(`Удалить вебхук «${hook.name}»?`)) return;
    if (await run(() => del(`/webhooks/${hook.id}/`), "Вебхук удалён")) { onSaved(); onClose(); }
  };

  return (
    <Modal title={hook ? hook.name : "Новый вебхук"} onClose={onClose} footer={<>
      {hook && can("webhook:delete") && <button className="btn danger" style={{ marginRight: "auto" }} onClick={remove}><Icon name="trash" size={15} />Удалить</button>}
      {hook && editable && <button className="btn" onClick={test}>Проверить</button>}
      <button className="btn" onClick={onClose}>Отмена</button>
      {editable && <button className="btn primary" disabled={!form.name.trim() || !/^https?:\/\/.+/.test(form.url)} onClick={save}>Сохранить</button>}
    </>}>
      <fieldset disabled={!editable} className="col" style={{ border: 0, padding: 0, margin: 0 }}>
        <Field label="Название"><input className="input" value={form.name} onChange={(e) => set({ name: e.target.value })} maxLength={256} autoFocus={!hook} /></Field>
        <Field label="Адрес" hint="Сервер отправит POST-запрос с описанием события в формате JSON">
          <input className="input mono" value={form.url} onChange={(e) => set({ url: e.target.value.trim() })} placeholder="http://192.168.1.50/open" />
        </Field>
        <Field label="Какие события отправлять">
          <div className="col" style={{ gap: 8 }}>
            <Seg value={filters.access ?? ""} onChange={(v) => setFilters({ ...filters, access: (v || null) as Filters["access"] })}
              options={[["", "Любой проход"], ["granted", "Только разрешённый"], ["denied", "Только отказ"]]} />
            <Seg value={filters.matched == null ? "" : String(filters.matched)} onChange={(v) => setFilters({ ...filters, matched: v === "" ? null : v === "true" })}
              options={[["", "Любые"], ["true", "С совпадением"], ["false", "Без совпадения"]]} />
          </div>
        </Field>
        {lists.length > 0 && (
          <Field label="Только по спискам наблюдения" hint="Ничего не отмечено — по любым">
            <div className="row wrap" style={{ gap: "6px 16px" }}>
              {lists.map((l) => <Check key={l.id} label={<span className="chip"><i style={{ background: l.color }} />{l.name}</span>} checked={filters.watch_lists.includes(l.id)} onChange={(v) => toggle("watch_lists", l.id, v)} />)}
            </div>
          </Field>
        )}
        {groups.length > 0 && (
          <Field label="Только по группам камер" hint="Ничего не отмечено — по любым">
            <div className="row wrap" style={{ gap: "6px 16px" }}>
              {groups.map((g) => <Check key={g.id} label={g.name} checked={filters.camera_groups.includes(g.id)} onChange={(v) => toggle("camera_groups", g.id, v)} />)}
            </div>
          </Field>
        )}
        <Field label="Секрет для подписи" hint={hook?.has_secret ? "Секрет задан. Введите новый, чтобы заменить." : "Необязательно. Запросы подписываются в заголовке X-Skud-Signature (HMAC-SHA256), чтобы получатель мог проверить отправителя."}>
          <input className="input" type="password" autoComplete="new-password" value={secret} onChange={(e) => setSecret(e.target.value)} maxLength={256} />
        </Field>
        {hook?.has_secret && !secret && <Check label="Убрать секрет" checked={clearSecret} onChange={setClearSecret} />}
        <Field label="Попыток отправки"><input className="input" type="number" min={1} max={10} value={form.send_attempts} onChange={(e) => set({ send_attempts: Number(e.target.value) })} /></Field>
        <Check label="Вебхук активен" checked={form.active} onChange={(v) => set({ active: v })} />
      </fieldset>
    </Modal>
  );
}
