import { useState } from "react";
import { del, patch, post } from "../api";
import { useApp, WatchList } from "../ctx";
import { describeSchedule, Schedule, ScheduleEditor } from "../schedule";
import { Check, Empty, Field, Icon, Modal, Seg, useAction } from "../ui";

export default function WatchListsPage() {
  const { lists, can, reloadRefs } = useApp();
  const [editing, setEditing] = useState<WatchList | "new" | null>(null);

  return (
    <>
      <div className="toolbar">
        <div className="muted grow" style={{ maxWidth: 640 }}>
          Список наблюдения объединяет карточки по смыслу: персонал, гости, нежелательные посетители. Система ищет людей только из активных списков.
        </div>
        <div className="grow" />
        {can("watchlist:add") && <button className="btn primary" onClick={() => setEditing("new")}><Icon name="plus" size={15} />Новый список</button>}
      </div>
      {lists.length === 0 ? <Empty>Списков нет.</Empty> : (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Название</th><th>Проход</th><th>Порог схожести</th><th>Правила</th><th>Состояние</th></tr></thead>
            <tbody>
              {lists.map((l) => (
                <tr key={l.id} style={{ cursor: "pointer" }} onClick={() => setEditing(l)}>
                  <td>
                    <span className="chip" style={{ fontSize: 14, fontWeight: 500 }}><i style={{ background: l.color, width: 10, height: 10 }} />{l.name}</span>
                    {l.comment && <div className="small muted">{l.comment}</div>}
                  </td>
                  <td>
                    {l.access_policy === "allow" && <><span className="badge ok">разрешает</span><div className="small muted">{describeSchedule(l.access_schedule)}</div></>}
                    {l.access_policy === "deny" && <span className="badge bad">запрещает</span>}
                    {l.access_policy === "none" && <span className="muted">не влияет</span>}
                  </td>
                  <td className="mono">{l.face_threshold != null ? `${(l.face_threshold * 100).toFixed(0)}%` : <span className="muted">общий</span>}</td>
                  <td>
                    <div className="row wrap">
                      {l.acknowledge && <span className="badge warn">требует принятия</span>}
                      {l.notify && <span className="badge">звук</span>}
                      {l.ignore_events && <span className="badge">без событий</span>}
                    </div>
                  </td>
                  <td>{l.active ? <span className="badge ok">активен</span> : <span className="badge">выключен</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {editing && <ListModal list={editing === "new" ? null : editing} onClose={() => setEditing(null)} onSaved={reloadRefs} />}
    </>
  );
}

function ListModal({ list, onClose, onSaved }: { list: WatchList | null; onClose: () => void; onSaved: () => void }) {
  const { can } = useApp();
  const run = useAction();
  const [form, setForm] = useState({
    name: list?.name ?? "", comment: list?.comment ?? "", color: list?.color ?? "#2fd6a3", active: list?.active ?? true,
    acknowledge: list?.acknowledge ?? false, notify: list?.notify ?? false, ignore_events: list?.ignore_events ?? false,
    access_policy: list?.access_policy ?? "none", access_schedule: (list?.access_schedule ?? {}) as Schedule,
  });
  const [ownThreshold, setOwnThreshold] = useState(list?.face_threshold != null);
  const [threshold, setThreshold] = useState(Math.round((list?.face_threshold ?? 0.7) * 100));
  const editable = list ? can("watchlist:change") : can("watchlist:add");
  const set = (p: Partial<typeof form>) => setForm((f) => ({ ...f, ...p }));

  const save = async () => {
    const body = { ...form, face_threshold: ownThreshold ? threshold / 100 : null };
    if (await run(() => (list ? patch(`/watch-lists/${list.id}/`, body) : post("/watch-lists/", body)), "Список сохранён")) { onSaved(); onClose(); }
  };
  const remove = async () => {
    if (!list || !confirm(`Удалить список «${list.name}»?`)) return;
    if (await run(() => del(`/watch-lists/${list.id}/`), "Список удалён")) { onSaved(); onClose(); }
  };

  return (
    <Modal title={list ? list.name : "Новый список наблюдения"} onClose={onClose} footer={<>
      {list && !list.predefined && can("watchlist:delete") && <button className="btn danger" style={{ marginRight: "auto" }} onClick={remove}><Icon name="trash" size={15} />Удалить</button>}
      <button className="btn" onClick={onClose}>Отмена</button>
      {editable && <button className="btn primary" disabled={!form.name.trim()} onClick={save}>Сохранить</button>}
    </>}>
      <fieldset disabled={!editable} style={{ border: 0, padding: 0, margin: 0 }} className="col">
        <div className="row" style={{ alignItems: "flex-end" }}>
          <div className="grow"><Field label="Название"><input className="input" value={form.name} onChange={(e) => set({ name: e.target.value })} maxLength={256} autoFocus={!list} /></Field></div>
          <Field label="Цвет"><input type="color" className="input" style={{ width: 52, padding: 2 }} value={form.color} onChange={(e) => set({ color: e.target.value })} /></Field>
        </div>
        <Field label="Комментарий"><textarea className="input" rows={2} value={form.comment} onChange={(e) => set({ comment: e.target.value })} maxLength={2048} /></Field>
        <Check label="Список активен — искать людей из него" checked={form.active} onChange={(v) => set({ active: v })} />
        <Check label="Требовать принятия событий оператором" checked={form.acknowledge} onChange={(v) => set({ acknowledge: v })} />
        <Check label="Звуковое оповещение при событии" checked={form.notify} onChange={(v) => set({ notify: v })} />
        <Check label="Не создавать события для людей из списка" checked={form.ignore_events} onChange={(v) => set({ ignore_events: v })} />
        <Field label="Контроль доступа" hint="Действует в группах камер, где включён контроль доступа. Запрет сильнее разрешения.">
          <Seg value={form.access_policy} onChange={(v) => set({ access_policy: v })} options={[["none", "Не влияет"], ["allow", "Разрешает проход"], ["deny", "Запрещает проход"]]} />
        </Field>
        {form.access_policy === "allow" && <ScheduleEditor value={form.access_schedule} onChange={(v) => set({ access_schedule: v })} />}
        <Check label="Собственный порог схожести" checked={ownThreshold} onChange={setOwnThreshold} />
        {ownThreshold && (
          <Field label={`Порог: ${threshold}%`} hint="Выше порог — меньше ложных совпадений, но больше пропусков. Общий порог подходит в большинстве случаев.">
            <input type="range" min={50} max={99} value={threshold} onChange={(e) => setThreshold(Number(e.target.value))} style={{ accentColor: "var(--accent)" }} />
          </Field>
        )}
      </fieldset>
    </Modal>
  );
}
