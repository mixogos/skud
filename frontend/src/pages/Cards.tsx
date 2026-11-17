import { ChangeEvent, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { del, fmtDate, get, Page, patch, pct, post, postForm, useLoad } from "../api";
import { ListChips, useApp } from "../ctx";
import { Schedule, ScheduleEditor } from "../schedule";
import { Check, Empty, ErrorBox, Face, Field, Icon, Modal, Pager, Seg, Spinner, useAction, useToast } from "../ui";

interface Card {
  id: number; name: string; comment: string; active: boolean; filled: boolean; watch_lists: number[]; meta: Record<string, any>;
  active_after: string | null; active_before: string | null; access_schedule: Schedule; face_objects: number; main_face: number | null;
  created_date: string; looks_like_confidence: number | null;
}
interface FaceObject { id: number; card_id: number; has_source_photo: boolean; created_date: string }

const LIMIT = 60;
// Поле datetime-local работает в местном времени, сервер хранит UTC.
const toLocalInput = (iso: string | null) => {
  if (!iso) return "";
  const d = new Date(iso + (iso.endsWith("Z") ? "" : "Z"));
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
};
const fromLocalInput = (value: string) => (value ? new Date(value).toISOString() : null);

function ListPicker({ value, onChange }: { value: number[]; onChange: (v: number[]) => void }) {
  const { lists } = useApp();
  return (
    <div className="row wrap" style={{ gap: "6px 16px" }}>
      {lists.map((l) => (
        <label key={l.id} className="check">
          <input type="checkbox" checked={value.includes(l.id)} onChange={(e) => onChange(e.target.checked ? [...value, l.id] : value.filter((x) => x !== l.id))} />
          <span className="chip"><i style={{ background: l.color }} />{l.name}</span>
        </label>
      ))}
    </div>
  );
}

export default function CardsPage() {
  const { lists, can } = useApp();
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const [name, setName] = useState("");
  const [query, setQuery] = useState("");
  const [listId, setListId] = useState("");
  const [active, setActive] = useState("");
  const [looksLike, setLooksLike] = useState("");
  const [offset, setOffset] = useState(0);
  const [editing, setEditing] = useState<Card | "new" | null>(null);
  const [batch, setBatch] = useState(false);
  const photoInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const timer = setTimeout(() => { setQuery(name); setOffset(0); }, 350);
    return () => clearTimeout(timer);
  }, [name]);

  const list = useLoad<Page<Card>>(
    () => get("/cards/humans/", { name_contains: query, watch_lists: listId ? [listId] : undefined, active, looks_like: looksLike, limit: LIMIT, offset }),
    [query, listId, active, looksLike, offset],
  );

  // Ссылка «Открыть карточку» из события: #/cards?open=<id>.
  const openId = params.get("open");
  useEffect(() => {
    if (!openId) return;
    get<Card>(`/cards/humans/${openId}/`).then(setEditing, (e) => toast(e.message, true));
    setParams({}, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openId]);

  const searchByPhoto = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    try {
      const form = new FormData();
      form.set("photo", file);
      const faces = (await postForm("/detect/", form)).objects.face;
      if (!faces.length) { toast("На фото не найдено лицо", true); return; }
      if (faces.length > 1) toast(`На фото лиц: ${faces.length}. Ищу по самому крупному.`);
      setLooksLike(faces[0].id);
      setOffset(0);
    } catch (err: any) {
      toast(err.message, true);
    }
  };

  return (
    <>
      <div className="toolbar">
        <Field label="Имя"><input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Поиск по имени" /></Field>
        <Field label="Список наблюдения">
          <select className="input" value={listId} onChange={(e) => { setListId(e.target.value); setOffset(0); }}>
            <option value="">Любой</option>
            {lists.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
          </select>
        </Field>
        <Field label="Состояние">
          <Seg value={active} onChange={(v) => { setActive(v); setOffset(0); }} options={[["", "Все"], ["true", "Активные"], ["false", "Отключённые"]]} />
        </Field>
        <input ref={photoInput} type="file" accept="image/jpeg,image/png,image/webp" hidden onChange={searchByPhoto} />
        <button className="btn" onClick={() => photoInput.current?.click()}><Icon name="search" size={15} />Поиск по фото</button>
        <div className="grow" />
        {can("batchupload_cards", "humancard:add", "faceobject:add") && <button className="btn" onClick={() => setBatch(true)}><Icon name="upload" size={15} />Пакетная загрузка</button>}
        {can("humancard:add") && <button className="btn primary" onClick={() => setEditing("new")}><Icon name="plus" size={15} />Новая карточка</button>}
      </div>
      {looksLike && (
        <div className="row" style={{ marginBottom: 12 }}>
          <span className="badge">Поиск по фото: похожие карточки
            <button className="btn ghost sm" style={{ height: 18, padding: 0 }} onClick={() => setLooksLike("")} aria-label="Сбросить"><Icon name="close" size={12} /></button>
          </span>
        </div>
      )}

      <ErrorBox>{list.error}</ErrorBox>
      {!list.data && list.loading && <Spinner />}
      {list.data && (list.data.results.length === 0
        ? <Empty>{looksLike ? "Похожих карточек не найдено." : "Карточек нет. Создайте первую или загрузите фото пакетом."}</Empty>
        : <>
            <div className="small muted" style={{ marginBottom: 8 }}>Всего: <span className="mono">{list.data.count}</span></div>
            <div className="cards">
              {list.data.results.map((card) => (
                <button key={card.id} className={`card${card.active ? "" : " off"}`} onClick={() => setEditing(card)}>
                  <Face src={card.main_face ? `/objects/faces/${card.main_face}/thumbnail/` : null} size="100%" alt={card.name} />
                  <div className="meta">
                    <div className="spread">
                      <strong className="ellipsis">{card.name}</strong>
                      {card.looks_like_confidence != null && <span className="conf">{pct(card.looks_like_confidence)}</span>}
                    </div>
                    <ListChips ids={card.watch_lists} />
                    {!card.active && <span className="small muted">отключена</span>}
                    {!card.filled && <span className="small" style={{ color: "var(--warn)" }}>нет фото</span>}
                  </div>
                </button>
              ))}
            </div>
            <Pager count={list.data.count} limit={LIMIT} offset={offset} onChange={setOffset} />
          </>)}

      {editing && <CardModal card={editing === "new" ? null : editing} onClose={() => setEditing(null)} onSaved={list.reload} />}
      {batch && <BatchModal onClose={() => setBatch(false)} onDone={list.reload} />}
    </>
  );
}

function CardModal({ card: initial, onClose, onSaved }: { card: Card | null; onClose: () => void; onSaved: () => void }) {
  const { can, lists } = useApp();
  const run = useAction();
  const toast = useToast();
  const [card, setCard] = useState<Card | null>(initial);
  const [form, setForm] = useState({
    name: initial?.name ?? "", comment: initial?.comment ?? "", active: initial?.active ?? true,
    watch_lists: initial?.watch_lists ?? (lists[0] ? [lists[0].id] : []),
    active_after: toLocalInput(initial?.active_after ?? null), active_before: toLocalInput(initial?.active_before ?? null),
    access_schedule: (initial?.access_schedule ?? {}) as Schedule,
  });
  const [busy, setBusy] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const editable = card ? can("humancard:change") : can("humancard:add");
  const faces = useLoad<Page<FaceObject>>(() => (card ? get("/objects/faces/", { card: card.id, limit: 200 }) : Promise.resolve({ count: 0, results: [] })), [card?.id]);
  const set = (patchValue: Partial<typeof form>) => setForm((f) => ({ ...f, ...patchValue }));

  const save = async () => {
    setBusy(true);
    const body = { ...form, active_after: fromLocalInput(form.active_after), active_before: fromLocalInput(form.active_before) };
    const ok = await run(async () => {
      setCard(card ? await patch(`/cards/humans/${card.id}/`, body) : await post("/cards/humans/", body));
    }, card ? "Карточка сохранена" : "Карточка создана — добавьте фото лица");
    setBusy(false);
    if (ok) { onSaved(); if (card) onClose(); }
  };

  const remove = async () => {
    if (!card || !confirm(`Удалить карточку «${card.name}» вместе с фото?`)) return;
    if (await run(() => del(`/cards/humans/${card.id}/`), "Карточка удалена")) { onSaved(); onClose(); }
  };

  const addPhotos = async (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []);
    e.target.value = "";
    if (!card) return;
    for (const file of files) {
      const body = new FormData();
      body.set("card", String(card.id));
      body.set("source_photo", file);
      try {
        await postForm("/objects/faces/", body);
      } catch (err: any) {
        // Несколько лиц на фото: берём самое крупное, сервер вернул их список.
        const candidates = err.detail?.faces;
        if (candidates?.length && confirm(`${file.name}: на фото лиц — ${candidates.length}. Добавить самое крупное?`)) {
          body.set("create_from", candidates[0].id);
          await postForm("/objects/faces/", body).catch((e2) => toast(`${file.name}: ${e2.message}`, true));
        } else if (!candidates) toast(`${file.name}: ${err.message}`, true);
      }
    }
    faces.reload();
    onSaved();
  };

  const removeFace = async (face: FaceObject) => {
    if (!confirm("Удалить это фото из карточки?")) return;
    if (await run(() => del(`/objects/faces/${face.id}/`))) { faces.reload(); onSaved(); }
  };

  return (
    <Modal wide title={card ? card.name : "Новая карточка"} onClose={onClose} footer={<>
      {card && can("humancard:delete") && <button className="btn danger" onClick={remove} style={{ marginRight: "auto" }}><Icon name="trash" size={15} />Удалить</button>}
      <button className="btn" onClick={onClose}>Закрыть</button>
      {editable && <button className="btn primary" disabled={busy || !form.name.trim() || !form.watch_lists.length} onClick={save}>{card ? "Сохранить" : "Создать"}</button>}
    </>}>
      <fieldset disabled={!editable} style={{ border: 0, padding: 0, margin: 0 }} className="col">
        <Field label="Имя"><input className="input" value={form.name} onChange={(e) => set({ name: e.target.value })} maxLength={256} autoFocus={!card} /></Field>
        <Field label="Комментарий"><textarea className="input" rows={2} value={form.comment} onChange={(e) => set({ comment: e.target.value })} maxLength={2048} /></Field>
        <Field label="Списки наблюдения" hint="Карточка должна входить хотя бы в один список"><ListPicker value={form.watch_lists} onChange={(v) => set({ watch_lists: v })} /></Field>
        <div className="row wrap" style={{ alignItems: "flex-end", gap: 14 }}>
          <Field label="Действует с"><input className="input" type="datetime-local" value={form.active_after} onChange={(e) => set({ active_after: e.target.value })} /></Field>
          <Field label="Действует до"><input className="input" type="datetime-local" value={form.active_before} onChange={(e) => set({ active_before: e.target.value })} /></Field>
          <div style={{ height: 32, display: "flex", alignItems: "center" }}><Check label="Карточка активна" checked={form.active} onChange={(v) => set({ active: v })} /></div>
        </div>
        <Field label="Личное расписание прохода" hint="Если задано, заменяет расписание списков наблюдения">
          <ScheduleEditor value={form.access_schedule} onChange={(v) => set({ access_schedule: v })} />
        </Field>
      </fieldset>

      {card && (
        <div className="col" style={{ gap: 8 }}>
          <div className="spread">
            <h3>Фото лица</h3>
            {can("faceobject:add") && <>
              <input ref={fileInput} type="file" accept="image/jpeg,image/png,image/webp" multiple hidden onChange={addPhotos} />
              <button className="btn sm" onClick={() => fileInput.current?.click()}><Icon name="plus" size={13} />Добавить фото</button>
            </>}
          </div>
          <ErrorBox>{faces.error}</ErrorBox>
          {faces.data?.results.length === 0 && <div className="muted small">Фото нет — без фото карточка не участвует в распознавании.</div>}
          <div className="photos">
            {faces.data?.results.map((face) => (
              <div key={face.id} className="photo">
                <Face src={`/objects/faces/${face.id}/thumbnail/`} size={104} alt="Фото лица" />
                {can("faceobject:delete") && <button className="btn sm danger" onClick={() => removeFace(face)} aria-label="Удалить фото"><Icon name="trash" size={13} /></button>}
              </div>
            ))}
          </div>
          <div className="row small muted">
            <span>Создана {fmtDate(card.created_date)}</span>
            {can("faceevent:view") && <Link to={`/events?matched_card=${card.id}`}>События с этим человеком</Link>}
          </div>
        </div>
      )}
    </Modal>
  );
}

function BatchModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const { lists } = useApp();
  const [files, setFiles] = useState<File[]>([]);
  const [chosen, setChosen] = useState<number[]>(lists[0] ? [lists[0].id] : []);
  const [mode, setMode] = useState<"reject" | "biggest">("reject");
  const [prefix, setPrefix] = useState("");
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState("");
  const [result, setResult] = useState<{ created: number; failed: number; errors: { file: string; error: string }[] } | null>(null);

  const start = async () => {
    setBusy(true);
    setError("");
    const total = { created: 0, failed: 0, errors: [] as { file: string; error: string }[] };
    try {
      // Сервер принимает до 100 фото за запрос; отправляем порциями по 20, чтобы видеть ход загрузки.
      for (let i = 0; i < files.length; i += 20) {
        const form = new FormData();
        files.slice(i, i + 20).forEach((f) => form.append("photos", f));
        chosen.forEach((id) => form.append("watch_lists", String(id)));
        form.set("on_multiple", mode);
        form.set("name_prefix", prefix);
        const part = await postForm("/cards/humans/batch/", form);
        total.created += part.created;
        total.failed += part.failed;
        total.errors.push(...part.results.filter((r: any) => r.status === "error"));
        setProgress(Math.min(files.length, i + 20));
      }
    } catch (e: any) {
      setError(e.message);
    }
    setResult(total);
    setBusy(false);
    onDone();
  };

  return (
    <Modal title="Пакетная загрузка карточек" onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>{result ? "Готово" : "Отмена"}</button>
      {!result && <button className="btn primary" disabled={busy || !files.length || !chosen.length} onClick={start}>{busy ? `Загружено ${progress} из ${files.length}…` : "Загрузить"}</button>}
    </>}>
      {!result ? <>
        <div className="muted">Из каждого фото создаётся карточка. Имя карточки — имя файла без расширения.</div>
        <Field label="Фото (JPEG, PNG, WEBP)">
          <input className="input" style={{ padding: "5px 8px" }} type="file" accept="image/jpeg,image/png,image/webp" multiple onChange={(e) => setFiles(Array.from(e.target.files ?? []))} />
        </Field>
        <Field label="Списки наблюдения"><ListPicker value={chosen} onChange={setChosen} /></Field>
        <Field label="Если на фото несколько лиц">
          <Seg value={mode} onChange={setMode} options={[["reject", "Отклонить фото"], ["biggest", "Взять самое крупное"]]} />
        </Field>
        <Field label="Приставка к имени (необязательно)"><input className="input" value={prefix} onChange={(e) => setPrefix(e.target.value)} maxLength={64} /></Field>
      </> : <>
        <div className="row"><span className="badge ok">создано: {result.created}</span>{result.failed > 0 && <span className="badge bad">не загружено: {result.failed}</span>}</div>
        <ErrorBox>{error}</ErrorBox>
        {result.errors.length > 0 && (
          <div className="table-wrap" style={{ maxHeight: 260, overflow: "auto" }}>
            <table className="table"><tbody>
              {result.errors.map((r, i) => <tr key={i}><td className="ellipsis" style={{ maxWidth: 200 }}>{r.file}</td><td className="muted">{r.error}</td></tr>)}
            </tbody></table>
          </div>
        )}
      </>}
    </Modal>
  );
}
