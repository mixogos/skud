import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { del, fmtDate, fmtDuration, fmtSize, get, Page, patch, post, postForm, useLoad } from "../api";
import { CameraGroup, useApp } from "../ctx";
import Cameras from "./Cameras";
import Detectors from "./Detectors";
import { Check, Empty, ErrorBox, Field, Icon, Modal, Spinner, useAction } from "../ui";

interface Video {
  id: number; name: string; camera_group_id: number; file_size: number; state: string; progress: number; error: string;
  duration: number; width: number; height: number; frames_processed: number; events_created: number; created_date: string;
}

const STATES: Record<string, [string, string]> = {
  UPLOADED: ["загружен", ""], QUEUED: ["в очереди", "warn"], PROCESSING: ["обрабатывается", "warn"],
  COMPLETED: ["обработан", "ok"], FAILED: ["ошибка", "bad"], CANCELLED: ["остановлен", ""],
};
const isActive = (v: Video) => v.state === "QUEUED" || v.state === "PROCESSING";

export default function VideosPage() {
  const { can, groups, groupById, reloadRefs } = useApp();
  const run = useAction();
  const [uploading, setUploading] = useState(false);
  const [group, setGroup] = useState<CameraGroup | "new" | null>(null);
  const list = useLoad<Page<Video>>(() => get("/videos/", { limit: 200 }), []);

  // Пока есть файлы в работе, состояние обновляется каждые полторы секунды.
  const busy = list.data?.results.some(isActive) ?? false;
  useEffect(() => {
    if (!busy) return;
    const timer = setInterval(list.reload, 1500);
    return () => clearInterval(timer);
  }, [busy, list.reload]);

  const act = async (video: Video, action: "process" | "stop") => {
    if (action === "process" && video.events_created > 0 && !confirm("Повторная обработка заменит события этого файла. Продолжить?")) return;
    if (await run(() => post(`/videos/${video.id}/${action}/`))) list.reload();
  };
  const remove = async (video: Video) => {
    if (!confirm(`Удалить «${video.name}» вместе с событиями (${video.events_created})?`)) return;
    if (await run(() => del(`/videos/${video.id}/`), "Видео удалено")) list.reload();
  };

  return (
    <div className="col" style={{ gap: 26 }}>
      <section>
        <div className="toolbar">
          <h2 className="grow">Видеофайлы</h2>
          {can("videoarchive:add") && <button className="btn primary" onClick={() => setUploading(true)}><Icon name="upload" size={15} />Загрузить видео</button>}
        </div>
        <ErrorBox>{list.error}</ErrorBox>
        {!list.data && list.loading && <Spinner />}
        {list.data && (list.data.results.length === 0
          ? <Empty>Видео нет. Загрузите запись — система найдёт на ней лица и создаст события.</Empty>
          : <div className="table-wrap">
              <table className="table">
                <thead><tr><th>Название</th><th>Группа камер</th><th>Запись</th><th>Состояние</th><th>События</th><th /></tr></thead>
                <tbody>
                  {list.data.results.map((v) => {
                    const [label, tone] = STATES[v.state] ?? [v.state, ""];
                    return (
                      <tr key={v.id}>
                        <td><strong>{v.name}</strong><div className="small muted">{fmtDate(v.created_date)}</div></td>
                        <td>{groupById.get(v.camera_group_id)?.name ?? `№ ${v.camera_group_id}`}</td>
                        <td className="mono small">{fmtDuration(v.duration)} · {v.width}×{v.height} · {fmtSize(v.file_size)}</td>
                        <td style={{ minWidth: 170 }}>
                          <span className={`badge ${tone}`}>{label}{v.state === "PROCESSING" ? ` ${(v.progress * 100).toFixed(0)}%` : ""}</span>
                          {v.state === "PROCESSING" && <div className="progress" style={{ marginTop: 6 }}><i style={{ width: `${v.progress * 100}%` }} /></div>}
                          {v.error && <div className="small" style={{ color: "var(--danger)" }}>{v.error}</div>}
                        </td>
                        <td>{v.events_created > 0 ? <Link className="mono" to={`/events?video=${v.id}`}>{v.events_created}</Link> : <span className="muted mono">0</span>}</td>
                        <td>
                          <div className="row" style={{ justifyContent: "flex-end" }}>
                            {can("videoarchive:change") && (isActive(v)
                              ? <button className="btn sm" onClick={() => act(v, "stop")}>Остановить</button>
                              : <button className="btn sm" onClick={() => act(v, "process")}><Icon name="play" size={12} />{v.state === "UPLOADED" ? "Обработать" : "Заново"}</button>)}
                            {can("videoarchive:delete") && !isActive(v) && <button className="btn sm danger" onClick={() => remove(v)} aria-label="Удалить"><Icon name="trash" size={13} /></button>}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>)}
      </section>

      <section>
        <div className="toolbar">
          <h2 className="grow">Группы камер</h2>
          {can("cameragroup:add") && <button className="btn" onClick={() => setGroup("new")}><Icon name="plus" size={15} />Новая группа</button>}
        </div>
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Название</th><th>Контроль доступа</th><th>Порог схожести</th><th>Дедупликация событий</th></tr></thead>
            <tbody>
              {groups.map((g) => (
                <tr key={g.id} style={{ cursor: "pointer" }} onClick={() => setGroup(g)}>
                  <td><strong>{g.name}</strong>{g.comment && <div className="small muted">{g.comment}</div>}</td>
                  <td>{g.access_control ? <span className="badge ok">{g.liveness_check ? "включён, с проверкой живости" : "включён"}</span> : <span className="muted">нет</span>}</td>
                  <td className="mono">{g.face_threshold != null ? `${(g.face_threshold * 100).toFixed(0)}%` : <span className="muted">общий</span>}</td>
                  <td>{g.deduplicate ? <span className="badge ok">{g.deduplicate_delay} с</span> : <span className="muted">выключена</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {can("camera:view") && <Cameras />}

      {can("camera:view") && <Detectors />}

      {uploading && <UploadModal onClose={() => setUploading(false)} onDone={list.reload} />}
      {group && <GroupModal group={group === "new" ? null : group} onClose={() => setGroup(null)} onSaved={reloadRefs} />}
    </div>
  );
}

function UploadModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const { groups } = useApp();
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [groupId, setGroupId] = useState(groups[0]?.id ?? 0);
  const [fps, setFps] = useState(5);
  const [process, setProcess] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const upload = async () => {
    if (!file) return;
    setBusy(true);
    setError("");
    const form = new FormData();
    form.set("source_file", file);
    form.set("camera_group", String(groupId));
    if (name.trim()) form.set("name", name.trim());
    form.set("stream_settings", JSON.stringify({ process_fps: fps }));
    form.set("process", String(process));
    try {
      await postForm("/videos/", form);
      onDone();
      onClose();
    } catch (e: any) {
      setError(e.message);
      setBusy(false);
    }
  };

  return (
    <Modal title="Загрузка видео" onClose={onClose} footer={<>
      <button className="btn" onClick={onClose} disabled={busy}>Отмена</button>
      <button className="btn primary" disabled={busy || !file || !groupId} onClick={upload}>{busy ? "Загрузка…" : "Загрузить"}</button>
    </>}>
      <Field label="Видеофайл" hint="MP4, AVI, MKV, MOV, WEBM и другие; до 4 ГБ">
        <input className="input" style={{ padding: "5px 8px" }} type="file" accept="video/*,.mkv,.avi,.ts,.flv,.wmv" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
      </Field>
      <Field label="Название" hint="По умолчанию — имя файла"><input className="input" value={name} onChange={(e) => setName(e.target.value)} maxLength={256} /></Field>
      <Field label="Группа камер">
        <select className="input" value={groupId} onChange={(e) => setGroupId(Number(e.target.value))}>
          {groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
        </select>
      </Field>
      <Field label={`Анализировать кадров в секунду: ${fps}`} hint="Больше кадров — надёжнее на быстром движении, но обработка дольше">
        <input type="range" min={1} max={15} value={fps} onChange={(e) => setFps(Number(e.target.value))} style={{ accentColor: "var(--accent)" }} />
      </Field>
      <Check label="Сразу начать обработку" checked={process} onChange={setProcess} />
      <ErrorBox>{error}</ErrorBox>
    </Modal>
  );
}

function GroupModal({ group, onClose, onSaved }: { group: CameraGroup | null; onClose: () => void; onSaved: () => void }) {
  const { can } = useApp();
  const run = useAction();
  const [form, setForm] = useState({
    name: group?.name ?? "", comment: group?.comment ?? "", deduplicate: group?.deduplicate ?? false, deduplicate_delay: group?.deduplicate_delay ?? 15,
    access_control: group?.access_control ?? false, liveness_check: group?.liveness_check ?? false,
    detect_people: group?.detect_people ?? false,
  });
  const [ownThreshold, setOwnThreshold] = useState(group?.face_threshold != null);
  const [threshold, setThreshold] = useState(Math.round((group?.face_threshold ?? 0.7) * 100));
  const editable = group ? can("cameragroup:change") : can("cameragroup:add");
  const set = (p: Partial<typeof form>) => setForm((f) => ({ ...f, ...p }));

  const save = async () => {
    const body = { ...form, face_threshold: ownThreshold ? threshold / 100 : null };
    if (await run(() => (group ? patch(`/camera-groups/${group.id}/`, body) : post("/camera-groups/", body)), "Группа сохранена")) { onSaved(); onClose(); }
  };
  const remove = async () => {
    if (!group || !confirm(`Удалить группу «${group.name}»?`)) return;
    if (await run(() => del(`/camera-groups/${group.id}/`), "Группа удалена")) { onSaved(); onClose(); }
  };

  return (
    <Modal title={group ? group.name : "Новая группа камер"} onClose={onClose} footer={<>
      {group && !group.predefined && can("cameragroup:delete") && <button className="btn danger" style={{ marginRight: "auto" }} onClick={remove}><Icon name="trash" size={15} />Удалить</button>}
      <button className="btn" onClick={onClose}>Отмена</button>
      {editable && <button className="btn primary" disabled={!form.name.trim()} onClick={save}>Сохранить</button>}
    </>}>
      <fieldset disabled={!editable} style={{ border: 0, padding: 0, margin: 0 }} className="col">
        <Field label="Название"><input className="input" value={form.name} onChange={(e) => set({ name: e.target.value })} maxLength={256} autoFocus={!group} /></Field>
        <Field label="Комментарий"><textarea className="input" rows={2} value={form.comment} onChange={(e) => set({ comment: e.target.value })} maxLength={2048} /></Field>
        <Check label="Точка прохода: принимать решение «пропустить / отказать»" checked={form.access_control} onChange={(v) => set({ access_control: v })} />
        {form.access_control && (
          <Check label="Проверять живость лица: отказывать, если перед камерой фото или экран" checked={form.liveness_check} onChange={(v) => set({ liveness_check: v })} />
        )}
        <Check label="Определять людей и одежду (силуэты, пол, аксессуары) — раздел «Люди»" checked={form.detect_people} onChange={(v) => set({ detect_people: v })} />
        <Check label="Не создавать повторные события одного человека" checked={form.deduplicate} onChange={(v) => set({ deduplicate: v })} />
        {form.deduplicate && (
          <Field label="Интервал, секунд" hint="Повторное появление человека в группе за это время не создаёт событие">
            <input className="input" type="number" min={1} max={3600} value={form.deduplicate_delay} onChange={(e) => set({ deduplicate_delay: Number(e.target.value) })} />
          </Field>
        )}
        <Check label="Собственный порог схожести" checked={ownThreshold} onChange={setOwnThreshold} />
        {ownThreshold && (
          <Field label={`Порог: ${threshold}%`}>
            <input type="range" min={50} max={99} value={threshold} onChange={(e) => setThreshold(Number(e.target.value))} style={{ accentColor: "var(--accent)" }} />
          </Field>
        )}
      </fieldset>
    </Modal>
  );
}
