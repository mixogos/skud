import { useState } from "react";
import { del, get, Page, patch, post, useLoad } from "../api";
import { useApp } from "../ctx";
import { Check, ErrorBox, Field, Icon, Modal, useAction, useToast } from "../ui";

interface Detector { id: number; name: string; group_id: number; comment: string; active: boolean; token: string | null }

export default function Detectors() {
  const { can, groupById } = useApp();
  const list = useLoad<Page<Detector>>(() => get("/cameras/", { external_detector: true }), []);
  const [editing, setEditing] = useState<Detector | "new" | null>(null);
  const [issued, setIssued] = useState<Detector | null>(null);

  return (
    <section>
      <div className="toolbar">
        <div className="grow">
          <h2>Внешние детекторы</h2>
          <div className="small muted">Терминалы у дверей и другие устройства, которые сами присылают кадр и получают решение о проходе.</div>
        </div>
        {can("camera:add") && <button className="btn" onClick={() => setEditing("new")}><Icon name="plus" size={15} />Новый детектор</button>}
      </div>
      <ErrorBox>{list.error}</ErrorBox>
      {list.data && (list.data.results.length === 0
        ? <div className="muted small">Детекторов нет.</div>
        : <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Название</th><th>Группа камер</th><th>Состояние</th></tr></thead>
              <tbody>
                {list.data.results.map((d) => (
                  <tr key={d.id} style={{ cursor: "pointer" }} onClick={() => setEditing(d)}>
                    <td><strong>{d.name}</strong>{d.comment && <div className="small muted">{d.comment}</div>}</td>
                    <td>{groupById.get(d.group_id)?.name ?? `№ ${d.group_id}`}{groupById.get(d.group_id)?.access_control === false && <div className="small" style={{ color: "var(--warn)" }}>у группы не включён контроль доступа</div>}</td>
                    <td>{d.active ? <span className="badge ok">активен</span> : <span className="badge">отключён</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>)}
      {editing && <DetectorModal detector={editing === "new" ? null : editing} onClose={() => setEditing(null)} onSaved={list.reload} onToken={setIssued} />}
      {issued && <TokenModal detector={issued} onClose={() => setIssued(null)} />}
    </section>
  );
}

function DetectorModal({ detector, onClose, onSaved, onToken }: { detector: Detector | null; onClose: () => void; onSaved: () => void; onToken: (d: Detector) => void }) {
  const { can, groups } = useApp();
  const run = useAction();
  const [form, setForm] = useState({ name: detector?.name ?? "", comment: detector?.comment ?? "", group_id: detector?.group_id ?? groups[0]?.id ?? 0, active: detector?.active ?? true });
  const editable = detector ? can("camera:change") : can("camera:add");
  const set = (p: Partial<typeof form>) => setForm((f) => ({ ...f, ...p }));

  const save = async () => {
    let created: Detector | undefined;
    const ok = await run(async () => {
      if (detector) await patch(`/cameras/${detector.id}/`, form);
      else created = await post("/cameras/", form);
    }, detector ? "Детектор сохранён" : undefined);
    if (!ok) return;
    onSaved();
    onClose();
    if (created) onToken(created);
  };
  const newToken = async () => {
    if (!detector || !confirm("Выдать новый токен? Прежний сразу перестанет действовать, устройство нужно будет перенастроить.")) return;
    let updated: Detector | undefined;
    if (await run(async () => { updated = await post(`/cameras/${detector.id}/token/`); })) { onClose(); onToken(updated!); }
  };
  const remove = async () => {
    if (!detector || !confirm(`Удалить детектор «${detector.name}»? События останутся.`)) return;
    if (await run(() => del(`/cameras/${detector.id}/`), "Детектор удалён")) { onSaved(); onClose(); }
  };

  return (
    <Modal title={detector ? detector.name : "Новый внешний детектор"} onClose={onClose} footer={<>
      {detector && can("camera:delete") && <button className="btn danger" style={{ marginRight: "auto" }} onClick={remove}><Icon name="trash" size={15} />Удалить</button>}
      <button className="btn" onClick={onClose}>Отмена</button>
      {editable && <button className="btn primary" disabled={!form.name.trim() || !form.group_id} onClick={save}>{detector ? "Сохранить" : "Создать"}</button>}
    </>}>
      <fieldset disabled={!editable} className="col" style={{ border: 0, padding: 0, margin: 0 }}>
        <Field label="Название"><input className="input" value={form.name} onChange={(e) => set({ name: e.target.value })} maxLength={256} autoFocus={!detector} /></Field>
        <Field label="Группа камер" hint="Решение о проходе принимается, если у группы включён контроль доступа">
          <select className="input" value={form.group_id} onChange={(e) => set({ group_id: Number(e.target.value) })}>
            {groups.map((g) => <option key={g.id} value={g.id}>{g.name}{g.access_control ? " — контроль доступа" : ""}</option>)}
          </select>
        </Field>
        <Field label="Комментарий"><textarea className="input" rows={2} value={form.comment} onChange={(e) => set({ comment: e.target.value })} maxLength={2048} /></Field>
        <Check label="Детектор активен" checked={form.active} onChange={(v) => set({ active: v })} />
      </fieldset>
      {detector && editable && <div><button className="btn" onClick={newToken}>Выдать новый токен</button></div>}
    </Modal>
  );
}

function TokenModal({ detector, onClose }: { detector: Detector; onClose: () => void }) {
  const toast = useToast();
  const copy = () => navigator.clipboard.writeText(detector.token ?? "").then(() => toast("Токен скопирован"), () => toast("Не удалось скопировать — выделите токен вручную", true));
  const example = `curl -X POST ${location.origin}/events/faces/add/ \\\n  -H "Authorization: Token <токен>" \\\n  -F "fullframe=@кадр.jpg"`;
  return (
    <Modal title={`Токен детектора «${detector.name}»`} onClose={onClose} footer={<button className="btn primary" onClick={onClose}>Я сохранил токен</button>}>
      <div className="error" style={{ color: "var(--warn)", borderColor: "color-mix(in srgb, var(--warn) 45%, transparent)", background: "color-mix(in srgb, var(--warn) 10%, transparent)" }}>
        Токен показывается один раз. Сохраните его в настройках устройства — после закрытия окна посмотреть его будет нельзя, только выдать новый.
      </div>
      <div className="row">
        <input className="input mono" readOnly value={detector.token ?? ""} onFocus={(e) => e.target.select()} aria-label="Токен детектора" />
        <button className="btn" onClick={copy}><Icon name="copy" size={15} />Копировать</button>
      </div>
      <Field label="Как устройство отправляет кадр">
        <pre className="panel mono small" style={{ margin: 0, overflowX: "auto", whiteSpace: "pre" }}>{example}</pre>
      </Field>
      <div className="small muted">В ответе придут поля <span className="mono">access</span> (<span className="mono">granted</span> или <span className="mono">denied</span>), <span className="mono">reason</span> и данные карточки.</div>
    </Modal>
  );
}
