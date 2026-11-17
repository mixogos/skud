import { useEffect, useState } from "react";
import { del, fmtSmart, get, Page, patch, post, useLoad } from "../api";
import { useApp } from "../ctx";
import { Check, ErrorBox, Field, Icon, Modal, useAction, useBlob } from "../ui";

interface Camera {
  id: number; name: string; group_id: number; comment: string; active: boolean; url: string;
  stream_settings: { process_fps?: number; max_track_duration?: number };
  state: string; state_message: string; frames_processed: number; events_created: number; fps: number;
  width: number; height: number; last_frame_date: string | null;
}

const STATES: Record<string, [string, string]> = {
  DISABLED: ["выключена", ""], CONNECTING: ["подключение", "warn"], ONLINE: ["на связи", "ok"], ERROR: ["нет связи", "bad"],
};

export default function Cameras() {
  const { can, groupById } = useApp();
  const list = useLoad<Page<Camera>>(() => get("/cameras/", { external_detector: false }), []);
  const [editing, setEditing] = useState<number | "new" | null>(null);

  // Состояние камер меняется само, поэтому список обновляется каждые три секунды.
  const anyActive = list.data?.results.some((c) => c.active) ?? false;
  useEffect(() => {
    if (!anyActive) return;
    const timer = setInterval(list.reload, 3000);
    return () => clearInterval(timer);
  }, [anyActive, list.reload]);
  const current = typeof editing === "number" ? list.data?.results.find((c) => c.id === editing) ?? null : null;

  return (
    <section>
      <div className="toolbar">
        <div className="grow">
          <h2>Камеры</h2>
          <div className="small muted">Сервер сам подключается к потоку камеры и создаёт события в реальном времени.</div>
        </div>
        {can("camera:add") && <button className="btn" onClick={() => setEditing("new")}><Icon name="plus" size={15} />Новая камера</button>}
      </div>
      <ErrorBox>{list.error}</ErrorBox>
      {list.data && (list.data.results.length === 0
        ? <div className="muted small">Камер нет. Добавьте адрес потока — например, <span className="mono">rtsp://логин:пароль@адрес-камеры/…</span></div>
        : <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Название</th><th>Группа камер</th><th>Состояние</th><th>Кадров в секунду</th><th>Событий</th><th>Последний кадр</th></tr></thead>
              <tbody>
                {list.data.results.map((c) => {
                  const [label, tone] = STATES[c.state] ?? [c.state, ""];
                  return (
                    <tr key={c.id} style={{ cursor: "pointer" }} onClick={() => setEditing(c.id)}>
                      <td><strong>{c.name}</strong><div className="small muted mono ellipsis" style={{ maxWidth: 320 }}>{c.url}</div></td>
                      <td>{groupById.get(c.group_id)?.name ?? `№ ${c.group_id}`}</td>
                      <td><span className={`badge ${tone}`}>{label}</span>{c.state_message && <div className="small muted" style={{ maxWidth: 280 }}>{c.state_message}</div>}</td>
                      <td className="mono">{c.state === "ONLINE" ? c.fps.toFixed(1) : "—"}</td>
                      <td className="mono">{c.events_created}</td>
                      <td className="mono small">{c.last_frame_date ? fmtSmart(c.last_frame_date) : "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>)}
      {editing && (editing === "new" || current) && <CameraModal camera={current} onClose={() => setEditing(null)} onSaved={list.reload} />}
    </section>
  );
}

function Preview({ camera }: { camera: Camera }) {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setTick((t) => t + 1), 2000);
    return () => clearInterval(timer);
  }, []);
  // Прежний кадр остаётся на экране, пока грузится следующий.
  const fresh = useBlob(camera.state === "ONLINE" ? `/cameras/${camera.id}/snapshot/?t=${tick}` : null);
  const [shown, setShown] = useState<string | null>(null);
  useEffect(() => { if (fresh) setShown(fresh); }, [fresh]);
  if (camera.state !== "ONLINE" || !shown) return null;
  return (
    <div>
      <img src={shown} alt={`Кадр с камеры «${camera.name}»`} style={{ width: "100%", borderRadius: 8, border: "1px solid var(--border)", display: "block" }} />
      <div className="small muted mono" style={{ marginTop: 4 }}>{camera.width}×{camera.height}, обрабатывается {camera.fps.toFixed(1)} кадр/с</div>
    </div>
  );
}

function CameraModal({ camera, onClose, onSaved }: { camera: Camera | null; onClose: () => void; onSaved: () => void }) {
  const { can, groups } = useApp();
  const run = useAction();
  const [form, setForm] = useState({
    name: camera?.name ?? "", url: camera?.url ?? "", comment: camera?.comment ?? "",
    group_id: camera?.group_id ?? groups[0]?.id ?? 0, active: camera?.active ?? true,
    process_fps: camera?.stream_settings.process_fps ?? 5, max_track_duration: camera?.stream_settings.max_track_duration ?? 0,
  });
  const editable = camera ? can("camera:change") : can("camera:add");
  const set = (p: Partial<typeof form>) => setForm((f) => ({ ...f, ...p }));

  const save = async () => {
    const { process_fps, max_track_duration, ...rest } = form;
    const body = { ...rest, stream_settings: { ...(camera?.stream_settings ?? {}), process_fps, max_track_duration } };
    if (await run(() => (camera ? patch(`/cameras/${camera.id}/`, body) : post("/cameras/", body)), "Камера сохранена")) { onSaved(); onClose(); }
  };
  const remove = async () => {
    if (!camera || !confirm(`Удалить камеру «${camera.name}»? События останутся.`)) return;
    if (await run(() => del(`/cameras/${camera.id}/`), "Камера удалена")) { onSaved(); onClose(); }
  };

  return (
    <Modal title={camera ? camera.name : "Новая камера"} onClose={onClose} footer={<>
      {camera && can("camera:delete") && <button className="btn danger" style={{ marginRight: "auto" }} onClick={remove}><Icon name="trash" size={15} />Удалить</button>}
      <button className="btn" onClick={onClose}>Отмена</button>
      {editable && <button className="btn primary" disabled={!form.name.trim() || !form.url.trim() || !form.group_id} onClick={save}>{camera ? "Сохранить" : "Создать"}</button>}
    </>}>
      {camera && <Preview camera={camera} />}
      <fieldset disabled={!editable} className="col" style={{ border: 0, padding: 0, margin: 0 }}>
        <Field label="Название"><input className="input" value={form.name} onChange={(e) => set({ name: e.target.value })} maxLength={256} autoFocus={!camera} /></Field>
        <Field label="Адрес потока" hint="RTSP, HTTP или RTMP. Для веб-камеры, подключённой к серверу, — её номер: 0. Пароль после сохранения скрывается.">
          <input className="input mono" value={form.url} onChange={(e) => set({ url: e.target.value })} maxLength={1024} placeholder="rtsp://логин:пароль@192.168.1.10:554/stream1" spellCheck={false} autoComplete="off" />
        </Field>
        <Field label="Группа камер">
          <select className="input" value={form.group_id} onChange={(e) => set({ group_id: Number(e.target.value) })}>
            {groups.map((g) => <option key={g.id} value={g.id}>{g.name}{g.access_control ? " — контроль доступа" : ""}</option>)}
          </select>
        </Field>
        <Field label="Анализировать кадров в секунду" hint="Больше — точнее на быстром движении, но выше нагрузка на процессор">
          <input className="input" type="number" min={1} max={30} value={form.process_fps} onChange={(e) => set({ process_fps: Number(e.target.value) })} />
        </Field>
        <Field label="Событие не позже чем через, секунд" hint="0 — событие создаётся, когда человек ушёл из кадра. Для двери ставьте 1–2 и включите в группе защиту от повторных событий.">
          <input className="input" type="number" min={0} max={600} step={0.5} value={form.max_track_duration} onChange={(e) => set({ max_track_duration: Number(e.target.value) })} />
        </Field>
        <Field label="Комментарий"><textarea className="input" rows={2} value={form.comment} onChange={(e) => set({ comment: e.target.value })} maxLength={2048} /></Field>
        <Check label="Камера включена" checked={form.active} onChange={(v) => set({ active: v })} />
      </fieldset>
    </Modal>
  );
}
